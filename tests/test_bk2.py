from __future__ import annotations

import io
import json
import re
import zipfile

import pytest

from controllerlog import tas
from controllerlog.formats import bk2, gm2
from controllerlog.formats.gm2 import (GB_FRAME_SAMPLES, HARD_RESET, Flags, Gm2Button,
                                       Gm2Header, Gm2Movie, Platform)
from controllerlog.model import BUTTON_INDEX, PadState

GB_LINE = re.compile(r"^\|[U.][D.][L.][R.][S.][s.][B.][A.][P.]\|$")


def fm_with(presses: dict[int, list[str]], n=6, system="gb") -> tas.FrameMovie:
    frames = [PadState() for _ in range(n)]
    for i, names in presses.items():
        for name in names:
            frames[i].buttons[BUTTON_INDEX[name]] = 1
    return tas.FrameMovie(59.7275, frames, system)


def entries(data: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {n: z.read(n).decode("utf-8", "replace") for n in z.namelist()}


def test_gb_movie_layout_matches_bizhawk():
    fm = fm_with({1: ["east"], 2: ["dpad_up", "start"], 3: ["dpad_left", "dpad_right", "south"]})
    mv = bk2.from_frame_movie(fm, "gbc", game_name="Pokemon Crystal", sha1="abc123")
    files = entries(bk2.dump_bk2(mv))
    assert set(files) >= {"Header.txt", "Input Log.txt", "SyncSettings.json", "BizState 1.0",
                          "Comments.txt", "Subtitles.txt"}
    log = files["Input Log.txt"].split("\r\n")
    assert log[0] == "[Input]"
    assert log[1] == "LogKey:#Up|Down|Left|Right|Start|Select|B|A|Power|"
    assert log[2:6] == ["|.........|", "|.......A.|", "|U...S....|", "|......B..|"]  # L+R cancelled
    assert all(GB_LINE.match(l) for l in log[2:8])
    assert log[8] == "[/Input]"
    hdr = dict(l.split(" ", 1) for l in files["Header.txt"].split("\r\n") if l)
    assert hdr["Platform"] == "GB" and hdr["Core"] == "Gambatte" and hdr["IsCGBMode"] == "1"
    assert hdr["SHA1"] == "ABC123" and hdr["MovieVersion"] == "BizHawk v2.0.0"
    sync = files["SyncSettings.json"].strip()
    assert "\n" not in sync and sync.startswith('{"o":{"$type":')
    assert json.loads(sync)["o"]["ConsoleMode"] == 2


def test_gba_movie_line_has_axes_first():
    mv = bk2.from_frame_movie(fm_with({0: ["east", "left_shoulder", "dpad_right"]}, n=1, system="gba"))
    lines = mv.frame_lines()
    assert lines == ["|    0,    0,    0,    0,...R...A.l.|".replace("...R...A.l.", "...R...Al..")]
    assert mv.log_key == "#Tilt X|Tilt Y|Tilt Z|Light Sensor|Up|Down|Left|Right|Start|Select|B|A|L|R|Power|"
    assert mv.header["Platform"] == "GBA" and mv.header["Core"] == "mGBA"


def test_roundtrip_read(tmp_path):
    fm = fm_with({0: ["east"], 4: ["back", "dpad_down"]})
    p = tmp_path / "m.bk2"
    bk2.write_bk2(p, bk2.from_frame_movie(fm, "gb", game_name="Tetris"))
    mv = bk2.read_bk2(p)
    assert mv.header["GameName"] == "Tetris"
    back = bk2.to_frame_movie(mv)
    assert back.system == "gb" and len(back) == 6
    assert [f.buttons for f in back.frames] == [f.buttons for f in fm.frames]


def test_read_subframe_and_multiplayer_lines(tmp_path):
    # Gambatte sub-frame mode (Input Length axis) and a P1/P2 NES-style layout.
    def make(key, lines, platform):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("Header.txt", f"MovieVersion BizHawk v2.0.0\nPlatform {platform}\n")
            z.writestr("Input Log.txt", "[Input]\nLogKey:" + key + "\n" + "\n".join(lines) + "\n[/Input]\n")
        p = tmp_path / f"{platform}.bk2"
        p.write_bytes(buf.getvalue())
        return bk2.read_bk2(p)

    mv = make("#Input Length|Up|Down|Left|Right|Start|Select|B|A|Power|",
              ["|35112,.......A.|", "|17556,U........|"], "GB")
    assert mv.frames[0]["Input Length"] == 35112 and mv.frames[0]["A"] is True
    assert mv.frames[1]["Input Length"] == 17556 and mv.frames[1]["Up"] is True
    assert "Input Length" in mv.axes
    nes = make("#Reset|Power|#P1 Up|P1 Down|P1 Left|P1 Right|P1 Start|P1 Select|P1 B|P1 A|"
               "#P2 Up|P2 Down|P2 Left|P2 Right|P2 Start|P2 Select|P2 B|P2 A|",
               ["|..|.......A|U.......|", "|.P|........|........|"], "NES")
    fm = bk2.to_frame_movie(nes)
    assert fm.system == "nes"
    assert fm.frames[0].buttons[BUTTON_INDEX["east"]] == 1          # P1 A
    assert fm.frames[0].buttons[BUTTON_INDEX["dpad_up"]] == 0       # P2 ignored
    assert fm.meta["power_frames"] == [1]


def test_short_line_rejected(tmp_path):
    with pytest.raises(bk2.Bk2Error):
        bk2.parse_frame("|UD|", [bk2.GB_BUTTONS])


def test_from_gm2_resets_become_power_and_saveram():
    recs = [(0, HARD_RESET)] + [(GB_FRAME_SAMPLES, 0)] * 2 + [(GB_FRAME_SAMPLES, Gm2Button.A)] + \
           [(0, HARD_RESET)] + [(GB_FRAME_SAMPLES, Gm2Button.START)]
    g = Gm2Movie(Gm2Header(platform=Platform.GBC, rom_name="Crystal", emu_version="0.6",
                           gb_rtc_dividers=5 * 2 ** 21), blob=b"\x42" * 32, records=recs)
    mv, warnings = bk2.from_gm2(g, sha1="ff")
    assert mv.frame_lines() == ["|........P|", "|.........|", "|.......A.|", "|....S...P|"]
    assert mv.saveram == b"\x42" * 32
    assert json.loads(mv.sync_settings)["o"]["InitialTime"] == 5
    assert len(warnings) == 1 and "gambatte-core" in warnings[0]  # GSE 0.6 core skew
    files = entries(bk2.dump_bk2(mv))
    assert "StartsFromSaveRam True" in files["Header.txt"]
    assert files["BizState 1.0"].strip() == "3"


def test_from_gm2_warnings_and_savestate_refusal():
    gbp = Gm2Movie(Gm2Header(platform=Platform.GBC_GBA, reset_stall=3309568),
                   records=[(17000, 0), (GB_FRAME_SAMPLES, 0)])
    mv, warnings = bk2.from_gm2(gbp)
    joined = " ".join(warnings)
    # GBC-in-GBA: EnableBIOS off so BizHawk's reset stall matches GSE's (0)
    assert json.loads(mv.sync_settings)["o"]["EnableBIOS"] is False
    assert json.loads(mv.sync_settings)["o"]["ConsoleMode"] == 3
    assert "Game Boy Player" in joined and "fade-out" in joined and "GBC_agb_gambatte" in joined
    ss = Gm2Movie(Gm2Header(flags=Flags.ZSTD | Flags.STARTS_FROM_SAVESTATE), records=[(GB_FRAME_SAMPLES, 0)])
    with pytest.raises(bk2.Bk2Error):
        bk2.from_gm2(ss)


def test_roundtrip_keeps_savestate_anchor_and_unknown_lumps(tmp_path):
    from compression import zstd
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("BizState 1.0", "2\r\n")
        z.writestr("Header.txt", "MovieVersion BizHawk v2.0.0\r\nPlatform GB\r\nStartsFromSavestate True\r\n")
        z.writestr("Input Log.txt", "[Input]\r\nLogKey:#Up|Down|Left|Right|Start|Select|B|A|Power|\r\n"
                                    "|.......A.|\r\n[/Input]\r\n")
        z.writestr("Core.bin", zstd.compress(b"core-state"))
        z.writestr("Framebuffer.bmp", b"BMfake")
        z.writestr("MovieSaveRam.bin", zstd.compress(b"\x11" * 8))  # 2.10: zstd inside plain .bin
    src = tmp_path / "anchored.bk2"
    src.write_bytes(buf.getvalue())
    mv = bk2.read_bk2(src)
    assert mv.saveram == b"\x11" * 8 and mv.bizstate == "2"
    assert set(mv.extra_lumps) == {"Core.bin", "Framebuffer.bmp"}
    files = {}
    with zipfile.ZipFile(io.BytesIO(bk2.dump_bk2(mv))) as z:
        for n in z.namelist():
            files[n] = z.read(n)
    assert zstd.decompress(files["Core.bin"]) == b"core-state"
    assert files["BizState 1.0"].strip() == b"2"                    # encoding version kept
    assert zstd.decompress(files["MovieSaveRam.bin"]) == b"\x11" * 8  # re-encoded for version 2
    assert b"StartsFromSavestate True" in files["Header.txt"]


def test_generic_system_needs_target():
    with pytest.raises(bk2.Bk2Error):
        bk2.from_frame_movie(fm_with({}, system="generic"))


# --- editing a .bk2 keeps what the movie needs to sync ------------------------------------

def test_bk2_edit_keeps_saveram_power_and_gbc_mode(tmp_path):
    from controllerlog import cli
    N = GB_FRAME_SAMPLES
    g = Gm2Movie(Gm2Header(platform=Platform.GBC, rom_name="X"), b"\x11" * 64,
                 [(0, HARD_RESET), (N, 0), (N, Gm2Button.A), (N, 0),
                  (0, HARD_RESET), (N, 0), (N, 0)])
    src, out = tmp_path / "a.bk2", tmp_path / "b.bk2"
    mv, _ = bk2.from_gm2(g, sha1="ab" * 20)
    bk2.write_bk2(src, mv)
    assert cli.main(["edit", str(src), str(out), "--do", "hold 3 east"]) == 0
    a, b = bk2.read_bk2(src), bk2.read_bk2(out)
    assert b.saveram == a.saveram
    assert b.header.get("SHA1") == a.header.get("SHA1")
    assert b.header.get("IsCGBMode") == "1"
    assert json.loads(b.sync_settings)["o"]["ConsoleMode"] == 2
    assert [i for i, f in enumerate(b.frames) if f.get("Power")] == \
           [i for i, f in enumerate(a.frames) if f.get("Power")]
    assert b.frames[3]["A"] and not a.frames[3]["A"]
    assert "TAS-edited with ControllerLog" in b.comments


def test_bk2_apply_frame_movie_moves_power_with_frames():
    fm0 = fm_with({1: ["east"]}, n=5, system="gbc")
    base = bk2.from_frame_movie(fm0, "gbc")
    base.frames[3]["Power"] = True
    fm = bk2.to_frame_movie(base)
    assert fm.system == "gbc" and fm.meta["power_frames"] == [3]
    ins = bk2.apply_frame_movie(base, tas.apply_script(fm, "insert 0 2"))
    assert [i for i, f in enumerate(ins.frames) if f["Power"]] == [5] and ins.frames[3]["A"]
    dele = bk2.apply_frame_movie(base, tas.apply_script(fm, "delete 2 2"))   # deletes the Power frame
    assert [i for i, f in enumerate(dele.frames) if f["Power"]] == [2] and len(dele.frames) == 3


def test_bk2_to_frame_movie_reads_cgb_mode():
    for system in ("gb", "gbc", "gbc_gba"):
        mv = bk2.from_frame_movie(fm_with({}, system=system), system)
        assert bk2.to_frame_movie(mv).system == system


def test_bk2_gba_export_counts_pressed_triggers_as_l_r():
    from controllerlog.model import AXIS_INDEX
    st = PadState()
    st.axes[AXIS_INDEX["left_trigger"]] = 32767
    st.axes[AXIS_INDEX["right_trigger"]] = 32767
    assert st.pressed("left_trigger") and st.pressed("right_trigger")
    mv = bk2.from_frame_movie(tas.FrameMovie(60.0, [st], "gba"), system="gba")
    assert mv.frames[0]["L"] and mv.frames[0]["R"]


# --- size caps ---------------------------------------------------------------------------

def _bk2_bomb(tmp_path, *, saveram: bool):
    from compression import zstd
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Header.txt", "MovieVersion BizHawk v2.0\nPlatform GB\n")
        z.writestr("Input Log.txt", "[Input]\nLogKey:#Up|Down|\n|..|\n[/Input]\n")
        if saveram:
            z.writestr("MovieSaveRam.bin", zstd.compress(bytes(8 << 20), level=19))
        else:
            z.writestr("Junk.bin", bytes(8 << 20))  # deflates to ~8 KB
    p = tmp_path / ("saveram.bk2" if saveram else "extra.bk2")
    p.write_bytes(buf.getvalue())
    return p


@pytest.mark.parametrize("saveram", [False, True])
def test_bk2_lumps_are_capped(tmp_path, monkeypatch, saveram):
    monkeypatch.setattr(bk2, "MAX_LUMP_BYTES", 1 << 20)
    path = _bk2_bomb(tmp_path, saveram=saveram)
    assert path.stat().st_size < 64 * 1024
    with pytest.raises(bk2.Bk2Error):
        bk2.read_bk2(path)
    monkeypatch.setattr(bk2, "MAX_LUMP_BYTES", 16 << 20)      # a generous cap reads it fine
    assert len(bk2.read_bk2(path).frames) == 1
