"""BizHawk ``.bk2`` movies: read any, write Game Boy / GBC / GBA (Gambatte, mGBA).

Implemented from BizHawk's source (2.11.x): ``Bk2Movie.IO.cs``,
``Bk2LogEntryGenerator.cs``, ``Bk2Controller.cs``, ``HeaderKeys.cs``,
``Gambatte.cs``, ``MGBAHawk.IEmulator.cs``, ``Bk2MnemonicLookup.cs``.

* A zip with entries at the root: ``Header.txt`` and ``Input Log.txt``
  (required), ``SyncSettings.json`` (one line), ``Comments.txt``,
  ``Subtitles.txt``, ``BizState 1.0``, ``BizVersion.txt``, optional
  ``MovieSaveRam.bin``. Text is UTF-8 with CRLF.
* ``Input Log.txt``: ``[Input]``, ``LogKey:#Up|Down|...|``, then one
  ``|...|`` line per emulated frame, then ``[/Input]``. Within a ``#`` group
  axes come first, as ``value.rjust(5) + ","``, then one character per button
  (mnemonic when pressed, ``.`` when released).

Only power-on (optionally + save RAM) movies are written: GSE/other
savestates are not BizHawk ``Core.bin`` states.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..model import BUTTON_INDEX, PadState

BIZHAWK_VERSION = "Version 2.11.1"
NL = "\r\n"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
# Size caps for reading (read at call time): a crafted zip / zstd lump can inflate
# ~1000x-30000x. Long real movies stay far below (a 2-hour GBA input log is ~20 MiB).
MAX_LUMP_BYTES = 64 << 20
MAX_TOTAL_BYTES = 256 << 20

MNEMONIC = {"Up": "U", "Down": "D", "Left": "L", "Right": "R", "Start": "S", "Select": "s",
            "L": "l", "R": "r", "Power": "P"}

GB_BUTTONS = ["Up", "Down", "Left", "Right", "Start", "Select", "B", "A", "Power"]
GBA_AXES = ["Tilt X", "Tilt Y", "Tilt Z", "Light Sensor"]
GBA_BUTTONS = ["Up", "Down", "Left", "Right", "Start", "Select", "B", "A", "L", "R", "Power"]

GAMBATTE_SYNC_TYPE = ("BizHawk.Emulation.Cores.Nintendo.Gameboy.Gameboy+GambatteSyncSettings, "
                      "BizHawk.Emulation.Cores")
MGBA_SYNC_TYPE = "BizHawk.Emulation.Cores.Nintendo.GBA.MGBAHawk+SyncSettings, BizHawk.Emulation.Cores"

# Gambatte ConsoleMode: Auto 0, GB 1, GBC 2, GBA (GBC-in-GBA) 3, SGB2 4.
SYSTEMS: dict[str, dict[str, Any]] = {
    "gb": {"platform": "GB", "core": "Gambatte", "axes": [], "buttons": GB_BUTTONS,
           "clock": 2097152, "console_mode": 1},
    "gbc": {"platform": "GB", "core": "Gambatte", "axes": [], "buttons": GB_BUTTONS,
            "clock": 2097152, "console_mode": 2, "extra_header": {"IsCGBMode": "1"}},
    "gbc_gba": {"platform": "GB", "core": "Gambatte", "axes": [], "buttons": GB_BUTTONS,
                "clock": 2097152, "console_mode": 3, "extra_header": {"IsCGBMode": "1"}},
    "gba": {"platform": "GBA", "core": "mGBA", "axes": GBA_AXES, "buttons": GBA_BUTTONS,
            "clock": 16777216},
}

# bk2 button name (after stripping "P1 ") -> canonical. Nintendo/SNES naming is positional.
BK2_TO_CANONICAL = {
    "Up": "dpad_up", "Down": "dpad_down", "Left": "dpad_left", "Right": "dpad_right",
    "Start": "start", "Select": "back", "A": "east", "B": "south", "X": "north", "Y": "west",
    "L": "left_shoulder", "R": "right_shoulder", "Mode": "guide",
}
CANONICAL_TO_BK2 = {v: k for k, v in BK2_TO_CANONICAL.items()}
SYSTEM_BUTTONS = {"Power", "Reset"}


class Bk2Error(ValueError):
    pass


@dataclass
class Bk2Movie:
    header: dict[str, str] = field(default_factory=dict)
    groups: list[list[str]] = field(default_factory=list)   # LogKey groups
    frames: list[dict[str, Any]] = field(default_factory=list)  # name -> bool | int
    sync_settings: str = ""
    comments: list[str] = field(default_factory=list)
    subtitles: list[str] = field(default_factory=list)
    saveram: bytes | None = None
    axes: set[str] = field(default_factory=set)
    # Zip entries we don't interpret (Core.bin savestate anchors, Framebuffer, SyncSettings
    # extras...), written back verbatim so a read/write round trip never drops them.
    extra_lumps: dict[str, bytes] = field(default_factory=dict)
    bizstate: str | None = None  # the source's "BizState 1.0" value (binary lump encoding)

    @property
    def platform(self) -> str:
        return self.header.get("Platform", "")

    @property
    def log_key(self) -> str:
        return "".join("#" + "".join(n + "|" for n in g) for g in self.groups)

    def frame_lines(self) -> list[str]:
        out = []
        for fr in self.frames:
            s = "|"
            for g in self.groups:
                for name in g:
                    if name in self.axes:
                        s += str(int(fr.get(name, 0))).rjust(5) + ","
                for name in g:
                    if name not in self.axes:
                        s += mnemonic(name) if fr.get(name) else "."
                s += "|"
            out.append(s)
        return out


def mnemonic(name: str) -> str:
    base = _strip_player(name)[1]
    return MNEMONIC.get(base, base[:1] or "?")


def _strip_player(name: str) -> tuple[int, str]:
    if name.startswith("P") and " " in name:
        head, rest = name.split(" ", 1)
        if head[1:].isdigit():
            return int(head[1:]), rest
    return 0, name


# --- reading -------------------------------------------------------------------------

def _entry_map(zf: zipfile.ZipFile) -> dict[str, str]:
    """Key zip entries like BizHawk 2.11: common folder stripped, name before the first '.'."""
    names = [n for n in zf.namelist() if not n.endswith("/")]
    prefix = ""
    if names and all("/" in n for n in names):
        first = names[0].split("/", 1)[0] + "/"
        if all(n.startswith(first) for n in names):
            prefix = first
    out = {}
    for n in names:
        key = n[len(prefix):].split(".", 1)[0]
        out.setdefault(key, n)
    return out


def _read(zf: zipfile.ZipFile, name: str, budget: list[int]) -> bytes:
    """Read one zip entry, refusing entries (or a total) that inflate past the caps.

    ``budget`` is a one-item list with the bytes still allowed for this file.
    ``ZipInfo.file_size`` is checked first but can lie, so the read is bounded too.
    """
    cap = min(MAX_LUMP_BYTES, budget[0])
    if zf.getinfo(name).file_size > cap:
        raise Bk2Error(f"{name}: zip entry too large (> {cap >> 20} MiB); not a BizHawk movie")
    with zf.open(name) as fh:
        data = fh.read(cap + 1)
    if len(data) > cap:
        raise Bk2Error(f"{name}: zip entry too large (> {cap >> 20} MiB); not a BizHawk movie")
    budget[0] -= len(data)
    return data


def _text(zf: zipfile.ZipFile, name: str | None, budget: list[int]) -> str:
    if not name:
        return ""
    return _read(zf, name, budget).decode("utf-8-sig", "replace")


def _zstd_decompress(data: bytes) -> bytes:
    from compression import zstd
    cap = MAX_LUMP_BYTES
    d = zstd.ZstdDecompressor()
    try:
        out = d.decompress(data, max_length=cap + 1)
    except zstd.ZstdError as e:
        raise Bk2Error(f"MovieSaveRam: bad zstd data: {e}") from None
    if len(out) > cap:
        raise Bk2Error(f"MovieSaveRam: decompresses to more than {cap >> 20} MiB; not a save file")
    if not d.eof:
        raise Bk2Error("MovieSaveRam: truncated zstd data")
    return out


def parse_log_key(line: str) -> list[list[str]]:
    if not line.startswith("LogKey:"):
        raise Bk2Error("Input Log.txt has no LogKey line")
    key = line[len("LogKey:"):].strip()
    groups = []
    for chunk in key.split("#"):
        if not chunk:
            continue
        groups.append([n for n in chunk.split("|") if n])
    return groups


def parse_frame(line: str, groups: list[list[str]]) -> tuple[dict[str, Any], set[str]]:
    """Parse one ``|...|`` line. Returns (values, axis names).

    Within each group, axes come first as comma-terminated integers, so the
    number of commas in a group's segment is its axis count.
    """
    segments = line.strip().strip("|").split("|")
    if len(segments) < len(groups):
        raise Bk2Error(f"frame line has {len(segments)} group(s), LogKey has {len(groups)}: {line!r}")
    fr: dict[str, Any] = {}
    axes: set[str] = set()
    for g, seg in zip(groups, segments):
        values = seg.split(",")
        n_axes = len(values) - 1
        if n_axes > len(g):
            raise Bk2Error(f"too many axis values in {line!r}")
        for name, v in zip(g[:n_axes], values[:-1]):
            fr[name] = int(v.strip() or 0)
            axes.add(name)
        chars = values[-1]
        buttons = g[n_axes:]
        if len(chars) < len(buttons):
            raise Bk2Error(f"frame line too short: {line!r}")
        for name, ch in zip(buttons, chars):
            fr[name] = ch != "."
    return fr, axes


def read_bk2(path: str | Path) -> Bk2Movie:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise Bk2Error(f"{path}: not a .bk2 (zip) file: {e}") from None
    with zf:
        entries = _entry_map(zf)
        if "Header" not in entries or "Input Log" not in entries:
            raise Bk2Error(f"{path}: missing Header.txt or Input Log.txt")
        budget = [MAX_TOTAL_BYTES]
        header: dict[str, str] = {}
        for line in _text(zf, entries["Header"], budget).splitlines():
            line = line.strip()
            if not line or " " not in line:
                continue
            k, v = line.split(" ", 1)
            header.setdefault(k, v)
        lines = _text(zf, entries["Input Log"], budget).splitlines()
        key_line = next((l for l in lines if l.startswith("LogKey:")), None)
        if key_line is None:
            raise Bk2Error(f"{path}: Input Log.txt has no LogKey")
        groups = parse_log_key(key_line)
        frames = []
        axes: set[str] = set()
        for l in lines:
            if l.startswith("|"):
                fr, ax = parse_frame(l, groups)
                frames.append(fr)
                axes |= ax
        sync = next((l for l in _text(zf, entries.get("SyncSettings"), budget).splitlines()
                     if l.strip()), "")
        comments = [l for l in _text(zf, entries.get("Comments"), budget).splitlines() if l.strip()]
        subtitles = [l for l in _text(zf, entries.get("Subtitles"), budget).splitlines() if l.strip()]
        saveram = _read(zf, entries["MovieSaveRam"], budget) if "MovieSaveRam" in entries else None
        if saveram is not None and saveram[:4] == ZSTD_MAGIC:
            # 2.11 writes "*.bin.zst"; 2.10 (BizState "2") zstd-compresses plain "*.bin" too.
            saveram = _zstd_decompress(saveram)
        bizstate = _text(zf, entries.get("BizState 1"), budget).strip() or None
        known = {"Header", "Input Log", "SyncSettings", "Comments", "Subtitles", "MovieSaveRam",
                 "BizState 1", "BizVersion"}
        extra = {name: _read(zf, name, budget) for key, name in entries.items() if key not in known}
    return Bk2Movie(header, groups, frames, sync, comments, subtitles, saveram, axes,
                    extra_lumps=extra, bizstate=bizstate)


# --- writing -------------------------------------------------------------------------

def sync_settings_json(type_name: str, fields: dict[str, Any]) -> str:
    """One-line JSON with ``$type`` first, as BizHawk's ConfigService.SaveWithType writes it."""
    return json.dumps({"o": {"$type": type_name, **fields}}, separators=(",", ":"))


