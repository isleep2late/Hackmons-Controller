"""Event hub: backends publish input events, sinks (recorder, overlay, bridge) consume them.

Clock contract: every event published to the hub carries ``t_ns`` in the
``time.perf_counter_ns()`` domain. Backends with their own clock (SDL ticks,
Android kernel time) convert before publishing.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

from .logfile import LogWriter
from .model import (AXIS, BUTTON, CONNECT, DISCONNECT, MARK, DeviceInfo,
                    InputEvent, PadState)

Sink = Callable[[InputEvent], None]


def now_ns() -> int:
    return time.perf_counter_ns()


class Hub:
    """Thread-safe fan-out with per-device state tracking.

    Backends call :meth:`connect` / :meth:`disconnect` / :meth:`publish` from
    any thread. Sinks are called synchronously under the hub lock, in
    registration order, so they must be fast and must not call back into the
    hub (hand work off to a queue/loop instead).
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sinks: list[Sink] = []
        self._next_id = 0
        self._keys: dict[tuple[str, Any], int] = {}
        self.devices: dict[int, DeviceInfo] = {}
        self.states: dict[int, PadState] = {}
        self.ignored: set[int] = set()

    # -- sinks -------------------------------------------------------------
    def add_sink(self, sink: Sink) -> None:
        with self._lock:
            self._sinks.append(sink)

    def remove_sink(self, sink: Sink) -> None:
        with self._lock:
            if sink in self._sinks:
                self._sinks.remove(sink)

    # -- devices -----------------------------------------------------------
    def connect(self, source_key: tuple[str, Any], info: DeviceInfo,
                t_ns: int | None = None) -> int:
        """Register a device; returns its hub id (stable for the session).

        ``source_key`` identifies the device inside its backend, e.g.
        ``("sdl3", instance_id)``. Reconnecting the same key reuses the id.
        """
        with self._lock:
            dev_id = self._keys.get(source_key)
            if dev_id is None:
                dev_id = self._next_id
                self._next_id += 1
                self._keys[source_key] = dev_id
            elif dev_id in self.devices and dev_id not in self.ignored:
                # Re-announced without a disconnect: release what sinks think is held.
                t = now_ns() if t_ns is None else t_ns
                old = self.states.get(dev_id)
                if old is not None:
                    for i, v in enumerate(old.buttons):
                        if v:
                            self._emit(InputEvent(t, dev_id, BUTTON, i, 0))
                    for i, v in enumerate(old.axes):
                        if v:
                            self._emit(InputEvent(t, dev_id, AXIS, i, 0))
            info.id = dev_id
            self.devices[dev_id] = info
            self.states[dev_id] = PadState()
            if dev_id not in self.ignored:
                self._emit(InputEvent(now_ns() if t_ns is None else t_ns, dev_id,
                                      CONNECT, None, info.to_json()))
            return dev_id

    def disconnect(self, dev_id: int, t_ns: int | None = None) -> None:
        with self._lock:
            if dev_id not in self.devices:
                return
            if dev_id in self.ignored:  # sinks already saw it leave in ignore()
                del self.devices[dev_id]
                self.states.pop(dev_id, None)
                return
            state = self.states.get(dev_id)
            t = now_ns() if t_ns is None else t_ns
            # Release everything so sinks (virtual pads, overlays) don't get stuck inputs.
            if state is not None:
                for i, v in enumerate(state.buttons):
                    if v:
                        self.publish(InputEvent(t, dev_id, BUTTON, i, 0))
                for i, v in enumerate(state.axes):
                    if v:
                        self.publish(InputEvent(t, dev_id, AXIS, i, 0))
            self._emit(InputEvent(t, dev_id, DISCONNECT))
            del self.devices[dev_id]
            self.states.pop(dev_id, None)

    def device_id(self, source_key: tuple[str, Any]) -> int | None:
        with self._lock:
            dev_id = self._keys.get(source_key)
            return dev_id if dev_id in self.devices else None

    def ignore(self, dev_id: int) -> None:
        """Stop forwarding a device (e.g. our own virtual pad).

        Sinks see it disconnect now (after its held inputs are released), and
        nothing more from it afterwards - including on reconnect - so live
        streams agree with :meth:`snapshot`.
        """
        with self._lock:
            if dev_id in self.ignored:
                return
            if dev_id in self.devices:
                t = now_ns()
                state = self.states.get(dev_id)
                if state is not None:
                    for i, v in enumerate(state.buttons):
                        if v:
                            self._emit(InputEvent(t, dev_id, BUTTON, i, 0))
                    for i, v in enumerate(state.axes):
                        if v:
                            self._emit(InputEvent(t, dev_id, AXIS, i, 0))
                    self.states[dev_id] = PadState()
                self._emit(InputEvent(t, dev_id, DISCONNECT))
            self.ignored.add(dev_id)

    # -- events ------------------------------------------------------------
    def publish(self, ev: InputEvent) -> None:
        """Publish a button/axis event; duplicates of the current state are dropped."""
        with self._lock:
            if ev.device in self.ignored:
                return
            if ev.kind in (BUTTON, AXIS):
                state = self.states.get(ev.device)
                if state is None or not state.apply(ev):
                    return
            self._emit(ev)

    def mark(self, label: str, t_ns: int | None = None) -> None:
        with self._lock:
            self._emit(InputEvent(now_ns() if t_ns is None else t_ns, None, MARK, None, label))

    def snapshot(self) -> dict[int, tuple[DeviceInfo, PadState]]:
        with self._lock:
            return {i: (self.devices[i], self.states[i].copy())
                    for i in self.devices if i not in self.ignored}

    def _emit(self, ev: InputEvent) -> None:
        for sink in list(self._sinks):
            try:
                sink(ev)
            except Exception as e:  # a broken sink must never kill input capture
                import logging
                logging.getLogger(__name__).exception("sink %r failed: %s", sink, e)


class Recorder:
    """Hub sink that writes a ``.ctlog`` file with timestamps relative to start."""

    def __init__(self, hub: Hub, path: str | Path, meta: dict[str, Any] | None = None,
                 devices: set[int] | None = None) -> None:
        self.hub = hub
        self.path = Path(path)
        self.only = devices
        self.t0 = now_ns()
        self.writer = LogWriter(self.path, meta=meta,
                                header_extra={"clock": "perf_counter_ns"})
        # Self-contained log: current devices and their non-neutral state at t=0. Snapshot,
        # t=0 rows and sink registration happen under the hub lock, so no event published by
        # a backend thread can fall between the snapshot and the live stream.
        with hub._lock:
            for dev_id, (info, state) in hub.snapshot().items():
                if self.only is not None and dev_id not in self.only:
                    continue
                self.writer.write(InputEvent(0, dev_id, CONNECT, None, info.to_json()))
                for i, v in enumerate(state.buttons):
                    if v:
                        self.writer.write(InputEvent(0, dev_id, BUTTON, i, v))
                for i, v in enumerate(state.axes):
                    if v:
                        self.writer.write(InputEvent(0, dev_id, AXIS, i, v))
            hub.add_sink(self)

    def __call__(self, ev: InputEvent) -> None:
        if self.only is not None and ev.device is not None and ev.device not in self.only:
            return
        rel = max(0, ev.t_ns - self.t0)
        self.writer.write(InputEvent(rel, ev.device, ev.kind, ev.code, ev.value))

    def mark(self, label: str) -> None:
        self.writer.mark(max(0, now_ns() - self.t0), label)

    @property
    def elapsed_ns(self) -> int:
        return now_ns() - self.t0

    def close(self) -> None:
        self.hub.remove_sink(self)
        self.writer.close()
