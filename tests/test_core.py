from __future__ import annotations

import gzip
import json
import threading
import time

import pytest

from controllerlog.hub import Hub, Recorder
from controllerlog.layouts import LayoutError, parse_map, remap_digital
from controllerlog.logfile import LogFormatError, LogWriter, Recording, read_log
from controllerlog.model import (AXIS, BUTTON, BUTTON_INDEX, CONNECT, DISCONNECT,
                                 MARK, DeviceInfo, InputEvent, PadState,
                                 family_for_sdl_type)
from controllerlog.playback import Player
from controllerlog.timeline import FPS, Timeline, frames_to_events, peak_rate

MS = 1_000_000
S = 1_000_000_000
A = BUTTON_INDEX["south"]
B = BUTTON_INDEX["east"]


def make_rec(events: list[InputEvent]) -> Recording:
    rec = Recording(header={"format": "controllerlog", "version": 1, "meta": {}})
    rec.devices[0] = DeviceInfo(0, "Test pad", sdl_type="ps5", family="playstation")
    rec.events = [InputEvent(0, 0, CONNECT, None, rec.devices[0].to_json()), *events]
    rec.sort()
    return rec


def test_family_mapping():
    assert family_for_sdl_type("ps4") == "playstation"
    assert family_for_sdl_type("joyconpair") == "switch"
    assert family_for_sdl_type("xboxone") == "xbox"
    assert family_for_sdl_type("") == "generic"
    assert family_for_sdl_type("somethingnew") == "generic"


def test_padstate_apply_and_mask():
    st = PadState()
    assert st.apply(InputEvent(0, 0, BUTTON, A, 1))
    assert not st.apply(InputEvent(1, 0, BUTTON, A, 1))  # no change
    assert st.apply(InputEvent(2, 0, AXIS, 5, 20000))
    assert st.pressed("south") and st.pressed("right_trigger")
    assert not st.pressed("left_trigger")
    assert st.button_mask() == 1 << A
    assert not st.apply(InputEvent(3, 0, BUTTON, 999, 1))  # out of range ignored


def test_event_row_roundtrip():
    for ev in (InputEvent(5, 0, BUTTON, 3, 1), InputEvent(6, 1, AXIS, 0, -32768),
               InputEvent(7, 0, DISCONNECT), InputEvent(8, None, MARK, None, "split:1"),
               InputEvent(9, 2, CONNECT, None, {"id": 2, "name": "x"})):
        assert InputEvent.from_row(json.loads(json.dumps(ev.to_row()))) == ev


@pytest.mark.parametrize("suffix", [".ctlog", ".ctlog.gz"])
def test_log_write_read_roundtrip(tmp_path, suffix):
    path = tmp_path / f"run{suffix}"
    dev = DeviceInfo(0, "DualSense", sdl_type="ps5", family="playstation", vendor_id=0x54C)
    with LogWriter(path, devices=[dev], meta={"game": "Pokemon Red"}) as w:
        w.write(InputEvent(10 * MS, 0, BUTTON, A, 1))
        w.write(InputEvent(20 * MS, 0, AXIS, 1, -32768))
        w.mark(30 * MS, "split:Brock")
        w.write(InputEvent(40 * MS, 0, BUTTON, A, 0))
    rec = read_log(path)
    assert rec.meta["game"] == "Pokemon Red"
    assert rec.devices[0].name == "DualSense" and rec.devices[0].vendor_id == 0x54C
    assert [e.kind for e in rec.events] == [CONNECT, BUTTON, AXIS, MARK, BUTTON]
    assert rec.duration_ns == 40 * MS
    assert rec.markers()[0].value == "split:Brock"
    assert rec.primary_device() == 0
    if suffix.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            assert json.loads(fh.readline())["format"] == "controllerlog"


