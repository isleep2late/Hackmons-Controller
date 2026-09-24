"""Mirror a physical controller onto a virtual Xbox 360 / DualShock 4 pad.

This is the DS4Windows / BetterJoy idea in software: SDL3 (with its HIDAPI
drivers) understands Switch Pro, Joy-Cons, DualShock 3/4, DualSense, Wii
remotes, 8BitDo, Stadia... pads over Bluetooth or USB; :class:`Bridge` copies
the canonical state of one of them onto a ViGEmBus virtual controller, so games
that only accept XInput (or DS4) controllers work with it. Everything still
flows through the hub, so the same session can log and overlay the input.

Feedback loops: the SDL backend also sees the bridge's *own* virtual pad. The
bridge recognises it (the first device with the target's SDL type and USB ids
that appears within ``own_pad_window_s`` of plugging the pad in), calls
``hub.ignore()`` on it and never mirrors it. Start the input backend before (or
at most a few seconds after) the bridge so that detection window is met.
Several bridges on one hub (e.g. two players) each claim a different pad.

Double input: games still see the physical controller next to the virtual
one, so a game that reads both gets every press twice. Hide the physical pad
with HidHide (see :data:`HIDHIDE_HELP`), or turn off Steam Input for the game.
"""

from __future__ import annotations

import logging
import math
import threading
import time
import weakref
from typing import Any, Callable, Mapping

from .hub import Hub, now_ns
from .model import (AXIS, AXIS_INDEX, AXIS_MAX, AXIS_MIN, BUTTON, CONNECT, DISCONNECT,
                    InputEvent, PadState)
from .output.virtual_pad import (TARGETS, VirtualPad, create_virtual_pad, normalize_target,
                                 remap_state, validate_remap)

log = logging.getLogger(__name__)

HIDHIDE_URL = "https://github.com/nefarius/HidHide"

HIDHIDE_HELP = f"""\
Avoiding double input with HidHide ({HIDHIDE_URL}/releases):
  1. Install HidHide and reboot if asked.
  2. Open "HidHide Configuration Client".
  3. Applications tab: add the Python interpreter that runs ControllerLog, so it can
     still read the hidden controller. For a virtual environment add both
     .venv\\Scripts\\python.exe and the base interpreter it launches
     (python -c "import sys; print(sys._base_executable)").
  4. Devices tab: tick your physical controller and enable "Enable device hiding".
  5. Restart the game: it now sees only the virtual controller.
  Untick "Enable device hiding" to give the controller back to every program.
  Steam Input can also create its own virtual pad; disable it for the game if so."""

_STICKS = ((AXIS_INDEX["left_x"], AXIS_INDEX["left_y"]),
           (AXIS_INDEX["right_x"], AXIS_INDEX["right_y"]))


def apply_deadzone(state: PadState, deadzone: int) -> PadState:
    """Scaled radial deadzone on both sticks.

    Vectors shorter than ``deadzone`` become 0; longer ones are rescaled so
    the output still starts at 0 and reaches full deflection. Triggers and
    buttons are untouched.
    """
    if deadzone <= 0:
        return state
    dz = min(int(deadzone), AXIS_MAX - 1)
    axes = list(state.axes)
    for ix, iy in _STICKS:
        x, y = axes[ix], axes[iy]
        mag = math.hypot(x, y)
        if mag <= dz:
            axes[ix] = axes[iy] = 0
            continue
        k = (mag - dz) / (AXIS_MAX - dz) * AXIS_MAX / mag
        axes[ix] = max(AXIS_MIN, min(AXIS_MAX, round(x * k)))
        axes[iy] = max(AXIS_MIN, min(AXIS_MAX, round(y * k)))
    return PadState(list(state.buttons), axes)


def _identity(info: dict[str, Any]) -> tuple[Any, ...]:
    """What identifies the same physical controller across reconnects."""
    return (info.get("vendor_id"), info.get("product_id"),
            info.get("serial") or info.get("path") or info.get("name"))


# Hub devices claimed as some bridge's own virtual pad, per hub. Shared so that two
# bridges plugging in look-alike pads at the same time don't both claim the first one
# (leaving the second unignored and adoptable: a feedback loop).
_claims_lock = threading.Lock()
_claims: "weakref.WeakKeyDictionary[Hub, set[int]]" = weakref.WeakKeyDictionary()


