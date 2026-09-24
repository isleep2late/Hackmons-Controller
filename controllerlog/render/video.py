"""Offline input-display rendering: recordings -> PNG sequence, GIF/WebP or video.

Frame ``i`` shows the controller state at ``start + round(i * 1e9 / fps)`` ns
(exact rational arithmetic, so Game Boy's 4194304/70224 fps never drifts). A
clip from ``start`` to ``end`` has every frame with a time <= ``end``, i.e.
``floor((end - start) * fps / 1e9) + 1`` frames, matching
:meth:`controllerlog.timeline.Timeline.frames`.

Video output pipes raw frames into ffmpeg (``CONTROLLERLOG_FFMPEG``, ``PATH`` or
the optional ``imageio-ffmpeg`` package).
"""

from __future__ import annotations

import collections
import io
import os
import shutil
import subprocess
import sys
import threading
import warnings
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from PIL import Image, ImageDraw

from ..layouts import LayoutError, layout_for_family, load_layout
from ..logfile import Recording
from ..model import (AXIS, AXIS_MAX, BUTTON, FAMILY_GENERIC, NUM_AXES, NUM_BUTTONS,
                     TRIGGER_AXES, PadState)
from ..timeline import NS_PER_S, Timeline, resolve_fps
from .draw import (MONO_FONTS, FontBook, HistoryPainter, LayoutPainter, composite,
                   default_row_colors, default_row_labels, normalize_layout, parse_color)

CHROMA_GREEN = (0, 255, 0, 255)
IMAGE_SEQUENCE = "png"
ANIMATED = {".gif": "gif", ".webp": "webp"}
VIDEO_SUFFIXES = (".mp4", ".webm", ".mov", ".mkv")
LONG_ANIMATION_FRAMES = 1800  # warn above this many frames for GIF/WebP

ProgressFn = Callable[[int, int], None]


class FFmpegNotFoundError(RuntimeError):
    """Raised when video output is requested but no ffmpeg executable is available."""


# --- timing ------------------------------------------------------------------------

def fps_fraction(fps: float | str | Fraction) -> Fraction:
    """Exact frame rate: ``"gb"`` -> 262144/4389, ``60.0`` -> 60, ``59.94`` -> 2997/50,
    ``"60000/1001"`` -> 60000/1001."""
    if isinstance(fps, Fraction):
        q = fps
    elif isinstance(fps, str) and "/" in fps:  # exact rational, e.g. "60000/1001"
        try:
            q = Fraction(fps.strip())
        except ZeroDivisionError:
            raise ValueError(f"bad fps {fps!r}: division by zero") from None
    else:
        q = Fraction(resolve_fps(fps)).limit_denominator(1_000_000)
    if q <= 0:
        raise ValueError("fps must be positive")
    return q


def frame_time_ns(i: int, start_ns: int, fps: float | str | Fraction) -> int:
    """Timestamp shown by frame ``i``: ``start + round(i * 1e9 / fps)`` (half up)."""
    q = fps_fraction(fps)
    num = i * NS_PER_S * q.denominator
    return start_ns + (2 * num + q.numerator) // (2 * q.numerator)


def frame_count(start_ns: int, end_ns: int, fps: float | str | Fraction) -> int:
    """Number of frames whose time is <= ``end_ns`` (at least 1)."""
    if end_ns < start_ns:
        raise ValueError(f"end ({end_ns}) is before start ({start_ns})")
    q = fps_fraction(fps)
    i = (end_ns - start_ns) * q.numerator // (NS_PER_S * q.denominator)
    while frame_time_ns(i + 1, start_ns, q) <= end_ns:
        i += 1
    while i > 0 and frame_time_ns(i, start_ns, q) > end_ns:
        i -= 1
    return i + 1


def format_timecode(ns: int) -> str:
    """``-`` + ``[h:]mm:ss.mmm`` (milliseconds truncated)."""
    sign = "-" if ns < 0 else ""
    ms = abs(ns) // 1_000_000
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{sign}{h}:{m:02d}:{s:02d}.{ms:03d}" if h else f"{sign}{m:02d}:{s:02d}.{ms:03d}"