def test_reader_tolerates_truncated_last_line_only(tmp_path):
    path = tmp_path / "crash.ctlog"
    with LogWriter(path) as w:
        w.write(InputEvent(1, 0, BUTTON, A, 1))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('[2,0,"b",0')  # crash mid-write
    rec = read_log(path)
    assert len(rec.events) == 1
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('\n[3,0,"b",0,0]\n')  # corruption followed by more data
    with pytest.raises(LogFormatError):
        read_log(path)


def test_reader_rejects_axis_row_without_value(tmp_path):
    path = tmp_path / "bad.ctlog"
    with LogWriter(path) as w:
        w.write(InputEvent(1, 0, BUTTON, A, 1))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('[2,0,"a",1]\n[3,0,"b",0,0]\n')
    with pytest.raises(LogFormatError):
        read_log(path)
    assert not PadState().apply(InputEvent(0, 0, AXIS, 1, None))


def test_hub_reannounce_releases_held_inputs():
    hub = Hub()
    seen = []
    hub.add_sink(seen.append)
    d = hub.connect(("k", 1), DeviceInfo(-1, "pad"))
    hub.publish(InputEvent(1, d, BUTTON, A, 1))
    assert hub.connect(("k", 1), DeviceInfo(-1, "pad"), t_ns=5) == d
    kinds = [(e.kind, e.code, e.value) for e in seen]
    assert kinds[-2:] == [(BUTTON, A, 0), (CONNECT, None, kinds[-1][2])]


def test_hub_ignore_announces_disconnect_once():
    hub = Hub()
    seen = []
    hub.add_sink(seen.append)
    d = hub.connect(("k", 1), DeviceInfo(-1, "virtual pad"))
    hub.publish(InputEvent(1, d, AXIS, 0, 500))
    hub.ignore(d)
    hub.ignore(d)
    hub.publish(InputEvent(2, d, BUTTON, A, 1))
    hub.disconnect(d)
    assert [(e.kind, e.code, e.value) for e in seen[1:]] == [(AXIS, 0, 500), (AXIS, 0, 0),
                                                             (DISCONNECT, None, None)]
    assert d not in hub.snapshot()


def test_reader_rejects_foreign_files(tmp_path):
    p = tmp_path / "x.ctlog"
    p.write_text('{"format": "other"}\n', encoding="utf-8")
    with pytest.raises(LogFormatError):
        read_log(p)


def test_recording_save_roundtrip(tmp_path):
    rec = make_rec([InputEvent(5 * MS, 0, BUTTON, A, 1), InputEvent(9 * MS, 0, BUTTON, A, 0)])
    rec.meta["runner"] = "me"
    rec.save(tmp_path / "copy.ctlog")
    back = read_log(tmp_path / "copy.ctlog")
    assert back.meta["runner"] == "me"
    assert [e.to_row() for e in back.events] == [e.to_row() for e in rec.events]


def test_hub_dedup_ignore_and_disconnect_release():
    hub = Hub()
    seen: list[InputEvent] = []
    hub.add_sink(seen.append)
    d = hub.connect(("fake", 1), DeviceInfo(-1, "pad"))
    assert d == 0
    hub.publish(InputEvent(1, d, BUTTON, A, 1))
    hub.publish(InputEvent(2, d, BUTTON, A, 1))  # duplicate dropped
    hub.publish(InputEvent(3, d, AXIS, 0, 1000))
    hub.disconnect(d, t_ns=10)
    kinds = [(e.kind, e.code, e.value) for e in seen]
    assert kinds[0][0] == CONNECT
    assert (BUTTON, A, 1) in kinds and kinds.count((BUTTON, A, 1)) == 1
    # disconnect releases held inputs before the "-" event
    assert kinds[-3:] == [(BUTTON, A, 0), (AXIS, 0, 0), (DISCONNECT, None, None)]
    # reconnect reuses the id; ignore() silences it
    d2 = hub.connect(("fake", 1), DeviceInfo(-1, "pad"))
    assert d2 == d
    hub.ignore(d2)
    n = len(seen)
    hub.publish(InputEvent(20, d2, BUTTON, B, 1))
    assert len(seen) == n


