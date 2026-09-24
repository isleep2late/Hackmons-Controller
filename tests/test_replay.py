"""Tests for virtual controller output, replay and the bridge.

Unit tests use a fake ``vgamepad`` module; ``@pytest.mark.vigem`` tests use the
real ViGEmBus driver and read the virtual pads back through SDL3 / XInput.
"""

from __future__ import annotations

import ctypes
import gc
import sys
import threading
import time
import types
import weakref

import pytest

from controllerlog.bridge import Bridge, apply_deadzone
from controllerlog.hub import Hub, now_ns
from controllerlog.logfile import Recording
from controllerlog.model import (AXIS, AXIS_INDEX, BUTTON, BUTTON_INDEX, BUTTONS, CONNECT,
                                 MARK, DeviceInfo, InputEvent, PadState)
from controllerlog.output import (NINTENDO_LABEL_REMAP, TARGETS, VirtualPad, VirtualPadError,
                                  VirtualPadUnavailable, create_virtual_pad, remap_state,
                                  virtual_pad_unavailable_reason)
from controllerlog.output.virtual_pad import (ds4_hat, ds4_report, stick_to_u8,
                                              stick_to_xinput, trigger_to_u8, x360_report)
from controllerlog.replay import ButtonTrigger, find_marker, replay_frames, replay_recording, wait_for_button

MS = 1_000_000


def mkstate(*buttons: str, **axes: int) -> PadState:
    st = PadState()
    for b in buttons:
        st.buttons[BUTTON_INDEX[b]] = 1
    for a, v in axes.items():
        st.axes[AXIS_INDEX[a]] = v
    return st


def wait_until(pred, timeout: float = 2.0, interval: float = 0.002) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


# --- fake vgamepad ----------------------------------------------------------------

X360_NEUTRAL = {"buttons": 0, "lt": 0, "rt": 0, "lx": 0, "ly": 0, "rx": 0, "ry": 0}
DS4_NEUTRAL = {"buttons": 0x8, "special": 0, "lt": 0, "rt": 0,
               "lx": 128, "ly": 128, "rx": 128, "ry": 128}


