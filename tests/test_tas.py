from __future__ import annotations

import pytest

from controllerlog import tas
from controllerlog.formats import gm2
from controllerlog.formats.gm2 import GB_FRAME_SAMPLES, HARD_RESET, Gm2Button, Gm2Header, Gm2Movie
from controllerlog.logfile import Recording
from controllerlog.model import BUTTON, BUTTON_INDEX, CONNECT, MARK, DeviceInfo, InputEvent, PadState
from controllerlog.timeline import FPS, Timeline

E = BUTTON_INDEX["east"]
S = BUTTON_INDEX["south"]
ST = BUTTON_INDEX["start"]


def movie(n=10) -> tas.FrameMovie:
    return tas.FrameMovie(60.0, [PadState() for _ in range(n)], "gb")


def pressed(m: tas.FrameMovie, b: int) -> list[int]:
    return [f.buttons[b] for f in m.frames]


def test_script_ops():
    m = tas.apply_script(movie(), """
        # comment
        hold 2-4 east
        release 3 east
        set 6 start,south
        insert 0 2 south      # shifts everything right by 2
        delete 9 1
        axis 1-2 left_x -32768
    """)
    assert len(m) == 11
    assert pressed(m, S)[:2] == [1, 1]
    assert pressed(m, E) == [0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0]
    assert m.frames[8].buttons[ST] == 1 and m.frames[8].buttons[S] == 1
    assert m.frames[1].axes[0] == -32768
    assert m.meta["tas_edited"] and len(m.meta["edit_history"]) == 6


def test_script_extends_and_copies():
    m = tas.apply_script(movie(3), "hold 5 north\ncopy 5-5 8")
    assert len(m) == 9 and m.frames[8].buttons[BUTTON_INDEX["north"]] == 1


@pytest.mark.parametrize("bad", ["hold 3 banana", "delete 50 1", "frobnicate 1", "hold 5-2 east",
                                 "axis 1 left_trigger -5"])
def test_script_errors(bad):
    with pytest.raises(tas.EditError):
        tas.apply_script(movie(), bad)


def test_original_untouched_by_script():
    m = movie()
    tas.apply_script(m, "hold 0-9 east")
    assert not any(pressed(m, E))


def test_csv_roundtrip():
    m = tas.apply_script(movie(), "hold 1-3 east\naxis 2 right_trigger 32767")
    text = tas.to_csv(m)
    assert text.splitlines()[0] == "frame,time_ms,east,right_trigger"
    back = tas.from_csv(text, 60.0)
    assert [f.buttons for f in back.frames] == [f.buttons for f in m.frames]
    assert back.frames[2].axes[5] == 32767


def test_recording_roundtrip_and_diff():
    rec = Recording(header={"meta": {}})
    rec.devices[0] = DeviceInfo(0, "pad")
    fr = 1e9 / 60
    rec.events = [InputEvent(0, 0, CONNECT, None, rec.devices[0].to_json()),
                  InputEvent(round(2.5 * fr), 0, BUTTON, E, 1),
                  InputEvent(round(2.7 * fr), 0, BUTTON, E, 0),   # sub-frame tap
                  InputEvent(round(5 * fr), None, MARK, None, "split:x"),
                  InputEvent(round(9 * fr), 0, BUTTON, S, 1)]
    m = tas.from_recording(rec, fps=60, mode="any")
    assert pressed(m, E)[2] == 1  # tap kept
    m2 = tas.from_recording(rec, fps=60, start_marker="split")
    assert pressed(m2, S)[4] == 1
    back = tas.to_recording(m)
    assert pressed(tas.from_recording(back, fps=60, mode="sample"), E)[:len(m)] == pressed(m, E)
    d = tas.diff(m, tas.apply_script(m, "release 2 east\nhold 3 north"))
    assert [(x.frame, x.only_a, x.only_b) for x in d] == [(2, ["east"], []), (3, [], ["north"])]


def test_gm2_frame_movie_edit_roundtrip():
    recs = [(0, HARD_RESET)] + [(GB_FRAME_SAMPLES, 0)] * 5 + [(0, HARD_RESET)] + [(GB_FRAME_SAMPLES, Gm2Button.A)] * 3
    mv = Gm2Movie(Gm2Header(rom_name="Tetris"), blob=b"sav", records=recs)
    fm = gm2.to_frame_movie(mv)
    assert fm.system == "gb" and len(fm) == 8 and pressed(fm, E)[5:] == [1, 1, 1]
    edited = tas.apply_script(fm, "hold 0 start\ndelete 6 2")
    out = gm2.apply_frame_movie(mv, edited)
    assert out.records[:2] == [(0, HARD_RESET), (GB_FRAME_SAMPLES, Gm2Button.START)]
    assert len(out.frames()) == 6 and out.records[-1] == (GB_FRAME_SAMPLES, Gm2Button.A)
    # truncation drops a dangling reset
    out2 = gm2.apply_frame_movie(mv, tas.apply_script(fm, "delete 5 3"))
    assert out2.records[-1] == (GB_FRAME_SAMPLES, 0) and len(out2.frames()) == 5


@pytest.mark.parametrize("fps,n", [(60.0, 3), (FPS["gb"], 6), (60.0, 1), (FPS["gb"], 199)])
def test_frame_movie_ctlog_round_trip_keeps_length(fps, n):
    frames = [PadState() for _ in range(n)]
    frames[0].buttons[E] = 1
    fm = tas.FrameMovie(fps, frames, "gb")
    back = tas.from_recording(tas.to_recording(fm), fps=fps)
    assert len(back) == n
    assert pressed(back, E) == pressed(fm, E)


def test_load_csv_accepts_utf8_bom_and_utf16(tmp_path):
    frames = [PadState() for _ in range(3)]
    frames[1].buttons[E] = 1
    text = tas.to_csv(tas.FrameMovie(60.0, frames))
    for enc in ("utf-8-sig", "utf-16"):          # Excel 'CSV UTF-8' / PowerShell 5.1 '>'
        p = tmp_path / f"t-{enc}.csv"
        p.write_text(text, encoding=enc)
        fm = tas.load_csv(p, 60.0)
        assert pressed(fm, E) == [0, 1, 0]


def test_load_csv_infers_its_frame_rate(tmp_path):
    p = tmp_path / "gb.csv"
    tas.save_csv(tas.FrameMovie(FPS["gb"], [PadState() for _ in range(5)]), p)
    assert tas.load_csv(p, None).fps == FPS["gb"]
    assert tas.load_csv(p, 30.0).fps == 30.0            # an explicit rate wins
    one = tmp_path / "one.csv"
    tas.save_csv(tas.FrameMovie(FPS["gb"], [PadState()]), one)
    assert tas.load_csv(one, None).fps == 60.0          # can't tell from one row


def test_edits_track_frame_origin():
    m = movie(6)
    m.origin = list(range(6))
    out = tas.apply_script(m, "insert 1 2\ndelete 4 2\ncopy 0-1 5\nhold 9 east")
    assert out.origin == [0, None, None, 1, 4, 5, None, None, None, None]
    assert m.origin == list(range(6))                  # the source movie is untouched
    assert tas.apply_script(movie(3), "insert 0 1").origin is None   # untracked stays untracked