def test_hub_broken_sink_does_not_break_others():
    hub = Hub()
    seen = []

    def bad(ev):
        raise RuntimeError("boom")

    hub.add_sink(bad)
    hub.add_sink(seen.append)
    d = hub.connect(("fake", 1), DeviceInfo(-1, "pad"))
    hub.publish(InputEvent(1, d, BUTTON, A, 1))
    assert len(seen) == 2


def test_recorder_is_self_contained(tmp_path):
    hub = Hub()
    d = hub.connect(("fake", 1), DeviceInfo(-1, "pad", family="xbox"))
    hub.publish(InputEvent(time.perf_counter_ns(), d, AXIS, 1, -20000))  # stick held before start
    rec_path = tmp_path / "r.ctlog"
    r = Recorder(hub, rec_path, meta={"game": "x"})
    hub.publish(InputEvent(time.perf_counter_ns(), d, BUTTON, A, 1))
    r.mark("split:1")
    r.close()
    hub.publish(InputEvent(time.perf_counter_ns(), d, BUTTON, A, 0))  # after close: not recorded
    rec = read_log(rec_path)
    tl = Timeline(rec)
    assert tl.state_at(d, 0).axes[1] == -20000
    assert rec.events[0].kind == CONNECT and rec.events[0].t_ns == 0
    assert sum(1 for e in rec.events if e.kind == BUTTON) == 1
    assert rec.markers()[0].value == "split:1"


def test_timeline_state_and_frames():
    fps = 60.0
    fr = S / fps
    # press A from 1.2 frames to 1.5 frames (a sub-frame tap), B held frames 3..5
    rec = make_rec([
        InputEvent(round(1.2 * fr), 0, BUTTON, A, 1), InputEvent(round(1.5 * fr), 0, BUTTON, A, 0),
        InputEvent(round(3.0 * fr), 0, BUTTON, B, 1), InputEvent(round(6.0 * fr), 0, BUTTON, B, 0),
        InputEvent(round(7.0 * fr), 0, AXIS, 0, 12345),
    ])
    tl = Timeline(rec)
    assert tl.state_at(0, round(1.3 * fr)).buttons[A] == 1
    sample = tl.frames(0, fps, mode="sample")
    anym = tl.frames(0, fps, mode="any")
    assert [f.buttons[A] for f in sample[:4]] == [0, 0, 0, 0]  # tap lost when sampling
    assert [f.buttons[A] for f in anym[:4]] == [0, 1, 0, 0]    # but kept in "any" mode
    assert [f.buttons[B] for f in sample[:8]] == [0, 0, 0, 1, 1, 1, 0, 0]
    assert sample[7].axes[0] == 12345
    # inverse transform reproduces the sampled frames
    back = Timeline(make_rec(frames_to_events(sample, fps)))
    assert [f.buttons for f in back.frames(0, fps, mode="sample", count=len(sample))] == \
           [f.buttons for f in sample]


def test_presses_and_stats():
    rec = make_rec([InputEvent(i * 100 * MS, 0, BUTTON, A, i % 2 == 0) for i in range(10)]
                   + [InputEvent(50 * MS, 0, AXIS, 4, 30000), InputEvent(80 * MS, 0, AXIS, 4, 0)])
    tl = Timeline(rec)
    ps = [p for p in tl.presses(0) if p.name == "south"]
    assert len(ps) == 5 and all(p.up_ns - p.down_ns == 100 * MS for p in ps)
    st = tl.stats(0, fps=60)
    assert st["south"]["presses"] == 5
    assert st["left_trigger"]["presses"] == 1
    assert st["south"]["hold_frames_mean"] == pytest.approx(6.0, abs=0.01)


def test_peak_rate():
    assert peak_rate([0, 100 * MS, 200 * MS, 5 * S]) == 3.0
    assert peak_rate([]) == 0


def test_fps_constants():
    assert FPS["gb"] == pytest.approx(59.7275, abs=1e-4)
    assert FPS["gba"] == pytest.approx(59.7275, abs=1e-4)
    assert FPS["nes"] == pytest.approx(60.0988, abs=1e-3)


