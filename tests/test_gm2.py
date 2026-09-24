from __future__ import annotations

import struct

import pytest

from controllerlog.formats import gm2
from controllerlog.formats.gm2 import (GB_FRAME_SAMPLES, HARD_RESET, Flags,
                                       Gm2Button, Gm2Error, Gm2Header,
                                       Gm2Movie, Platform)
from controllerlog.model import AXIS, BUTTON, BUTTON_INDEX, PadState
from controllerlog.timeline import FPS, Timeline


def sample_movie(platform=Platform.GB, version=2) -> Gm2Movie:
    hdr = Gm2Header(version=version, platform=platform, flags=Flags.ZSTD,
                    start_timestamp=1_790_000_000, rom_name="Pokemon Red",
                    emu_version="0.6", gba_rtc_time=0)
    A, B, RIGHT, LEFT = Gm2Button.A, Gm2Button.B, Gm2Button.RIGHT, Gm2Button.LEFT
    nominal = gm2.GBA_FRAME_CYCLES if platform == Platform.GBA else GB_FRAME_SAMPLES
    recs = [(0, HARD_RESET)] + [(nominal, 0)] * 3 + [(nominal, A)] * 2 + \
           [(nominal, A | RIGHT)] + [(nominal, B)] + [(0, HARD_RESET)] + [(nominal, LEFT)] * 2
    return Gm2Movie(hdr, blob=b"\x01\x02savefile" * 10, records=recs)


def test_header_offsets_match_spec():
    raw = gm2.dump_gm2(sample_movie(), compress=False)
    assert raw[:8] == b"GSEMOVIE"
    version, plat, stall, flags, ts, rtc, blob_size = struct.unpack_from("<IIIIqQI", raw, 8)
    assert (version, plat, stall, ts, blob_size) == (2, 0, 0, 1_790_000_000, 100)
    assert flags & Flags.ZSTD == 0  # compress=False clears the bit
    assert raw[44] == len("Pokemon Red") and raw[45:45 + 11] == b"Pokemon Red"
    assert raw[300] == 3 and raw[301:304] == b"0.6"
    assert len(raw) == 1024 + 100 + 8 * 11
    # v1 header is 560 bytes and has no GBA RTC field
    raw1 = gm2.dump_gm2(sample_movie(version=1), compress=False)
    assert len(raw1) == 560 + 100 + 8 * 11


@pytest.mark.parametrize("compress", [True, False])
@pytest.mark.parametrize("version", [1, 2])
def test_roundtrip(tmp_path, compress, version):
    mv = sample_movie(version=version)
    p = tmp_path / "run.gm2"
    gm2.write_gm2(p, mv, compress=compress)
    back = gm2.read_gm2(p)
    assert back.header.rom_name == "Pokemon Red" and back.header.version == version
    assert back.blob == mv.blob and back.records == mv.records
    assert not back.truncated
    assert bool(back.header.flags & Flags.ZSTD) == compress