def find_marker(rec: Recording, label: str, after_ns: int | None = None) -> int:
    """Time of the first marker labelled ``label`` (exact match, else prefix match)."""
    marks = [ev for ev in rec.markers() if after_ns is None or ev.t_ns >= after_ns]
    for ev in marks:
        if str(ev.value) == label:
            return ev.t_ns
    for ev in marks:
        if str(ev.value).startswith(label):
            return ev.t_ns
    names = sorted({str(ev.value) for ev in rec.markers()})
    raise ValueError(f"marker {label!r} not found (markers: {', '.join(names) or 'none'})")


def resolve_range(rec: Recording, start_ns: int = 0, end_ns: int | None = None,
                  start_marker: str | None = None, end_marker: str | None = None,
                  duration_ns: int | None = None) -> tuple[int, int]:
    """Clip range in recording time.

    ``start_marker`` makes ``start_ns`` an offset from that marker (negative =
    lead-in). The end is ``start + duration_ns``, else the first ``end_marker``
    strictly after the start (and after the start marker, so ``start_marker=
    "split:2", end_marker="split"`` ends at the next split), else ``end_ns``
    (absolute), else the end of the recording.
    """
    start = int(start_ns)
    anchor = start
    if start_marker:
        mark = find_marker(rec, start_marker)
        start += mark
        anchor = max(start, mark)
    if duration_ns is not None:
        end = start + int(duration_ns)
    elif end_marker:
        end = find_marker(rec, end_marker, after_ns=anchor + 1)
    elif end_ns is not None:
        end = int(end_ns)
    else:
        end = max(start, rec.duration_ns)
    if end < start:
        raise ValueError(f"clip end ({end} ns) is before its start ({start} ns)")
    return start, end


def iter_frame_states(rec: Recording, device: int | None, fps: float | str | Fraction,
                      start_ns: int, count: int, mode: str = "sample") -> Iterator[tuple[int, PadState]]:
    """Yield ``(t_ns, state)`` per frame, with :meth:`Timeline.frames` semantics.

    ``sample``: state at the frame time. ``any``: buttons pressed at any moment
    of ``[t_i, t_i+1)`` count as held; axes are the value at the end of the frame.
    """
    if mode not in ("sample", "any"):
        raise ValueError("mode must be 'sample' or 'any'")
    q = fps_fraction(fps)
    events = sorted((ev for ev in rec.events if ev.kind in (BUTTON, AXIS) and ev.device == device),
                    key=lambda ev: ev.t_ns)
    n = len(events)
    st = PadState()
    j = 0
    t = frame_time_ns(0, start_ns, q)
    for i in range(count):
        while j < n and events[j].t_ns <= t:
            st.apply(events[j])
            j += 1
        t_next = frame_time_ns(i + 1, start_ns, q)
        if mode == "sample":
            yield t, st.copy()
        else:
            held = list(st.buttons)
            while j < n and events[j].t_ns < t_next:
                ev = events[j]
                st.apply(ev)
                if ev.kind == BUTTON and ev.value and ev.code is not None and 0 <= ev.code < NUM_BUTTONS:
                    held[ev.code] = 1
                j += 1
            yield t, PadState(held, list(st.axes))
        t = t_next


# --- composition ---------------------------------------------------------------------

def parse_background(value: str | None) -> tuple[int, int, int, int]:
    """``"chroma"`` (#00ff00), ``"transparent"``, or any CSS colour -> RGBA."""
    if value is None or str(value).lower() in ("chroma", "green", "greenscreen"):
        return CHROMA_GREEN
    if str(value).lower() in ("transparent", "alpha", "none"):
        return (0, 0, 0, 0)
    c = parse_color(value)
    if c is None:
        raise ValueError(f"bad background {value!r}")
    return c