def test_player_order_timing_and_stop():
    evs = [InputEvent(i * 5 * MS, 0, BUTTON, A, i % 2) for i in range(20)]
    got = []
    groups = []
    p = Player(evs, lambda e: got.append((time.perf_counter_ns(), e)),
               on_group_done=lambda: groups.append(1))
    rep = p.run()
    assert [e for _, e in got] == evs and len(groups) == 20
    assert rep.max_late_ms < 15, rep.summary()  # generous: the suite may run alongside heavy load
    t_total = (got[-1][0] - got[0][0]) / MS
    assert 90 <= t_total <= 125

    slow = Player([InputEvent(10 * S, 0, BUTTON, A, 1)], got.append)
    th = slow.run_in_thread()
    time.sleep(0.05)
    slow.stop()
    th.join(1.0)
    assert not th.is_alive()


def test_player_stops_even_when_behind_schedule():
    stop = threading.Event()
    got = []

    def slow_cb(ev):
        got.append(ev)
        if len(got) == 3:
            stop.set()
        time.sleep(0.01)  # every callback overruns the 1 ms spacing

    evs = [InputEvent(i * MS, 0, BUTTON, A, i % 2) for i in range(200)]
    Player(evs, slow_cb, stop_event=stop).run()
    assert len(got) == 3


def test_player_speed_and_window():
    evs = [InputEvent(i * 10 * MS, 0, BUTTON, A, i % 2) for i in range(10)]
    got = []
    p = Player(evs, lambda e: got.append(e), speed=2.0, start_ns=30 * MS, end_ns=80 * MS)
    t = time.perf_counter()
    p.run()
    assert [e.t_ns for e in got] == [30 * MS, 40 * MS, 50 * MS, 60 * MS, 70 * MS]
    assert time.perf_counter() - t < 0.06


def test_parse_map_and_remap():
    m = parse_map("south:east, east:south")
    assert m == {"south": "east", "east": "south"}
    out = remap_digital({"south": True, "east": False, "start": True}, m)
    assert out == {"south": False, "east": True, "start": True}
    with pytest.raises(LayoutError):
        parse_map("south")
    with pytest.raises(LayoutError):
        parse_map("south:banana")


def test_hub_thread_safety():
    hub = Hub()
    seen = []
    hub.add_sink(seen.append)
    ids = [hub.connect(("fake", i), DeviceInfo(-1, f"p{i}")) for i in range(4)]

    def spam(d):
        for i in range(2000):
            hub.publish(InputEvent(i, d, AXIS, 0, i + 1))

    ths = [threading.Thread(target=spam, args=(d,)) for d in ids]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    assert sum(1 for e in seen if e.kind == AXIS) == 8000


# --- crash safety of the writer / reader -------------------------------------------------

def test_truncated_gz_recording_is_readable(tmp_path):
    import shutil
    p = tmp_path / "crash.ctlog.gz"
    w = LogWriter(p)
    try:
        for i in range(2000):
            w.write(InputEvent(i * 1000, 0, BUTTON, 0, i % 2))
        w.flush()                            # what the periodic flush leaves on disk
        crashed = tmp_path / "copy.ctlog.gz"
        shutil.copyfile(p, crashed)          # stream flushed but never closed (crash)
    finally:
        w.close()
    rec = read_log(crashed)
    assert len(rec.events) == 2000
    head_only = tmp_path / "head.ctlog.gz"
    head_only.write_bytes(p.read_bytes()[:12])   # cut inside the gzip header/first block
    with pytest.raises(LogFormatError):
        read_log(head_only)


def _rows_on_disk(path) -> int:
    with open(path, "rb") as fh:            # independent handle, like a post-crash reader
        return fh.read().count(b"\n") - 1   # minus the header line