def test_truncated_zstd_body_recovers_prefix():
    hdr = Gm2Header(rom_name="x")
    mv = Gm2Movie(hdr, blob=b"", records=[(GB_FRAME_SAMPLES, (i // 50) & 0xFF) for i in range(200_000)])
    raw = gm2.dump_gm2(mv)
    cut = gm2.parse_gm2(raw[: 1024 + (len(raw) - 1024) // 2])
    assert cut.truncated
    assert 0 < len(cut.records) < 200_000
    assert cut.records == mv.records[: len(cut.records)]


def test_bad_inputs():
    with pytest.raises(Gm2Error):
        gm2.parse_gm2(b"NOTAMOVIE" + b"\0" * 2000)
    raw = bytearray(gm2.dump_gm2(sample_movie(), compress=False))
    raw[8:12] = struct.pack("<I", 9)
    with pytest.raises(Gm2Error):
        gm2.parse_gm2(bytes(raw))


def test_frames_skip_resets_and_time():
    mv = sample_movie()
    frames = mv.frames()
    assert len(frames) == 9
    assert frames[0].reset_before and frames[7].reset_before and not frames[1].reset_before
    assert frames[3].names() == ["A"] and frames[5].names() == ["A", "Right"]
    fr_ns = GB_FRAME_SAMPLES * 1e9 / 2 ** 21
    assert frames[5].t_ns == pytest.approx(5 * fr_ns, abs=1)
    assert 1e9 / fr_ns == pytest.approx(FPS["gb"], rel=1e-9)


def test_gba_uses_cpu_cycles():
    mv = sample_movie(platform=Platform.GBA)
    frames = mv.frames()
    assert frames[1].t_ns == pytest.approx(280896 * 1e9 / 2 ** 24, abs=1)


def test_to_recording_lights_positional_buttons():
    rec = gm2.gm2_to_recording(sample_movie())
    tl = Timeline(rec)
    fps = FPS["gb"]
    frames = tl.frames(0, fps, mode="sample", count=9)
    east, south, right, left = (BUTTON_INDEX[n] for n in ("east", "south", "dpad_right", "dpad_left"))
    assert [f.buttons[east] for f in frames] == [0, 0, 0, 1, 1, 1, 0, 0, 0]  # GB A == east
    assert [f.buttons[south] for f in frames] == [0, 0, 0, 0, 0, 0, 1, 0, 0]  # GB B == south
    assert frames[5].buttons[right] == 1 and frames[7].buttons[left] == 1 and frames[8].buttons[left] == 1
    labels = [m.value for m in rec.markers()]
    assert labels.count("reset") == 2 and labels[-1] == "gm2:end"
    assert rec.devices[0].family == "gameboy"
    # all buttons released at the end
    end_state = tl.state_at(0, rec.duration_ns)
    assert not any(end_state.buttons)


def test_state_to_mask_rules():
    st = PadState()
    st.buttons[BUTTON_INDEX["east"]] = 1
    st.buttons[BUTTON_INDEX["dpad_left"]] = 1
    st.buttons[BUTTON_INDEX["dpad_right"]] = 1   # opposite directions cancel (GSE behaviour)
    st.axes[5] = 6000                            # right trigger past GSE threshold -> R
    assert gm2.state_to_mask(st, Platform.GBA) == Gm2Button.A | Gm2Button.R
    assert gm2.state_to_mask(st, Platform.GB) == Gm2Button.A  # GB mask drops L/R
    st2 = PadState()
    st2.axes[1] = -25000
    assert gm2.state_to_mask(st2, stick_to_dpad=True) == Gm2Button.UP
    assert gm2.state_to_mask(st2) == 0


def test_tas_edit_preserves_cycles_and_resets():
    mv = sample_movie()
    masks = gm2.frame_masks(mv)
    masks[0] = Gm2Button.START
    masks[1] = Gm2Button.LEFT | Gm2Button.RIGHT  # gets cleaned
    edited = gm2.with_frame_masks(mv, masks + [Gm2Button.B])
    assert [c for c, _ in edited.records[: len(mv.records)]] == [c for c, _ in mv.records]
    assert edited.records[0] == (0, HARD_RESET)
    assert edited.records[1] == (GB_FRAME_SAMPLES, Gm2Button.START)
    assert edited.records[2] == (GB_FRAME_SAMPLES, 0)
    assert edited.records[-1] == (GB_FRAME_SAMPLES, Gm2Button.B)
    assert gm2.parse_gm2(gm2.dump_gm2(edited)).records == edited.records


def test_approximate_from_states():
    s1 = PadState()
    s1.buttons[BUTTON_INDEX["start"]] = 1
    mv = gm2.approximate_from_states([PadState(), s1], Gm2Header())
    assert mv.records == [(GB_FRAME_SAMPLES, 0), (GB_FRAME_SAMPLES, Gm2Button.START)]


# --- TAS insert/delete keep resets with their frames -----------------------------------

def _reset_movie() -> Gm2Movie:
    A, B, START, N = Gm2Button.A, Gm2Button.B, Gm2Button.START, GB_FRAME_SAMPLES
    recs = [(N, 0), (N, A), (N, 0), (N, B), (N, 0), (0, HARD_RESET),
            (N, START), (N, 0), (N, A), (N, 0)]
    return Gm2Movie(Gm2Header(platform=Platform.GBC), b"", recs)


def test_insert_shifts_reset_with_inputs():
    from controllerlog import tas
    m = _reset_movie()
    out = gm2.apply_frame_movie(m, tas.apply_script(gm2.to_frame_movie(m), "insert 1 2"))
    fr = out.frames()
    assert [f.index for f in fr if f.reset_before] == [7]
    assert fr[7].buttons == Gm2Button.START and fr[5].buttons == Gm2Button.B
    assert [f.cycles for f in fr] == [GB_FRAME_SAMPLES] * 11   # inserted frames: nominal budget


def test_delete_shifts_reset_with_inputs():
    from controllerlog import tas
    m = _reset_movie()
    out = gm2.apply_frame_movie(m, tas.apply_script(gm2.to_frame_movie(m), "delete 1 2"))
    fr = out.frames()
    assert [f.index for f in fr if f.reset_before] == [3]
    assert fr[3].buttons == Gm2Button.START and fr[1].buttons == Gm2Button.B


def test_edits_keep_short_budgets_with_their_frames():
    from controllerlog import tas
    N = GB_FRAME_SAMPLES
    # GBP-style: a reset followed by a short fade-out budget record, then normal frames.
    recs = [(N, 0)] * 4 + [(0, HARD_RESET), (1234, 0), (N, Gm2Button.A), (N, 0)]
    m = Gm2Movie(Gm2Header(platform=Platform.GBC_GBA, reset_stall=3309568), b"", recs)
    fm = gm2.to_frame_movie(m)
    out = gm2.apply_frame_movie(m, tas.apply_script(fm, "insert 0 1\nhold 7 start"))
    assert out.records == [(N, 0)] * 5 + [(0, HARD_RESET), (1234, 0), (N, Gm2Button.A),
                                           (N, Gm2Button.START)]
    # a reset in front of a deleted frame moves to the next surviving frame
    out = gm2.apply_frame_movie(m, tas.apply_script(fm, "delete 4 1"))
    assert out.records == [(N, 0)] * 4 + [(0, HARD_RESET), (N, Gm2Button.A), (N, 0)]
    # unchanged length and no structural edit: identical records
    assert gm2.apply_frame_movie(m, fm).records == recs


# --- frame times agree with timeline.frame_time_ns --------------------------------------

def test_gm2_any_mode_quantization_matches_frames():
    from controllerlog.timeline import FPS as _FPS
    N = GB_FRAME_SAMPLES
    for k in range(1, 50):  # a frame whose start time has fractional ns >= 0.5
        if (k * N * 10**9 % gm2.GB_SAMPLE_HZ) * 2 >= gm2.GB_SAMPLE_HZ:
            break
    recs = [(N, 0)] * (k + 3)
    recs[k] = (N, Gm2Button.A)
    m = Gm2Movie(Gm2Header(platform=Platform.GBC), b"", recs)
    got = [st.buttons[BUTTON_INDEX["east"]] for st in Timeline(gm2.gm2_to_recording(m)).frames(
        0, fps=_FPS["gb"], mode="any", count=len(recs))]
    assert got == [int(bool(x & 1)) for x in gm2.frame_masks(m)]


def test_gm2_from_recording_any_mode_matches_masks():
    from controllerlog import tas
    N = GB_FRAME_SAMPLES
    recs = [(N, Gm2Button.A if i % 7 == 3 else 0) for i in range(600)]
    m = Gm2Movie(Gm2Header(platform=Platform.GBC), b"", recs)
    fm = tas.from_recording(gm2.gm2_to_recording(m), fps=FPS["gb"], mode="any")
    assert len(fm) == 600
    assert [st.buttons[BUTTON_INDEX["east"]] for st in fm.frames] == \
           [int(bool(x & 1)) for x in gm2.frame_masks(m)]


# --- triggers count as GBA L/R in write-back -------------------------------------------

def test_gm2_gba_write_back_counts_pressed_triggers_as_l_r():
    from controllerlog import tas
    from controllerlog.model import AXIS_INDEX
    st = PadState()
    st.axes[AXIS_INDEX["left_trigger"]] = 32767
    st.axes[AXIS_INDEX["right_trigger"]] = 32767
    g = Gm2Movie(Gm2Header(platform=Platform.GBA), b"", [(gm2.GBA_FRAME_CYCLES, 0)])
    out = gm2.apply_frame_movie(g, tas.FrameMovie(60.0, [st], "gba"))
    assert out.records[0][1] == Gm2Button.L | Gm2Button.R


# --- decompression caps ----------------------------------------------------------------

def _gm2_bomb(inflated: int) -> bytes:
    from compression import zstd
    header = gm2.dump_gm2(gm2.Gm2Movie(), compress=True)[:gm2.HEADER_SIZE[2]]  # ZSTD flag, blob 0
    return header + zstd.compress(bytes(inflated), level=19)


def test_gm2_zstd_body_is_capped(monkeypatch):
    monkeypatch.setattr(gm2, "MAX_BODY_BYTES", 1 << 20)
    raw = _gm2_bomb(8 << 20)            # ~1 KB file that inflates to 8 MiB
    assert len(raw) < 4096
    with pytest.raises(Gm2Error):
        gm2.parse_gm2(raw)


def test_gm2_normal_log_still_parses_under_the_cap(monkeypatch):
    mv = gm2.Gm2Movie(records=[(35112, i & 0xFF) for i in range(50_000)])
    assert len(gm2.parse_gm2(gm2.dump_gm2(mv)).records) == 50_000
    monkeypatch.setattr(gm2, "MAX_BODY_BYTES", 400_000)   # the body is exactly 400,000 bytes
    assert len(gm2.parse_gm2(gm2.dump_gm2(mv)).records) == 50_000
    assert len(gm2.parse_gm2(gm2.dump_gm2(mv, compress=False)).records) == 50_000
    monkeypatch.setattr(gm2, "MAX_BODY_BYTES", 399_999)
    with pytest.raises(Gm2Error):
        gm2.parse_gm2(gm2.dump_gm2(mv, compress=False))


def test_resets_of_several_deleted_frames_collapse_into_one():
    from controllerlog import tas
    N = GB_FRAME_SAMPLES
    recs = [(N, 0), (N, 0), (0, HARD_RESET), (N, 0), (0, HARD_RESET), (N, 0), (N, Gm2Button.A)]
    m = Gm2Movie(Gm2Header(platform=Platform.GBC), b"", recs)
    out = gm2.apply_frame_movie(m, tas.apply_script(gm2.to_frame_movie(m), "delete 2 2"))
    assert out.records == [(N, 0), (N, 0), (0, HARD_RESET), (N, Gm2Button.A)]