def all_pressed_state() -> PadState:
    """Every button down, triggers full, sticks pushed to a corner (palette probe)."""
    axes = [AXIS_MAX if a in TRIGGER_AXES else 23170 for a in range(NUM_AXES)]
    return PadState([1] * NUM_BUTTONS, axes)


class FrameRenderer:
    """Composes one output frame: background, frame counter strip, controller, history lane.

    ``layout`` is a layout name/path or dict. Layout pixels are multiplied by
    ``scale``; the history lane (``layout["history"]`` rows) sits under the
    controller and the frame counter (frame number + time since ``origin_ns``)
    gets its own strip above it, so neither hides any part of the controller.
    :meth:`render` returns RGB for opaque backgrounds and RGBA for transparent
    ones. ``family`` picks fallback history labels when the layout declares no
    ``families``. ``even_size`` pads to even dimensions (needed by yuv420p).
    ``duration_ns`` (the clip length) sizes the frame-counter box so the largest
    frame number and ``h:mm:ss.mmm`` timecode fit without the box changing width;
    without it the box is sized for ``000000  -00:00.000`` and widens as needed.
    """

    def __init__(self, layout: str | Path | Mapping[str, Any], *, scale: float = 1.0,
                 background: str | None = "chroma", history: bool = True,
                 history_seconds: float = 4.0, fps: float | str | Fraction = 60.0,
                 frame_counter: bool = False, mapping: Mapping[str, str] | str | None = None,
                 font_path: str | None = None, family: str | None = None,
                 even_size: bool = False, supersample: int = 2,
                 duration_ns: int | None = None) -> None:
        lay = load_layout(layout) if isinstance(layout, (str, Path)) else normalize_layout(layout)
        self.layout = lay
        self.fps = fps_fraction(fps)
        self.scale = float(scale)
        self.painter = LayoutPainter(lay, scale=scale, mapping=mapping, font_path=font_path,
                                     supersample=supersample)
        self.background = parse_background(background)
        self.transparent = self.background[3] < 255
        family = (lay.get("families") or [family or FAMILY_GENERIC])[0]
        margin = max(2, round(8 * scale))
        gap = max(2, round(6 * scale))
        th = lay["theme"]
        self.frame_counter = frame_counter
        self._counter: tuple[int, int, int, int] | None = None
        top = 0
        if frame_counter:
            self._mono = FontBook(None, MONO_FONTS).get(max(8, round(13 * scale)), "0123456789:.-")
            pad = max(2, round(4 * scale))
            template = "000000  -00:00.000"
            if duration_ns is not None:
                last = frame_count(0, max(0, int(duration_ns)), self.fps) - 1
                template = "".join("0" if c.isdigit() else c for c in
                                   self._counter_text(last, max(0, int(duration_ns))))
            l, t, r, b = self._mono.getbbox(template, anchor="lm")
            self._counter = (margin, gap, int(r - l) + 2 * pad, int(b - t) + 2 * pad)
            self._counter_colors = (parse_color(th["idle"]), parse_color(th["idle_stroke"]),
                                    parse_color(th["label"]))
            top = gap + self._counter[3] + gap
        self.controller_dest = (0, top)
        cw, ch = self.painter.size
        w, h = cw, top + ch
        self.history: HistoryPainter | None = None
        self.history_dest = (0, 0)
        rows = list(dict.fromkeys(lay.get("history", [])))
        if history and rows:
            row_h = max(7, round(14 * scale))
            pad = max(2, round(4 * scale))
            hh = row_h * len(rows) + 2 * pad
            self.history = HistoryPainter(
                rows, cw - 2 * margin, hh, history_seconds, float(self.fps), th,
                labels=default_row_labels(rows, family, lay), colors=default_row_colors(lay),
                mapping=self.painter.mapping, font_path=font_path, scale=scale, family=family,
                background=None if self.transparent else self.background)
            self.history_dest = (margin, top + ch + gap)
            h = top + ch + gap + hh + margin
        if even_size:
            w, h = w + (w & 1), h + (h & 1)
        self.size = (w, h)
        self._bg = Image.new(self.mode, self.size, self.background if self.transparent else self.background[:3])
        self._ctrl_key: tuple[Any, ...] | None = None
        self._ctrl: Image.Image = self._bg

    @property
    def dynamic(self) -> bool:
        """True when every frame differs (scrolling history lane or frame counter)."""
        return self.history is not None or self.frame_counter

    @property
    def mode(self) -> str:
        return "RGBA" if self.transparent else "RGB"

    def key(self, state: PadState) -> tuple[Any, ...]:
        """Frames with equal keys are identical when :attr:`dynamic` is False."""
        return self.painter.visual_key(state)

    def render(self, state: PadState, *, timeline: Timeline | None = None, device: int | None = None,
               t_ns: int = 0, origin_ns: int = 0, frame: int | None = None) -> Image.Image:
        """Render one frame. ``timeline``/``device``/``t_ns`` feed the history lane;
        ``origin_ns`` aligns its frame grid and is the counter's zero time."""
        key = self.painter.visual_key(state)
        if key != self._ctrl_key:  # controller composite is cached per visual key
            ctrl = self._bg.copy()
            self.painter.paint(state, ctrl, self.controller_dest)
            self._ctrl_key, self._ctrl = key, ctrl
        canvas = self._ctrl.copy()
        if self.history is not None:
            self.history.paint(timeline, device, t_ns, canvas, self.history_dest, origin_ns=origin_ns)
        if self._counter is not None:
            self._draw_counter(canvas, frame if frame is not None else 0, t_ns - origin_ns)
        return canvas

    @staticmethod
    def _counter_text(frame: int, rel_ns: int) -> str:
        return f"{frame:06d}  {format_timecode(rel_ns)}"

    def _draw_counter(self, canvas: Image.Image, frame: int, rel_ns: int) -> None:
        assert self._counter is not None
        x, y, w, h = self._counter
        fill, border, color = self._counter_colors
        pad = max(2, round(4 * self.scale))
        text = self._counter_text(frame, rel_ns)
        l, _, r, _ = self._mono.getbbox(text, anchor="lm")
        if r - l + 2 * pad > w:  # longer than the box was sized for: widen, never clip
            w = max(w, min(int(r - l) + 2 * pad, canvas.width - x))
        sprite = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(sprite)
        d.rounded_rectangle([0, 0, w - 1, h - 1], radius=pad, fill=fill, outline=border,
                            width=max(1, round(self.scale)))
        d.text((pad, h / 2), text, font=self._mono, fill=color, anchor="lm")
        composite(canvas, sprite, x, y)