class PadLog:
    """Reports a fake pad sent; outlives the pad so tests can inspect it after close()."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, dict]] = []

    @property
    def last(self) -> dict:
        return self.sent[-1][1]


class _FakePad:
    registry: "FakeVG"

    def __init__(self) -> None:
        self.report = self.default()
        self.log = PadLog()
        self.registry.logs.append(self.log)
        self.registry.refs.append(weakref.ref(self))

    def press_button(self, b: int) -> None:
        self.report["buttons"] |= int(b)

    def release_button(self, b: int) -> None:
        self.report["buttons"] &= ~int(b) & 0xFFFF

    def left_trigger(self, v: int) -> None:
        assert 0 <= v <= 255
        self.report["lt"] = v

    def right_trigger(self, v: int) -> None:
        assert 0 <= v <= 255
        self.report["rt"] = v

    def left_joystick(self, x: int, y: int) -> None:
        self.report["lx"], self.report["ly"] = x, y

    def right_joystick(self, x: int, y: int) -> None:
        self.report["rx"], self.report["ry"] = x, y

    def update(self) -> None:
        if self.registry.fail_updates:
            raise Exception("VIGEM_ERROR_TARGET_NOT_PLUGGED_IN")
        self.log.sent.append((time.perf_counter_ns(), dict(self.report)))

    def __del__(self) -> None:
        self.registry.deleted += 1


class FakeX360(_FakePad):
    def default(self) -> dict:
        return dict(X360_NEUTRAL)


class FakeDS4(_FakePad):
    def default(self) -> dict:
        return dict(DS4_NEUTRAL)

    def press_special_button(self, b: int) -> None:
        self.report["special"] |= int(b)

    def release_special_button(self, b: int) -> None:
        self.report["special"] &= ~int(b) & 0xFF

    def directional_pad(self, d: int) -> None:
        assert 0 <= d <= 8
        self.report["buttons"] = (self.report["buttons"] & ~0xF) | int(d)

    def left_joystick(self, x: int, y: int) -> None:
        assert 0 <= x <= 255 and 0 <= y <= 255
        super().left_joystick(x, y)

    def right_joystick(self, x: int, y: int) -> None:
        assert 0 <= x <= 255 and 0 <= y <= 255
        super().right_joystick(x, y)


class FakeVG:
    def __init__(self) -> None:
        self.logs: list[PadLog] = []
        self.refs: list[weakref.ref] = []      # weak: the VirtualPad must own the device
        self.deleted = 0
        self.fail_updates = False
        reg = self
        self.module = types.ModuleType("vgamepad")
        self.module.VX360Gamepad = type("VX360Gamepad", (FakeX360,), {"registry": reg})
        self.module.VDS4Gamepad = type("VDS4Gamepad", (FakeDS4,), {"registry": reg})

    @property
    def created(self) -> list[PadLog]:
        return self.logs

    @property
    def pad(self) -> PadLog:
        """Report log of the most recently created fake device."""
        return self.logs[-1]


@pytest.fixture
def fakevg(monkeypatch):
    vg = FakeVG()
    monkeypatch.setitem(sys.modules, "vgamepad", vg.module)
    monkeypatch.setattr(VirtualPad, "RELEASE_DELAY_S", 0.0)
    monkeypatch.setattr("controllerlog.output.virtual_pad.X360_INVERT_Y", True)  # XInput convention
    return vg


# --- mapping ------------------------------------------------------------------------

X360_EXPECTED = {
    "south": 0x1000, "east": 0x2000, "west": 0x4000, "north": 0x8000,
    "back": 0x0020, "start": 0x0010, "guide": 0x0400,
    "left_stick": 0x0040, "right_stick": 0x0080,
    "left_shoulder": 0x0100, "right_shoulder": 0x0200,
    "dpad_up": 0x0001, "dpad_down": 0x0002, "dpad_left": 0x0004, "dpad_right": 0x0008,
}
DS4_EXPECTED = {
    "south": 1 << 5, "east": 1 << 6, "west": 1 << 4, "north": 1 << 7,
    "back": 1 << 12, "start": 1 << 13, "left_stick": 1 << 14, "right_stick": 1 << 15,
    "left_shoulder": 1 << 8, "right_shoulder": 1 << 9,
}


@pytest.mark.parametrize("name", BUTTONS)
def test_x360_button_mapping(fakevg, name):
    with create_virtual_pad("x360") as pad:
        pad.apply(mkstate(name))
        assert fakevg.pad.last["buttons"] == X360_EXPECTED.get(name, 0)


def test_x360_constants_match_vgamepad_enums():
    try:
        from vgamepad.win.vigem_commons import DS4_BUTTONS, XUSB_BUTTON  # noqa: PLC0415
    except Exception:
        pytest.skip("vgamepad not importable")
    assert X360_EXPECTED["south"] == XUSB_BUTTON.XUSB_GAMEPAD_A
    assert X360_EXPECTED["guide"] == XUSB_BUTTON.XUSB_GAMEPAD_GUIDE
    assert DS4_EXPECTED["west"] == DS4_BUTTONS.DS4_BUTTON_SQUARE
    assert DS4_EXPECTED["back"] == DS4_BUTTONS.DS4_BUTTON_SHARE


def test_x360_axes_triggers_and_y_inversion(fakevg):
    with create_virtual_pad("xbox360") as pad:
        pad.apply(mkstate(left_x=-32768, left_y=-32768, right_x=32767, right_y=32767,
                          left_trigger=32767, right_trigger=16384))
        r = fakevg.pad.last
        assert (r["lx"], r["ly"], r["rx"], r["ry"]) == (-32768, 32767, 32767, -32767)
        assert (r["lt"], r["rt"]) == (255, 128)
        pad.apply(mkstate(left_y=1000, right_y=-1, left_trigger=128))
        r = fakevg.pad.last
        assert (r["ly"], r["ry"], r["lt"], r["rt"]) == (-1000, 1, 1, 0)
    assert [trigger_to_u8(v) for v in (0, 64, 65, 16383, 32767, -5, 40000)] == [0, 0, 1, 127, 255, 0, 255]
    assert stick_to_xinput(-32768, invert=True) == 32767
    assert stick_to_xinput(32767, invert=True) == -32767
    assert stick_to_xinput(0, invert=True) == 0


@pytest.mark.parametrize("name", BUTTONS)
def test_ds4_button_mapping(fakevg, name):
    with create_virtual_pad("ds4") as pad:
        pad.apply(mkstate(name))
        r = fakevg.pad.last
        hat = {"dpad_up": 0, "dpad_down": 4, "dpad_left": 6, "dpad_right": 2}.get(name, 8)
        assert r["buttons"] & 0xF == hat
        assert r["buttons"] & ~0xF == DS4_EXPECTED.get(name, 0)
        assert r["special"] == {"guide": 1, "touchpad": 2}.get(name, 0)


def test_ds4_axes_and_trigger_buttons(fakevg):
    with create_virtual_pad("ps4") as pad:
        pad.apply(PadState())
        assert fakevg.pad.last == {"buttons": 8, "special": 0, "lt": 0, "rt": 0,
                                   "lx": 128, "ly": 128, "rx": 128, "ry": 128}
        pad.apply(mkstate(left_x=-32768, left_y=-32768, right_x=32767, right_y=32767,
                          left_trigger=16384, right_trigger=16383))
        r = fakevg.pad.last
        # DS4 Y grows downwards like the canonical model: no inversion.
        assert (r["lx"], r["ly"], r["rx"], r["ry"]) == (0, 0, 255, 255)
        assert (r["lt"], r["rt"]) == (128, 127)
        assert r["buttons"] & (1 << 10) and not r["buttons"] & (1 << 11)
    assert [stick_to_u8(v) for v in (0, -32768, 32767, 128, -129, 257 * 64 - 32768)] == [128, 0, 255, 128, 127, 64]


@pytest.mark.parametrize("dirs,hat", [
    ((), 8), (("up",), 0), (("up", "right"), 1), (("right",), 2), (("down", "right"), 3),
    (("down",), 4), (("down", "left"), 5), (("left",), 6), (("up", "left"), 7),
    (("up", "down"), 8), (("left", "right"), 8), (("up", "down", "left"), 6),
    (("up", "left", "right"), 0), (("up", "down", "left", "right"), 8),
])
def test_ds4_dpad_combination(fakevg, dirs, hat):
    assert ds4_hat("up" in dirs, "down" in dirs, "left" in dirs, "right" in dirs) == hat
    with create_virtual_pad("ds4") as pad:
        pad.apply(mkstate(*(f"dpad_{d}" for d in dirs)))
        assert fakevg.pad.last["buttons"] == hat


def test_x360_opposite_dpad_passes_through():
    assert x360_report(mkstate("dpad_up", "dpad_down"))[0] == 0x0003
    assert ds4_report(mkstate("south", "dpad_up", "dpad_down"))[:3] == (1 << 5, 0, 8)


def test_remap_state():
    st = mkstate("south", "misc1", left_trigger=20000, right_trigger=5000)
    out = remap_state(st, {"south": "east", "east": "south", "misc1": "guide",
                           "left_trigger": "left_shoulder"})
    assert out.pressed("east") and not out.pressed("south") and out.pressed("guide")
    assert out.pressed("left_shoulder") and out.axes[AXIS_INDEX["left_trigger"]] == 0
    assert out.axes[AXIS_INDEX["right_trigger"]] == 5000
    out = remap_state(mkstate("west"), {"west": "right_trigger", "right_trigger": "west"})
    assert out.axes[AXIS_INDEX["right_trigger"]] == 32767 and not out.pressed("west")
    assert remap_state(mkstate("south"), NINTENDO_LABEL_REMAP).pressed("east")
    assert remap_state(st, {}) is st
    with pytest.raises(ValueError):
        VirtualPad("x360", remap={"south": "jump"})


def test_virtual_pad_remap_paddles(fakevg):
    with create_virtual_pad("x360", remap={"left_paddle1": "south"}) as pad:
        pad.apply(mkstate("left_paddle1"))
        assert fakevg.pad.last["buttons"] == 0x1000


def test_incremental_updates_skip_and_force(fakevg):
    with create_virtual_pad("x360") as pad:
        n0 = len(fakevg.pad.sent)
        pad.set_button("south", True)
        pad.set_button(BUTTON_INDEX["east"], 1)
        pad.set_axis("left_y", -32768)
        pad.set_axis(AXIS_INDEX["right_trigger"], 32767)
        assert pad.update() is True
        assert fakevg.pad.last == {"buttons": 0x3000, "lt": 0, "rt": 255, "lx": 0,
                                   "ly": 32767, "rx": 0, "ry": 0}
        assert pad.update() is False                 # identical report skipped
        pad.set_button("misc1", True)                # unmapped: same report
        assert pad.update() is False
        assert pad.update(force=True) is True
        assert len(fakevg.pad.sent) == n0 + 2
        assert pad.state.pressed("misc1")
        pad.set_button("south", False)
        pad.update()
        assert fakevg.pad.last["buttons"] == 0x2000
        with pytest.raises(ValueError):
            pad.set_button(99, True)
        with pytest.raises(KeyError):
            pad.set_axis("nope", 1)


def test_close_releases_and_unplugs(fakevg):
    pad = create_virtual_pad("ds4")
    pad.apply(mkstate("south", "dpad_up", left_x=30000, right_trigger=32767))
    assert fakevg.refs[-1]() is not None
    pad.close()
    assert fakevg.refs[-1]() is None and fakevg.deleted == 1     # unplugged immediately
    assert fakevg.pad.last == DS4_NEUTRAL                         # released first
    pad.close()                                                   # idempotent
    assert pad.closed and fakevg.deleted == 1
    with pytest.raises(VirtualPadError):
        pad.update()


def test_close_sends_neutral_report(fakevg):
    pad = create_virtual_pad("x360")
    pad.apply(mkstate("south", left_y=-20000, left_trigger=32767))
    pad.close()
    assert fakevg.pad.last == X360_NEUTRAL


def test_update_error_is_wrapped(fakevg):
    with create_virtual_pad("x360") as pad:
        fakevg.fail_updates = True
        with pytest.raises(VirtualPadError, match="NOT_PLUGGED_IN"):
            pad.apply(mkstate("south"))
        fakevg.fail_updates = False


def test_unavailable_not_installed(monkeypatch):
    monkeypatch.setitem(sys.modules, "vgamepad", None)
    with pytest.raises(VirtualPadUnavailable, match="pip install vgamepad"):
        create_virtual_pad()
    assert "pip install vgamepad" in virtual_pad_unavailable_reason()


def test_unavailable_driver_missing_at_import(monkeypatch, tmp_path):
    pkg = tmp_path / "vgamepad"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("raise Exception('VIGEM_ERROR_BUS_NOT_FOUND')\n")
    monkeypatch.delitem(sys.modules, "vgamepad", raising=False)
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(VirtualPadUnavailable) as ei:
        create_virtual_pad("x360")
    msg = str(ei.value)
    assert "VIGEM_ERROR_BUS_NOT_FOUND" in msg
    if sys.platform == "win32":
        assert "github.com/nefarius/ViGEmBus/releases" in msg


def test_unavailable_on_connect(monkeypatch):
    mod = types.ModuleType("vgamepad")

    def boom():
        raise AssertionError("The virtual device could not connect to ViGEmBus.")
    mod.VX360Gamepad = boom
    mod.VDS4Gamepad = boom
    monkeypatch.setitem(sys.modules, "vgamepad", mod)
    with pytest.raises(VirtualPadUnavailable, match="could not connect"):
        create_virtual_pad("ds4")
    with pytest.raises(ValueError):
        create_virtual_pad("gamecube")


# --- replay -------------------------------------------------------------------------

def make_recording(rows, markers=()) -> Recording:
    rec = Recording(header={"format": "controllerlog", "version": 1})
    rec.devices[0] = DeviceInfo(0, "Test pad")
    rec.events.append(InputEvent(0, 0, CONNECT, None, rec.devices[0].to_json()))
    for t_ms, kind, name, value in rows:
        code = BUTTON_INDEX[name] if kind == BUTTON else AXIS_INDEX[name]
        rec.events.append(InputEvent(int(t_ms * MS), 0, kind, code, value))
    for t_ms, label in markers:
        rec.events.append(InputEvent(int(t_ms * MS), None, MARK, None, label))
    rec.sort()
    return rec


def reports_after(dev: PadLog, t0: int) -> list[tuple[float, dict]]:
    return [((t - t0) / MS, r) for t, r in dev.sent if t >= t0]


def test_replay_order_timing_and_release(fakevg):
    rec = make_recording([
        (0, BUTTON, "start", 1),                       # part of the initial state
        (20, BUTTON, "south", 1), (20, AXIS, "left_x", 20000),
        (40, BUTTON, "south", 0), (40, BUTTON, "start", 0),
        (60, AXIS, "left_trigger", 32767),
        (80, AXIS, "left_trigger", 0), (80, AXIS, "left_x", 0),
    ])
    status: list[str] = []
    rep = replay_recording(rec, countdown_s=0.1, settle_s=0, on_status=status.append)
    dev = fakevg.pad
    assert rep.aborted is None and rep.scheduled == 7 and rep.count == 4
    # Initial state (Start held) was applied before the countdown.
    before = [r for t, r in dev.sent if t < rep.t0_ns]
    assert before[0]["buttons"] == 0x0010
    after = reports_after(dev, rep.t0_ns)
    expect = [(20, 0x1010, 0, 20000), (40, 0, 0, 20000), (60, 0, 255, 20000), (80, 0, 0, 0)]
    assert [(r["buttons"], r["lt"], r["lx"]) for _, r in after[:4]] == [e[1:] for e in expect]
    for (t, _), (te, *_rest) in zip(after, expect):
        assert te <= t < te + 5, (t, te)
    assert after[-1][1] == X360_NEUTRAL                  # released at the end
    assert fakevg.deleted == 1
    assert rep.max_late_ms < 5
    assert any(s.startswith("starting in") for s in status) and status[-1].startswith("done")


def test_replay_start_marker_window_and_speed(fakevg):
    rec = make_recording([
        (10, BUTTON, "west", 1),
        (100, BUTTON, "north", 1), (140, BUTTON, "north", 0),
        (180, BUTTON, "west", 0), (300, BUTTON, "east", 1),
    ], markers=[(50, "reset"), (90, "split:Level 2"), (95, "split:Level 3")])
    assert find_marker(rec, "split:Level").value == "split:Level 2"
    with pytest.raises(ValueError, match="markers"):
        find_marker(rec, "nope")
    rep = replay_recording(rec, start_marker="split:Level", end_ns=200 * MS,
                           speed=2.0, countdown_s=0.05, settle_s=0)
    dev = fakevg.pad
    assert rep.start_ns == 90 * MS and rep.scheduled == 3
    assert [r for t, r in dev.sent if t < rep.t0_ns][0]["buttons"] == 0x4000   # West held
    after = reports_after(dev, rep.t0_ns)
    got = [(round(t), r["buttons"]) for t, r in after]
    # (100-90)/2 = 5 ms, (140-90)/2 = 25 ms, (180-90)/2 = 45 ms, release at (200-90)/2 = 55 ms
    assert [b for _, b in got] == [0xC000, 0x4000, 0, 0]
    for (t, _), te in zip(got, (5, 25, 45, 55)):
        assert te <= t <= te + 5
    assert all(r["buttons"] != 0x2000 for _, r in after)          # East is past end_ns


def test_replay_stop_event_aborts_promptly(fakevg):
    rec = make_recording([(i * 1000, BUTTON, "south", i % 2) for i in range(1, 20)])
    stop = threading.Event()
    out: list = []
    th = threading.Thread(target=lambda: out.append(
        replay_recording(rec, countdown_s=0.0, settle_s=0, stop_event=stop)))
    th.start()
    assert wait_until(lambda: fakevg.created and len(fakevg.pad.sent) >= 3, 3.0)
    t = time.perf_counter()
    stop.set()
    th.join(2.0)
    assert not th.is_alive() and time.perf_counter() - t < 0.2
    rep = out[0]
    assert rep.aborted == "stopped" and "aborted" in rep.summary()
    assert fakevg.created[0].sent[-1][1]["buttons"] == 0 and fakevg.deleted == 1


def test_replay_stop_during_countdown(fakevg):
    stop = threading.Event()
    threading.Timer(0.1, stop.set).start()
    t = time.perf_counter()
    rep = replay_recording(make_recording([(10, BUTTON, "south", 1)]), countdown_s=5.0, settle_s=0,
                           stop_event=stop)
    assert rep.aborted == "stopped" and time.perf_counter() - t < 0.5
    assert fakevg.deleted == 1


def test_replay_keyboard_interrupt_releases(fakevg):
    def status(msg: str) -> None:
        if msg.startswith("replaying"):
            raise KeyboardInterrupt
    rec = make_recording([(0, BUTTON, "south", 1), (500, BUTTON, "south", 0)])
    rep = replay_recording(rec, countdown_s=0.0, settle_s=0, on_status=status)
    assert rep.aborted == "interrupted"
    assert fakevg.created[0].sent[-1][1]["buttons"] == 0 and fakevg.deleted == 1


def test_replay_settles_before_applying_initial_state(fakevg):
    rec = make_recording([(0, BUTTON, "west", 1), (10, BUTTON, "west", 0)])
    status: list[str] = []
    t = now_ns()
    rep = replay_recording(rec, countdown_s=0.05, settle_s=0.15, on_status=status.append)
    log = fakevg.pad
    first_west = next(ts for ts, r in log.sent if r["buttons"] == 0x4000)
    assert first_west - t >= 150 * MS                     # held input applied after settling
    assert rep.t0_ns - first_west >= 45 * MS              # ...and before the countdown ends
    assert any("detected" in s for s in status)
    stop = threading.Event()
    threading.Timer(0.05, stop.set).start()
    rep = replay_recording(rec, countdown_s=0, settle_s=5, stop_event=stop)
    assert rep.aborted == "stopped" and fakevg.deleted == 2


def test_replay_reuses_given_pad_without_closing(fakevg):
    rec = make_recording([(0, BUTTON, "south", 1), (10, BUTTON, "south", 0), (20, BUTTON, "east", 1)])
    with create_virtual_pad("x360") as pad:
        rep = replay_recording(rec, countdown_s=0.0, pad=pad)
        assert rep.aborted is None and not pad.closed and fakevg.deleted == 0
        assert fakevg.pad.last["buttons"] == 0
    with pytest.raises(ValueError):
        replay_recording(Recording(), countdown_s=0)


def test_replay_frames_timing(fakevg):
    fps = 100.0
    frames = [mkstate(), mkstate("south"), mkstate("south"), mkstate("south", "dpad_right"),
              mkstate(), mkstate(), mkstate("east")]
    rep = replay_frames(frames, fps, target="ds4", countdown_s=0.05, settle_s=0)
    assert rep.aborted is None and rep.count == 4 and rep.end_ns == 70 * MS
    after = reports_after(fakevg.pad, rep.t0_ns)
    seq = [(round(t), r["buttons"]) for t, r in after]
    assert [b for _, b in seq] == [0x28, 0x22, 0x08, 0x48, 0x08]   # hat bits: 8 = none, 2 = east
    for (t, _), te in zip(seq, (10, 30, 40, 60, 70)):
        assert te <= t <= te + 5, seq
    with pytest.raises(ValueError):
        replay_frames(frames, 0)


def test_wait_for_button_and_trigger_start(fakevg):
    hub = Hub()
    dev = hub.connect(("test", 1), DeviceInfo(-1, "Physical"))
    other = hub.connect(("test", 2), DeviceInfo(-1, "Other"))
    press_t: list[int] = []

    def press_later() -> None:
        time.sleep(0.1)
        hub.publish(InputEvent(now_ns(), other, BUTTON, BUTTON_INDEX["start"], 1))   # wrong pad
        hub.publish(InputEvent(now_ns(), dev, BUTTON, BUTTON_INDEX["back"], 1))      # wrong button
        t = now_ns()
        press_t.append(t)
        hub.publish(InputEvent(t, dev, BUTTON, BUTTON_INDEX["start"], 1))
    threading.Thread(target=press_later).start()
    rec = make_recording([(0, BUTTON, "north", 1), (30, BUTTON, "north", 0)])
    rep = replay_recording(rec, countdown_s=0.02, settle_s=0,
                           wait_for_trigger=ButtonTrigger(hub, dev, "start"))
    assert rep.aborted is None
    assert rep.t0_ns == press_t[0] + 20 * MS
    after = reports_after(fakevg.pad, rep.t0_ns)
    assert after and 30 <= after[0][0] < 35 and after[0][1]["buttons"] == 0
    assert not hub._sinks                                        # trigger sink removed
    # Timeout and release-edge variants.
    rep = replay_recording(rec, countdown_s=0, settle_s=0,
                           wait_for_trigger=ButtonTrigger(hub, dev, "south", timeout_s=0.05))
    assert rep.aborted == "trigger timeout"
    th = threading.Thread(target=lambda: (time.sleep(0.05),
                                          hub.publish(InputEvent(now_ns(), dev, AXIS, 4, 30000)),
                                          hub.publish(InputEvent(now_ns(), dev, AXIS, 4, 100))))
    th.start()
    t = wait_for_button(hub, dev, "left_trigger", on_release=True, timeout_s=2)
    th.join()
    assert t is not None and hub.states[dev].axes[4] == 100
    with pytest.raises(ValueError):
        wait_for_button(hub, dev, "jump")


# --- bridge -------------------------------------------------------------------------

def x360_info(**kw) -> DeviceInfo:
    return DeviceInfo(-1, kw.pop("name", "Xbox 360 Controller"), sdl_type="xbox360",
                      family="xbox", vendor_id=0x045E, product_id=0x028E, **kw)


def pro_info() -> DeviceInfo:
    return DeviceInfo(-1, "Nintendo Switch Pro Controller", sdl_type="switchpro", family="switch",
                      vendor_id=0x057E, product_id=0x2009, serial="98-b6-e9-00-00-01")


def test_bridge_ignores_its_own_virtual_pad(fakevg):
    hub = Hub()
    phys = hub.connect(("sdl3", 1), pro_info())
    with Bridge(hub) as br:
        assert br.source == phys
        own = hub.connect(("sdl3", 2), x360_info())         # SDL now reports our pad
        assert br.own_pad_id == own
        assert wait_until(lambda: own in hub.ignored)
        hub.publish(InputEvent(now_ns(), own, BUTTON, BUTTON_INDEX["north"], 1))
        hub.publish(InputEvent(now_ns(), phys, BUTTON, BUTTON_INDEX["south"], 1))
        hub.publish(InputEvent(now_ns(), phys, AXIS, AXIS_INDEX["left_y"], -32768))
        pad = fakevg.pad
        assert wait_until(lambda: pad.sent and pad.last["buttons"] == 0x1000
                          and pad.last["ly"] == 32767)
        assert br.source == phys and own not in hub.snapshot()
        assert br.stats()["own_pad"] == own
    assert fakevg.deleted == 1 and pad.last == X360_NEUTRAL


def test_bridge_never_adopts_lookalike_before_own_pad_seen(fakevg):
    hub = Hub()
    br = Bridge(hub, target="x360", own_pad_window_s=0.0).start()
    try:
        assert br.source is None
        time.sleep(0.01)                                     # detection window has passed
        ghost = hub.connect(("sdl3", 7), x360_info())
        assert br.source is None and br.own_pad_id is None   # looks like our pad: not mirrored
        phys = hub.connect(("sdl3", 8), pro_info())
        assert br.source == phys
        assert ghost not in br.own_devices
    finally:
        br.stop()


def test_bridge_source_disconnect_releases_and_follows(fakevg):
    hub = Hub()
    br = Bridge(hub, target="ds4", remap={"south": "east", "east": "south"}, deadzone=8000)
    br.start()
    try:
        phys = hub.connect(("sdl3", 1), pro_info())
        assert br.source == phys
        hub.connect(("sdl3", 2), DeviceInfo(-1, "PS4 Controller", sdl_type="ps4",
                                            vendor_id=0x054C, product_id=0x05C4))
        assert br.own_pad_id is not None
        pad = fakevg.pad
        hub.publish(InputEvent(now_ns(), phys, BUTTON, BUTTON_INDEX["south"], 1))
        hub.publish(InputEvent(now_ns(), phys, AXIS, AXIS_INDEX["left_x"], 5000))   # in deadzone
        hub.publish(InputEvent(now_ns(), phys, AXIS, AXIS_INDEX["right_x"], 32767))
        assert wait_until(lambda: pad.sent and pad.last["buttons"] == (1 << 6) | 8
                          and pad.last["rx"] == 255)
        assert pad.last["lx"] == 128
        hub.disconnect(phys)                                  # e.g. Bluetooth drop
        assert wait_until(lambda: pad.last["buttons"] == 8 and pad.last["rx"] == 128)
        assert br.source is None
        second = hub.connect(("sdl3", 3), pro_info())         # auto mode picks the next pad
        assert br.source == second
    finally:
        br.stop()


def test_bridge_explicit_source_readopted_after_reconnect(fakevg):
    hub = Hub()
    a = hub.connect(("sdl3", 1), pro_info())
    b = hub.connect(("sdl3", 2), DeviceInfo(-1, "DualSense", sdl_type="ps5", vendor_id=0x054C,
                                            product_id=0x0CE6))
    hub.publish(InputEvent(now_ns(), a, BUTTON, BUTTON_INDEX["west"], 1))
    with Bridge(hub, source_device=a) as br:
        assert br.source == a
        pad = fakevg.pad
        assert wait_until(lambda: pad.sent and pad.last["buttons"] == 0x4000)  # held before start
        hub.publish(InputEvent(now_ns(), b, BUTTON, BUTTON_INDEX["north"], 1))
        time.sleep(0.02)
        assert pad.last["buttons"] == 0x4000                 # other pads are not mirrored
        hub.disconnect(a)
        assert br.source is None
        hub.connect(("sdl3", 3), DeviceInfo(-1, "Other", vendor_id=1, product_id=2))
        assert br.source is None
        a2 = hub.connect(("sdl3", 4), pro_info())            # same controller, new SDL id
        assert br.source == a2


def test_bridge_coalesces_bursts(fakevg):
    hub = Hub()
    phys = hub.connect(("sdl3", 1), pro_info())
    with Bridge(hub, min_interval_s=0.005) as br:
        pad = fakevg.pad
        for i in range(1, 501):
            hub.publish(InputEvent(now_ns(), phys, AXIS, AXIS_INDEX["left_x"], i * 60))
        assert wait_until(lambda: pad.sent and pad.last["lx"] == 30000)
        s = br.stats()
        assert s["events_in"] == 500 and 1 <= s["updates_sent"] < 100
        assert s["max_latency_ms"] < 500


def test_bridge_sink_is_fast(fakevg):
    hub = Hub()
    phys = hub.connect(("sdl3", 1), pro_info())
    with Bridge(hub):
        t = time.perf_counter()
        for i in range(2000):
            hub.publish(InputEvent(now_ns(), phys, BUTTON, BUTTON_INDEX["south"], i % 2))
        per_event_us = (time.perf_counter() - t) / 2000 * 1e6
    assert per_event_us < 200


def test_bridge_start_failure_removes_sink(monkeypatch):
    monkeypatch.setitem(sys.modules, "vgamepad", None)
    hub = Hub()
    with pytest.raises(VirtualPadUnavailable):
        Bridge(hub).start()
    assert not hub._sinks


def test_apply_deadzone():
    st = mkstate("south", left_x=3000, left_y=-3000, right_x=32767, right_trigger=100)
    out = apply_deadzone(st, 8000)
    assert out.axes[:2] == [0, 0] and out.axes[2] == 32767 and out.axes[5] == 100
    assert out.pressed("south")
    half = apply_deadzone(mkstate(left_x=20000), 8000).axes[0]
    assert 0 < half < 20000
    assert apply_deadzone(st, 0) is st


# --- review regressions ------------------------------------------------------------------

def test_failed_update_error_does_not_keep_device_plugged(fakevg):
    pad = create_virtual_pad("x360")
    fakevg.fail_updates = True
    with pytest.raises(VirtualPadError) as ei:
        pad.apply(mkstate("south"))
    kept = ei.value                        # e.g. Bridge.error holds on to it
    fakevg.fail_updates = False
    pad.close()
    gc.collect()
    assert fakevg.refs[-1]() is None, "the error's traceback kept the virtual device alive"
    assert "NOT_PLUGGED_IN" in str(kept) and kept.__cause__ is not None


def test_x360_y_not_inverted_where_vgamepad_feeds_evdev(fakevg, monkeypatch):
    import controllerlog.output.virtual_pad as vp
    monkeypatch.setattr(vp, "X360_INVERT_Y", False)        # Linux: ABS_Y down = positive
    with create_virtual_pad("x360") as pad:
        pad.apply(mkstate(left_y=-32768, right_y=1000))
        assert (fakevg.pad.last["ly"], fakevg.pad.last["ry"]) == (-32768, 1000)
    assert x360_report(mkstate(left_y=5), invert_y=False)[4] == 5


def test_replay_rejects_devices_without_input(fakevg):
    with pytest.raises(ValueError, match="no input"):
        replay_recording(make_recording([]), countdown_s=0, settle_s=0)   # CONNECT row only
    rec = make_recording([(10, BUTTON, "south", 1)])
    with pytest.raises(ValueError, match="devices with input: 0"):
        replay_recording(rec, device=5, countdown_s=0, settle_s=0)
    with pytest.raises(ValueError, match="trigger button"):
        replay_recording(rec, countdown_s=0, settle_s=0,
                         wait_for_trigger=ButtonTrigger(Hub(), None, "jump"))
    assert not fakevg.created                                # failed before plugging a pad in


def test_replay_skips_codes_the_model_does_not_know(fakevg):
    rec = make_recording([(10, BUTTON, "south", 1), (20, BUTTON, "south", 0)])
    rec.events += [InputEvent(15 * MS, 0, BUTTON, 40, 1), InputEvent(16 * MS, 0, AXIS, 9, 100),
                   InputEvent(17 * MS, 0, BUTTON, None, 1)]
    rec.sort()
    rep = replay_recording(rec, countdown_s=0, settle_s=0)
    assert rep.aborted is None and rep.scheduled == 2


def test_replay_schedule_is_built_before_t0(fakevg):
    """Sorting a long run's events must not delay the first inputs past t0."""
    rows = [(10, BUTTON, "south", 1), (20, BUTTON, "south", 0)]
    rec = make_recording(rows)
    rec.events += [InputEvent(3_600 * 10**9 + i * 1000, 0, AXIS, 0, i % 30000 + 1)
                   for i in range(300_000)]
    stop = threading.Event()

    def status(msg: str) -> None:
        if msg.startswith("replaying"):
            threading.Timer(0.4, stop.set).start()
    rep = replay_recording(rec, countdown_s=0.2, settle_s=0, stop_event=stop, on_status=status)
    assert rep.aborted == "stopped" and rep.count == 2
    assert rep.lateness_ns[0] < 5 * MS, rep.summary()