def dump_bk2(movie: Bk2Movie) -> bytes:
    buf = io.BytesIO()
    header = dict(movie.header)
    # Keep the source's binary-lump encoding version when we carry its binary lumps verbatim.
    zipver = movie.bizstate if (movie.extra_lumps and movie.bizstate) else "3"
    if movie.saveram is not None:
        header["StartsFromSaveRam"] = "True"
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("BizState 1.0", zipver + NL)
        z.writestr("BizVersion.txt", BIZHAWK_VERSION + NL)
        z.writestr("Header.txt", "".join(f"{k} {v}{NL}" for k, v in header.items() if v != "") + NL)
        z.writestr("Comments.txt", "".join(c + NL for c in movie.comments) + NL)
        z.writestr("Subtitles.txt", "".join(s + NL for s in movie.subtitles) + NL)
        z.writestr("SyncSettings.json", movie.sync_settings + NL)
        body = ("[Input]" + NL + "LogKey:" + movie.log_key + NL
                + "".join(l + NL for l in movie.frame_lines()) + "[/Input]" + NL)
        z.writestr("Input Log.txt", body)
        if movie.saveram is not None:
            data = movie.saveram
            if zipver == "2":  # BizHawk 2.10's loader zstd-decodes every binary lump
                from compression import zstd
                data = zstd.compress(data)
            # BizState "3" + uncompressed .bin is read correctly by BizHawk 2.11+.
            z.writestr("MovieSaveRam.bin", data)
        for name, data in movie.extra_lumps.items():
            z.writestr(name, data)
    return buf.getvalue()