# --- writers ---------------------------------------------------------------------------

class _PngSequenceWriter:
    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self._ver = -1
        self._data = b""
        self.count = 0

    def add(self, img: Image.Image, version: int, rel_ns: int) -> None:
        if version != self._ver:
            buf = io.BytesIO()
            img.save(buf, "PNG", compress_level=1)
            self._data, self._ver = buf.getvalue(), version
        (self.dir / f"frame_{self.count:06d}.png").write_bytes(self._data)
        self.count += 1

    def close(self, total_ns: int) -> None:
        stale = self.dir / f"frame_{self.count:06d}.png"
        if stale.exists():
            warnings.warn(f"{self.dir} contains frame files from an older, longer render "
                          f"(from {stale.name} on)", RuntimeWarning, stacklevel=3)

    def abort(self) -> None:
        pass


class _AnimationWriter:
    """GIF / WebP. Identical frames are merged; frame starts are quantized to the
    format's delay unit and frames shown for less than the minimum delay that
    browsers honour are skipped (GIF: 10 ms units, >= 20 ms)."""

    def __init__(self, path: Path, fmt: str, transparent: bool,
                 probe: Callable[[], Image.Image] | None = None) -> None:
        self.path, self.fmt, self.transparent = path, fmt, transparent
        self.unit, self.min_ms = (10, 20) if fmt == "gif" else (1, 11)
        self._probe = probe
        self._palette: Image.Image | None = None
        self._frames: list[tuple[Image.Image, int]] = []
        self._ver = -1
        self.skipped = 0

    def _q(self, rel_ns: int) -> int:
        return (rel_ns + self.unit * 500_000) // (self.unit * 1_000_000) * self.unit

    def add(self, img: Image.Image, version: int, rel_ns: int) -> None:
        if version == self._ver:
            return
        t = self._q(rel_ns)
        if self._frames and t - self._frames[-1][1] < self.min_ms:
            self.skipped += 1
            return
        self._frames.append((self._convert(img), t))
        self._ver = version

    def _convert(self, img: Image.Image) -> Image.Image:
        if self.fmt != "gif":
            return img
        rgb = img.convert("RGB")
        if self._palette is None:
            probe = [rgb] + ([self._probe().convert("RGB")] if self._probe else [])
            sheet = Image.new("RGB", (max(p.width for p in probe), sum(p.height for p in probe)))
            y = 0
            for p in probe:
                sheet.paste(p, (0, y))
                y += p.height
            self._palette = sheet.quantize(colors=255, method=Image.Quantize.MEDIANCUT,
                                           dither=Image.Dither.NONE)
        out = rgb.quantize(palette=self._palette, dither=Image.Dither.NONE)
        if self.transparent:
            clear = img.getchannel("A").point(lambda a: 255 if a < 128 else 0, "L")
            out.paste(255, mask=clear)
        return out

    def close(self, total_ns: int) -> None:
        if not self._frames:
            raise RuntimeError("no frames rendered")
        # The last frame also gets the minimum delay (browsers stretch shorter ones to 100 ms).
        total = max(self._q(total_ns), self._frames[-1][1] + self.min_ms)
        starts = [t for _, t in self._frames] + [total]
        durations = [max(self.unit, b - a) for a, b in zip(starts, starts[1:])]
        frames = [f for f, _ in self._frames]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.fmt == "gif":
            extra: dict[str, Any] = {"disposal": 2 if self.transparent else 1}
            if self.transparent:
                extra["transparency"] = 255
            frames[0].save(self.path, format="GIF", save_all=True, append_images=frames[1:],
                           duration=durations, loop=0, optimize=False, **extra)
        else:
            frames[0].save(self.path, format="WEBP", save_all=True, append_images=frames[1:],
                           duration=durations, loop=0, lossless=True, quality=40, method=2,
                           background=(0, 0, 0, 0))
        if self.skipped:
            warnings.warn(f"{self.path.name}: {self.skipped} frame(s) shorter than {self.min_ms} ms "
                          f"were merged ({self.fmt.upper()} timing limits); use .webm/.mp4 or a PNG "
                          f"sequence for frame-exact output", RuntimeWarning, stacklevel=3)

    def abort(self) -> None:
        self._frames.clear()