def test_replay_trigger_on_any_device_ignores_own_pad(fakevg):
    """``device=None``: the replay's own pad, seen by the hub's backend, must not start it."""
    hub = Hub()
    phys = hub.connect(("sdl3", 1), pro_info())

    class EchoX360(fakevg.module.VX360Gamepad):              # an SDL backend seeing our pad
        def update(self) -> None:
            super().update()
            if not hasattr(self, "hub_id"):
                self.hub_id = hub.connect(("sdl3", 100 + len(fakevg.logs)), x360_info())
            ev = InputEvent(now_ns(), self.hub_id, BUTTON, BUTTON_INDEX["start"],
                            1 if self.report["buttons"] & 0x10 else 0)
            threading.Timer(0.005, hub.publish, (ev,)).start()   # asynchronous, like SDL
    fakevg.module.VX360Gamepad = EchoX360
    rec = make_recording([(0, BUTTON, "start", 1), (30, BUTTON, "start", 0)])
    rep = replay_recording(rec, countdown_s=0, settle_s=0,
                           wait_for_trigger=ButtonTrigger(hub, None, "start", timeout_s=0.3))
    assert rep.aborted == "trigger timeout"                  # our held Start didn't count
    threading.Timer(0.1, hub.publish,
                    (InputEvent(now_ns(), phys, BUTTON, BUTTON_INDEX["start"], 1),)).start()
    rep = replay_recording(rec, countdown_s=0, settle_s=0,
                           wait_for_trigger=ButtonTrigger(hub, None, "start", timeout_s=2))
    assert rep.aborted is None                               # a real controller still does
    assert not hub._sinks