def write_bk2(path: str | Path, movie: Bk2Movie) -> None:
    Path(path).write_bytes(dump_bk2(movie))


def new_movie(system: str, game_name: str = "", sha1: str = "", author: str = "ControllerLog",
              sync_fields: dict[str, Any] | None = None) -> Bk2Movie:
    """An empty power-on movie for ``system`` in {gb, gbc, gbc_gba, gba}."""
    if system not in SYSTEMS:
        raise Bk2Error(f"bk2 export supports {', '.join(SYSTEMS)} (got {system!r})")
    spec = SYSTEMS[system]
    header = {"MovieVersion": "BizHawk v2.0.0", "emuVersion": BIZHAWK_VERSION,
              "OriginalEmuVersion": BIZHAWK_VERSION, "Platform": spec["platform"],
              "Core": spec["core"], "GameName": game_name, "SHA1": sha1.upper(),
              "Author": author, "rerecordCount": "0", "ClockRate": str(spec["clock"]),
              **spec.get("extra_header", {})}
    if spec["core"] == "Gambatte":
        fields = {"EnableBIOS": True, "ConsoleMode": spec["console_mode"], "FrameLength": 0}
        fields.update(sync_fields or {})
        sync = sync_settings_json(GAMBATTE_SYNC_TYPE, fields)
    else:
        fields = {"SkipBios": False, "RTCUseRealTime": False}
        fields.update(sync_fields or {})
        sync = sync_settings_json(MGBA_SYNC_TYPE, fields)
    return Bk2Movie(header=header, groups=[spec["axes"] + spec["buttons"]], frames=[],
                    sync_settings=sync, axes=set(spec["axes"]))


