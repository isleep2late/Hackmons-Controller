"""CLI smoke tests for the commands that need no hardware."""
from __future__ import annotations

import json
import zipfile

import pytest

from controllerlog.cli import main
from controllerlog.formats import gm2
from controllerlog.formats.gm2 import GB_FRAME_SAMPLES, HARD_RESET, Gm2Button as B, Gm2Header, Gm2Movie
from controllerlog.logfile import read_log


@pytest.fixture
def sample_gm2(tmp_path):
    recs = [(0, HARD_RESET)]
    for n, mask in [(30, 0), (2, B.START), (10, 0), (1, B.A), (5, 0), (20, B.RIGHT), (3, B.A | B.RIGHT),
                    (10, 0), (1, B.A), (1, 0), (1, B.A), (15, B.DOWN | B.B)]:
        recs += [(GB_FRAME_SAMPLES, int(mask))] * n
    p = tmp_path / "run.gm2"
    gm2.write_gm2(p, Gm2Movie(Gm2Header(rom_name="Sample", emu_version="0.6"), blob=b"\0" * 64, records=recs))
    return p


def test_gm2_info_json(sample_gm2, capsys):
    assert main(["gm2", "info", str(sample_gm2), "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["frames"] == 99 and info["resets"] == 1 and info["rom_name"] == "Sample"


def test_convert_chain(sample_gm2, tmp_path, capsys):
    ct, csv, bk = tmp_path / "a.ctlog", tmp_path / "a.csv", tmp_path / "a.bk2"
    assert main(["convert", str(sample_gm2), str(ct)]) == 0
    assert main(["convert", str(sample_gm2), str(csv)]) == 0
    assert main(["convert", str(sample_gm2), str(bk)]) == 0
    rec = read_log(ct)
    assert rec.devices[0].family == "gameboy"
    assert csv.read_text().splitlines()[0].startswith("frame,time_ms,")
    with zipfile.ZipFile(bk) as z:
        lines = [l for l in z.read("Input Log.txt").decode().splitlines() if l.startswith("|")]
    assert len(lines) == 99 and lines[30] == "|....S....|"
    # bk2 back to csv round trip keeps the frames
    csv2 = tmp_path / "b.csv"
    assert main(["convert", str(bk), str(csv2)]) == 0
    assert csv2.read_text().splitlines()[1:] == csv.read_text().splitlines()[1:]


def test_stats_and_diff_and_edit(sample_gm2, tmp_path, capsys):
    assert main(["stats", str(sample_gm2), "--fps", "gb", "--json"]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["devices"]["0"]["east"]["presses"] == 4
    out = tmp_path / "edited.gm2"
    assert main(["edit", str(sample_gm2), str(out), "--do", "hold 0 start", "--do", "delete 90 9"]) == 0
    edited = gm2.read_gm2(out)
    assert len(edited.frames()) == 90 and edited.frames()[0].buttons == B.START
    capsys.readouterr()
    assert main(["diff", str(sample_gm2), str(out), "--fps", "gb"]) == 0
    assert "frame(s) differ" in capsys.readouterr().out


def test_edit_requires_commands(sample_gm2, tmp_path):
    assert main(["edit", str(sample_gm2), str(tmp_path / "x.gm2")]) == 2


def test_writing_gm2_from_scratch_needs_template(sample_gm2, tmp_path):
    csv = tmp_path / "a.csv"
    main(["convert", str(sample_gm2), str(csv)])
    with pytest.raises(SystemExit):
        main(["convert", str(csv), str(tmp_path / "x.gm2"), "--fps", "gb"])
    assert main(["convert", str(csv), str(tmp_path / "y.gm2"), "--fps", "gb",
                 "--template", str(sample_gm2)]) == 0


def test_layouts_lists_all(capsys):
    assert main(["layouts"]) == 0
    out = capsys.readouterr().out
    for name in ("xbox", "playstation", "switch", "gameboy", "gba", "generic"):
        assert name in out
    assert "INVALID" not in out


# --- shared fixtures for the regression tests below -------------------------------------

MS = 1_000_000
N = GB_FRAME_SAMPLES


def two_ctlog(path):
    """Two pads: device 0 taps south 20x, device 1 taps east 5x; run/split markers."""
    from controllerlog.logfile import Recording
    from controllerlog.model import BUTTON, CONNECT, MARK, DeviceInfo, InputEvent
    rec = Recording(header={"format": "controllerlog", "version": 1, "meta": {}})
    d0, d1 = DeviceInfo(0, "Xbox Wireless Controller"), DeviceInfo(1, "DualSense")
    rec.devices = {0: d0, 1: d1}
    ev = [InputEvent(0, 0, CONNECT, None, d0.to_json()), InputEvent(0, 1, CONNECT, None, d1.to_json()),
          InputEvent(10 * MS, None, MARK, None, "run:start")]
    for i in range(20):  # device 0: south taps
        t = 100 * MS + i * 300 * MS
        ev += [InputEvent(t, 0, BUTTON, 0, 1), InputEvent(t + 140 * MS, 0, BUTTON, 0, 0)]
    for i in range(5):   # device 1: east taps
        t = 200 * MS + i * 500 * MS
        ev += [InputEvent(t, 1, BUTTON, 1, 1), InputEvent(t + 100 * MS, 1, BUTTON, 1, 0)]
    ev += [InputEvent(3000 * MS, None, MARK, None, "split:1"),
           InputEvent(6500 * MS, None, MARK, None, "run:end")]
    rec.events = sorted(ev, key=lambda e: e.t_ns)
    rec.save(path)
    return path


def gbc_gm2(path, rom_name="Sample", platform=gm2.Platform.GBC):
    """GBC log: save-load reset, START on 20-21, a mid-run reset, START on the frame after it."""
    recs = ([(0, HARD_RESET)] + [(N, 0)] * 20 + [(N, int(B.START))] * 2 + [(N, 0)] * 18
            + [(0, HARD_RESET), (N, int(B.START))] + [(N, 0)] * 20)
    hdr = Gm2Header(platform=platform, rom_name=rom_name, emu_version="0.6",
                    gb_rtc_dividers=131 * 2 ** 21, start_timestamp=1_757_300_000)
    gm2.write_gm2(path, Gm2Movie(hdr, blob=bytes(range(256)) * 4, records=recs))
    return path


class _Report:
    def summary(self):
        return "ok"


@pytest.fixture
def fake_replay(monkeypatch):
    from controllerlog import replay
    calls = []

    def fake_replay_frames(frames, fps, **kw):
        calls.append((list(frames), fps, kw))
        return _Report()

    monkeypatch.setattr(replay, "replay_frames", fake_replay_frames)
    return calls


def _power(m):
    return [i for i, fr in enumerate(m.frames) if fr.get("Power")]


# --- replay ------------------------------------------------------------------------------

def test_replay_bk2_uses_its_frame_movie(tmp_path, fake_replay):
    from controllerlog import tas
    from controllerlog.formats import bk2
    from controllerlog.model import PadState
    frames = [PadState() for _ in range(60)]
    frames[10].buttons[1] = 1  # east = GB "A"
    p = tmp_path / "short.bk2"
    bk2.write_bk2(p, bk2.from_frame_movie(tas.FrameMovie(59.73, frames, "gb"), "gb"))
    assert main(["replay", str(p), "--countdown", "0", "--quiet"]) == 0  # AttributeError before
    got, fps, _ = fake_replay[0]
    assert all(isinstance(f, PadState) for f in got)
    assert len(got) == 60 and got[10].buttons[1] == 1
    assert abs(fps - 4194304 / 70224) < 1e-6


def test_replay_bk2_from_gm2_uses_frame_movie(tmp_path, fake_replay):
    from controllerlog.formats import bk2
    from controllerlog.model import BUTTON_INDEX
    recs = [(0, HARD_RESET)] + [(N, 0)] * 5 + [(N, int(B.A))] * 3
    mv, _ = bk2.from_gm2(Gm2Movie(Gm2Header(rom_name="T", emu_version="0.6"), b"", recs))
    p = tmp_path / "run.bk2"
    bk2.write_bk2(p, mv)
    assert main(["replay", str(p), "--countdown", "0", "--quiet"]) == 0
    frames, fps, _ = fake_replay[0]
    assert len(frames) == 8 and abs(fps - 59.7275) < 0.01
    assert frames[5].buttons[BUTTON_INDEX["east"]] == 1


def test_replay_frame_path_honours_device_speed_and_marker(tmp_path, fake_replay):
    ct = two_ctlog(tmp_path / "two.ctlog")
    assert main(["replay", str(ct), "--frames", "--device", "1", "--speed", "4",
                 "--countdown", "0", "--quiet"]) == 0
    frames, _, kw = fake_replay[0]
    assert kw.get("speed") == 4
    assert any(f.buttons[1] for f in frames)       # device 1 pressed east
    assert not any(f.buttons[0] for f in frames)   # device 0's south must not be replayed
    g = gbc_gm2(tmp_path / "g.gm2")
    assert main(["replay", str(g), "--from-marker", "nope", "--countdown", "0", "--quiet"]) != 0
    assert main(["replay", str(g), "--device", "3", "--countdown", "0", "--quiet"]) != 0
    assert len(fake_replay) == 1


def test_frame_replay_honours_speed_and_from_marker(tmp_path, fake_replay):
    from controllerlog.model import BUTTON_INDEX
    recs = [(N, 0)] * 10 + [(0, HARD_RESET)] + [(N, int(B.A))] * 5
    p = tmp_path / "run.gm2"
    gm2.write_gm2(p, Gm2Movie(Gm2Header(rom_name="T", emu_version="0.6"), b"", recs))
    assert main(["replay", str(p), "--from-marker", "reset", "--speed", "2",
                 "--countdown", "0", "--quiet"]) == 0
    frames, _, kw = fake_replay[0]
    assert kw.get("speed") == 2
    assert frames[0].buttons[BUTTON_INDEX["east"]] == 1 and len(frames) == 5


# --- edit / convert to .bk2 keep what the movie needs to sync ----------------------------

def test_edit_bk2_to_bk2_keeps_saveram_mode_sha1_and_power(tmp_path):
    from controllerlog.formats import bk2
    c = tmp_path / "c.bk2"
    assert main(["convert", str(gbc_gm2(tmp_path / "c.gm2")), str(c), "--sha1", "abc"]) == 0
    src = bk2.read_bk2(c)
    assert src.saveram and src.header.get("IsCGBMode") == "1" and _power(src)
    out = tmp_path / "ce.bk2"
    assert main(["edit", str(c), str(out), "--do", "hold 5-6 east"]) == 0
    ed = bk2.read_bk2(out)
    assert ed.saveram == src.saveram
    assert ed.header.get("IsCGBMode") == "1" and ed.header.get("SHA1") == "ABC"
    assert ed.sync_settings == src.sync_settings
    assert _power(ed) == _power(src)
    # convert .bk2 -> .bk2 is a faithful copy too
    cc = tmp_path / "cc.bk2"
    assert main(["convert", str(c), str(cc)]) == 0
    assert bk2.read_bk2(cc).frame_lines() == src.frame_lines()


def test_edit_gm2_to_bk2_matches_faithful_convert(tmp_path):
    from controllerlog.formats import bk2
    g = gbc_gm2(tmp_path / "c.gm2")
    ref, _ = bk2.from_gm2(gm2.read_gm2(g))
    out = tmp_path / "ge.bk2"
    assert main(["edit", str(g), str(out), "--do", "hold 5-6 east"]) == 0
    ed = bk2.read_bk2(out)
    assert ed.saveram == ref.saveram
    assert ed.sync_settings == ref.sync_settings   # ConsoleMode, EnableBIOS, InitialTime
    assert _power(ed) == _power(ref)
    # an insert moves the Power of the mid-run reset along with its frame
    out2 = tmp_path / "gi.bk2"
    assert main(["edit", str(g), str(out2), "--do", "insert 3 2"]) == 0
    assert _power(bk2.read_bk2(out2)) == [0, _power(ref)[1] + 2]


def test_bk2_output_of_edit_keeps_saveram_resets_and_sync(tmp_path):
    from controllerlog.formats import bk2
    recs = ([(0, HARD_RESET)] + [(N, 0)] * 10 + [(0, HARD_RESET)] + [(N, int(B.A))] * 10)
    src = tmp_path / "a.gm2"
    gm2.write_gm2(src, Gm2Movie(Gm2Header(platform=gm2.Platform.GBC_GBA, rom_name="T",
                                          emu_version="0.6"), b"\x01" * 64, recs))
    bk = tmp_path / "a.bk2"
    assert main(["convert", str(src), str(bk), "--sha1", "ABCDEF"]) == 0
    a = bk2.read_bk2(bk)
    assert a.saveram and _power(a) == [0, 10]
    out = tmp_path / "b.bk2"                       # .bk2 -> edit -> .bk2
    assert main(["edit", str(bk), str(out), "--do", "hold 3 east"]) == 0
    b = bk2.read_bk2(out)
    assert b.saveram == a.saveram and _power(b) == _power(a)
    assert b.sync_settings == a.sync_settings and b.header.get("SHA1") == "ABCDEF"
    out2 = tmp_path / "c.bk2"                      # .gm2 -> edit -> .bk2
    assert main(["edit", str(src), str(out2), "--do", "hold 3 east"]) == 0
    c = bk2.read_bk2(out2)
    assert c.saveram == a.saveram and _power(c) == _power(a) and c.sync_settings == a.sync_settings


# --- .gm2 insert/delete keep resets with their frames ------------------------------------

def test_gm2_delete_moves_resets_with_inputs(tmp_path):
    recs = [(N, 0)] * 10 + [(0, HARD_RESET)] + [(N, int(B.A))] * 10
    src, out = tmp_path / "a.gm2", tmp_path / "b.gm2"
    gm2.write_gm2(src, Gm2Movie(Gm2Header(rom_name="T", emu_version="0.6"), b"", recs))
    assert main(["edit", str(src), str(out), "--do", "delete 2 3"]) == 0
    fr = gm2.read_gm2(out).frames()
    assert [f.index for f in fr if f.reset_before] == [7]      # the reset moves with the inputs after it
    assert all(f.buttons == 0 for f in fr[:7]) and all(f.buttons == B.A for f in fr[7:])


def test_gm2_delete_keeps_resets_attached_to_the_following_input(tmp_path):
    g = gbc_gm2(tmp_path / "g.gm2")
    assert [f for f in gm2.read_gm2(g).frames() if f.reset_before][-1].buttons == B.START
    out = tmp_path / "e.gm2"
    assert main(["edit", str(g), str(out), "--do", "delete 2 3"]) == 0
    after = [f for f in gm2.read_gm2(out).frames() if f.reset_before]
    assert after[-1].buttons == B.START   # START still lands on the first frame after the reset


# --- argument validation -------------------------------------------------------------------

def test_render_bad_fps_is_a_clean_error(tmp_path):
    ct = two_ctlog(tmp_path / "two.ctlog")
    with pytest.raises(SystemExit) as e:  # argparse error, not an ArgumentTypeError traceback
        main(["render", str(ct), str(tmp_path / "o.mp4"), "--fps", "abc"])
    assert e.value.code == 2


@pytest.mark.parametrize("fps", ["0", "1/0", "-60", "nan", "inf"])
def test_fps_must_be_finite_and_positive(tmp_path, fps):
    ct = two_ctlog(tmp_path / "two.ctlog")
    for cmd in (["stats", str(ct)], ["render", str(ct), str(tmp_path / "o.gif")]):
        with pytest.raises(SystemExit) as e:  # argparse error, not ZeroDivisionError / bogus output
            main(cmd + [f"--fps={fps}"])
        assert e.value.code == 2


@pytest.mark.parametrize("argv", [
    ["view", "--port", "70000"],
    ["live", "--port", "-1"],
    ["optimize", "--movie", "a.bk2", "--out", "b.bk2", "--start", "0", "--window", "10",
     "--objective", "max WRAM:0x10:1", "--port", "70000"],
])
def test_port_is_range_checked(argv):
    from controllerlog.cli import build_parser
    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)


def test_global_verbose_survives_switch2_subparser():
    from controllerlog.cli import build_parser
    assert build_parser().parse_args(["-v", "switch2", "scan"]).verbose
    assert build_parser().parse_args(["switch2", "-vv", "scan"]).switch2_verbose == 2


def test_stats_unknown_device_is_an_error(tmp_path, capsys):
    ct = two_ctlog(tmp_path / "two.ctlog")
    assert main(["stats", str(ct), "--device", "7"]) != 0
    assert "devices with input: 0, 1" in capsys.readouterr().err
    assert main(["stats", str(ct), "--device", "1"]) == 0


def test_stats_defaults_to_the_gm2_native_frame_rate(tmp_path, capsys):
    assert main(["stats", str(gbc_gm2(tmp_path / "g.gm2")), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["fps"] == pytest.approx(4194304 / 70224)


def test_devices_no_enhanced(monkeypatch):
    from controllerlog.input import sdl3_backend
    from controllerlog.output import virtual_pad
    seen = {}

    def fake_list_gamepads(hints=None, settle_s=1.0):
        seen["hints"] = hints
        return "3.x", []

    monkeypatch.setattr(sdl3_backend, "list_gamepads", fake_list_gamepads)
    monkeypatch.setattr(virtual_pad, "virtual_pad_unavailable_reason", lambda: None)
    assert main(["devices", "--no-enhanced", "--json", "--settle", "0"]) == 0
    assert seen["hints"] is not None and seen["hints"]["SDL_JOYSTICK_ENHANCED_REPORTS"] != "1"


def test_live_forwards_switch2_orientation_and_deadzone(monkeypatch):
    import threading
    from controllerlog.input import switch2_ble
    seen = {}

    class FakeBackend:
        name = "switch2"

        def __init__(self, hub, address=None, *a, **kw):
            seen.update(kw, address=address)
            self.ready = threading.Event()
            self.ready.set()
            self.error = "stop here"

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(switch2_ble, "Switch2BleBackend", FakeBackend)
    main(["live", "--no-sdl", "--no-overlay", "--quiet", "--switch2", "AA:BB:CC:DD:EE:FF",
          "--switch2-orientation", "vertical", "--switch2-deadzone", "0"])
    assert seen.get("orientation") == "vertical" and seen.get("deadzone") == 0
    assert seen["address"] == "AA:BB:CC:DD:EE:FF"


# --- files and output --------------------------------------------------------------------

def test_convert_ctlog_to_ctlog_gz_is_lossless(tmp_path):
    ct = two_ctlog(tmp_path / "two.ctlog")
    out = tmp_path / "copy.ctlog.gz"
    assert main(["convert", str(ct), str(out)]) == 0
    a, b = read_log(ct), read_log(out)
    assert {k: v.name for k, v in b.devices.items()} == {k: v.name for k, v in a.devices.items()}
    assert [m.value for m in b.markers()] == [m.value for m in a.markers()]
    key = lambda e: (e.t_ns, e.device, e.kind, e.code, e.value)
    assert [key(e) for e in b.input_events()] == [key(e) for e in a.input_events()]


def test_view_accepts_a_path_to_a_recording(tmp_path, monkeypatch):
    from pathlib import Path
    from controllerlog import cli
    from controllerlog.overlay import server as srv
    (tmp_path / "sub").mkdir()
    ct = two_ctlog(tmp_path / "sub" / "run.ctlog")
    seen = {}

    class FakeServer:
        def __init__(self, hub, host, port, recordings_dir):
            seen["dir"] = Path(recordings_dir)

        def start(self):
            pass

        def viewer_url(self, file=None, **kw):
            seen["file"] = file
            return "http://127.0.0.1/viewer"

        def stop(self):
            pass

    def stop_loop(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(srv, "OverlayServer", FakeServer)
    monkeypatch.setattr(cli.time, "sleep", stop_loop)
    monkeypatch.chdir(tmp_path)
    assert main(["view", str(ct), "--no-open"]) == 0
    # the viewer's ?file= must be something the server can resolve
    assert srv.recording_path(seen["dir"], seen["file"]) == ct.resolve()
    assert main(["view", "sub/run.ctlog", "--no-open"]) == 0 and seen["file"] == "run.ctlog"
    assert main(["view", "run.ctlog", "--dir", "sub", "--no-open"]) == 0
    assert main(["view", "sub/missing.ctlog", "--no-open"]) == 2
    assert main(["view", str(gbc_gm2(tmp_path / "g.gm2")), "--no-open"]) == 2   # needs convert
    (tmp_path / "afile").write_text("x")
    assert main(["view", "--dir", "afile", "--no-open"]) == 2


def test_non_ascii_output_on_a_cp1252_stdout(tmp_path, monkeypatch):
    import io
    import sys
    g = gbc_gm2(tmp_path / "jp.gm2", rom_name="ポケモン クリスタル")
    buf = io.BytesIO()
    out = io.TextIOWrapper(buf, encoding="cp1252", newline="\n")  # redirected stdout on Windows
    monkeypatch.setattr(sys, "stdout", out)
    rc = main(["gm2", "info", str(g)])
    out.flush()
    text = buf.getvalue().decode("cp1252")
    assert rc == 0 and "rom_name" in text and "duration_s" in text


def test_bom_edit_script_and_bom_csv(tmp_path):
    ct = two_ctlog(tmp_path / "two.ctlog")
    script = tmp_path / "e.txt"
    script.write_bytes(b"\xef\xbb\xbfhold 10-20 east\r\n")   # Excel / PowerShell 'utf8'
    assert main(["edit", str(ct), str(tmp_path / "o.ctlog"), "--script", str(script)]) == 0
    script.write_bytes("hold 10-20 east\r\n".encode("utf-16"))   # PowerShell 5.1 '>'
    assert main(["edit", str(ct), str(tmp_path / "o2.ctlog"), "--script", str(script)]) == 0
    csv = tmp_path / "m.csv"
    assert main(["convert", str(gbc_gm2(tmp_path / "g.gm2")), str(csv)]) == 0
    csv.write_bytes(b"\xef\xbb\xbf" + csv.read_bytes())
    assert main(["convert", str(csv), str(tmp_path / "o2.ctlog"), "--fps", "gb"]) == 0


def test_first_do_error_is_not_reported_as_line_2(tmp_path, capsys):
    ct = two_ctlog(tmp_path / "two.ctlog")
    assert main(["edit", str(ct), str(tmp_path / "o.ctlog"), "--do", "hold 10 bogus"]) != 0
    err = capsys.readouterr().err
    assert "line 1:" in err and "line 2" not in err


def test_folder_is_not_reported_as_locked_by_gse(tmp_path, capsys):
    d = tmp_path / "Input Log.gm2"
    d.mkdir()
    assert main(["gm2", "info", str(d)]) != 0
    err = capsys.readouterr().err
    assert "locked" not in err and "folder" in err
    assert main(["stats", str(d)]) != 0 and "folder" in capsys.readouterr().err
    (tmp_path / "syn").mkdir()
    assert main(["stats", str(tmp_path / "syn")]) != 0 and "folder" in capsys.readouterr().err


def test_align_json_with_out_and_out_suffix(tmp_path, capsys):
    g = gbc_gm2(tmp_path / "g.gm2")
    host = tmp_path / "host.ctlog"
    assert main(["convert", str(g), str(host)]) == 0
    capsys.readouterr()
    assert main(["align", str(host), str(g), "--json", "--out", str(tmp_path / "a.ctlog")]) == 0
    json.loads(capsys.readouterr().out)  # stdout must be only the JSON document
    assert (tmp_path / "a.ctlog").exists()
    assert main(["align", str(host), str(g), "--out", str(tmp_path / "al.bk2")]) != 0
    assert not (tmp_path / "al.bk2").exists()   # a .ctlog must not be written under a .bk2 name


def test_convert_truncated_gm2_to_bk2_warns(tmp_path, capsys):
    raw = gm2.dump_gm2(gm2.read_gm2(gbc_gm2(tmp_path / "g.gm2")), compress=False)
    t = tmp_path / "t.gm2"
    t.write_bytes(raw[:-3])          # cut mid-record, as after a GSE crash
    assert gm2.read_gm2(t).truncated
    assert main(["convert", str(t), str(tmp_path / "t.bk2")]) == 0
    assert "truncated" in capsys.readouterr().err
