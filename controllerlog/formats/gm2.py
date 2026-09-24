"""GSE (Game Boy Speedrun Emulator) ``.gm2`` input logs: read, write, convert, edit.

Format (GM2 v2, GSE v0.5+; v1 = GSE v0.4), implemented from the spec in
docs/FORMATS.md, which was derived from GSE's ``GSE.Emu/EmuInputLog.cs``:

* Header, little-endian (except the magic): ``"GSEMOVIE"`` magic, u32
  version, platform, reset_stall, flags; i64 start_timestamp; u64
  gb_rtc_dividers; u32 blob size; RomName and EmuVersion as (u8 length + 255
  bytes UTF-8); v2 adds i64 gba_rtc_time at offset 560. v1 header = 560 bytes,
  v2 header = 1024 bytes.
* Body (zstd-compressed when flag bit 1 is set, which GSE always sets): the
  start blob (power-on save file, or savestate if flag bit 0), then 8-byte
  records ``(u32 cycles, u32 buttons)`` until EOF.
* Buttons: bit0 A, 1 B, 2 Select, 3 Start, 4 Right, 5 Left, 6 Up, 7 Down,
  8 R, 9 L; a hard reset is the single record ``(0, 0x80000000)``.
* GB-family ``cycles`` is the requested sample budget at 2^21 Hz (normally
  35112 = one frame); GBA ``cycles`` is the real CPU cycle count at 2^24 Hz.

A ``.gm2`` is an *emulator-internal* movie: its frame boundaries come from
GSE's emulation loop, so it cannot be synthesised in sync from an external
controller log. Use it the other way round: import GSE's own logs for
frame-exact display/rendering/analysis, and edit their inputs for TAS work:
each frame keeps its cycle field and the resets in front of it (they move
with the frame on insert/delete), and the header and start blob stay intact.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field, replace
from enum import IntEnum, IntFlag
from pathlib import Path
from typing import Iterable, Sequence

from ..logfile import Recording
from ..model import (BUTTON, BUTTON_INDEX, CONNECT, FAMILY_GAMEBOY, FAMILY_GBA,
                     MARK, TRIGGER_PRESS_THRESHOLD, DeviceInfo, InputEvent, PadState)

try:  # Python 3.14+
    from compression import zstd as _zstd
except ImportError:  # pragma: no cover - older Pythons
    _zstd = None

MAGIC = b"GSEMOVIE"
HEADER_SIZE = {1: 560, 2: 1024}
GB_SAMPLE_HZ = 2 ** 21          # Gambatte "samples" (2 T-cycles at single speed)
GBA_CYCLE_HZ = 2 ** 24
GB_FRAME_SAMPLES = 35112        # 70224 T-cycles
GBA_FRAME_CYCLES = 280896
HARD_RESET = 0x80000000
_FIXED = struct.Struct("<IIIIqQI")  # at offset 8
NS_PER_S = 1_000_000_000
# Largest decompressed body accepted (read at call time). 256 MiB is 30M+ records,
# over 100 hours of GB input; the largest real logs are well under 10 MiB.
MAX_BODY_BYTES = 256 << 20


class Platform(IntEnum):
    GB = 0
    GBC = 1
    GBC_GBA = 2   # GBC game on a GBA / Game Boy Player (reset_stall != 0 => GBP)
    SGB2 = 3
    GBA = 4


class Flags(IntFlag):
    STARTS_FROM_SAVESTATE = 1
    ZSTD = 2
    GBA_RTC_DISABLED = 4


class Gm2Button(IntFlag):
    A = 1 << 0
    B = 1 << 1
    SELECT = 1 << 2
    START = 1 << 3
    RIGHT = 1 << 4
    LEFT = 1 << 5
    UP = 1 << 6
    DOWN = 1 << 7
    R = 1 << 8
    L = 1 << 9


# GB/GBA button bit -> canonical button (Nintendo positional: A = east, B = south).
GM2_TO_CANONICAL: dict[int, str] = {
    0: "east", 1: "south", 2: "back", 3: "start",
    4: "dpad_right", 5: "dpad_left", 6: "dpad_up", 7: "dpad_down",
    8: "right_shoulder", 9: "left_shoulder",
}
CANONICAL_TO_GM2: dict[str, int] = {v: k for k, v in GM2_TO_CANONICAL.items()}
GM2_BUTTON_NAMES = ("A", "B", "Select", "Start", "Right", "Left", "Up", "Down", "R", "L")

# GSE's own host thresholds (SDLJoysticks.cs): stick direction >= 20000, trigger >= 5000.
GSE_STICK_THRESHOLD = 20000
GSE_TRIGGER_THRESHOLD = 5000


class Gm2Error(ValueError):
    pass


def is_gba(platform: int) -> bool:
    return platform == Platform.GBA


def button_mask_limit(platform: int) -> int:
    return 0x3FF if is_gba(platform) else 0xFF


@dataclass
class Gm2Header:
    version: int = 2
    platform: Platform = Platform.GB
    reset_stall: int = 0
    flags: Flags = Flags.ZSTD
    start_timestamp: int = 0
    gb_rtc_dividers: int = 0
    rom_name: str = ""
    emu_version: str = ""
    gba_rtc_time: int = 0

    @property
    def starts_from_savestate(self) -> bool:
        return bool(self.flags & Flags.STARTS_FROM_SAVESTATE)

    @property
    def platform_name(self) -> str:
        if self.platform == Platform.GBC_GBA and self.reset_stall:
            return "GBP"
        return self.platform.name


@dataclass
class Gm2Movie:
    header: Gm2Header = field(default_factory=Gm2Header)
    blob: bytes = b""
    records: list[tuple[int, int]] = field(default_factory=list)
    truncated: bool = False   # body ended mid zstd frame / mid record (e.g. GSE crashed)

    @property
    def gba(self) -> bool:
        return is_gba(self.header.platform)

    def frames(self) -> list["Gm2Frame"]:
        """Input records as frames (hard-reset records are folded into ``reset_before``)."""
        out: list[Gm2Frame] = []
        clock_hz = GBA_CYCLE_HZ if self.gba else GB_SAMPLE_HZ
        elapsed = 0
        pending_reset = False
        for i, (cycles, buttons) in enumerate(self.records):
            if buttons & HARD_RESET:
                pending_reset = True
                continue
            if cycles == 0:
                continue  # ILP skips 0-cycle records
            out.append(Gm2Frame(index=len(out), record=i, cycles=cycles,
                                buttons=buttons & button_mask_limit(self.header.platform),
                                t_ns=_cycles_to_ns(elapsed, clock_hz),
                                reset_before=pending_reset))
            pending_reset = False
            elapsed += cycles
        return out

    @property
    def duration_ns(self) -> int:
        clock_hz = GBA_CYCLE_HZ if self.gba else GB_SAMPLE_HZ
        return _cycles_to_ns(sum(c for c, b in self.records if not b & HARD_RESET), clock_hz)


def _cycles_to_ns(cycles: int, clock_hz: int) -> int:
    """Emulated time in ns, rounded half up like :func:`controllerlog.timeline.frame_time_ns`
    (nominal GB and GBA frames both reduce to 4389/262144 s, so the two agree exactly)."""
    return (2 * cycles * NS_PER_S + clock_hz) // (2 * clock_hz)


@dataclass(frozen=True)
class Gm2Frame:
    index: int          # frame number (input records only)
    record: int         # index into Gm2Movie.records
    cycles: int
    buttons: int
    t_ns: int           # emulated time at frame start (GB: from requested budgets, see module doc)
    reset_before: bool  # a hard reset happened right before this frame

    def names(self) -> list[str]:
        return [GM2_BUTTON_NAMES[b] for b in range(10) if self.buttons >> b & 1]


# --- binary I/O ---------------------------------------------------------------

def _pack_str(s: str) -> bytes:
    enc = s.encode("utf-8")[:255]
    return bytes([len(enc)]) + enc.ljust(255, b"\0")


def _unpack_str(b: bytes) -> str:
    return b[1:1 + b[0]].decode("utf-8", "replace")


def _too_big(cap: int) -> Gm2Error:
    return Gm2Error(f"decompressed .gm2 body exceeds {cap >> 20} MiB; not a GSE input log "
                    "(or a decompression bomb)")


def _decompress(data: bytes) -> tuple[bytes, bool]:
    """Decompress one or more zstd frames; tolerate a truncated final frame.

    The output is capped at :data:`MAX_BODY_BYTES` so a tiny crafted file can't
    exhaust memory.
    """
    if _zstd is None:
        raise Gm2Error("zstd support needs Python 3.14+ (compression.zstd)")
    cap = MAX_BODY_BYTES
    out = bytearray()
    truncated = False
    while data:
        d = _zstd.ZstdDecompressor()
        try:
            out += d.decompress(data, max_length=cap - len(out) + 1)
            while len(out) <= cap and not d.eof and not d.needs_input:
                out += d.decompress(b"", max_length=cap - len(out) + 1)
        except _zstd.ZstdError:
            truncated = True
            break
        if len(out) > cap:
            raise _too_big(cap)
        if not d.eof:
            truncated = True
            break
        data = d.unused_data
    return bytes(out), truncated


def parse_gm2(raw: bytes) -> Gm2Movie:
    if not raw:
        raise Gm2Error("empty .gm2: GSE was closed forcibly before it wrote the first data block "
                       "(it only flushes every 128 KiB of input, ~3 minutes)")
    if raw[:8] != MAGIC:
        raise Gm2Error("not a GSE .gm2 input log (bad magic)")
    if len(raw) < 44:
        raise Gm2Error("truncated .gm2 header")
    version, plat, stall, flags, ts, gbrtc, blob_size = _FIXED.unpack_from(raw, 8)
    if version not in HEADER_SIZE:
        raise Gm2Error(f"unsupported .gm2 version {version}")
    hsize = HEADER_SIZE[version]
    if len(raw) < hsize:
        raise Gm2Error("truncated .gm2 header")
    try:
        platform = Platform(plat)
    except ValueError:
        raise Gm2Error(f"unknown platform {plat}") from None
    header = Gm2Header(
        version=version, platform=platform, reset_stall=stall, flags=Flags(flags),
        start_timestamp=ts, gb_rtc_dividers=gbrtc,
        rom_name=_unpack_str(raw[44:300]), emu_version=_unpack_str(raw[300:556]),
        gba_rtc_time=struct.unpack_from("<q", raw, 560)[0] if version >= 2 else 0,
    )
    body = raw[hsize:]
    truncated = False
    if flags & Flags.ZSTD:
        body, truncated = _decompress(body)
    elif len(body) > MAX_BODY_BYTES:
        raise _too_big(MAX_BODY_BYTES)
    if len(body) < blob_size:
        raise Gm2Error(f"body shorter ({len(body)}) than start blob ({blob_size})")
    blob, rest = body[:blob_size], body[blob_size:]
    whole = len(rest) // 8 * 8
    truncated = truncated or whole != len(rest)
    records = list(struct.iter_unpack("<II", rest[:whole]))
    return Gm2Movie(header, blob, records, truncated)


def read_gm2(path: str | Path) -> Gm2Movie:
    if Path(path).is_dir():
        raise Gm2Error(f"{path} is a folder, not a .gm2 file; list its logs with "
                       f"`controllerlog gm2 list --dir \"{path}\"`")
    try:
        raw = Path(path).read_bytes()
    except PermissionError as e:
        # ERROR_SHARING_VIOLATION (32) / ERROR_LOCK_VIOLATION (33): GSE holds the file open.
        if getattr(e, "winerror", None) in (32, 33):
            raise Gm2Error(f"{path} is locked: GSE is still writing it. GSE closes the log when you "
                           "load another ROM/state/save or exit, then it can be read.") from None
        raise
    return parse_gm2(raw)


def dump_gm2(movie: Gm2Movie, compress: bool = True) -> bytes:
    h = movie.header
    flags = int(h.flags)
    flags = (flags | Flags.ZSTD) if compress else (flags & ~Flags.ZSTD)
    if h.version not in HEADER_SIZE:
        raise Gm2Error(f"unsupported .gm2 version {h.version}")
    out = bytearray(HEADER_SIZE[h.version])
    out[:8] = MAGIC
    out[8:44] = _FIXED.pack(h.version, int(h.platform), h.reset_stall, flags,
                            h.start_timestamp, h.gb_rtc_dividers, len(movie.blob))
    out[44:300] = _pack_str(h.rom_name)
    out[300:556] = _pack_str(h.emu_version)
    if h.version >= 2:
        out[560:568] = struct.pack("<q", h.gba_rtc_time)
    body = bytes(movie.blob) + b"".join(struct.pack("<II", c & 0xFFFFFFFF, b & 0xFFFFFFFF)
                                        for c, b in movie.records)
    if compress:
        if _zstd is None:
            raise Gm2Error("zstd support needs Python 3.14+; use compress=False")
        body = _zstd.compress(body, level=3)
    return bytes(out) + body


def write_gm2(path: str | Path, movie: Gm2Movie, compress: bool = True) -> None:
    Path(path).write_bytes(dump_gm2(movie, compress))


# --- conversions ----------------------------------------------------------------

def mask_to_state(mask: int) -> PadState:
    st = PadState()
    for bit, name in GM2_TO_CANONICAL.items():
        if mask >> bit & 1:
            st.buttons[BUTTON_INDEX[name]] = 1
    return st


def state_to_mask(state: PadState, platform: int = Platform.GB, stick_to_dpad: bool = False,
                  triggers_to_lr: bool = True,
                  trigger_threshold: int = GSE_TRIGGER_THRESHOLD) -> int:
    """Canonical state -> GB/GBA button mask with GSE's rules (opposite directions cancel).

    ``trigger_threshold`` defaults to GSE's own host threshold, so a *live controller*
    state maps the way GSE would have mapped it. Frame movies (TAS edits) use the
    model's ``TRIGGER_PRESS_THRESHOLD`` instead, like every other digital consumer.
    """
    mask = 0
    for name, bit in CANONICAL_TO_GM2.items():
        if state.buttons[BUTTON_INDEX[name]]:
            mask |= 1 << bit
    if triggers_to_lr:
        if state.axes[4] >= trigger_threshold:
            mask |= Gm2Button.L
        if state.axes[5] >= trigger_threshold:
            mask |= Gm2Button.R
    if stick_to_dpad:
        x, y = state.axes[0], state.axes[1]
        if x >= GSE_STICK_THRESHOLD:
            mask |= Gm2Button.RIGHT
        if x <= -GSE_STICK_THRESHOLD:
            mask |= Gm2Button.LEFT
        if y <= -GSE_STICK_THRESHOLD:
            mask |= Gm2Button.UP
        if y >= GSE_STICK_THRESHOLD:
            mask |= Gm2Button.DOWN
    if mask & Gm2Button.LEFT and mask & Gm2Button.RIGHT:
        mask &= ~(Gm2Button.LEFT | Gm2Button.RIGHT)
    if mask & Gm2Button.UP and mask & Gm2Button.DOWN:
        mask &= ~(Gm2Button.UP | Gm2Button.DOWN)
    return mask & button_mask_limit(platform)


def gm2_to_recording(movie: Gm2Movie, source: str = "") -> Recording:
    """Convert a GSE log to a .ctlog Recording (device 0, frame-aligned timestamps).

    Hard resets become ``reset`` markers; a ``gm2:start`` marker is placed at 0.
    """
    family = FAMILY_GBA if movie.gba else FAMILY_GAMEBOY
    dev = DeviceInfo(0, f"GSE {movie.header.platform_name} input log", backend="gm2",
                     family=family, extra={"platform": movie.header.platform_name})
    rec = Recording(header={
        "format": "controllerlog", "version": 1, "time_unit": "ns", "clock": "emulated",
        "meta": {"game": movie.header.rom_name, "emulator": f"GSE {movie.header.emu_version}",
                 "source": source, "gm2_version": movie.header.version,
                 "platform": movie.header.platform_name,
                 "starts_from_savestate": movie.header.starts_from_savestate,
                 "start_timestamp": movie.header.start_timestamp,
                 "fps": 4194304 / 70224},
    })
    rec.devices[0] = dev
    rec.events.append(InputEvent(0, 0, CONNECT, None, dev.to_json()))
    rec.events.append(InputEvent(0, None, MARK, None, "gm2:start"))
    prev = 0
    for fr in movie.frames():
        if fr.reset_before:
            rec.events.append(InputEvent(fr.t_ns, None, MARK, None, "reset"))
        changed = prev ^ fr.buttons
        for bit in range(10):
            if changed >> bit & 1:
                rec.events.append(InputEvent(fr.t_ns, 0, BUTTON,
                                             BUTTON_INDEX[GM2_TO_CANONICAL[bit]],
                                             fr.buttons >> bit & 1))
        prev = fr.buttons
    end = movie.duration_ns
    for bit in range(10):
        if prev >> bit & 1:
            rec.events.append(InputEvent(end, 0, BUTTON, BUTTON_INDEX[GM2_TO_CANONICAL[bit]], 0))
    rec.events.append(InputEvent(end, None, MARK, None, "gm2:end"))
    rec.sort()
    return rec


def frame_masks(movie: Gm2Movie) -> list[int]:
    return [f.buttons for f in movie.frames()]


def with_frame_masks(movie: Gm2Movie, masks: Sequence[int], truncate: bool = False) -> Gm2Movie:
    """TAS edit: return a copy whose input-frame buttons are replaced by ``masks``, by position.

    Mask ``k`` goes into the record of frame ``k``: cycle fields, resets and the
    start blob stay where they are, so this is only right for edits that don't
    move frames (hold/release/set/clear/copy, or a table with no link to this
    log). Insert/delete edits go through :func:`apply_frame_movie`, which moves
    resets and budgets with their frames. If ``masks`` is shorter than the
    movie, the remaining frames keep their inputs — or are dropped with
    ``truncate=True``. Extra masks are appended as new frames with the nominal
    cycle count.
    """
    limit = button_mask_limit(movie.header.platform)
    frames = movie.frames()
    records = list(movie.records)
    for fr, mask in zip(frames, masks):
        records[fr.record] = (fr.cycles, _clean_mask(mask, limit))
    if truncate and len(masks) < len(frames):
        records = records[:frames[len(masks)].record]
        while records and records[-1][1] & HARD_RESET:
            records.pop()  # a trailing reset with no frame after it
    nominal = GBA_FRAME_CYCLES if movie.gba else GB_FRAME_SAMPLES
    for mask in masks[len(frames):]:
        records.append((nominal, _clean_mask(mask, limit)))
    return replace(movie, records=records, truncated=False)


def system_name(platform: int) -> str:
    return {Platform.GB: "gb", Platform.GBC: "gbc", Platform.GBC_GBA: "gbc",
            Platform.SGB2: "gb", Platform.GBA: "gba"}[Platform(platform)]


def to_frame_movie(movie: Gm2Movie):
    """GSE log -> :class:`controllerlog.tas.FrameMovie` (one frame per input record).

    ``origin`` links every frame to its source frame, so :func:`apply_frame_movie`
    can keep resets and cycle budgets attached to their frames across edits.
    """
    from ..tas import FrameMovie
    from ..timeline import FPS
    h = movie.header
    frames = movie.frames()
    return FrameMovie(FPS["gba"] if movie.gba else FPS["gb"],
                      [mask_to_state(f.buttons) for f in frames],
                      system_name(h.platform),
                      {"source": "gm2", "game": h.rom_name, "emulator": f"GSE {h.emu_version}",
                       "platform": h.platform_name},
                      origin=list(range(len(frames))))


def _valid_origin(origin, n_frames: int, n_edited: int) -> bool:
    if origin is None or len(origin) != n_edited or not n_frames:
        return False
    last = -1
    for o in origin:
        if o is None:
            continue
        if not isinstance(o, int) or o <= last or o >= n_frames:
            return False
        last = o
    return True


def apply_frame_movie(movie: Gm2Movie, fm, truncate: bool = True) -> Gm2Movie:
    """Write an edited FrameMovie's buttons back into a copy of the original GSE log.

    When ``fm`` came from :func:`to_frame_movie` of this log (its ``origin`` is
    set), the records are rebuilt around the edit: every surviving frame keeps
    its own cycle budget and the reset / zero-cycle records in front of it, so
    resets and GBP fade-out budgets move with their frames on insert/delete.
    Inserted frames get the nominal budget (35112 samples GB, 280896 cycles GBA)
    and no reset. A reset in front of a deleted frame moves to the next
    surviving frame (it is dropped if none follows). Without ``origin`` the
    buttons are written back by position (:func:`with_frame_masks`).

    Triggers past ``TRIGGER_PRESS_THRESHOLD`` count as L/R (GBA only; GB masks drop them).
    """
    platform = movie.header.platform
    masks = [state_to_mask(s, platform, triggers_to_lr=True,
                           trigger_threshold=TRIGGER_PRESS_THRESHOLD) for s in fm.frames]
    frames = movie.frames()
    origin = getattr(fm, "origin", None)
    if not _valid_origin(origin, len(frames), len(masks)):
        return with_frame_masks(movie, masks, truncate=truncate)
    limit = button_mask_limit(platform)
    nominal = GBA_FRAME_CYCLES if movie.gba else GB_FRAME_SAMPLES
    recs = movie.records

    def prefix(k: int) -> list[tuple[int, int]]:
        """Non-frame records (resets, 0-cycle records) right before source frame k."""
        start = frames[k - 1].record + 1 if k else 0
        return list(recs[start:frames[k].record])

    def has_reset(recs_: list[tuple[int, int]]) -> bool:
        return any(b & HARD_RESET for _, b in recs_)

    records: list[tuple[int, int]] = []
    carried_reset = False                 # a deleted frame had a reset in front of it
    next_src = 0
    for mask, o in zip(masks, origin):
        mask = _clean_mask(mask, limit)
        if o is None:
            records.append((nominal, mask))
            continue
        for k in range(next_src, o):      # source frames the edit deleted
            carried_reset = carried_reset or has_reset(prefix(k))
        own = prefix(o)
        if carried_reset and not has_reset(own):
            records.append((0, HARD_RESET))
        carried_reset = False
        records += own
        records.append((frames[o].cycles, mask))
        next_src = o + 1
        if o == len(frames) - 1:
            records += recs[frames[o].record + 1:]   # trailing records after the last frame
    return replace(movie, records=records, truncated=False)


def _clean_mask(mask: int, limit: int) -> int:
    mask &= limit
    if mask & Gm2Button.LEFT and mask & Gm2Button.RIGHT:
        mask &= ~(Gm2Button.LEFT | Gm2Button.RIGHT)
    if mask & Gm2Button.UP and mask & Gm2Button.DOWN:
        mask &= ~(Gm2Button.UP | Gm2Button.DOWN)
    return mask


def approximate_from_states(states: Iterable[PadState], header: Gm2Header,
                            blob: bytes = b"", **mask_kwargs) -> Gm2Movie:
    """Build a .gm2 from per-frame states (e.g. a quantized controller log).

    WARNING: the result is only *approximately* aligned with any real GSE run —
    GSE decides frame boundaries internally. Useful for experiments and tests,
    not for verification. ``blob`` must be the correct power-on save data (or
    savestate with the flag set) for InputLogPlayer to load it.
    """
    nominal = GBA_FRAME_CYCLES if is_gba(header.platform) else GB_FRAME_SAMPLES
    records = [(nominal, state_to_mask(s, header.platform, **mask_kwargs)) for s in states]
    return Gm2Movie(header=header, blob=blob, records=records)


def default_log_dir() -> Path:
    """Where GSE writes input logs on Windows (non-portable install)."""
    import os
    return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "GSE" / "Input Log"