def _digital(state: PadState, name: str) -> bool:
    """A bk2 button (player prefix stripped) from a canonical state. A trigger past
    ``TRIGGER_PRESS_THRESHOLD`` also presses L / R (GBA shoulder buttons)."""
    down = bool(state.buttons[BUTTON_INDEX[BK2_TO_CANONICAL[name]]])
    if name in ("L", "R") and not down:
        down = state.pressed("left_trigger" if name == "L" else "right_trigger")
    return down


def _cancel_opposites(fr: dict[str, Any], prefix: str = "") -> None:
    # GB/GBA hardware can't press opposite directions; GSE and BizHawk movies never do.
    for a, b in (("Left", "Right"), ("Up", "Down")):
        a, b = prefix + a, prefix + b
        if fr.get(a) and fr.get(b):
            fr[a] = fr[b] = False


def state_to_frame(state: PadState, system: str, power: bool = False) -> dict[str, Any]:
    spec = SYSTEMS[system]
    fr: dict[str, Any] = {a: 0 for a in spec["axes"]}
    for name in spec["buttons"]:
        if name == "Power":
            fr[name] = power
        else:
            fr[name] = _digital(state, name)
    _cancel_opposites(fr)
    return fr


def from_frame_movie(fm, system: str | None = None, game_name: str = "", sha1: str = "") -> Bk2Movie:
    """FrameMovie -> bk2 (one line per frame, no Power presses).

    A fresh power-on movie: to edit an existing .bk2 (or a .gm2) keep its save RAM,
    resets and sync settings with :func:`apply_frame_movie` instead.
    """
    system = system or fm.system
    if system == "generic":
        raise Bk2Error("bk2 needs a target system: pass --system gb/gbc/gba")
    mv = new_movie(system, game_name=game_name, sha1=sha1)
    mv.frames = [state_to_frame(s, system) for s in fm.frames]
    if fm.meta.get("tas_edited"):
        mv.comments.append(TAS_EDIT_COMMENT)
    return mv