def test_two_bridges_claim_different_pads(fakevg):
    hub = Hub()
    a_src = hub.connect(("sdl3", 1), pro_info())
    b_src = hub.connect(("sdl3", 2), DeviceInfo(-1, "DualSense", sdl_type="ps5",
                                                vendor_id=0x054C, product_id=0x0CE6))
    a = Bridge(hub, source_device=a_src).start()
    b = Bridge(hub).start()
    try:
        assert b.source == b_src
        pa = hub.connect(("sdl3", 3), x360_info())
        pb = hub.connect(("sdl3", 4), x360_info())
        assert (a.own_pad_id, b.own_pad_id) == (pa, pb)
        assert wait_until(lambda: {pa, pb} <= hub.ignored)
        hub.disconnect(b_src)                    # b (auto) falls back to a physical pad,
        assert wait_until(lambda: b.source == a_src)   # never to a's virtual one
    finally:
        b.stop()
        a.stop()


def test_bridge_adopts_lookalike_once_own_pad_known(fakevg):
    hub = Hub()
    real = hub.connect(("sdl3", 1), x360_info(name="8BitDo (X-input mode)"))
    br = Bridge(hub).start()
    try:
        assert br.source is None                 # could be our own pad: not adopted yet
        own = hub.connect(("sdl3", 2), x360_info())
        assert br.own_pad_id == own
        assert wait_until(lambda: br.source == real), br.stats()
    finally:
        br.stop()