def _claim(hub: Hub, dev: int) -> bool:
    """Claim ``dev`` as a bridge's own pad; False if another bridge already has."""
    with _claims_lock:
        claimed = _claims.setdefault(hub, set())
        if dev in claimed:
            return False
        claimed.add(dev)
        return True


def _claimed(hub: Hub, dev: int) -> bool:
    with _claims_lock:
        return dev in _claims.get(hub, ())


class Bridge:
    """Hub sink mirroring one physical controller onto a virtual controller.

    * ``source_device``: hub device id to mirror; ``None`` follows the most
      recently connected controller (skipping virtual/replay devices, the
      bridge's own pad and, until that pad has been identified, anything
      that looks like it). An explicit source that disconnects is re-adopted
      when the same controller (vendor/product/serial) reconnects.
    * ``target``: ``"x360"`` (XInput) or ``"ds4"``.
    * ``remap``: canonical->canonical, e.g. ``{"south": "east", "east": "south"}``
      (see :data:`~controllerlog.output.virtual_pad.NINTENDO_LABEL_REMAP`).
    * ``deadzone``: scaled radial stick deadzone (0..32767), see :func:`apply_deadzone`.
    * ``min_interval_s``: output reports are coalesced to at most one per
      interval (default 1 ms); the first change after idle is sent at once.

    The sink only records the newest state and wakes a dedicated output
    thread, which talks to the driver; it never blocks input capture.
    Everything is released when the source disconnects and on :meth:`stop`.
    """

    def __init__(self, hub: Hub, source_device: int | None = None, target: str = "x360",
                 remap: Mapping[str, str] | None = None, deadzone: int = 0,
                 min_interval_s: float = 0.001, own_pad_window_s: float = 3.0,
                 pad_factory: Callable[[str], VirtualPad] | None = None) -> None:
        self.hub = hub
        self.target = normalize_target(target)
        self.remap = validate_remap(remap)
        self.deadzone = max(0, int(deadzone))
        self.min_interval_ns = int(min_interval_s * 1e9)
        self.own_pad_window_ns = int(own_pad_window_s * 1e9)
        self._pad_factory = pad_factory or (lambda t: create_virtual_pad(t))
        self._requested = source_device
        self._auto = source_device is None
        self.pad: VirtualPad | None = None
        self.own_devices: set[int] = set()      # every virtual pad this bridge created
        self.own_pad_id: int | None = None       # hub id of the current one, once seen
        self.error: BaseException | None = None
        # Stats (read without locking; approximate while running).
        self.events_in = 0
        self.updates_sent = 0
        self.max_latency_ns = 0
        self._latency_total_ns = 0
        # State shared between the sink (hub thread) and the output thread.
        self._cond = threading.Condition(threading.Lock())
        self._source: int | None = None
        self._follow: tuple[Any, ...] | None = None
        self._pending: PadState | None = None
        self._pending_t: int = 0          # hub timestamp of the oldest unsent change
        self._seq = 0
        self._to_ignore: list[int] = []
        self._reselect = False
        self._stopping = False
        self._watch_from: int | None = None
        self._watch_until: int | None = None
        self._thread: threading.Thread | None = None
        self._started = False

    # -- lifecycle -------------------------------------------------------------
    def start(self) -> "Bridge":
        """Plug in the virtual pad and start mirroring. Raises VirtualPadUnavailable."""
        if self._started:
            return self
        self.own_pad_id = None
        self._stopping = False
        self._watch_from = now_ns()
        self._watch_until = None                   # open until the pad exists
        self.hub.add_sink(self)
        try:
            self.pad = self._pad_factory(self.target)
        except BaseException:
            self.hub.remove_sink(self)
            with self._cond:                       # the sink may have adopted a source
                self._source = None
                self._pending = None
            raise
        with self._cond:
            self._watch_until = now_ns() + self.own_pad_window_ns
        self._started = True
        self._thread = threading.Thread(target=self._run, name="bridge-output", daemon=True)
        self._thread.start()
        self._select_source()
        log.info("bridge started (target %s, source %s)", self.target,
                 "auto" if self._auto else self._requested)
        return self

    def stop(self, timeout: float = 2.0) -> None:
        """Stop mirroring, release every control and unplug the virtual pad."""
        if not self._started:
            return
        self.hub.remove_sink(self)
        with self._cond:
            self._stopping = True
            self._source = None
            self._pending = None
            self._reselect = False
            self._cond.notify_all()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout)
        self._thread = None
        with self._cond:
            ignore, self._to_ignore = self._to_ignore, []
        for dev in ignore:
            self.hub.ignore(dev)
        if self.pad is not None:
            self.pad.close()
        self._started = False
        log.info("bridge stopped: %s", self.stats())

    def __enter__(self) -> "Bridge":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- info ------------------------------------------------------------------
    @property
    def source(self) -> int | None:
        """Hub id of the controller currently mirrored (None = waiting for one)."""
        return self._source

    @property
    def running(self) -> bool:
        return self._started

    def reset_stats(self) -> None:
        """Zero the event/update counters and latency figures."""
        self.events_in = self.updates_sent = 0
        self.max_latency_ns = self._latency_total_ns = 0

    def stats(self) -> dict[str, Any]:
        """Counters: ``events_in`` (source changes seen), ``updates_sent`` (pad
        reports pushed) and hub-timestamp-to-report latency (mean/max ms)."""
        n = self.updates_sent
        return {
            "target": self.target,
            "source": self._source,
            "own_pad": self.own_pad_id,
            "own_devices": sorted(self.own_devices),
            "events_in": self.events_in,
            "updates_sent": n,
            "mean_latency_ms": round(self._latency_total_ns / n / 1e6, 3) if n else 0.0,
            "max_latency_ms": round(self.max_latency_ns / 1e6, 3),
        }

    # -- sink (runs on the input thread, under the hub lock) --------------------
    def __call__(self, ev: InputEvent) -> None:
        kind = ev.kind
        if kind == BUTTON or kind == AXIS:
            if ev.device != self._source or ev.device is None:
                return
            st = self.hub.states.get(ev.device)   # authoritative, already updated
            if st is None:
                return
            with self._cond:
                if ev.device != self._source:
                    return
                if self._pending is None:
                    self._pending_t = ev.t_ns
                self._pending = st.copy()
                self._seq += 1
                self.events_in += 1
                self._cond.notify()
        elif kind == CONNECT:
            self._on_connect(ev)
        elif kind == DISCONNECT:
            with self._cond:
                if ev.device == self._source:
                    info = self.hub.devices.get(ev.device)
                    if info is not None and not self._auto:
                        self._follow = _identity(info.to_json())
                    log.info("bridge source #%s disconnected; releasing", ev.device)
                    self._source = None
                    self._pending = PadState()
                    self._pending_t = ev.t_ns
                    self._reselect = self._auto
                    self._seq += 1
                    self._cond.notify()

    def _looks_like_own_pad(self, info: dict[str, Any]) -> bool:
        # Our ViGEm pad is only ever seen through the local SDL backend; a real DS4 on a
        # phone (adb backend) with the same IDs must never be mistaken for it.
        t = TARGETS[self.target]
        return (info.get("backend", "sdl3") == "sdl3" and info.get("sdl_type") == t.sdl_type
                and info.get("vendor_id") == t.vendor_id and info.get("product_id") == t.product_id)

    def _eligible(self, dev: int, info: dict[str, Any]) -> bool:
        if dev in self.own_devices or dev in self.hub.ignored or _claimed(self.hub, dev):
            return False
        if info.get("backend") in ("virtual", "replay"):
            return False
        # Until our own pad is identified, never auto-adopt something that looks like it.
        return not (self._auto and self.own_pad_id is None and self._looks_like_own_pad(info))

    def _on_connect(self, ev: InputEvent) -> None:
        info = ev.value if isinstance(ev.value, dict) else {}
        dev = ev.device
        if dev is None:
            return
        with self._cond:
            if (self.own_pad_id is None and self._watch_from is not None
                    and ev.t_ns >= self._watch_from - 50_000_000
                    and (self._watch_until is None or ev.t_ns <= self._watch_until)
                    and dev != self._source and self._looks_like_own_pad(info)
                    and _claim(self.hub, dev)):
                self.own_devices.add(dev)
                self.own_pad_id = dev
                self._to_ignore.append(dev)   # hub.ignore() from the output thread
                # Look-alikes are adoptable now that our own pad is known.
                self._reselect = self._reselect or (self._auto and self._source is None)
                self._cond.notify()
                log.info("bridge: device #%s is our own virtual pad; ignoring it", dev)
                return
            if dev == self._source:
                # Same hub key connected again: the hub reset its state to neutral.
                if self._pending is None:
                    self._pending_t = ev.t_ns
                self._pending = PadState()
                self._seq += 1
                self._cond.notify()
                return
            if self._source is not None or self._stopping:
                return
            if self._auto:
                adopt = self._eligible(dev, info)
            else:
                adopt = dev == self._requested or (self._follow is not None
                                                   and _identity(info) == self._follow)
                adopt = adopt and dev not in self.own_devices and not _claimed(self.hub, dev)
            if adopt:
                self._adopt(dev, PadState(), ev.t_ns)

    def _adopt(self, dev: int, state: PadState, t_ns: int) -> None:
        """Make ``dev`` the source (caller holds ``_cond``)."""
        self._source = dev
        self._requested = dev if not self._auto else self._requested
        self._follow = None
        self._pending = state
        self._pending_t = t_ns
        self._seq += 1
        self._cond.notify()
        log.info("bridge: mirroring device #%s", dev)

    # -- output thread -----------------------------------------------------------
    def _select_source(self) -> None:
        """Pick a source from the hub's current devices (never under ``_cond``)."""
        snap = self.hub.snapshot()
        with self._cond:
            if self._source is not None or self._stopping:
                return
            if self._auto:
                cands = [d for d, (info, _) in snap.items() if self._eligible(d, info.to_json())]
                dev = max(cands) if cands else None
            else:
                dev = self._requested if self._requested in snap else None
                if dev is not None and (dev in self.own_devices or _claimed(self.hub, dev)):
                    log.error("bridge: device #%s is a bridge's own virtual pad", dev)
                    dev = None
            if dev is None:
                return
            self._adopt(dev, snap[dev][1], now_ns())
            seq = self._seq
        # Refresh with a state taken after adoption unless the sink already sent a newer one.
        fresh = self.hub.snapshot().get(dev)
        with self._cond:
            if self._source != dev:
                return
            if fresh is None:
                # It disconnected between the two snapshots, i.e. before the sink could
                # see its DISCONNECT as the source's: release and look again.
                log.info("bridge: device #%s vanished while being adopted", dev)
                if not self._auto:
                    self._follow = _identity(snap[dev][0].to_json())
                self._source = None
                self._pending = PadState()
                self._pending_t = now_ns()
                self._reselect = self._auto
                self._seq += 1
                self._cond.notify()
            elif self._seq == seq:
                self._pending = fresh[1]
                self._cond.notify()

    def _run(self) -> None:
        last_sent = 0
        while True:
            with self._cond:
                while not (self._pending is not None or self._stopping
                           or self._to_ignore or self._reselect):
                    self._cond.wait()
                if self._stopping:
                    return
                ignore, self._to_ignore = self._to_ignore, []
                reselect, self._reselect = self._reselect, False
            for dev in ignore:
                self.hub.ignore(dev)
            if reselect:
                self._select_source()
            wait = last_sent + self.min_interval_ns - now_ns()
            # Coalesce bursts into one report; sleep(0) at least yields the GIL so the
            # input thread can publish the rest of one controller report first.
            time.sleep(wait / 1e9 if wait > 0 else 0)
            with self._cond:
                state, self._pending = self._pending, None
                t_first = self._pending_t
            if state is None:
                continue
            try:
                sent = self.pad.apply(remap_state(apply_deadzone(state, self.deadzone),
                                                  self.remap))
            except Exception as e:
                if self.error is None:
                    log.error("bridge: virtual pad update failed: %s", e)
                self.error = e
                continue
            if not sent:                           # e.g. only an unmapped button changed
                continue
            last_sent = now_ns()
            lat = max(0, last_sent - t_first)
            self.updates_sent += 1
            self._latency_total_ns += lat
            self.max_latency_ns = max(self.max_latency_ns, lat)


__all__ = ["Bridge", "apply_deadzone", "HIDHIDE_HELP", "HIDHIDE_URL"]