TAS_EDIT_COMMENT = "TAS-edited with ControllerLog"
_HANDHELD_PLATFORMS = {"GB", "GBC", "SGB", "GBA"}


def _is_p1_button(name: str, axes: set[str]) -> str | None:
    """The bk2 base name if ``name`` is a player-1 button a FrameMovie carries, else None."""
    if name in axes:
        return None
    player, base = _strip_player(name)
    if player > 1 or base in SYSTEM_BUTTONS or base not in BK2_TO_CANONICAL:
        return None
    return base


def apply_frame_movie(base: Bk2Movie, fm, sha1: str | None = None) -> Bk2Movie:
    """Write an edited FrameMovie back into a copy of ``base`` (a TAS edit of a .bk2).

    Only the player-1 digital buttons are replaced. Header (SHA1, core, mode),
    sync settings, save RAM, comments, extra lumps, axes, other players and
    Power/Reset stay as they were. With ``fm.origin`` (a movie from
    :func:`to_frame_movie` of ``base``, or from a .gm2 that ``base`` was
    converted from with :func:`from_gm2`) Power/Reset move with their frames
    on insert/delete: inserted frames have none, and a Power on a deleted frame
    moves to the next surviving frame. Inserted frames copy the axis values of
    the frame before them. ``sha1`` replaces the header's SHA1 when given.
    """
    src = base.frames
    n = len(fm.frames)
    origin = getattr(fm, "origin", None)
    if origin is not None:
        last = -1
        for o in origin:
            if o is None:
                continue
            if not isinstance(o, int) or o <= last or o >= len(src):
                origin = None
                break
            last = o
    if origin is None or len(origin) != n:
        origin = [i if i < len(src) else None for i in range(n)]
    names = [name for g in base.groups for name in g]
    handheld = base.platform.upper() in _HANDHELD_PLATFORMS

    frames: list[dict[str, Any]] = []
    pending: set[str] = set()   # Power/Reset of deleted frames
    next_src = 0
    for st, o in zip(fm.frames, origin):
        if o is None:
            like = frames[-1] if frames else (src[0] if src else {})
            fr = {name: (like.get(name, 0) if name in base.axes else False) for name in names}
        else:
            for k in range(next_src, o):
                pending |= {name for name, v in src[k].items()
                            if v and _strip_player(name)[1] in SYSTEM_BUTTONS}
            fr = dict(src[o])
            for name in pending:
                fr[name] = True
            pending = set()
            next_src = o + 1
        for name in names:
            b = _is_p1_button(name, base.axes)
            if b is not None:
                fr[name] = _digital(st, b)
        if handheld:
            _cancel_opposites(fr)
            _cancel_opposites(fr, "P1 ")
        frames.append(fr)
    header = dict(base.header)
    if sha1:
        header["SHA1"] = sha1.upper()
    comments = list(base.comments)
    if fm.meta.get("tas_edited"):
        header.pop("CycleCount", None)   # running time of the old input, as BizHawk drops it
        if TAS_EDIT_COMMENT not in comments:
            comments.append(TAS_EDIT_COMMENT)
    return replace(base, header=header, groups=[list(g) for g in base.groups], frames=frames,
                   comments=comments, subtitles=list(base.subtitles), axes=set(base.axes),
                   extra_lumps=dict(base.extra_lumps))


