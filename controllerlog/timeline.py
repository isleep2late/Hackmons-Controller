"""State reconstruction, frame quantization and press statistics for recordings."""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, Sequence

from .logfile import Recording
from .model import (AXIS, BUTTON, BUTTONS, NUM_AXES, NUM_BUTTONS,
                    TRIGGER_PRESS_THRESHOLD, InputEvent, PadState)

NS_PER_S = 1_000_000_000

# Native frame rates (frames per second) of common targets.
FPS = {
    "gb": 4194304 / 70224,       # 59.727500569... (GB/GBC: 70224 cycles per frame)
    "gbc": 4194304 / 70224,
    "gba": 16777216 / 280896,    # 59.727500569... (GBA: 280896 cycles per frame)
    "nes": (39375000 / 11 * 6 / 4) / 89341.5,  # ~60.0988 (NTSC PPU clock / dots per frame)
    "snes": 21477272.727 / 357366,       # ~60.0988 (NTSC, non-interlaced)
    "n64": 60.0,
    "60": 60.0,
    "30": 30.0,
    "50": 50.0,
}


def resolve_fps(value: str | float) -> float:
    """Frame rate from a number, a name (gb, gba, nes, ...) or ``num/den``.

    Raises ``ValueError`` unless the result is a positive, finite number.
    """
    if isinstance(value, (int, float)):
        v = float(value)
    else:
        key = str(value).lower().strip()
        try:
            if key in FPS:
                v = FPS[key]
            elif "/" in key:  # exact rational, e.g. 60000/1001
                num, den = key.split("/", 1)
                v = float(Fraction(int(num), int(den)))
            else:
                v = float(key)
        except (ZeroDivisionError, OverflowError) as e:
            raise ValueError(f"bad fps {value!r}: {e}") from None
    if not math.isfinite(v) or v <= 0:
        raise ValueError(f"fps must be a positive, finite number (got {value!r})")
    return v


def exact_fps(fps: float | Fraction) -> Fraction:
    """Exact rational frame rate (59.7275... -> 262144/4389)."""
    return fps if isinstance(fps, Fraction) else Fraction(fps).limit_denominator(1_000_000)


def frame_time_ns(i: int, fps: float | Fraction, offset_ns: int = 0) -> int:
    """Start of frame ``i`` in ns, rounded half up with exact rational arithmetic.

    Shared by quantization, TAS movies and the video renderer so every tool
    agrees on frame boundaries to the nanosecond.
    """
    f = exact_fps(fps)
    num = i * NS_PER_S * f.denominator
    return offset_ns + (2 * num + f.numerator) // (2 * f.numerator)


@dataclass(frozen=True)
class Press:
    """One button press: held from ``down_ns`` until ``up_ns`` (None = still held at end)."""

    device: int
    button: int
    down_ns: int
    up_ns: int | None

    @property
    def name(self) -> str:
        """Canonical name; triggers use pseudo-indices NUM_BUTTONS / NUM_BUTTONS + 1."""
        return press_name(self.button)

    def duration_ns(self, end_ns: int) -> int:
        return (self.up_ns if self.up_ns is not None else end_ns) - self.down_ns