class _FFmpegWriter:
    def __init__(self, exe: str, path: Path, size: tuple[int, int], fps: Fraction, mode: str,
                 codec_args: list[str]) -> None:
        self.path = path
        self.mode = mode
        path.parent.mkdir(parents=True, exist_ok=True)
        self._before = self._stat()
        cmd = [exe, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-f", "rawvideo", "-pix_fmt", "rgba" if mode == "RGBA" else "rgb24",
               "-s", f"{size[0]}x{size[1]}", "-framerate", f"{fps.numerator}/{fps.denominator}",
               "-i", "pipe:0", "-an", *codec_args, str(path)]
        self.cmd = cmd
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, creationflags=flags)
        self._err: collections.deque[str] = collections.deque(maxlen=40)
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()
        self._ver = -1
        self._data = b""

    def _stat(self) -> tuple[int, int] | None:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return st.st_size, st.st_mtime_ns

    def _drain(self) -> None:
        assert self.proc.stderr is not None
        for line in iter(self.proc.stderr.readline, b""):
            self._err.append(line.decode("utf-8", "replace").rstrip())

    def _error(self, what: str) -> RuntimeError:
        tail = "\n".join(self._err) or "(no output)"
        return RuntimeError(f"ffmpeg {what} (exit {self.proc.returncode}) for {self.path}:\n{tail}")

    def add(self, img: Image.Image, version: int, rel_ns: int) -> None:
        if version != self._ver:
            self._data, self._ver = img.tobytes(), version
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write(self._data)
        except (BrokenPipeError, OSError):
            self.abort()
            raise self._error("stopped accepting frames") from None

    def close(self, total_ns: int) -> None:
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            rc = self.proc.wait(timeout=600)
        except subprocess.TimeoutExpired:
            self.abort()
            raise self._error("timed out") from None
        self._reader.join(timeout=5)
        if rc != 0:
            raise self._error("failed")

    def abort(self) -> None:
        """Kill ffmpeg and delete the partial (unplayable) file it wrote, if any."""
        if self.proc.poll() is None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
            except OSError:
                pass
            self.proc.kill()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        self._reader.join(timeout=5)
        now = self._stat()
        if self.proc.returncode is not None and now is not None and now != self._before:
            try:
                self.path.unlink()
            except OSError:
                pass