def _cgb_system(movie: Bk2Movie) -> str:
    """Gambatte movies all say Platform GB: tell DMG, GBC and GBC-in-GBA apart."""
    if movie.header.get("IsCGBMode", "").strip() not in ("1", "True", "true"):
        return "gb"
    try:
        mode = json.loads(movie.sync_settings)["o"].get("ConsoleMode")
    except (ValueError, KeyError, TypeError, AttributeError):
        mode = None
    return "gbc_gba" if mode == 3 else "gbc"


def to_frame_movie(movie: Bk2Movie):
    """bk2 -> FrameMovie (player 1 / unprefixed buttons; axes and Power are dropped).

    Power frames are listed in ``meta["power_frames"]``; ``origin`` lets
    :func:`apply_frame_movie` put them (and everything else) back.
    """
    from ..tas import FrameMovie
    from ..timeline import FPS
    plat = movie.platform.upper()
    system = {"GB": "gb", "SGB": "gb", "GBC": "gbc", "GBA": "gba", "NES": "nes", "SNES": "snes"}.get(plat, "generic")
    if plat == "GB":
        system = _cgb_system(movie)
    fps = FPS["gb"] if system in ("gb", "gbc", "gbc_gba") else FPS.get(system, 60.0)
    frames = []
    resets = []
    for i, fr in enumerate(movie.frames):
        st = PadState()
        for name, v in fr.items():
            player, base = _strip_player(name)
            if base in SYSTEM_BUTTONS:
                if v:
                    resets.append(i)
                continue
            if player > 1 or name in movie.axes:
                continue
            canon = BK2_TO_CANONICAL.get(base)
            if canon and v:
                st.buttons[BUTTON_INDEX[canon]] = 1
        frames.append(st)
    meta = {"source": "bk2", "game": movie.header.get("GameName", ""),
            "platform": movie.platform, "core": movie.header.get("Core", "")}
    if resets:
        meta["power_frames"] = resets
    return FrameMovie(fps, frames, system, meta, origin=list(range(len(frames))))


# --- GSE .gm2 -> bk2 ---------------------------------------------------------------------