def test_bridge_source_vanishing_during_adoption(fakevg, monkeypatch):
    hub = Hub()
    phys = hub.connect(("sdl3", 1), pro_info())
    real_snapshot = hub.snapshot
    raced: list[int] = []

    def racy_snapshot():
        snap = real_snapshot()
        if not raced:
            raced.append(1)
            hub.disconnect(phys)                 # lands between snapshot and adoption
        return snap
    monkeypatch.setattr(hub, "snapshot", racy_snapshot)
    br = Bridge(hub).start()
    try:
        assert br.source is None
        second = hub.connect(("sdl3", 2), pro_info())
        assert wait_until(lambda: br.source == second)
    finally:
        br.stop()


def test_bridge_restart_and_reconnect_same_key(fakevg):
    hub = Hub()
    phys = hub.connect(("adb", "pad"), pro_info())
    br = Bridge(hub)
    br.start()
    own1 = hub.connect(("sdl3", 2), x360_info())
    br.stop()
    hub.disconnect(own1)
    with br:                                     # start again
        assert br.source == phys
        own2 = hub.connect(("sdl3", 3), x360_info())
        assert br.own_pad_id == own2
        hub.publish(InputEvent(now_ns(), phys, BUTTON, BUTTON_INDEX["south"], 1))
        assert wait_until(lambda: fakevg.pad.sent and fakevg.pad.last["buttons"] == 0x1000)
        # Same hub key connected again without a disconnect: the hub state is neutral again.
        assert hub.connect(("adb", "pad"), pro_info()) == phys and br.source == phys
        assert wait_until(lambda: fakevg.pad.last["buttons"] == 0)
    assert fakevg.deleted == 2


