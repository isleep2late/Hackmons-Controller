"""The ``.ctlog`` recording format (JSON Lines).

Line 1 is a header object; every following line is one compact event row::

    {"format": "controllerlog", "version": 1, "created_utc": "...", ...}
    [0, 0, "+", null, {"id": 0, "name": "DualSense Wireless Controller", ...}]
    [0, 0, "a", 1, -412]
    [1532000123, 0, "b", 0, 1]
    [1612000456, 0, "b", 0, 0]
    [2000000000, null, "m", null, "split:Level 1"]

Row = ``[t_ns, device, kind, code, value]`` where ``t_ns`` is nanoseconds since
the recording started. Kinds: ``b`` button (code = button index, value 1/0),
``a`` axis (code = axis index, value int16), ``+`` device connected (value =
device json), ``-`` device disconnected, ``m`` marker (value = label).
See docs/FORMAT.md for the full specification.

The writer flushes regularly so a crash loses at most the last fraction of a
second; the reader tolerates a truncated final line. Paths ending in ``.gz``
are transparently gzip-compressed.
"""

from __future__ import annotations

import datetime as _dt
import gzip
import io
import json
import threading
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, TextIO

from .model import (AXIS, BUTTON, CONNECT, DISCONNECT, EVENT_KINDS, MARK,
                    DeviceInfo, InputEvent)

FORMAT_NAME = "controllerlog"
FORMAT_VERSION = 1


class LogFormatError(ValueError):
    pass


@dataclass
class Recording:
    """An in-memory recording: header, devices and time-ordered events."""

    header: dict[str, Any] = field(default_factory=dict)
    devices: dict[int, DeviceInfo] = field(default_factory=dict)
    events: list[InputEvent] = field(default_factory=list)

    @property
    def duration_ns(self) -> int:
        return self.events[-1].t_ns if self.events else 0

    @property
    def meta(self) -> dict[str, Any]:
        return self.header.setdefault("meta", {})

    def input_events(self, device: int | None = None) -> Iterator[InputEvent]:
        for ev in self.events:
            if ev.kind in (BUTTON, AXIS) and (device is None or ev.device == device):
                yield ev

    def markers(self) -> list[InputEvent]:
        return [ev for ev in self.events if ev.kind == MARK]

    def primary_device(self) -> int | None:
        """Device with the most input events (the one a single-player run used)."""
        counts: dict[int, int] = {}
        for ev in self.input_events():
            counts[ev.device] = counts.get(ev.device, 0) + 1
        if counts:
            return max(counts, key=counts.__getitem__)
        return min(self.devices) if self.devices else None

    def sort(self) -> None:
        # Stable: preserves write order for identical timestamps.
        self.events.sort(key=lambda e: e.t_ns)

    def save(self, path: str | Path) -> None:
        with LogWriter(path, devices=[], meta=self.meta,
                       header_extra={k: v for k, v in self.header.items()
                                     if k not in ("format", "version", "meta", "devices")}) as w:
            known = set()
            for ev in self.events:
                if ev.kind == CONNECT:
                    known.add(ev.device)
                elif ev.device is not None and ev.device not in known and ev.device in self.devices:
                    # Make the file self-describing even if the source lacked "+" rows.
                    w.write(InputEvent(ev.t_ns, ev.device, CONNECT, None,
                                       self.devices[ev.device].to_json()))
                    known.add(ev.device)
                w.write(ev)


def _open_text(path: Path, mode: str) -> TextIO:
    if path.suffix.lower() == ".gz":
        return io.TextIOWrapper(gzip.open(path, mode.replace("t", "") + "b"),
                                encoding="utf-8", newline="\n")
    return open(path, mode, encoding="utf-8", newline="\n")


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds")