# --- ffmpeg ------------------------------------------------------------------------------

def find_ffmpeg(explicit: str | None = None) -> str | None:
    """Locate ffmpeg: ``explicit``, ``$CONTROLLERLOG_FFMPEG``, ``PATH``, then imageio-ffmpeg."""
    for cand in (explicit, os.environ.get("CONTROLLERLOG_FFMPEG")):
        if cand:
            if Path(cand).is_file():
                return str(cand)
            found = shutil.which(cand)
            if found:
                return found
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # type: ignore[import-not-found]
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).is_file():
            return str(exe)
    except Exception:
        pass
    return None


# RGB -> limited-range BT.709 YUV, tagged as such (matrix, primaries and transfer).
_TO_709 = ["-vf", "scale=out_color_matrix=bt709:out_range=tv,"
                  "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv"]


def codec_args(suffix: str, alpha: bool, codec: str | None = None, crf: int | None = None) -> list[str]:
    """ffmpeg output arguments for a container suffix (``.mp4/.webm/.mov/.mkv``).

    Defaults: mp4 = H.264 yuv420p (no alpha); webm = VP9 (yuva420p with alpha);
    mov = QuickTime Animation/qtrle (lossless, argb with alpha), or
    ``codec="prores"`` for ProRes 4444; mkv = FFV1 lossless.
    """
    suffix = suffix.lower()
    codec = (codec or {".mp4": "h264", ".webm": "vp9", ".mov": "qtrle", ".mkv": "ffv1"}[suffix]).lower()
    if codec in ("h264", "libx264", "x264"):
        if alpha:
            raise ValueError("H.264 has no alpha channel: use .webm (VP9) or .mov (qtrle/prores) "
                             "for a transparent background, or --bg chroma/#rrggbb")
        return ["-c:v", "libx264", "-preset", "medium", "-crf", str(16 if crf is None else crf),
                "-pix_fmt", "yuv420p", *_TO_709,
                *(["-movflags", "+faststart"] if suffix in (".mp4", ".mov") else [])]
    if codec in ("vp9", "libvpx-vp9"):
        args = ["-c:v", "libvpx-vp9", "-crf", str(24 if crf is None else crf), "-b:v", "0",
                "-row-mt", "1", "-deadline", "good", "-cpu-used", "4"]
        if alpha:
            return args + ["-pix_fmt", "yuva420p", "-auto-alt-ref", "0"]
        return args + ["-pix_fmt", "yuv420p", *_TO_709]
    if codec == "qtrle":
        return ["-c:v", "qtrle", "-pix_fmt", "argb" if alpha else "rgb24"]
    if codec in ("prores", "prores_ks", "prores4444"):
        return ["-c:v", "prores_ks", "-profile:v", "4444", "-vendor", "apl0",
                "-pix_fmt", "yuva444p10le" if alpha else "yuv444p10le", *_TO_709]
    if codec == "ffv1":
        return ["-c:v", "ffv1", "-level", "3", "-pix_fmt", "bgra" if alpha else "bgr0"]
    raise ValueError(f"unsupported codec {codec!r} (h264, vp9, qtrle, prores, ffv1)")


