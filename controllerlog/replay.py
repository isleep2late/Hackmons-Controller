"""Replay recorded runs or TAS frame tables through a virtual controller.

The virtual pad (see :mod:`controllerlog.output.virtual_pad`) is plugged in
first, the recording's state at the start point is applied, and after an
optional trigger and countdown every timestamp group is pushed as one pad
report with :class:`~controllerlog.playback.Player`'s sub-millisecond
scheduler. The pad is always released and unplugged at the end, on
``stop_event`` and on Ctrl+C.

Tips:

* Give the game time to notice the new controller (the countdown does that)
  and make sure it reads the *virtual* pad, e.g. as player 1.
* Double input: when you start a replay with a physical controller button
  (:class:`ButtonTrigger`), that press also reaches the game. Pick a button the
  game ignores at that moment (Guide/Share/touchpad), use ``on_release``, or
  hide the physical pad from the game with HidHide (see :mod:`controllerlog.bridge`).
* Replays are only deterministic if the game is: real-time replays of
  real-hardware runs drift with loading times and lag; frame-based replays
  (``replay_frames``) against an emulator are the reliable TAS path.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from .hub import Hub, now_ns
from .logfile import Recording
from .model import (AXIS, AXIS_INDEX, BUTTON, BUTTON_INDEX, NUM_AXES, NUM_BUTTONS,
                    TRIGGER_PRESS_THRESHOLD, InputEvent, PadState)
from .output.virtual_pad import VirtualPad, create_virtual_pad
from .playback import Player, TimingReport, high_resolution_timer, sleep_until
from .timeline import NS_PER_S, Timeline, frames_to_events

log = logging.getLogger(__name__)

StatusCallback = Callable[[str], None]

# Time for Windows/the game/SDL to open a freshly plugged virtual pad. Readers
# that only see *changes* (SDL's RawInput path) miss state applied earlier.
DEFAULT_SETTLE_S = 1.0


@dataclass
class ReplayReport(TimingReport):
    """:class:`TimingReport` plus what happened to the replay as a whole."""

    aborted: str | None = None   # None (completed), "stopped", "interrupted", "trigger timeout"
    t0_ns: int = 0               # perf_counter time at which start_ns was played
    start_ns: int = 0            # recording time the replay started from
    end_ns: int = 0              # recording time the pad was released at
    scheduled: int = 0           # input events scheduled for playback
    target: str = ""

    def summary(self) -> str:
        s = super().summary()
        return f"{s} (aborted: {self.aborted})" if self.aborted else s


@dataclass
class ButtonTrigger:
    """Start a replay when ``button`` is pressed on a physical controller.

    ``device`` is a hub device id. ``None`` accepts any controller except the
    replay's own virtual pad: a device with the pad's SDL type and USB ids that
    appeared after the replay plugged it in (with a ``pad`` passed to the
    replay, every such look-alike is skipped). Prefer an explicit id.
    ``button`` is a canonical button name or ``left_trigger``/``right_trigger``.
    With ``on_release`` the replay starts when the button is let go after a
    press, so the physical press is over before the first replayed input.
    """

    hub: Hub
    device: int | None
    button: str = "start"
    on_release: bool = False
    timeout_s: float | None = None


class _Stopped(Exception):
    pass


def wait_for_button(hub: Hub, device: int | None, button: str, on_release: bool = False,
                    timeout_s: float | None = None,
                    stop_event: threading.Event | None = None,
                    exclude: Callable[[int], bool] | None = None) -> int | None:
    """Block until ``button`` is pressed (or released after a press) on ``device``.

    Returns the hub timestamp (``perf_counter_ns`` domain) of that edge, or
    ``None`` on timeout / ``stop_event``. A button already held when waiting
    starts must be released and pressed again. ``device=None`` listens to
    every device for which ``exclude(dev_id)`` (if given) is false; ``exclude``
    runs inside the hub sink, so it must be fast and must not call hub methods.
    """
    if button in BUTTON_INDEX:
        kind, code = BUTTON, BUTTON_INDEX[button]
    elif button in ("left_trigger", "right_trigger"):
        kind, code = AXIS, AXIS_INDEX[button]
    else:
        raise ValueError(f"unknown button {button!r}")
    held: dict[int, bool] = {}
    armed: set[int] = set()
    hit: list[int] = []
    done = threading.Event()

    def sink(ev: InputEvent) -> None:
        if done.is_set() or ev.kind != kind or ev.code != code or ev.device is None:
            return
        if device is not None and ev.device != device:
            return
        if device is None and exclude is not None and exclude(ev.device):
            return
        down = bool(ev.value) if kind == BUTTON else ev.value >= TRIGGER_PRESS_THRESHOLD
        was = held.get(ev.device, False)
        held[ev.device] = down
        if down and not was:
            if not on_release:
                hit.append(ev.t_ns)
                done.set()
            else:
                armed.add(ev.device)
        elif was and not down and on_release and ev.device in armed:
            hit.append(ev.t_ns)
            done.set()

    # Snapshot and subscribe atomically: a release published in between would otherwise
    # leave the button "held" here, and the next real press wouldn't count as an edge.
    with hub._lock:
        for dev, (_, st) in hub.snapshot().items():
            held[dev] = st.pressed(button)
        hub.add_sink(sink)
    try:
        deadline = None if timeout_s is None else now_ns() + int(timeout_s * NS_PER_S)
        while not done.is_set():
            if stop_event is not None and stop_event.is_set():
                return None
            wait = 0.05
            if deadline is not None:
                left = (deadline - now_ns()) / NS_PER_S
                if left <= 0:
                    return None
                wait = min(wait, left)
            done.wait(wait)
        return hit[0]
    finally:
        hub.remove_sink(sink)


def find_marker(rec: Recording, label: str) -> InputEvent:
    """First marker whose label equals ``label`` or starts with it."""
    for ev in rec.markers():
        if isinstance(ev.value, str) and (ev.value == label or ev.value.startswith(label)):
            return ev
    names = sorted({str(ev.value) for ev in rec.markers()})
    raise ValueError(f"no marker matching {label!r} (markers: {', '.join(names) or 'none'})")


def replay_recording(rec: Recording, device: int | None = None, target: str = "x360",
                     speed: float = 1.0, start_ns: int = 0, end_ns: int | None = None,
                     start_marker: str | None = None, countdown_s: float = 3.0,
                     on_status: StatusCallback | None = None,
                     stop_event: threading.Event | None = None,
                     wait_for_trigger: ButtonTrigger | None = None,
                     remap: Mapping[str, str] | None = None,
                     pad: VirtualPad | None = None,
                     settle_s: float = DEFAULT_SETTLE_S) -> ReplayReport:
    """Replay one device of a recording in real time on a virtual controller.

    * ``device``: recording device id (default :meth:`Recording.primary_device`);
      ``ValueError`` if that device (or the whole recording) has no input events.
    * ``start_ns`` / ``end_ns``: recording-time window; ``start_marker`` (first
      marker equal to or starting with it) overrides ``start_ns``. The full
      state at the start point is applied before the countdown, so holds that
      began earlier are honoured. The pad is released at ``end_ns`` (default:
      end of the recording).
    * ``speed``: 2.0 plays twice as fast (the countdown is not scaled).
    * ``wait_for_trigger``: wait for a physical button first; the countdown
      then runs from the press (``countdown_s=0`` starts on the press itself).
    * ``pad``: reuse an already-plugged :class:`VirtualPad` (it is reset but
      not closed at the end); otherwise one is created for ``target``/``remap``.
    * ``settle_s``: after plugging in a new pad, wait this long before applying
      the initial state, so the game (or SDL) has opened the device and sees
      held inputs as changes. Skipped when ``pad`` is given.

    Blocks until done. Ctrl+C and ``stop_event`` abort promptly and are
    reported in :attr:`ReplayReport.aborted` (KeyboardInterrupt is not re-raised).
    """
    with_input = sorted({e.device for e in rec.input_events() if e.device is not None})
    if not with_input:
        raise ValueError("recording has no input events")
    dev = rec.primary_device() if device is None else device
    if dev not in with_input:
        raise ValueError(f"device {dev} has no input events in this recording "
                         f"(devices with input: {', '.join(map(str, with_input))})")
    if start_marker is not None:
        start_ns = find_marker(rec, start_marker).t_ns
    if end_ns is not None and end_ns < start_ns:
        raise ValueError("end_ns is before the start point")
    initial = Timeline(rec).state_at(dev, start_ns)
    # Events at exactly start_ns are already part of the initial state. Codes the
    # canonical model doesn't know (newer/foreign logs) are skipped, as Timeline does.
    events = [e for e in rec.input_events(dev)
              if e.t_ns > start_ns and (end_ns is None or e.t_ns < end_ns)
              and isinstance(e.code, int)
              and 0 <= e.code < (NUM_BUTTONS if e.kind == BUTTON else NUM_AXES)]
    stop_at = end_ns if end_ns is not None else max(start_ns, rec.duration_ns)
    return _run(initial, events, start_ns, stop_at, target=target, speed=speed,
                countdown_s=countdown_s, on_status=on_status, stop_event=stop_event,
                trigger=wait_for_trigger, remap=remap, pad=pad, settle_s=settle_s)


def replay_frames(frames: Sequence[PadState], fps: float, target: str = "x360",
                  countdown_s: float = 3.0, on_status: StatusCallback | None = None,
                  stop_event: threading.Event | None = None, speed: float = 1.0,
                  wait_for_trigger: ButtonTrigger | None = None,
                  remap: Mapping[str, str] | None = None,
                  pad: VirtualPad | None = None,
                  settle_s: float = DEFAULT_SETTLE_S) -> ReplayReport:
    """Replay a frame table (bk2 import, TAS edit, ``Timeline.frames``) on a virtual controller.

    Frame ``i`` is applied at ``t0 + i / fps`` (divided by ``speed``); only
    frames that change something produce a report. The last frame is held
    for one frame, then everything is released. Other options as in
    :func:`replay_recording`.
    """
    if fps <= 0:
        raise ValueError("fps must be > 0")
    events = frames_to_events(frames, fps)
    stop_at = round(len(frames) * NS_PER_S / fps)
    return _run(PadState(), events, 0, stop_at, target=target, speed=speed,
                countdown_s=countdown_s, on_status=on_status, stop_event=stop_event,
                trigger=wait_for_trigger, remap=remap, pad=pad, settle_s=settle_s)


def _countdown(t0: int, status: StatusCallback, stop: threading.Event) -> bool:
    """Announce whole seconds until ``t0``; False if stopped."""
    last = None
    while True:
        left = t0 - now_ns()
        if left <= 0:
            return True
        secs = -(-left // NS_PER_S)
        if secs != last:
            status(f"starting in {secs}...")
            last = secs
        if not sleep_until(t0 - (secs - 1) * NS_PER_S, stop):
            return False


def _run(initial: PadState, events: list[InputEvent], start_ns: int, stop_at_ns: int, *,
         target: str, speed: float, countdown_s: float, on_status: StatusCallback | None,
         stop_event: threading.Event | None, trigger: ButtonTrigger | None,
         remap: Mapping[str, str] | None, pad: VirtualPad | None,
         settle_s: float) -> ReplayReport:
    if speed <= 0:
        raise ValueError("speed must be > 0")
    if trigger is not None and trigger.button not in BUTTON_INDEX \
            and trigger.button not in ("left_trigger", "right_trigger"):
        raise ValueError(f"unknown trigger button {trigger.button!r}")
    status: StatusCallback = on_status or (lambda msg: None)
    stop = stop_event if stop_event is not None else threading.Event()
    owns_pad = pad is None
    listen_any = trigger is not None and trigger.device is None
    # Devices present before our pad existed can't be it (see ButtonTrigger).
    known = set(trigger.hub.snapshot()) if listen_any and owns_pad else None
    if pad is None:
        pad = create_virtual_pad(target, remap=remap)
    own = pad.target

    def is_own_pad(dev: int) -> bool:
        """Might hub device ``dev`` be our virtual pad? (runs in the hub sink)"""
        info = trigger.hub.devices.get(dev)
        return (info is not None and (known is None or dev not in known)
                and info.backend == "sdl3"  # virtual pads are only seen through local SDL
                and info.sdl_type == own.sdl_type and info.vendor_id == own.vendor_id
                and info.product_id == own.product_id)

    report = ReplayReport(start_ns=start_ns, end_ns=stop_at_ns, scheduled=len(events),
                          target=pad.target.name)

    def deliver(ev: InputEvent) -> None:
        if stop.is_set():
            raise _Stopped
        if ev.kind == BUTTON:
            pad.set_button(ev.code, ev.value)
        else:
            pad.set_axis(ev.code, ev.value)

    def play() -> None:
        if owns_pad and settle_s > 0:
            status(f"virtual {pad.target.label} connected; waiting for it to be detected")
            if not sleep_until(now_ns() + int(settle_s * NS_PER_S), stop):
                report.aborted = "stopped"
                return
        status(f"virtual {pad.target.label} ready")
        pad.apply(initial)
        # Build the schedule (filter + sort, ~0.1 s for a million events) and announce it
        # before t0, so nothing but delivery happens once the first input is due.
        player = Player(events, deliver, speed=speed, start_ns=start_ns,
                        on_group_done=pad.update)
        player.stop_event = stop
        player.report = report
        span_s = (stop_at_ns - start_ns) / NS_PER_S / speed
        status(f"replaying {len(events)} events ({span_s:.1f} s)")
        t_start = now_ns()
        if trigger is not None:
            who = "any controller" if trigger.device is None else f"device {trigger.device}"
            status(f"waiting for {trigger.button} on {who}...")
            t_press = wait_for_button(trigger.hub, trigger.device, trigger.button,
                                      on_release=trigger.on_release,
                                      timeout_s=trigger.timeout_s, stop_event=stop,
                                      exclude=is_own_pad if listen_any else None)
            if t_press is None:
                report.aborted = "stopped" if stop.is_set() else "trigger timeout"
                return
            t_start = t_press
        t0 = t_start + int(countdown_s * NS_PER_S)
        report.t0_ns = t0
        if not _countdown(t0, status, stop):
            report.aborted = "stopped"
            return
        try:
            player.run(t0)
        except _Stopped:
            pass
        # Hold the final state until the end of the window, then release.
        if stop.is_set() or not sleep_until(t0 + int((stop_at_ns - start_ns) / speed), stop):
            report.aborted = "stopped"

    try:
        with high_resolution_timer():
            play()
    except KeyboardInterrupt:
        report.aborted = "interrupted"
    finally:
        try:
            if owns_pad:
                pad.close()
            elif not pad.closed:
                pad.reset()
        except Exception as e:  # never mask the replay result with a cleanup error
            log.warning("could not release virtual controller: %s", e)
    status(f"{'stopped' if report.aborted else 'done'}: {report.summary()}")
    return report


__all__ = ["ReplayReport", "ButtonTrigger", "replay_recording", "replay_frames",
           "wait_for_button", "find_marker", "DEFAULT_SETTLE_S"]