class Timeline:
    """Random access to the controller state of a recording at any time."""

    def __init__(self, rec: Recording) -> None:
        self.rec = rec
        self.end_ns = rec.duration_ns
        # Per device: sorted change times and the full state after each change.
        self._times: dict[int, list[int]] = {}
        self._states: dict[int, list[PadState]] = {}
        # Per device: raw input events (for "any"-mode quantization).
        self._events: dict[int, list[InputEvent]] = {}
        current: dict[int, PadState] = {}
        events = rec.events
        if any(events[i].t_ns > events[i + 1].t_ns for i in range(len(events) - 1)):
            events = sorted(events, key=lambda e: e.t_ns)  # stable: keeps same-time order
        for ev in events:
            if ev.kind not in (BUTTON, AXIS) or ev.device is None:
                continue
            st = current.setdefault(ev.device, PadState())
            self._events.setdefault(ev.device, []).append(ev)
            if st.apply(ev):
                times = self._times.setdefault(ev.device, [])
                states = self._states.setdefault(ev.device, [])
                if times and times[-1] == ev.t_ns:
                    states[-1] = st.copy()
                else:
                    times.append(ev.t_ns)
                    states.append(st.copy())

    @property
    def devices(self) -> list[int]:
        return sorted(self._events)

    def state_at(self, device: int, t_ns: int) -> PadState:
        """State after applying every event with timestamp <= t_ns."""
        times = self._times.get(device)
        if not times:
            return PadState()
        i = bisect.bisect_right(times, t_ns) - 1
        return self._states[device][i].copy() if i >= 0 else PadState()

    def changes(self, device: int) -> list[tuple[int, PadState]]:
        return list(zip(self._times.get(device, []), self._states.get(device, [])))

    # -- frames --------------------------------------------------------------
    def frames(self, device: int, fps: float = 60.0, offset_ns: int = 0,
               mode: str = "sample", count: int | None = None) -> list[PadState]:
        """Quantize the recording into per-frame states.

        Frame ``i`` spans ``[offset + i*T, offset + (i+1)*T)`` with ``T = 1/fps``.

        * ``mode="sample"``: the state at the *start* of each frame, i.e. what a
          game that latches input once per frame would see.
        * ``mode="any"``: a button counts as held in frame ``i`` if it was down
          at any moment during the frame, so taps shorter than a frame are not
          lost. Axes use the value at the end of the frame.
        """
        if mode not in ("sample", "any"):
            raise ValueError("mode must be 'sample' or 'any'")
        f = exact_fps(fps)
        if count is None:
            span = max(0, self.end_ns - offset_ns)
            count = int(span * f // NS_PER_S) + 1
        out: list[PadState] = []
        if mode == "sample":
            for i in range(count):
                out.append(self.state_at(device, frame_time_ns(i, f, offset_ns)))
            return out
        events = self._events.get(device, [])
        keys = [e.t_ns for e in events]
        for i in range(count):
            start = frame_time_ns(i, f, offset_ns)
            end = frame_time_ns(i + 1, f, offset_ns)
            st = self.state_at(device, start)
            j = bisect.bisect_right(keys, start)
            k = bisect.bisect_left(keys, end)
            held = list(st.buttons)
            for ev in events[j:k]:
                if ev.kind == BUTTON and ev.value and 0 <= ev.code < NUM_BUTTONS:
                    held[ev.code] = 1
            end_state = self.state_at(device, end - 1)
            out.append(PadState(held, list(end_state.axes)))
        return out

    # -- presses & stats -------------------------------------------------------
    def presses(self, device: int, include_triggers: bool = True) -> list[Press]:
        """Every press interval, in chronological order of press."""
        out: list[Press] = []
        down: dict[int, int] = {}
        prev = PadState()
        for t, st in self.changes(device):
            for b in range(NUM_BUTTONS):
                if st.buttons[b] and not prev.buttons[b]:
                    down[b] = t
                elif not st.buttons[b] and prev.buttons[b] and b in down:
                    out.append(Press(device, b, down.pop(b), t))
            if include_triggers:
                for ax, pseudo in ((4, NUM_BUTTONS), (5, NUM_BUTTONS + 1)):
                    now = st.axes[ax] >= TRIGGER_PRESS_THRESHOLD
                    was = prev.axes[ax] >= TRIGGER_PRESS_THRESHOLD
                    if now and not was:
                        down[pseudo] = t
                    elif was and not now and pseudo in down:
                        out.append(Press(device, pseudo, down.pop(pseudo), t))
            prev = st
        for b, t in down.items():
            out.append(Press(device, b, t, None))
        out.sort(key=lambda p: (p.down_ns, p.button))
        return out

    def stats(self, device: int, fps: float = 60.0) -> dict[str, dict[str, float]]:
        """Per-button press count, total/min/max/mean hold (frames) and peak mash rate (Hz)."""
        frame_ns = NS_PER_S / fps
        by_button: dict[str, list[Press]] = {}
        for p in self.presses(device):
            name = press_name(p.button)
            by_button.setdefault(name, []).append(p)
        result: dict[str, dict[str, float]] = {}
        for name, ps in sorted(by_button.items(), key=lambda kv: -len(kv[1])):
            holds = [p.duration_ns(self.end_ns) / frame_ns for p in ps]
            downs = [p.down_ns for p in ps]
            result[name] = {
                "presses": len(ps),
                "held_frames_total": round(sum(holds), 2),
                "hold_frames_min": round(min(holds), 2),
                "hold_frames_max": round(max(holds), 2),
                "hold_frames_mean": round(sum(holds) / len(holds), 2),
                "peak_mash_hz": round(peak_rate(downs, window_ns=NS_PER_S), 2),
            }
        return result


def press_name(button: int) -> str:
    if button < NUM_BUTTONS:
        return BUTTONS[button]
    return ("left_trigger", "right_trigger")[button - NUM_BUTTONS]


def peak_rate(times_ns: Sequence[int], window_ns: int = NS_PER_S) -> float:
    """Max number of events inside any sliding window, expressed per second."""
    best = 0
    j = 0
    for i, t in enumerate(times_ns):
        while times_ns[j] < t - window_ns + 1:
            j += 1
        best = max(best, i - j + 1)
    return best * NS_PER_S / window_ns


def frames_to_events(frames: Iterable[PadState], fps: float, device: int = 0,
                     start_ns: int = 0) -> list[InputEvent]:
    """Inverse of :meth:`Timeline.frames`: turn a frame table back into change events."""
    f = exact_fps(fps)
    out: list[InputEvent] = []
    prev = PadState()
    for i, st in enumerate(frames):
        t = frame_time_ns(i, f, start_ns)
        for b in range(NUM_BUTTONS):
            if st.buttons[b] != prev.buttons[b]:
                out.append(InputEvent(t, device, BUTTON, b, st.buttons[b]))
        for a in range(NUM_AXES):
            if st.axes[a] != prev.axes[a]:
                out.append(InputEvent(t, device, AXIS, a, st.axes[a]))
        prev = st
    return out