def test_bridge_auto_falls_back_to_older_pad(fakevg):
    hub = Hub()
    a = hub.connect(("sdl3", 1), pro_info())
    b = hub.connect(("sdl3", 2), DeviceInfo(-1, "DualSense", sdl_type="ps5",
                                            vendor_id=0x054C, product_id=0x0CE6))
    hub.publish(InputEvent(now_ns(), a, BUTTON, BUTTON_INDEX["north"], 1))
    with Bridge(hub) as br:
        assert br.source == b                    # most recent first
        hub.disconnect(b)
        assert wait_until(lambda: br.source == a)
        assert wait_until(lambda: fakevg.pad.last["buttons"] == 0x8000)   # a's held Y


# --- live tests (ViGEmBus + SDL3) ------------------------------------------------------

def _require_live():
    reason = virtual_pad_unavailable_reason()
    if reason:
        pytest.skip(reason)
    try:
        from controllerlog.input.sdl3 import load_library  # noqa: PLC0415
        load_library()
    except Exception as e:
        pytest.skip(f"SDL3 unavailable: {e}")


@pytest.fixture
def live_hub():
    _require_live()
    from controllerlog.input.sdl3_backend import SDL3Backend  # noqa: PLC0415
    hub = Hub()
    backend = SDL3Backend(hub)
    backend.start()
    if not backend.ready.wait(10) or backend.error:
        backend.stop()
        pytest.skip(f"SDL3 backend failed: {backend.error}")
    try:
        yield hub
    finally:
        backend.stop()


