"""Real-time playback of recordings with sub-millisecond scheduling.

Used by the virtual-controller replay, the overlay's replay mode and anything
else that needs events delivered "as they happened".
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator

from .model import InputEvent

SPIN_NS = 1_500_000  # busy-wait the last 1.5 ms before each event


@contextmanager
def high_resolution_timer() -> Iterator[None]:
    """On Windows, raise the system timer resolution to 1 ms for the duration."""
    winmm = None
    if sys.platform == "win32":
        try:
            winmm = ctypes.WinDLL("winmm")
            winmm.timeBeginPeriod(1)
        except OSError:
            winmm = None
    try:
        yield
    finally:
        if winmm is not None:
            winmm.timeEndPeriod(1)


def sleep_until(deadline_ns: int, stop: threading.Event | None = None) -> bool:
    """Sleep until ``time.perf_counter_ns() >= deadline_ns``. Returns False if stopped."""
    while True:
        remaining = deadline_ns - time.perf_counter_ns()
        if remaining <= 0:
            return True
        if stop is not None and stop.is_set():
            return False
        if remaining > SPIN_NS:
            time.sleep(min((remaining - SPIN_NS) / 1e9, 0.05))
        # else: spin


@dataclass
class TimingReport:
    """How late each event was delivered relative to its schedule."""

    count: int = 0
    total_late_ns: int = 0
    max_late_ns: int = 0
    lateness_ns: list[int] = field(default_factory=list)

    def add(self, late_ns: int) -> None:
        self.count += 1
        self.total_late_ns += late_ns
        self.max_late_ns = max(self.max_late_ns, late_ns)
        self.lateness_ns.append(late_ns)

    @property
    def mean_late_ms(self) -> float:
        return self.total_late_ns / self.count / 1e6 if self.count else 0.0

    @property
    def max_late_ms(self) -> float:
        return self.max_late_ns / 1e6

    def percentile_ms(self, p: float) -> float:
        if not self.lateness_ns:
            return 0.0
        s = sorted(self.lateness_ns)
        return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))] / 1e6

    def summary(self) -> str:
        return (f"{self.count} events, lateness mean {self.mean_late_ms:.3f} ms, "
                f"p99 {self.percentile_ms(99):.3f} ms, max {self.max_late_ms:.3f} ms")


class Player:
    """Deliver events to ``callback`` at ``t0 + (ev.t_ns - start_ns) / speed``.

    Events are grouped by identical timestamps and delivered together, then
    ``on_group_done`` (if given) is called — e.g. to push one virtual-pad
    report per group instead of one per changed control.
    """

    def __init__(self, events: Iterable[InputEvent], callback: Callable[[InputEvent], None],
                 speed: float = 1.0, start_ns: int = 0, end_ns: int | None = None,
                 on_group_done: Callable[[], None] | None = None,
                 stop_event: threading.Event | None = None,
                 report: TimingReport | None = None) -> None:
        if speed <= 0:
            raise ValueError("speed must be > 0")
        self.events = [e for e in events
                       if e.t_ns >= start_ns and (end_ns is None or e.t_ns < end_ns)]
        self.events.sort(key=lambda e: e.t_ns)
        self.callback = callback
        self.speed = speed
        self.start_ns = start_ns
        self.on_group_done = on_group_done
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self.report = report if report is not None else TimingReport()
        self.position_ns = start_ns

    def stop(self) -> None:
        self.stop_event.set()

    def run(self, t0_ns: int | None = None) -> TimingReport:
        """Blocking playback. ``t0_ns`` (perf_counter domain) is when ``start_ns`` plays."""
        with high_resolution_timer():
            t0 = time.perf_counter_ns() if t0_ns is None else t0_ns
            i, n = 0, len(self.events)
            while i < n:
                if self.stop_event.is_set():  # also when running behind schedule
                    break
                t_rec = self.events[i].t_ns
                due = t0 + int((t_rec - self.start_ns) / self.speed)
                if not sleep_until(due, self.stop_event):
                    break
                late = time.perf_counter_ns() - due
                while i < n and self.events[i].t_ns == t_rec:
                    self.callback(self.events[i])
                    i += 1
                if self.on_group_done:
                    self.on_group_done()
                self.report.add(max(0, late))
                self.position_ns = t_rec
        return self.report

    def run_in_thread(self, t0_ns: int | None = None) -> threading.Thread:
        th = threading.Thread(target=self.run, args=(t0_ns,), name="player", daemon=True)
        th.start()
        return th