class LogWriter:
    """Streaming, thread-safe ``.ctlog`` writer.

    Events passed to :meth:`write` must already use recording-relative
    timestamps (ns since start). :class:`controllerlog.hub.Recorder` does the
    rebasing for live capture.
    """

    def __init__(self, path: str | Path, devices: Iterable[DeviceInfo] = (),
                 meta: dict[str, Any] | None = None,
                 header_extra: dict[str, Any] | None = None,
                 flush_interval_s: float = 0.25) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = _open_text(self.path, "wt")
        self._lock = threading.Lock()
        self._flush_interval = flush_interval_s
        self._last_flush = time.monotonic()
        self._timer: threading.Timer | None = None   # flushes a tail written just after a flush
        self.count = 0
        header: dict[str, Any] = {
            "format": FORMAT_NAME,
            "version": FORMAT_VERSION,
            "created_utc": _utc_now_iso(),
            "time_unit": "ns",
        }
        if header_extra:
            header.update(header_extra)
        header["meta"] = dict(meta or {})
        self._fh.write(json.dumps(header, ensure_ascii=False) + "\n")
        for dev in devices:
            self.write(InputEvent(0, dev.id, CONNECT, None, dev.to_json()))
        self._fh.flush()

    def write(self, ev: InputEvent) -> None:
        line = json.dumps(ev.to_row(), ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            if self._fh.closed:
                return
            self._fh.write(line + "\n")
            self.count += 1
            now = time.monotonic()
            if now - self._last_flush >= self._flush_interval:
                self._fh.flush()
                self._last_flush = now
            elif self._timer is None:
                # Nothing may follow (end of a burst): flush this tail when the interval is up,
                # so a crash during an idle period loses at most flush_interval of input.
                self._timer = threading.Timer(self._flush_interval - (now - self._last_flush),
                                              self._timed_flush)
                self._timer.daemon = True
                self._timer.start()

    def _timed_flush(self) -> None:
        with self._lock:
            self._timer = None
            if not self._fh.closed:
                self._fh.flush()
                self._last_flush = time.monotonic()

    def mark(self, t_ns: int, label: str) -> None:
        self.write(InputEvent(t_ns, None, MARK, None, label))

    def flush(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.flush()
                self._last_flush = time.monotonic()

    def close(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if not self._fh.closed:
                self._fh.flush()
                self._fh.close()

    def __enter__(self) -> "LogWriter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# Longest line accepted (characters). Real rows are well under 1 KB; the cap keeps a
# crafted file (e.g. one huge gzip-compressed line) from being read into memory whole.
MAX_LINE_CHARS = 1 << 20


def _readline(fh: TextIO, path: Path) -> str:
    line = fh.readline(MAX_LINE_CHARS + 1)
    if len(line) > MAX_LINE_CHARS:
        raise LogFormatError(f"{path}: line longer than {MAX_LINE_CHARS} characters; "
                             "not a controllerlog file")
    return line


def _lines(fh: TextIO, path: Path) -> Iterator[str]:
    """Lines of ``fh``; a gzip stream cut short (a crash while recording ``.ctlog.gz``:
    no end-of-stream marker) ends like a truncated file instead of failing the whole read."""
    while True:
        try:
            line = _readline(fh, path)
        except (EOFError, gzip.BadGzipFile, zlib.error):
            return
        if not line:
            return
        yield line


def iter_log(path: str | Path) -> Iterator[dict[str, Any] | InputEvent]:
    """Yield the header dict, then each event. Tolerates a truncated last line
    (and a ``.gz`` stream that was flushed but never closed)."""
    path = Path(path)
    with _open_text(path, "rt") as fh:
        try:
            first = _readline(fh, path)
        except (EOFError, gzip.BadGzipFile, zlib.error) as e:
            raise LogFormatError(f"{path}: truncated or corrupt gzip data: {e}") from None
        if not first.strip():
            raise LogFormatError(f"{path}: empty file")
        try:
            header = json.loads(first)
        except json.JSONDecodeError as e:
            raise LogFormatError(f"{path}: bad header: {e}") from None
        if not isinstance(header, dict) or header.get("format") != FORMAT_NAME:
            raise LogFormatError(f"{path}: not a controllerlog file")
        if int(header.get("version", 0)) > FORMAT_VERSION:
            raise LogFormatError(f"{path}: format version {header.get('version')} is newer "
                                 f"than supported ({FORMAT_VERSION})")
        yield header
        pending_error: str | None = None
        for lineno, line in enumerate(_lines(fh, path), start=2):
            if not line.strip():
                continue
            if pending_error:
                # A bad line that is *not* the last one is real corruption.
                raise LogFormatError(pending_error)
            try:
                row = json.loads(line)
                ev = InputEvent.from_row(row)
                if ev.kind not in EVENT_KINDS:
                    raise ValueError(f"unknown kind {ev.kind!r}")
                if ev.kind in (BUTTON, AXIS) and (not isinstance(ev.code, int)
                                                  or not isinstance(ev.value, int)):
                    raise ValueError("button/axis rows need integer code and value")
            except (ValueError, TypeError, IndexError) as e:
                pending_error = f"{path}:{lineno}: bad row: {e}"
                continue
            yield ev


def read_log(path: str | Path) -> Recording:
    rec = Recording()
    it = iter_log(path)
    rec.header = next(it)  # type: ignore[assignment]
    for ev in it:
        assert isinstance(ev, InputEvent)
        if ev.kind == CONNECT and isinstance(ev.value, dict):
            info = DeviceInfo.from_json({**ev.value, "id": ev.device})
            rec.devices[ev.device] = info
        rec.events.append(ev)
    rec.sort()
    return rec


def events_between(events: list[InputEvent], start_ns: int, end_ns: int) -> list[InputEvent]:
    """Events with start_ns <= t < end_ns (events must be sorted)."""
    import bisect
    keys = [e.t_ns for e in events]
    return events[bisect.bisect_left(keys, start_ns):bisect.bisect_left(keys, end_ns)]


__all__ = ["Recording", "LogWriter", "LogFormatError", "read_log", "iter_log",
           "events_between", "FORMAT_NAME", "FORMAT_VERSION", "DISCONNECT"]