def output_kind(out: Path) -> str:
    """``"png"`` (directory / no suffix), ``"gif"``, ``"webp"`` or ``"video"``."""
    if out.is_dir() or not out.suffix:
        return IMAGE_SEQUENCE
    suffix = out.suffix.lower()
    if suffix in ANIMATED:
        return ANIMATED[suffix]
    if suffix in VIDEO_SUFFIXES:
        return "video"
    raise ValueError(f"unsupported output {out.name!r}: use a folder (PNG sequence), .gif, .webp, "
                     f"{', '.join(VIDEO_SUFFIXES)}")


# --- main entry point ------------------------------------------------------------------------

def _pick_device(rec: Recording, device: int | None) -> int | None:
    if device is not None:
        return device
    return rec.primary_device()


def _sorted_recording(rec: Recording) -> Recording:
    ev = rec.events
    if all(ev[i].t_ns <= ev[i + 1].t_ns for i in range(len(ev) - 1)):
        return rec
    return Recording(rec.header, rec.devices, sorted(ev, key=lambda e: e.t_ns))


def resolve_layout(rec: Recording, layout: str | Path | Mapping[str, Any] | None,
                   device: int | None) -> dict[str, Any]:
    """Layout dict for ``layout`` (name, path or dict), or the device family's default."""
    if layout is None:
        fam = rec.devices[device].family if device in rec.devices else FAMILY_GENERIC
        try:
            layout = layout_for_family(fam)
        except IndexError:
            raise LayoutError("no controller layouts installed (controllerlog/layouts/*.json)") from None
    if isinstance(layout, (str, Path)):
        return load_layout(layout)
    return normalize_layout(layout)


