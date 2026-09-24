"""Recordings written by the GC Bridge Android app read like PC recordings.

``tests/fixtures/android/gcbridge_sample.ctlog`` was produced by the app's CtlogWriter
(android/gcbridge, JVM harness); regenerate it if the writer's output format changes.
"""
from __future__ import annotations

from pathlib import Path

from controllerlog.logfile import read_log
from controllerlog.model import AXIS, BUTTON, BUTTON_INDEX, CONNECT, DISCONNECT, MARK, PadState
from controllerlog.timeline import Timeline

SAMPLE = Path(__file__).parent / "fixtures" / "android" / "gcbridge_sample.ctlog"


def test_sample_reads_like_a_pc_recording():
    rec = read_log(SAMPLE)
    assert rec.header["format"] == "controllerlog" and rec.header["version"] == 1
    assert rec.header["source"] == "gcbridge"
    assert rec.header["time_unit"] == "ns"
    assert rec.header["meta"] == {"button_capture": True}
    assert set(rec.devices) == {0, 1}
    gc = rec.devices[0]
    assert gc.family == "gamecube" and gc.backend == "gcbridge-usb"
    assert (gc.vendor_id, gc.product_id) == (0x057E, 0x2073)
    assert gc.extra["reader"] == "gcbridge" and gc.extra["report"] == "0x05"
    assert rec.devices[1].family == "playstation" and rec.devices[1].backend == "android"
    kinds = {ev.kind for ev in rec.events}
    assert kinds == {CONNECT, BUTTON, AXIS, MARK, DISCONNECT}
    assert rec.primary_device() == 0
    assert [m.value for m in rec.markers()] == ["split:1"]
    # timestamps are monotonic nanoseconds from the start
    ts = [ev.t_ns for ev in rec.events]
    assert ts == sorted(ts) and ts[0] == 0
    st = PadState()
    for ev in rec.input_events(0):
        st.apply(ev)
    assert st.buttons[BUTTON_INDEX["south"]] == 0          # released before the disconnect
    assert st.axes[0] == 12000 + 19 * 500


def test_sample_feeds_the_analysis_tools():
    rec = read_log(SAMPLE)
    stats = Timeline(rec).stats(0, fps=60.0)
    assert stats["south"]["presses"] == 20
    assert stats["left_trigger"]["presses"] == 10