def test_logwriter_tail_reaches_disk_while_idle(tmp_path):
    p = tmp_path / "idle.ctlog"
    w = LogWriter(p, flush_interval_s=0.2)
    try:
        for i in range(10):                 # a burst (e.g. the final inputs of a run) ...
            w.write(InputEvent(i * 1000, 0, BUTTON, 0, i % 2))
        w.mark(20000, "split:1")
        deadline = time.monotonic() + 1.0   # ... then nothing for 5 flush intervals
        while _rows_on_disk(p) < 11 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert _rows_on_disk(p) == 11, "tail still only in the process's buffer"
    finally:
        w.close()


def test_logwriter_flushes_while_idle(tmp_path):
    p = tmp_path / "a.ctlog"
    w = LogWriter(p, flush_interval_s=0.25)
    try:
        w.write(InputEvent(1, 0, "b", 0, 1))
        time.sleep(0.6)                    # no further events: docs promise <= 250 ms of loss
        assert len(p.read_text(encoding="utf-8").splitlines()) == 2
    finally:
        w.close()


# --- Recorder start is atomic with respect to backend threads ---------------------------

def test_recorder_keeps_event_published_while_snapshot_rows_are_written(tmp_path, monkeypatch):
    import controllerlog.hub as hubmod
    hub = Hub()
    dev = hub.connect(("sdl3", 1), DeviceInfo(-1, "Pad", sdl_type="xbox360"))
    hub.publish(InputEvent(time.perf_counter_ns(), dev, BUTTON, 0, 1))   # A held at start
    threads = []

    class PreemptedWriter(LogWriter):
        fired = False

        def write(self, ev):
            # Stand-in for a GIL switch while Recorder writes its t=0 snapshot rows.
            if not PreemptedWriter.fired:
                PreemptedWriter.fired = True
                th = threading.Thread(target=hub.publish,
                                      args=(InputEvent(time.perf_counter_ns(), dev, BUTTON, 0, 0),))
                th.start()
                th.join(0.2)       # blocks only if the hub lock is held (it is, after the fix)
                threads.append(th)
            super().write(ev)

    monkeypatch.setattr(hubmod, "LogWriter", PreemptedWriter)
    rec = Recorder(hub, tmp_path / "r.ctlog")
    for th in threads:
        th.join(2)
    rec.close()
    assert hub.states[dev].buttons[0] == 0
    a = None
    for ev in read_log(tmp_path / "r.ctlog").events:
        if ev.kind == BUTTON and ev.device == dev and ev.code == 0:
            a = ev.value
    assert a == 0, "recording still shows A held although it was released"


def test_recorder_does_not_lose_event_published_during_start(tmp_path):
    hub = Hub()
    dev = hub.connect(("t", 0), DeviceInfo(0, "pad"))
    east = BUTTON_INDEX["east"]
    real_snapshot = hub.snapshot
    threads = []

    def racing_snapshot():
        snap = real_snapshot()
        # A backend thread publishes right after the snapshot was taken.
        th = threading.Thread(target=hub.publish, args=(InputEvent(10**18, dev, BUTTON, east, 1),))
        th.start()
        th.join(0.2)
        threads.append(th)
        return snap

    hub.snapshot = racing_snapshot
    rec = Recorder(hub, tmp_path / "r.ctlog")
    for th in threads:
        th.join(2)
    hub.publish(InputEvent(10**18 + 5, dev, BUTTON, east, 0))
    rec.close()
    rows = [e.value for e in read_log(tmp_path / "r.ctlog").events
            if e.kind == BUTTON and e.code == east]
    assert rows == [1, 0]


def test_reader_rejects_giant_lines(tmp_path, monkeypatch):
    import controllerlog.logfile as logfile
    p = tmp_path / "big.ctlog.gz"
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({"format": "controllerlog", "version": 1}) + "\n")
        fh.write('[0, null, "m", null, "' + "x" * 5000 + '"]\n')   # one huge row
    monkeypatch.setattr(logfile, "MAX_LINE_CHARS", 4096)
    with pytest.raises(LogFormatError):
        read_log(p)
    monkeypatch.setattr(logfile, "MAX_LINE_CHARS", 1 << 20)
    assert read_log(p).markers()[0].value == "x" * 5000