def _find_pad_device(hub: Hub, pad: VirtualPad, before: set[int], timeout: float = 4.0) -> int:
    """Hub id of ``pad``: a new device with its USB ids that reacts to a probe state."""
    t = pad.target
    probe = ("west", "north", "left_shoulder")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # Toggle the probe: SDL only reports changes after it has opened the device.
        pad.reset()
        time.sleep(0.05)
        pad.apply(mkstate(*probe))
        end = time.monotonic() + 0.25
        while time.monotonic() < end:
            for d, info in list(hub.devices.items()):
                st = hub.states.get(d)
                if (d not in before and st is not None and info.vendor_id == t.vendor_id
                        and info.product_id == t.product_id
                        and all(st.pressed(b) for b in probe)):
                    pad.reset()
                    return d
            time.sleep(0.005)
    raise AssertionError(f"virtual {t.label} never showed up in SDL")


def _prime(pad: VirtualPad) -> None:
    """Move every analog control once: SDL ignores/rebases an axis' first movements."""
    for st in (mkstate(left_x=32767, left_y=32767, right_x=32767, right_y=32767,
                       left_trigger=32767, right_trigger=32767),
               mkstate(left_x=-32768, left_y=-32768, right_x=-32768, right_y=-32768),
               PadState()):
        pad.apply(st)
        time.sleep(0.08)


LIVE_STATES = [
    mkstate("south", "east", "west", "north"),
    mkstate("back", "start", "guide", "left_stick", "right_stick"),
    mkstate("left_shoulder", "right_shoulder", "dpad_up", "dpad_right",
            left_x=-32768, left_y=-32768, right_x=32767, right_y=32767),
    mkstate("dpad_down", "dpad_left", left_x=12345, left_y=20000, right_x=-7000, right_y=-30000,
            left_trigger=16384, right_trigger=32767),
    mkstate(left_y=32767, left_trigger=5000, right_trigger=25000),
    PadState(),
]


def _expected_readback(target: str, st: PadState) -> PadState:
    exp = st.copy()
    if target == "ds4":
        b = exp.buttons
        for a, c in (("dpad_up", "dpad_down"), ("dpad_left", "dpad_right")):
            if b[BUTTON_INDEX[a]] and b[BUTTON_INDEX[c]]:
                b[BUTTON_INDEX[a]] = b[BUTTON_INDEX[c]] = 0
    else:
        exp.buttons[BUTTON_INDEX["touchpad"]] = 0
    return exp


def _close_enough(target: str, got: PadState, exp: PadState) -> bool:
    if got.buttons != exp.buttons:
        return False
    stick_tol = 2 if target == "x360" else 129
    for i, (g, e) in enumerate(zip(got.axes, exp.axes)):
        tol = 130 if i in (4, 5) else stick_tol
        if abs(g - e) > tol:
            return False
    return True


@pytest.mark.vigem
@pytest.mark.sdl
@pytest.mark.parametrize("target", ["x360", "ds4"])
def test_live_roundtrip_through_sdl(live_hub, target):
    hub = live_hub
    before = set(hub.devices)
    with create_virtual_pad(target) as pad:
        dev = _find_pad_device(hub, pad, before)
        info = hub.devices[dev]
        assert info.sdl_type == TARGETS[target].sdl_type
        _prime(pad)
        states = LIVE_STATES + ([mkstate("touchpad", "dpad_up", "dpad_down")] if target == "ds4" else [])
        for st in states:
            pad.apply(st)
            exp = _expected_readback(target, st)
            ok = wait_until(lambda: _close_enough(target, hub.states[dev], exp), 1.5)
            assert ok, (f"{target}: sent {st.to_json()}\n expected ~{exp.to_json()}\n"
                        f" got {hub.states[dev].to_json()}")
    assert wait_until(lambda: dev not in hub.devices, 3.0)


class _XInputGamepad(ctypes.Structure):
    _fields_ = [("wButtons", ctypes.c_ushort), ("bLeftTrigger", ctypes.c_ubyte),
                ("bRightTrigger", ctypes.c_ubyte), ("sThumbLX", ctypes.c_short),
                ("sThumbLY", ctypes.c_short), ("sThumbRX", ctypes.c_short),
                ("sThumbRY", ctypes.c_short)]


class _XInputState(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_uint32), ("Gamepad", _XInputGamepad)]


def _xinput_pads() -> list[tuple[int, int, int, int]]:
    """(wButtons, LT, LX, LY) of every connected XInput slot, as a game would read them."""
    lib = ctypes.WinDLL("xinput1_4")
    out = []
    for i in range(4):
        st = _XInputState()
        if lib.XInputGetState(i, ctypes.byref(st)) == 0:
            g = st.Gamepad
            out.append((g.wButtons, g.bLeftTrigger, g.sThumbLX, g.sThumbLY))
    return out


@pytest.mark.vigem
@pytest.mark.sdl
@pytest.mark.skipif(sys.platform != "win32", reason="XInput is Windows-only")
def test_live_bridge_ds4_to_x360(live_hub):
    """A virtual DS4 stands in for a physical pad; the bridge mirrors it onto an X360 pad."""
    hub = live_hub
    before = set(hub.devices)
    with create_virtual_pad("ds4") as src:
        src_dev = _find_pad_device(hub, src, before)
        _prime(src)
        with Bridge(hub, source_device=src_dev, target="x360") as br:
            assert br.source == src_dev
            assert wait_until(lambda: br.own_pad_id is not None and br.own_pad_id in hub.ignored,
                              4.0), f"own pad not detected: {br.stats()}"
            assert br.own_pad_id not in hub.snapshot()
            time.sleep(0.1)
            br.reset_stats()        # the first report to a just-plugged pad can block for a while
            src.apply(mkstate("south", "dpad_up", left_y=-32768, left_x=-32768,
                              left_trigger=32767))
            want = (0x1000 | 0x0001, 255, -32768, 32767)       # A + up, LT full, stick up-left
            assert wait_until(lambda: want in _xinput_pads(), 2.0), (_xinput_pads(), br.stats())
            src.apply(PadState())
            assert wait_until(lambda: br.pad.state.button_mask() == 0, 2.0)
            s = br.stats()
            print("bridge stats:", s)
            assert s["updates_sent"] >= 2 and s["mean_latency_ms"] < 10, s
        assert br.pad.closed


