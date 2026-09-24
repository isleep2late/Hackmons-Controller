"""Frame-based input movies for TAS-style editing.

A :class:`FrameMovie` is a list of per-frame controller states at a fixed frame
rate — the representation TAS tools work with. It can be built from a
recording (quantized), a GSE ``.gm2`` or a BizHawk ``.bk2``, edited with
simple operations or a small edit script, compared against another movie, and
exported back.

Edit script syntax (one command per line, ``#`` comments, frames are 0-based,
ranges are inclusive ``a-b``)::

    hold 120-130 east            # press GB "A" (= east) on frames 120..130
    release 125 east             # release it on frame 125
    set 200 start,dpad_up        # frame 200 = exactly these buttons (others released)
    clear 300-310                # release everything
    insert 50 3                  # insert 3 neutral frames before frame 50
    insert 50 2 south            # insert 2 frames holding south
    delete 60 2                  # delete frames 60 and 61
    copy 10-19 40                # copy frames 10..19 over frames 40..49
    axis 70-80 left_x -32768     # set an axis on a range
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .logfile import Recording
from .model import (AXES, AXIS_INDEX, BUTTON_INDEX, BUTTONS, CONNECT, MARK,
                    NUM_AXES, NUM_BUTTONS, DeviceInfo, InputEvent, PadState)
from .timeline import FPS, NS_PER_S, Timeline, exact_fps, frame_time_ns, frames_to_events


class EditError(ValueError):
    pass


@dataclass
class FrameMovie:
    fps: float
    frames: list[PadState] = field(default_factory=list)
    system: str = "generic"          # "gb", "gbc", "gba", "nes", "snes", "generic"...
    meta: dict[str, Any] = field(default_factory=dict)
    # For movies imported from a .gm2/.bk2: per frame, the index of the source frame it
    # came from (None = a frame added by an edit). The edit operations keep it in step
    # with ``frames`` so writing back can move resets / Power / cycle budgets with their
    # frames on insert and delete. None = not tracked (recordings, CSV).
    origin: list[int | None] | None = None

    def __len__(self) -> int:
        return len(self.frames)

    def copy(self) -> "FrameMovie":
        return FrameMovie(self.fps, [f.copy() for f in self.frames], self.system, dict(self.meta),
                          None if self.origin is None else list(self.origin))

    @property
    def duration_s(self) -> float:
        return len(self.frames) / self.fps

    def frame_time_ns(self, i: int) -> int:
        return frame_time_ns(i, self.fps)

    def used_buttons(self) -> list[str]:
        used = [False] * NUM_BUTTONS
        for f in self.frames:
            for i, v in enumerate(f.buttons):
                if v:
                    used[i] = True
        return [BUTTONS[i] for i, u in enumerate(used) if u]

    def used_axes(self) -> list[str]:
        return [AXES[a] for a in range(NUM_AXES) if any(f.axes[a] for f in self.frames)]

    def mark_edited(self, note: str) -> None:
        """TAS edits are labelled so edited input is never mistaken for an RTA recording."""
        self.meta["tas_edited"] = True
        self.meta.setdefault("edit_history", []).append(note)


# --- conversions ------------------------------------------------------------------

# Markers that end a recording converted from a frame movie (.bk2/.csv/TAS edit) or a .gm2.
MOVIE_END_MARKERS = ("movie:end", "gm2:end")


def frames_before(end_ns: int, fps: float, start_ns: int) -> int:
    """Number of frames ``i >= 0`` whose start ``frame_time_ns(i)`` is before ``end_ns``."""
    if end_ns <= start_ns:
        return 0
    f = exact_fps(fps)
    n = -(-(end_ns - start_ns) * f.numerator // (NS_PER_S * f.denominator))  # ceil
    while n > 0 and frame_time_ns(n - 1, f, start_ns) >= end_ns:
        n -= 1
    while frame_time_ns(n, f, start_ns) < end_ns:
        n += 1
    return n


def from_recording(rec: Recording, device: int | None = None, fps: float = 60.0,
                   offset_ns: int = 0, mode: str = "any", start_marker: str | None = None,
                   end_ns: int | None = None, system: str = "generic") -> FrameMovie:
    """Quantize a recording into frames (``mode='any'`` keeps sub-frame taps)."""
    dev = rec.primary_device() if device is None else device
    if dev is None:
        return FrameMovie(fps, [], system)
    start = offset_ns
    if start_marker:
        m = find_marker(rec, start_marker)
        if m is None:
            raise EditError(f"marker {start_marker!r} not found")
        start = m.t_ns + offset_ns
    tl = Timeline(rec)
    end = rec.duration_ns if end_ns is None else end_ns
    last = rec.events[-1] if rec.events else None
    if end_ns is None and last is not None and last.kind == MARK and last.value in MOVIE_END_MARKERS:
        # A converted frame movie ends exactly where its last frame ends: count only the
        # frames that start before that point, so a round trip doesn't grow the movie.
        count = frames_before(end, fps, start)
    else:
        count = max(0, int((end - start) * fps // NS_PER_S) + 1)
    frames = tl.frames(dev, fps=fps, offset_ns=start, mode=mode, count=count)
    meta = {"source": "recording", "quantize_mode": mode, "start_ns": start,
            **{k: v for k, v in rec.meta.items() if k in ("game", "category", "runner")}}
    return FrameMovie(fps, frames, system, meta)


def to_recording(movie: FrameMovie, device: DeviceInfo | None = None) -> Recording:
    dev = device or DeviceInfo(0, f"{movie.system} frame movie", backend="tas",
                               family={"gb": "gameboy", "gbc": "gameboy", "gbc_gba": "gameboy",
                                       "gba": "gba"}.get(movie.system, "generic"))
    dev.id = 0
    rec = Recording(header={"format": "controllerlog", "version": 1, "time_unit": "ns",
                            "clock": "frames", "meta": {**movie.meta, "fps": movie.fps,
                                                        "system": movie.system}})
    rec.devices[0] = dev
    rec.events.append(InputEvent(0, 0, CONNECT, None, dev.to_json()))
    rec.events.extend(frames_to_events(movie.frames, movie.fps, device=0))
    end = movie.frame_time_ns(len(movie.frames))
    last = movie.frames[-1] if movie.frames else PadState()
    for b in range(NUM_BUTTONS):
        if last.buttons[b]:
            rec.events.append(InputEvent(end, 0, "b", b, 0))
    rec.events.append(InputEvent(end, None, MARK, None, "movie:end"))
    rec.sort()
    return rec


def find_marker(rec: Recording, label: str) -> InputEvent | None:
    for m in rec.markers():
        if isinstance(m.value, str) and (m.value == label or m.value.startswith(label)):
            return m
    return None


# --- edit operations ------------------------------------------------------------

def _check_range(movie: FrameMovie, a: int, b: int) -> None:
    if a < 0 or b < a:
        raise EditError(f"bad frame range {a}-{b}")


def _ensure_len(movie: FrameMovie, n: int) -> None:
    while len(movie.frames) < n:
        movie.frames.append(PadState())
        if movie.origin is not None:
            movie.origin.append(None)


def _buttons(names: Iterable[str]) -> list[int]:
    out = []
    for n in names:
        n = n.strip()
        if not n:
            continue
        if n not in BUTTON_INDEX:
            raise EditError(f"unknown button {n!r}")
        out.append(BUTTON_INDEX[n])
    return out


def hold(movie: FrameMovie, a: int, b: int, names: Iterable[str], down: bool = True) -> None:
    _check_range(movie, a, b)
    _ensure_len(movie, b + 1)
    idx = _buttons(names)
    for f in movie.frames[a:b + 1]:
        for i in idx:
            f.buttons[i] = 1 if down else 0


def set_exact(movie: FrameMovie, a: int, b: int, names: Iterable[str]) -> None:
    _check_range(movie, a, b)
    _ensure_len(movie, b + 1)
    idx = set(_buttons(names))
    for f in movie.frames[a:b + 1]:
        f.buttons = [1 if i in idx else 0 for i in range(NUM_BUTTONS)]


def clear(movie: FrameMovie, a: int, b: int) -> None:
    set_exact(movie, a, b, [])


def set_axis(movie: FrameMovie, a: int, b: int, axis: str, value: int) -> None:
    _check_range(movie, a, b)
    if axis not in AXIS_INDEX:
        raise EditError(f"unknown axis {axis!r}")
    lo = 0 if axis.endswith("trigger") else -32768
    if not lo <= value <= 32767:
        raise EditError(f"axis value {value} out of range")
    _ensure_len(movie, b + 1)
    for f in movie.frames[a:b + 1]:
        f.axes[AXIS_INDEX[axis]] = value


def insert(movie: FrameMovie, at: int, count: int, names: Iterable[str] = ()) -> None:
    if at < 0 or count < 0:
        raise EditError("bad insert")
    _ensure_len(movie, at)
    idx = set(_buttons(names))
    new = [PadState([1 if i in idx else 0 for i in range(NUM_BUTTONS)], [0] * NUM_AXES)
           for _ in range(count)]
    movie.frames[at:at] = new
    if movie.origin is not None:
        movie.origin[at:at] = [None] * count


def delete(movie: FrameMovie, at: int, count: int) -> None:
    if at < 0 or count < 0 or at + count > len(movie.frames):
        raise EditError(f"cannot delete {count} frame(s) at {at} (movie has {len(movie.frames)})")
    del movie.frames[at:at + count]
    if movie.origin is not None:
        del movie.origin[at:at + count]


def copy_range(movie: FrameMovie, a: int, b: int, dest: int) -> None:
    """Copy the inputs of frames a..b over frames dest.. (the destination frames keep
    their place, so a .gm2/.bk2 keeps its resets / Power there)."""
    _check_range(movie, a, b)
    if b >= len(movie.frames):
        raise EditError("copy source beyond end of movie")
    src = [f.copy() for f in movie.frames[a:b + 1]]
    _ensure_len(movie, dest + len(src))
    movie.frames[dest:dest + len(src)] = src


def _parse_range(tok: str) -> tuple[int, int]:
    try:
        if "-" in tok:
            a, b = tok.split("-", 1)
            return int(a), int(b)
        v = int(tok)
        return v, v
    except ValueError:
        raise EditError(f"bad frame/range {tok!r}") from None


def apply_script(movie: FrameMovie, script: str) -> FrameMovie:
    """Apply an edit script (see module docstring) to a copy of ``movie``."""
    out = movie.copy()
    for lineno, raw in enumerate(script.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        cmd, args = parts[0].lower(), parts[1:]
        try:
            if cmd in ("hold", "press") and len(args) == 2:
                a, b = _parse_range(args[0])
                hold(out, a, b, args[1].split(","))
            elif cmd == "release" and len(args) == 2:
                a, b = _parse_range(args[0])
                hold(out, a, b, args[1].split(","), down=False)
            elif cmd == "set" and len(args) in (1, 2):
                a, b = _parse_range(args[0])
                set_exact(out, a, b, args[1].split(",") if len(args) == 2 else [])
            elif cmd == "clear" and len(args) == 1:
                a, b = _parse_range(args[0])
                clear(out, a, b)
            elif cmd == "insert" and len(args) in (2, 3):
                insert(out, int(args[0]), int(args[1]), args[2].split(",") if len(args) == 3 else [])
            elif cmd == "delete" and len(args) == 2:
                delete(out, int(args[0]), int(args[1]))
            elif cmd == "copy" and len(args) == 2:
                a, b = _parse_range(args[0])
                copy_range(out, a, b, int(args[1]))
            elif cmd == "axis" and len(args) == 3:
                a, b = _parse_range(args[0])
                set_axis(out, a, b, args[1], int(args[2]))
            else:
                raise EditError(f"unknown command or wrong arguments: {line!r}")
        except (EditError, ValueError) as e:
            raise EditError(f"line {lineno}: {e}") from None
        out.mark_edited(line)
    return out


# --- comparison -------------------------------------------------------------------

@dataclass
class FrameDiff:
    frame: int
    only_a: list[str]
    only_b: list[str]


def diff(a: FrameMovie, b: FrameMovie, limit: int | None = None) -> list[FrameDiff]:
    """Frames whose digital inputs differ between two movies."""
    out: list[FrameDiff] = []
    for i in range(max(len(a), len(b))):
        fa = a.frames[i] if i < len(a) else PadState()
        fb = b.frames[i] if i < len(b) else PadState()
        if fa.buttons != fb.buttons:
            out.append(FrameDiff(i, [BUTTONS[j] for j in range(NUM_BUTTONS) if fa.buttons[j] and not fb.buttons[j]],
                                 [BUTTONS[j] for j in range(NUM_BUTTONS) if fb.buttons[j] and not fa.buttons[j]]))
            if limit and len(out) >= limit:
                break
    return out


# --- CSV frame tables ---------------------------------------------------------------

def to_csv(movie: FrameMovie, buttons: list[str] | None = None, axes: list[str] | None = None) -> str:
    buttons = buttons if buttons is not None else movie.used_buttons()
    axes = axes if axes is not None else movie.used_axes()
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["frame", "time_ms", *buttons, *axes])
    bi = [BUTTON_INDEX[n] for n in buttons]
    ai = [AXIS_INDEX[n] for n in axes]
    for i, f in enumerate(movie.frames):
        w.writerow([i, f"{movie.frame_time_ns(i) / 1e6:.3f}",
                    *(f.buttons[j] for j in bi), *(f.axes[j] for j in ai)])
    return buf.getvalue()


def from_csv(text: str, fps: float, system: str = "generic") -> FrameMovie:
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return FrameMovie(fps, [], system)
    head = [h.lstrip("﻿").strip() for h in rows[0]]
    cols = []
    for j, name in enumerate(head):
        if name in BUTTON_INDEX:
            cols.append((j, "b", BUTTON_INDEX[name]))
        elif name in AXIS_INDEX:
            cols.append((j, "a", AXIS_INDEX[name]))
        elif name not in ("frame", "time_ms"):
            raise EditError(f"unknown CSV column {name!r}")
    frames = []
    for row in rows[1:]:
        if not row:
            continue
        st = PadState()
        for j, kind, idx in cols:
            v = int(row[j] or 0)
            if kind == "b":
                st.buttons[idx] = 1 if v else 0
            else:
                st.axes[idx] = v
        frames.append(st)
    return FrameMovie(fps, frames, system, {"source": "csv"})


def save_csv(movie: FrameMovie, path: str | Path, **kw) -> None:
    Path(path).write_text(to_csv(movie, **kw), encoding="utf-8", newline="\n")


def read_text(path: str | Path) -> str:
    """Read a user-edited text file: UTF-8 with or without BOM (Excel "CSV UTF-8",
    PowerShell 5.1 ``Out-File -Encoding utf8``), or UTF-16 with BOM (PowerShell 5.1 ``>``)."""
    data = Path(path).read_bytes()
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    return data.decode("utf-8-sig")


def csv_fps(text: str) -> float | None:
    """Frame rate implied by a frame table's ``frame`` / ``time_ms`` columns (None if unknown).

    Snapped to a known rate (gb, nes, 60, ...) when within 0.1 %.
    """
    rows = [r for r in csv.reader(io.StringIO(text)) if r]
    if len(rows) < 3:
        return None
    head = [h.lstrip("﻿").strip() for h in rows[0]]
    if "frame" not in head or "time_ms" not in head:
        return None
    jf, jt = head.index("frame"), head.index("time_ms")
    try:
        f0, t0 = int(rows[1][jf]), float(rows[1][jt])
        f1, t1 = int(rows[-1][jf]), float(rows[-1][jt])
    except (ValueError, IndexError):
        return None
    if f1 <= f0 or t1 <= t0:
        return None
    est = (f1 - f0) * 1000.0 / (t1 - t0)
    best = min(FPS.values(), key=lambda v: abs(v - est))
    return best if abs(best - est) <= best * 1e-3 else est


def load_csv(path: str | Path, fps: float | None, system: str = "generic") -> FrameMovie:
    """Load a frame table; ``fps=None`` uses the rate implied by its time_ms column (else 60)."""
    text = read_text(path)
    if fps is None:
        fps = csv_fps(text) or 60.0
    return from_csv(text, fps, system)