def from_gm2(g, sha1: str = "") -> tuple[Bk2Movie, list[str]]:
    """Convert a GSE log (power-on start) to a BizHawk movie. Returns (movie, warnings).

    GSE samples input once per VBlank-bounded ``runfor(35112)`` call, exactly
    like BizHawk's default Gambatte mode, so normal frames convert 1:1.
    Warnings flag what BizHawk can't reproduce exactly.
    """
    from .gm2 import GB_FRAME_SAMPLES, GBA_FRAME_CYCLES, HARD_RESET, Platform, mask_to_state
    h = g.header
    warnings: list[str] = []
    if h.starts_from_savestate:
        raise Bk2Error("this GSE log starts from a savestate; GSE (gambatte/Mesen) savestates "
                       "can't be converted to BizHawk Core.bin states. Use a log that starts at "
                       "power-on (ROM or save-file load).")
    plat = Platform(h.platform)
    system = {Platform.GB: "gb", Platform.GBC: "gbc", Platform.GBC_GBA: "gbc_gba",
              Platform.SGB2: "gb", Platform.GBA: "gba"}[plat]
    if g.truncated:
        warnings.append("the .gm2 body is truncated (GSE crashed or is still writing); the movie "
                        "ends where the log could be recovered")
    if plat == Platform.SGB2:
        warnings.append("SGB2 log written as a plain GB movie (BizHawk's SGB mode uses a 4-player "
                        "layout and a different core revision); expect desyncs")
    if plat == Platform.GBC_GBA and h.reset_stall:
        warnings.append("Game Boy Player mode has no BizHawk equivalent (GSE's reset stall "
                        f"{h.reset_stall} and random fade-out); written as GBC-in-GBA mode")
    if plat == Platform.GBC_GBA:
        warnings.append("GBC-in-GBA: select the patched 'GBC_agb_gambatte.bin' BIOS in BizHawk to "
                        "match GSE's BIOS patch")
    if plat == Platform.GBA:
        warnings.append("GSE v0.6+ records GBA with the Mesen core; BizHawk only has mGBA, so this "
                        "movie is a transcription and will likely desync")
    if system != "gba" and h.emu_version.startswith("0.6"):
        warnings.append("GSE 0.6 uses a newer gambatte-core than BizHawk 2.11; rare desyncs are "
                        "possible - compare with InputLogPlayer --dump-av")
    sync: dict[str, Any] = {}
    if system != "gba" and h.gb_rtc_dividers:
        sync["InitialTime"] = h.gb_rtc_dividers // 2 ** 21
    if system == "gbc_gba":
        # GSE resets GBC-in-GBA with stall 0. BizHawk matches that only with EnableBIOS off
        # (it still forces the BIOS for movies) - with it on, Power stalls 485808 samples.
        sync["EnableBIOS"] = False
    mv = new_movie(system, game_name=h.rom_name, sha1=sha1, sync_fields=sync or None)
    if system != "gba" and h.gb_rtc_dividers % (2 ** 21):
        warnings.append("RTC start time isn't a whole number of seconds; BizHawk's InitialTime "
                        "is rounded down")
    if g.blob and not h.starts_from_savestate:
        mv.saveram = bytes(g.blob)
    mv.comments.append(f"Converted from GSE {h.emu_version} input log by ControllerLog")
    # Every GSE hard reset - including the one a save-file load starts with - becomes
    # Power on the next frame; BizHawk applies Power before running that frame.
    pending_power = False
    short = 0
    nominal = GBA_FRAME_CYCLES if system == "gba" else GB_FRAME_SAMPLES
    for cycles, buttons in g.records:
        if buttons & HARD_RESET:
            pending_power = True
            continue
        if cycles == 0:
            continue
        if system != "gba" and cycles != nominal:
            short += 1
        mv.frames.append(state_to_frame(mask_to_state(buttons), system, power=pending_power))
        pending_power = False
    if short:
        warnings.append(f"{short} record(s) with a budget other than {nominal} samples (GBP reset "
                        "fade-out) can't be expressed in VBlank-driven mode")
    return mv, warnings