@pytest.mark.vigem
@pytest.mark.sdl
def test_live_replay_timing(live_hub):
    hub = live_hub
    seen: list[InputEvent] = []
    hub.add_sink(seen.append)
    rec = make_recording([
        (0, BUTTON, "south", 1), (100, BUTTON, "south", 0), (100, AXIS, "left_x", 20000),
        (150, BUTTON, "east", 1), (200, BUTTON, "east", 0), (200, AXIS, "left_x", 0),
        (250, AXIS, "right_trigger", 32767), (300, AXIS, "right_trigger", 0),
    ], markers=[(400, "end")])
    before = set(hub.devices)
    rep = replay_recording(rec, target="x360", countdown_s=0.5)
    wait_until(lambda: set(hub.devices) <= before, 3.0)       # SDL saw the unplug
    hub.remove_sink(seen.append)
    assert rep.aborted is None and rep.max_late_ms < 5, rep.summary()
    t = TARGETS["x360"]
    new = {ev.device for ev in seen if ev.kind == CONNECT and ev.value["vendor_id"] == t.vendor_id
           and ev.value["product_id"] == t.product_id}
    by_dev = {d: [ev for ev in seen if ev.device == d and ev.kind in (BUTTON, AXIS)] for d in new}
    expected = [(100, BUTTON, BUTTON_INDEX["south"], 0), (100, AXIS, 0, 20000),
                (150, BUTTON, BUTTON_INDEX["east"], 1), (200, BUTTON, BUTTON_INDEX["east"], 0),
                (200, AXIS, 0, 0), (250, AXIS, 5, 32767), (300, AXIS, 5, 0)]
    for d, evs in by_dev.items():
        played = [e for e in evs if e.t_ns >= rep.t0_ns]
        keys = [(e.kind, e.code, e.value) for e in played]
        if keys[:len(expected)] == [x[1:] for x in expected]:
            for e, (t_ms, *_r) in zip(played, expected):
                late_ms = (e.t_ns - rep.t0_ns) / MS - t_ms
                assert -1 <= late_ms < 8, (t_ms, late_ms)
            # South was held from the initial state (applied before the countdown).
            assert any(e.code == BUTTON_INDEX["south"] and e.value == 1 and e.t_ns < rep.t0_ns
                       for e in evs if e.kind == BUTTON)
            break
    else:
        pytest.fail(f"replayed input not seen through SDL: { {d: [(e.kind, e.code, e.value) for e in v] for d, v in by_dev.items()} }")


@pytest.mark.vigem
@pytest.mark.sdl
def test_live_trigger_on_any_device_ignores_own_pad(live_hub):
    """The replay's pad holds Start (initial state) while waiting; SDL sees it, it mustn't count."""
    rec = make_recording([(0, BUTTON, "start", 1), (30, BUTTON, "start", 0)])
    seen: list[InputEvent] = []
    live_hub.add_sink(seen.append)
    try:
        rep = replay_recording(rec, countdown_s=0,
                               wait_for_trigger=ButtonTrigger(live_hub, None, "start",
                                                              timeout_s=1.0))
    finally:
        live_hub.remove_sink(seen.append)
    own_presses = [e for e in seen if e.kind == BUTTON and e.code == BUTTON_INDEX["start"]
                   and e.value == 1]
    if not own_presses:
        pytest.skip("SDL did not report the virtual pad's held Start in time")
    assert rep.aborted == "trigger timeout", rep.summary()


# --- start-up races and shared serials (no hardware) ------------------------------------

def test_wait_for_button_sees_press_after_release_during_subscribe(monkeypatch):
    hub = Hub()
    dev = hub.connect(("sdl3", 1), x360_info())
    start = BUTTON_INDEX["start"]
    hub.publish(InputEvent(now_ns(), dev, BUTTON, start, 1))       # Start held when waiting begins
    real_snapshot = hub.snapshot
    threads = []

    def preempted_snapshot():
        snap = real_snapshot()
        th = threading.Thread(target=hub.publish, args=(InputEvent(now_ns(), dev, BUTTON, start, 0),))
        th.start()
        th.join(0.2)       # blocks only while the hub lock is held (it is, after the fix)
        threads.append(th)
        return snap

    monkeypatch.setattr(hub, "snapshot", preempted_snapshot)
    pressed_at = []

    def press_later():
        time.sleep(0.4)
        hub.publish(InputEvent(now_ns(), dev, BUTTON, start, 0))   # no-op if already released
        t = now_ns()
        pressed_at.append(t)
        hub.publish(InputEvent(t, dev, BUTTON, start, 1))          # a fresh press

    presser = threading.Thread(target=press_later)
    presser.start()
    got = wait_for_button(hub, dev, "start", timeout_s=1.5)
    presser.join()
    for th in threads:
        th.join(2)
    assert got is not None and got == pressed_at[0]


VIGEM_DS4_SERIAL = "c0-13-37-66-3c-55"     # what SDL reports for every vgamepad VDS4Gamepad


def _ds4_desc(iid, serial):
    return {"name": "PS4 Controller", "sdl_type": "ps4", "vendor_id": 0x054C,
            "product_id": 0x05C4, "connection": "wired", "serial": serial,
            "path": f"hid#{iid}", "instance_id": iid, "real_sdl_type": None}


class _FakeSDL:
    def __init__(self):
        self.descs = {}

    def OpenGamepad(self, iid):
        return iid + 1000

    def describe(self, pad):
        return self.descs[pad - 1000]

    def CloseGamepad(self, pad):
        pass

    def GetGamepadButton(self, pad, b):
        return 0

    def GetGamepadAxis(self, pad, a):
        return 0

    def error(self):
        return ""


def test_restarted_ds4_bridge_identifies_its_new_pad():
    import itertools
    from controllerlog.input.sdl3_backend import SDL3Backend
    hub = Hub()
    be = SDL3Backend(hub)                 # driven by hand: no SDL, no thread
    be.sdl = _FakeSDL()
    iids = itertools.count(10)

    class FakeVDS4:                       # a ViGEm DS4 as the SDL backend sees it
        def __init__(self, target):
            self.iid = next(iids)
            be.sdl.descs[self.iid] = _ds4_desc(self.iid, VIGEM_DS4_SERIAL)
            be._open(self.iid, now_ns())

        def apply(self, state):
            return True

        def close(self):
            be._close(self.iid, now_ns())

    br = Bridge(hub, target="ds4", pad_factory=FakeVDS4)
    br.start()
    try:
        own1 = br.own_pad_id
        assert own1 is not None and wait_until(lambda: own1 in hub.ignored)
        be.sdl.descs[1] = _ds4_desc(1, "a4-ae-12-34-56-78")      # a physical DualShock 4 v1
        be._open(1, now_ns())
        phys = be._device(1)
        assert wait_until(lambda: br.source == phys)
    finally:
        br.stop()

    br.start()                                                   # restart: a new virtual DS4
    try:
        assert br.own_pad_id is not None, "new virtual pad never identified"
        assert wait_until(lambda: br.source == phys), "physical DS4 never adopted again"
    finally:
        br.stop()
    be._close(1, now_ns())