def render_recording(rec: Recording, out: str | Path, layout: str | Path | Mapping[str, Any] | None = None,
                     device: int | None = None, fps: float | str | Fraction = 60.0, start_ns: int = 0,
                     end_ns: int | None = None, start_marker: str | None = None, scale: float = 1.0,
                     background: str = "chroma", history: bool = True, history_seconds: float = 4.0,
                     frame_counter: bool = False, mapping: Mapping[str, str] | str | None = None,
                     mode: str = "sample", progress: ProgressFn | None = None, *,
                     end_marker: str | None = None, duration_ns: int | None = None,
                     codec: str | None = None, crf: int | None = None, font_path: str | None = None,
                     ffmpeg: str | None = None, supersample: int = 2) -> Path:
    """Render an input-display clip of one device of ``rec`` and return the output path.

    Output type follows ``out``: a directory or suffix-less path -> PNG sequence
    (``frame_000000.png``...), ``.gif``/``.webp`` -> animation, ``.mp4``/``.webm``/
    ``.mov``/``.mkv`` -> video through ffmpeg. ``background`` is ``"chroma"``
    (#00ff00), ``"transparent"`` (PNG/WebP/GIF/.webm/.mov/.mkv) or a colour.
    ``layout=None`` picks the layout for the device's family; ``device=None`` the
    recording's primary device. See :func:`resolve_range` for ``start_ns``,
    ``end_ns``, ``start_marker``, ``end_marker`` and ``duration_ns``, and
    :func:`iter_frame_states` for ``mode``. ``progress(done, total)`` is called
    as frames are produced.
    """
    out = Path(out)
    kind = output_kind(out)
    q = fps_fraction(fps)
    bg = parse_background(background)
    transparent = bg[3] < 255
    args: list[str] = []
    exe: str | None = None
    if kind == "video":
        args = codec_args(out.suffix, transparent, codec, crf)
        exe = find_ffmpeg(ffmpeg)
        if exe is None:
            raise FFmpegNotFoundError(
                f"ffmpeg is needed for {out.suffix} output but was not found. Install it with "
                f"'pip install imageio-ffmpeg' (or put ffmpeg on PATH / set CONTROLLERLOG_FFMPEG), "
                f"or render a PNG sequence (a folder path), .gif or .webp instead.")
    rec = _sorted_recording(rec)
    dev = _pick_device(rec, device)
    lay = resolve_layout(rec, layout, dev)
    start, end = resolve_range(rec, start_ns, end_ns, start_marker, end_marker, duration_ns)
    count = frame_count(start, end, q)
    family = rec.devices[dev].family if dev in rec.devices else None
    renderer = FrameRenderer(lay, scale=scale, background=background, history=history,
                             history_seconds=history_seconds, fps=q, frame_counter=frame_counter,
                             mapping=mapping, font_path=font_path, family=family,
                             even_size=kind == "video", supersample=supersample,
                             duration_ns=end - start)
    timeline = Timeline(rec) if renderer.history is not None else None
    total_ns = frame_time_ns(count, start, q) - start

    writer: Any
    if kind == IMAGE_SEQUENCE:
        writer = _PngSequenceWriter(out)
    elif kind in ("gif", "webp"):
        if count > LONG_ANIMATION_FRAMES:
            warnings.warn(f"{count} frames ({total_ns / NS_PER_S:.0f} s) as an animated {kind.upper()} "
                          f"needs a lot of memory and makes a large file; consider .mp4/.webm or a "
                          f"PNG sequence", RuntimeWarning, stacklevel=2)

        def probe() -> Image.Image:
            return renderer.render(all_pressed_state(), timeline=timeline, device=dev,
                                   t_ns=start, origin_ns=start)
        writer = _AnimationWriter(out, kind, transparent, probe)
    else:
        assert exe is not None
        writer = _FFmpegWriter(exe, out, renderer.size, q, renderer.mode, args)

    step = max(1, count // 200)
    version = 0
    prev_key: tuple[Any, ...] | None = None
    img: Image.Image | None = None
    try:
        for i, (t, state) in enumerate(iter_frame_states(rec, dev, q, start, count, mode)):
            if renderer.dynamic:
                img = renderer.render(state, timeline=timeline, device=dev, t_ns=t,
                                      origin_ns=start, frame=i)
                version += 1
            else:
                key = renderer.key(state)
                if key != prev_key:  # unchanged look: reuse the previous frame as is
                    img = renderer.render(state)
                    version += 1
                    prev_key = key
            assert img is not None
            writer.add(img, version, t - start)
            if progress is not None and ((i + 1) % step == 0 or i + 1 == count):
                progress(i + 1, count)
        writer.close(total_ns)
    except BaseException:
        writer.abort()
        raise
    return out


def render_frame(rec: Recording, t_ns: int, layout: str | Path | Mapping[str, Any] | None = None,
                 device: int | None = None, *, scale: float = 1.0, background: str = "transparent",
                 history: bool = True, history_seconds: float = 4.0, fps: float | str | Fraction = 60.0,
                 mapping: Mapping[str, str] | str | None = None, font_path: str | None = None) -> Image.Image:
    """One still image of the input display at recording time ``t_ns``."""
    rec = _sorted_recording(rec)
    dev = _pick_device(rec, device)
    lay = resolve_layout(rec, layout, dev)
    family = rec.devices[dev].family if dev in rec.devices else None
    renderer = FrameRenderer(lay, scale=scale, background=background, history=history,
                             history_seconds=history_seconds, fps=fps, mapping=mapping,
                             font_path=font_path, family=family)
    timeline = Timeline(rec)
    state = timeline.state_at(dev, t_ns) if dev is not None else PadState()
    return renderer.render(state, timeline=timeline, device=dev, t_ns=t_ns)


__all__ = ["render_recording", "render_frame", "FrameRenderer", "FFmpegNotFoundError",
           "find_ffmpeg", "codec_args", "output_kind", "fps_fraction", "frame_time_ns",
           "frame_count", "format_timecode", "find_marker", "resolve_range", "resolve_layout",
           "iter_frame_states", "parse_background", "all_pressed_state", "CHROMA_GREEN"]
