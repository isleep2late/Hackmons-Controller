from __future__ import annotations

import json
import random
import sys
import threading
import time
from pathlib import Path

import pytest

from controllerlog.hub import Hub
from controllerlog.input import adb_backend as ab
from controllerlog.input.adb_backend import (
    ABS_BRAKE, ABS_GAS, ABS_HAT0Y, ABS_RX, ABS_RY, ABS_RZ, ABS_X, ABS_Y, ABS_Z, BTN_LEFT, KEY_BACK,
    KEY_HOMEPAGE, KEY_RECORD, AbsInfo, AdbBackend, AdbDeviceError, AdbNotFoundError, ClockSync,
    DeviceAdded, DeviceName, DeviceRemoved, EvdevDevice, EvdevMapper, RawEvent, adb_connect,
    adb_pair, build_profile, list_adb_devices, list_input_devices, parse_adb_devices,
    parse_getevent_info, parse_stream_line, resolve_adb, scale_stick, scale_trigger,
    select_device)
from controllerlog.model import (AXIS, AXIS_INDEX, BUTTON, BUTTON_INDEX, CONNECT, DISCONNECT,
                                 InputEvent)

FIX = Path(__file__).parent / "fixtures" / "adb"
FAKE = [sys.executable, str(FIX / "fake_adb.py")]
MS = 1_000_000
S = 1_000_000_000
B = BUTTON_INDEX
A = AXIS_INDEX


def devices(name: str) -> list[EvdevDevice]:
    return parse_getevent_info((FIX / name).read_text(encoding="utf-8"))


def node(name: str, path: str) -> EvdevDevice:
    return next(d for d in devices(name) if d.path == path)


def gamepads(name: str) -> list[str]:
    return [d.path for d in devices(name) if d.is_gamepad]


# --- getevent -i / -p parsing ------------------------------------------------------

def test_parse_dualsense_phone_info():
    devs = devices("dualsense_phone_info.txt")
    assert [d.path for d in devs] == [f"/dev/input/event{i}" for i in (0, 1, 2, 4, 5, 6, 7)]
    ds = devs[3]
    assert ds.name == "DualSense Wireless Controller"
    assert (ds.bus, ds.vendor, ds.product, ds.version) == (5, 0x054c, 0x0ce6, 0x8100)
    assert ds.has_ids and ds.uniq == "a0:ab:51:12:34:56" and ds.location == "3c:28:6d:aa:bb:cc"
    assert ds.driver_version == "1.0.1"
    assert ds.keys == {0x130, 0x131, 0x133, 0x134, 0x136, 0x137, 0x138, 0x139, 0x13a, 0x13b,
                       0x13c, 0x13d, 0x13e}
    assert ds.abs[ABS_X] == AbsInfo(128, 0, 255, 0, 0, 0)
    assert ds.abs[ABS_HAT0Y] == AbsInfo(0, -1, 1, 0, 0, 0)
    assert 0x50 in ds.events[0x15]
    assert ds.connection == "wireless" and ds.family == "playstation"
    assert devs[4].props == {0, 2} and devs[5].props == {6} and devs[2].props == {1}
    assert devs[5].abs[ABS_RX] == AbsInfo(3, -2097152, 2097152, 16, 0, 1024)
    assert devs[5].events[0x04] == {0x05}
    assert devs[6].events[0x05] == {0x02, 0x04}


def test_classification_of_all_fixtures():
    assert gamepads("dualsense_phone_info.txt") == ["/dev/input/event4"]
    assert gamepads("switchpro_info.txt") == ["/dev/input/event8"]            # IMU excluded
    assert gamepads("xbox_bt_info.txt") == ["/dev/input/event5"]
    assert gamepads("generic_bt_info.txt") == ["/dev/input/event6"]           # not consumer ctl
    assert gamepads("ds4_hidgeneric_info.txt") == ["/dev/input/event5"]
    assert gamepads("old_style_info.txt") == ["/dev/input/event3"]
    joystick = EvdevDevice("/dev/input/event9", keys={0x120, 0x121},
                           abs={ABS_X: AbsInfo(), ABS_Y: AbsInfo()})
    assert joystick.is_gamepad
    keyboard = EvdevDevice("/dev/input/event3", keys=set(range(1, 100)))
    assert not keyboard.is_gamepad


def test_label_output_matches_numeric():
    num = {d.path: d for d in devices("switchpro_info.txt")}
    lab = {d.path: d for d in devices("switchpro_info_labels.txt")}
    assert num.keys() == lab.keys()
    for path in num:
        assert lab[path].keys == num[path].keys
        assert lab[path].abs == num[path].abs
        assert lab[path].props == num[path].props
        assert lab[path].name == num[path].name
    assert not lab["/dev/input/event8"].has_ids and num["/dev/input/event8"].has_ids
    assert [p for p, d in lab.items() if d.is_gamepad] == ["/dev/input/event8"]


def test_one_line_id_format_and_missing_resolution():
    d = devices("old_style_info.txt")[0]
    assert (d.bus, d.vendor, d.product, d.version) == (5, 0x054c, 0x09cc, 0x8100)
    assert d.abs[ABS_X] == AbsInfo(125, 0, 255, 0, 0, 0)
    assert len(d.keys) == 13


def test_pressed_keys_and_hid_descriptor_section():
    xbox = node("xbox_bt_info.txt", "/dev/input/event5")
    assert xbox.pressed == {0x137}
    text = (FIX / "ds4_hidgeneric_info.txt").read_text() + (
        "  HID descriptor: 0005:054C:09CC.0001\n\n    05 01 09 05 a1 01 85 01 09 30 09 31\n"
        "    0130  0131\n\n") + (FIX / "old_style_info.txt").read_text()
    devs = parse_getevent_info(text)
    assert [d.path for d in devs] == ["/dev/input/event5", "/dev/input/event3"]
    assert devs[0].keys == set(range(0x130, 0x13e))


# --- stream lines -------------------------------------------------------------------

@pytest.mark.parametrize("line, expected", [
    ("[    5000.100000] /dev/input/event4: 0003 0000 000000ff",
     RawEvent(5000_100_000_000, "/dev/input/event4", 3, 0, 255)),
    ("[    5000.650000] /dev/input/event4: 0003 0011 ffffffff",
     RawEvent(5000_650_000_000, "/dev/input/event4", 3, 0x11, -1)),
    ("[  12.000001] /dev/input/event8: 0003 0000 ffff8001\r\n",
     RawEvent(12_000_001_000, "/dev/input/event8", 3, 0, -32767)),
    ("0003 0001 80000000", RawEvent(None, None, 3, 1, -2147483648)),
    ("0001 0130 00000001", RawEvent(None, None, 1, 0x130, 1)),
    ("1234-567890: 0001 0130 00000000", RawEvent(1234_567_890_000, None, 1, 0x130, 0)),
    ("[     123.000001] /dev/input/event4: EV_KEY       BTN_GAMEPAD          DOWN                ",
     RawEvent(123_000_001_000, "/dev/input/event4", 1, 0x130, 1)),
    ("EV_ABS       ABS_HAT0Y            ffffffff            ", RawEvent(None, None, 3, 0x11, -1)),
    ("EV_SYN       SYN_REPORT           00000000            ", RawEvent(None, None, 0, 0, 0)),
    ("add device 8: /dev/input/event8", DeviceAdded(8, "/dev/input/event8")),
    ("remove device 9: /dev/input/event9", DeviceRemoved(9, "/dev/input/event9")),
    ('  name:     "Nintendo Switch Pro Controller"', DeviceName("Nintendo Switch Pro Controller")),
    ("could not open /dev/input/event9, Permission denied", None),
    ("", None),
])
def test_parse_stream_line(line, expected):
    assert parse_stream_line(line) == expected


# --- profiles -----------------------------------------------------------------------

def test_profile_dualsense_hid_playstation():
    p = build_profile(node("dualsense_phone_info.txt", "/dev/input/event4"))
    assert p.family == "playstation"
    assert {c: p.buttons[c] for c in (0x130, 0x131, 0x133, 0x134)} == {
        0x130: "south", 0x131: "east", 0x133: "north", 0x134: "west"}
    assert 0x138 not in p.buttons and 0x139 not in p.buttons     # analog L2/R2 win
    assert p.buttons[0x13a] == "back" and p.buttons[0x13c] == "guide"
    assert p.buttons[BTN_LEFT] == "touchpad"
    assert p.axes[ABS_RX].target == "right_x" and p.axes[ABS_RY].target == "right_y"
    assert p.axes[ABS_Z].target == "left_trigger" and p.axes[ABS_RZ].target == "right_trigger"
    assert p.axes[0x10].target == "hat_x" and p.axes[0x11].target == "hat_y"
    assert p.name.startswith("positional/")


def test_profile_switch_pro_hid_nintendo_and_swap():
    dev = node("switchpro_info.txt", "/dev/input/event8")
    p = build_profile(dev)
    assert p.family == "switch"
    # hid-nintendo is positional: A -> BTN_EAST, B -> BTN_SOUTH, X -> BTN_NORTH, Y -> BTN_WEST
    assert [p.buttons[c] for c in (0x130, 0x131, 0x133, 0x134)] == ["south", "east", "north", "west"]
    assert p.buttons[0x138] == "left_trigger" and p.buttons[0x139] == "right_trigger"
    assert p.buttons[0x135] == "misc1"                              # Capture
    assert p.axes[ABS_RX].target == "right_x"
    assert not any(m.target.endswith("trigger") for m in p.axes.values())
    swapped = build_profile(dev, nintendo_swap=True)
    assert [swapped.buttons[c] for c in (0x130, 0x131, 0x133, 0x134)] == [
        "east", "south", "west", "north"]
    assert swapped.name.endswith("/swapped")


def test_profile_switch2_gamecube_standard_hid():
    """NSO GameCube controller in the standard HID mode GC Bridge switches on (hid-generic)."""
    dev = node("switch2_gc_standard_info.txt", "/dev/input/event12")
    assert dev.is_gamepad and dev.family == "gamecube" and dev.sdl_type_guess == "gamecube"
    p = build_profile(dev)
    face = {0x130: "west", 0x131: "south", 0x132: "north", 0x133: "east"}   # B A Y X
    assert {c: p.buttons[c] for c in face} == face
    assert p.buttons[0x134] == "right_trigger" and p.buttons[0x135] == "right_shoulder"   # R click, Z
    assert p.buttons[0x136] == "start"
    assert [p.buttons[c] for c in (0x138, 0x139, 0x13a, 0x13b)] == ["dpad_down", "dpad_right", "dpad_left", "dpad_up"]
    assert p.buttons[0x13c] == "left_trigger" and p.buttons[0x13e] == "back"
    assert p.buttons[0x2c0] == "guide" and p.buttons[0x2c1] == "misc1" and p.buttons[0x2c4] == "misc2"
    assert p.axes[0x00].target == "left_x" and not p.axes[0x00].invert
    assert p.axes[0x01].target == "left_y" and p.axes[0x01].invert
    assert p.axes[0x03].target == "right_x" and not p.axes[0x03].invert
    assert p.axes[0x05].target == "right_y" and p.axes[0x05].invert
    # a Switch 1 Pro Controller through hid-nintendo is untouched
    pro = build_profile(node("switchpro_info.txt", "/dev/input/event8"))
    assert pro.buttons[0x130] == "south" and 0x2c0 not in pro.buttons


def test_profile_xbox_bluetooth():
    p = build_profile(node("xbox_bt_info.txt", "/dev/input/event5"))
    assert p.family == "xbox"
    assert [p.buttons[c] for c in (0x130, 0x131, 0x133, 0x134)] == ["south", "east", "west", "north"]
    assert p.buttons[KEY_BACK] == "back" and p.buttons[KEY_HOMEPAGE] == "guide"
    assert p.buttons[KEY_RECORD] == "misc1" and p.buttons[0x13b] == "start"
    assert p.axes[ABS_Z].target == "right_x" and p.axes[ABS_RZ].target == "right_y"
    assert p.axes[ABS_BRAKE].target == "left_trigger" and p.axes[ABS_GAS].target == "right_trigger"
    assert 0x138 not in p.buttons and 0x139 not in p.buttons


def test_profile_generic_android_gamepad():
    p = build_profile(node("generic_bt_info.txt", "/dev/input/event6"))
    assert p.family == "generic"
    assert p.buttons[0x133] == "west" and p.buttons[0x134] == "north"
    assert p.axes[ABS_Z].target == "right_x" and p.axes[ABS_RZ].target == "right_y"
    assert p.axes[ABS_BRAKE].target == "left_trigger" and p.axes[ABS_GAS].target == "right_trigger"


def test_profile_ds4_on_hid_generic():
    p = build_profile(node("ds4_hidgeneric_info.txt", "/dev/input/event5"))
    assert p.name.startswith("sony_hid")
    assert [p.buttons[c] for c in range(0x130, 0x134)] == ["west", "south", "east", "north"]
    assert p.buttons[0x13d] == "touchpad" and p.buttons[0x138] == "back"
    assert p.axes[ABS_Z].target == "right_x" and p.axes[ABS_RZ].target == "right_y"
    assert p.axes[ABS_RX].target == "left_trigger" and p.axes[ABS_RY].target == "right_trigger"
    assert 0x136 not in p.buttons and 0x137 not in p.buttons


def test_profile_heuristics_for_unknown_drivers():
    stick = AbsInfo(0, -32768, 32767, 16, 128)
    trig = AbsInfo(0, 0, 255)
    xpad_like = EvdevDevice("/dev/input/event9", vendor=0x046d, keys={0x130, 0x131, 0x133, 0x134} | {
        0x2c0, 0x2c1, 0x2c2, 0x2c3}, abs={ABS_X: stick, ABS_Y: stick, ABS_RX: stick, ABS_RY: stick,
                                          ABS_Z: trig, ABS_RZ: trig})
    p = build_profile(xpad_like)
    assert p.axes[ABS_RX].target == "right_x" and p.axes[ABS_Z].target == "left_trigger"
    assert [p.buttons[0x2c0 + i] for i in range(4)] == ["dpad_left", "dpad_right", "dpad_up",
                                                        "dpad_down"]
    raw = AbsInfo(128, 0, 255)
    rest = AbsInfo(0, 0, 255)
    swapped = EvdevDevice("/dev/input/event9", keys={0x130}, abs={
        ABS_X: raw, ABS_Y: raw, ABS_Z: raw, ABS_RZ: raw, ABS_RX: rest, ABS_RY: rest})
    p = build_profile(swapped)
    assert p.axes[ABS_Z].target == "right_x" and p.axes[ABS_RX].target == "left_trigger"
    steam = EvdevDevice("/dev/input/event9", vendor=0x28de, keys={0x130}, abs={
        ABS_X: stick, ABS_Y: stick, ABS_RX: stick, ABS_RY: stick,
        0x14: AbsInfo(0, 0, 255), 0x15: AbsInfo(0, 0, 255)})
    p = build_profile(steam)
    assert p.axes[0x15].target == "left_trigger" and p.axes[0x14].target == "right_trigger"


def test_profile_override():
    dev = node("generic_bt_info.txt", "/dev/input/event6")
    p = build_profile(dev, {"buttons": {"BTN_C": "guide", "0x13a": None, 317: "right_stick"},
                            "axes": {"ABS_Z": "-right_x", "ABS_GAS": {"to": "left_trigger"},
                                     "ABS_BRAKE": None},
                            "apply_flat": True})
    assert p.buttons[0x132] == "guide" and 0x13a not in p.buttons
    assert p.buttons[317] == "right_stick"
    assert p.axes[ABS_Z].target == "right_x" and p.axes[ABS_Z].invert
    assert p.axes[ABS_GAS].target == "left_trigger" and ABS_BRAKE not in p.axes
    assert p.buttons[0x139] == "right_trigger"          # no analog RT any more -> digital
    assert p.apply_flat
    assert build_profile(dev, {"face": "positional"}).buttons[0x133] == "north"
    json.dumps(p.to_json())
    for bad in ({"buttons": {"BTN_C": "jump"}}, {"axes": {"ABS_Z": "sideways"}},
                {"buttons": {"NOT_A_KEY": "south"}}, {"face": "diagonal"},
                {"button": {"BTN_C": "guide"}},                     # typo: silently ignored before
                {"buttons": ["BTN_C"]}, {"axes": {"ABS_Z": {"invert": True}}},
                {"axes": {"ABS_Z": 5}}, {"buttons": {"BTN_C": 3}}):
        with pytest.raises(ValueError):
            build_profile(dev, bad)
    assert build_profile(dev, {"force_gamepad": True}).buttons == build_profile(dev).buttons
    assert build_profile(dev, {"swap_ab": True}).name.endswith("/swapped")


# --- scaling & mapper ---------------------------------------------------------------

def test_axis_scaling():
    u8 = AbsInfo(0, 0, 255, 0, 15)
    assert [scale_stick(v, u8) for v in (0, 128, 255)] == [-32768, 0, 32767]
    assert scale_stick(127, u8) == -256 and scale_stick(140, u8, flat=True) == 0
    s16 = AbsInfo(0, -32767, 32767, 250, 500)
    assert [scale_stick(v, s16) for v in (-32767, 0, 32767)] == [-32768, 0, 32767]
    u16 = AbsInfo(0, 0, 65535)
    assert [scale_stick(v, u16) for v in (0, 32768, 65535)] == [-32768, 0, 32767]
    full = AbsInfo(0, -32768, 32767)
    assert [scale_stick(v, full) for v in (-32768, 0, 32767)] == [-32768, 0, 32767]
    t10 = AbsInfo(0, 0, 1023, 3, 63)
    assert [scale_trigger(v, t10) for v in (0, 1023)] == [0, 32767]
    assert scale_trigger(512, t10) > 16384 > scale_trigger(500, t10)
    assert scale_trigger(60, t10, flat=True) == 0 and scale_trigger(60, t10) > 0
    assert scale_stick(5, AbsInfo(0, 0, 0)) == 0 and scale_trigger(5, AbsInfo(0, 3, 3)) == 0


def test_mapper_batches_until_syn_report():
    dev = node("dualsense_phone_info.txt", "/dev/input/event4")
    m = EvdevMapper(build_profile(dev), dev)
    assert m.load(dev) == [(AXIS, A["left_y"], -256), (AXIS, A["right_x"], 258)]
    m.feed(3, ABS_X, 255)
    m.feed(1, 0x130, 1)
    assert m.axes[A["left_x"]] == 0 and not m.buttons[B["south"]]
    assert m.sync() == [(BUTTON, B["south"], 1), (AXIS, A["left_x"], 32767)]
    assert m.sync() == []
    m.feed(3, ABS_HAT0Y, -1)
    m.feed(3, ABS_RZ, 255)
    m.feed(1, 0x139, 1)                                   # digital R2 ignored: analog exists
    m.discard()                                           # SYN_DROPPED
    assert m.sync() == []
    m.feed(3, ABS_HAT0Y, -1)
    m.feed(3, ABS_RZ, 255)
    m.feed(1, 0x139, 1)
    assert m.sync() == [(BUTTON, B["dpad_up"], 1), (AXIS, A["right_trigger"], 32767)]
    m.feed(3, ABS_HAT0Y, 1)
    assert m.sync() == [(BUTTON, B["dpad_up"], 0), (BUTTON, B["dpad_down"], 1)]
    m.feed(1, BTN_LEFT, 1, companion=True)
    m.feed(1, 0x131, 1)
    assert m.sync(companion=True) == [(BUTTON, B["touchpad"], 1)]
    assert m.sync() == [(BUTTON, B["east"], 1)]
    assert m.reset() == [
        (BUTTON, B["south"], 0), (BUTTON, B["east"], 0), (BUTTON, B["dpad_down"], 0),
        (BUTTON, B["touchpad"], 0), (AXIS, A["left_x"], 0), (AXIS, A["left_y"], 0),
        (AXIS, A["right_x"], 0), (AXIS, A["right_trigger"], 0)]
    assert m.buttons == [0] * len(m.buttons) and m.axes == [0] * len(m.axes)


def test_mapper_digital_triggers_and_initial_state():
    sp = node("switchpro_info.txt", "/dev/input/event8")
    m = EvdevMapper(build_profile(sp), sp)
    m.load(sp)
    m.feed(1, 0x138, 1)
    m.feed(3, ABS_Y, -32767)
    assert m.sync() == [(AXIS, A["left_y"], -32768), (AXIS, A["left_trigger"], 32767)]
    xbox = node("xbox_bt_info.txt", "/dev/input/event5")
    mx = EvdevMapper(build_profile(xbox), xbox)
    changes = dict(((k, i), v) for k, i, v in mx.load(xbox))
    assert changes[(BUTTON, B["right_shoulder"])] == 1                 # '0137*' in getevent -i
    assert changes[(AXIS, A["left_trigger"])] == 32767                  # ABS_BRAKE at 1023
    assert changes[(AXIS, A["left_y"])] == scale_stick(30000, AbsInfo(0, 0, 65535)) < 0
    mx.feed(1, KEY_BACK, 1)
    mx.feed(1, 0x13c, 1)
    mx.feed(1, KEY_HOMEPAGE, 1)
    assert mx.sync() == [(BUTTON, B["back"], 1), (BUTTON, B["guide"], 1)]
    mx.feed(1, 0x13c, 0)                                  # guide still held via KEY_HOMEPAGE
    assert mx.sync() == []


# --- clock --------------------------------------------------------------------------

def test_clock_rejects_transport_jitter():
    rng = random.Random(1)
    off = 123 * S
    c = ClockSync()
    for i in range(2000):
        remote = 5000 * S + i * 4 * MS
        lat = MS + int(rng.expovariate(1 / (6 * MS)))
        local = remote + off + lat
        c.update(remote, local)
        assert c.to_local(remote) <= local
    assert off + MS <= c.offset_ns <= off + 2 * MS


def test_clock_tracks_drift_and_stays_bounded():
    rng = random.Random(2)
    ppm = 200e-6
    c = ClockSync(window_ns=5 * S)
    first = None
    for i in range(12000):                                 # 120 s at 100 Hz
        local_true = i * 10 * MS
        remote = int(local_true * (1 - ppm)) + 777 * S
        local = local_true + 2 * MS + int(rng.random() * 8 * MS)
        c.update(remote, local)
        first = first if first is not None else c.offset_ns
    true_now = 119_990 * MS - int(119_990 * MS * (1 - ppm)) - 777 * S
    # window x drift = 1 ms of lag at most; a fixed first offset would be ~16-24 ms off
    assert abs(c.offset_ns - (true_now + 2 * MS)) < 2 * MS
    assert abs(first - (true_now + 2 * MS)) > 15 * MS


def test_clock_jumps_and_window():
    c = ClockSync(window_ns=10 * S, jump_ns=S, jump_hold_ns=S)
    c.update(1000 * S, 5 * S)
    assert c.offset_ns == -995 * S
    c.update(1003 * S, 6 * S)                              # remote jumped forward 2 s
    assert c.offset_ns == -997 * S
    for k in range(30):                                    # remote jumped back 60 s
        c.update(950 * S + k * 100 * MS, 7 * S + k * 100 * MS)
    assert c.offset_ns == -943 * S and c.resets == 1
    c2 = ClockSync(window_ns=2 * S)
    c2.update(0, 1 * MS)                                    # best sample, then it expires
    for k in range(1, 40):
        c2.update(k * 100 * MS, k * 100 * MS + 5 * MS)
    assert c2.offset_ns == 5 * MS


# --- backend frame handling (no subprocess) ----------------------------------------

class Collector:
    def __init__(self) -> None:
        self.events: list[InputEvent] = []
        self.lock = threading.Lock()

    def __call__(self, ev: InputEvent) -> None:
        with self.lock:
            self.events.append(ev)

    def of(self, dev: int | None = None, kind: str | None = None) -> list[InputEvent]:
        with self.lock:
            return [e for e in self.events if (dev is None or e.device == dev)
                    and (kind is None or e.kind == kind)]


def offline_backend(info: str = "dualsense_phone_info.txt", **kw) -> tuple[AdbBackend, Collector]:
    hub = Hub()
    col = Collector()
    hub.add_sink(col)
    be = AdbBackend(hub, serial="R5CT1234ABC", adb=FAKE, **kw)
    descs = devices(info)
    be._probe = lambda path: [d for d in descs if path is None or d.path == path]
    be._reconcile(descs, 1 * S)
    return be, col


def feed(be: AdbBackend, lines: list[str], rx: int) -> None:
    for i, line in enumerate(lines):
        be._handle_line(line, rx + i)


def test_backend_publishes_at_syn_with_converted_time():
    be, col = offline_backend()
    assert len(col.of(kind=CONNECT)) == 1
    dev = col.of(kind=CONNECT)[0].device
    n0 = len(col.of(dev))
    feed(be, ["[    5000.100000] /dev/input/event4: 0003 0000 000000ff",
              "[    5000.100000] /dev/input/event4: 0001 0130 00000001"], 10 * S)
    assert len(col.of(dev)) == n0                          # nothing before SYN_REPORT
    feed(be, ["[    5000.100000] /dev/input/event4: 0000 0000 00000000"], 10 * S + 50 * MS)
    new = col.of(dev)[n0:]
    assert [(e.kind, e.code, e.value) for e in new] == [(BUTTON, B["south"], 1),
                                                        (AXIS, A["left_x"], 32767)]
    assert all(e.t_ns == 10 * S + 50 * MS for e in new)    # first sample: offset = rx - remote
    feed(be, ["[    5000.300000] /dev/input/event4: 0001 0130 00000000",
              "[    5000.300000] /dev/input/event4: 0000 0000 00000000"], 10 * S + 260 * MS)
    rel = col.of(dev)[-1]
    assert rel.value == 0 and rel.t_ns == 10 * S + 250 * MS  # 200 ms after the press (min filter)


def test_backend_ignores_foreign_nodes_and_merges_touchpad():
    be, col = offline_backend()
    dev = col.of(kind=CONNECT)[0].device
    n0 = len(col.events)
    feed(be, ["[    5000.000000] /dev/input/event6: 0003 0000 ffffff8a",
              "[    5000.000000] /dev/input/event6: 0000 0000 00000000",
              "[    5000.010000] /dev/input/event2: 0003 0039 0000002a",
              "[    5000.010000] /dev/input/event2: 0000 0000 00000000",
              "[    5000.020000] /dev/input/event5: 0003 0000 00000100",
              "[    5000.020000] /dev/input/event5: 0000 0000 00000000"], 3 * S)
    assert len(col.events) == n0
    feed(be, ["[    5000.030000] /dev/input/event4: 0003 0000 00000000",   # gamepad frame open
              "[    5000.030000] /dev/input/event5: 0001 0110 00000001",
              "[    5000.030000] /dev/input/event5: 0000 0000 00000000",   # touchpad frame
              "[    5000.030000] /dev/input/event4: 0000 0000 00000000"], 3 * S + 10 * MS)
    got = [(e.kind, e.code, e.value) for e in col.events[n0:]]
    assert got == [(BUTTON, B["touchpad"], 1), (AXIS, A["left_x"], -32768)]
    assert all(e.device == dev for e in col.events[n0:])


def test_backend_syn_dropped_resyncs_and_time_is_monotonic():
    be, col = offline_backend("xbox_bt_info.txt")
    dev = col.of(kind=CONNECT)[0].device
    assert col.of(dev, BUTTON)[-1].code == B["right_shoulder"]      # initial state published
    feed(be, ["[     900.000000] /dev/input/event5: 0001 0130 00000001",
              "[     900.000000] /dev/input/event5: 0000 0000 00000000"], 20 * S)
    feed(be, ["[     900.100000] /dev/input/event5: 0000 0003 00000000",   # SYN_DROPPED
              "[     900.100000] /dev/input/event5: 0001 0131 00000001",
              "[     900.100000] /dev/input/event5: 0000 0000 00000000"], 20 * S + 101 * MS)
    east = [e for e in col.of(dev, BUTTON) if e.code == B["east"]]
    assert east == []                                       # partial frame discarded
    south = [e for e in col.of(dev, BUTTON) if e.code == B["south"]]
    assert [e.value for e in south] == [1, 0]               # resync: probe says A is up
    feed(be, ["[     899.000000] /dev/input/event5: 0001 0133 00000001",   # clock went back
              "[     899.000000] /dev/input/event5: 0000 0000 00000000"], 20 * S + 200 * MS)
    ts = [e.t_ns for e in col.of(dev)]
    assert ts == sorted(ts)


def test_backend_hotplug_lines():
    be, col = offline_backend()
    sp = devices("switchpro_info.txt")
    base = be._probe
    be._probe = lambda path: [d for d in sp if d.path == path] or base(path)
    feed(be, ["add device 8: /dev/input/event8", '  name:     "Nintendo Switch Pro Controller"',
              "add device 9: /dev/input/event9",
              '  name:     "Nintendo Switch Pro Controller IMU"',
              "[    5000.900000] /dev/input/event8: 0001 0131 00000001",
              "[    5000.900000] /dev/input/event8: 0000 0000 00000000"], 4 * S)
    connects = col.of(kind=CONNECT)
    assert [c.value["name"] for c in connects] == ["DualSense Wireless Controller",
                                                   "Nintendo Switch Pro Controller"]
    sw = connects[1].device
    assert connects[1].value["family"] == "switch"
    assert col.of(sw, BUTTON)[-1].code == B["east"]
    # startup scan duplicates of known nodes do not trigger probes or reconnects
    be._probe = lambda path: pytest.fail("unexpected probe")
    feed(be, ["add device 4: /dev/input/event4", '  name:     "DualSense Wireless Controller"'],
         5 * S)
    feed(be, ["remove device 9: /dev/input/event9", "remove device 8: /dev/input/event8"], 6 * S)
    assert [e.device for e in col.of(kind=DISCONNECT)] == [sw]
    assert col.of(sw, BUTTON)[-1].value == 0                # released on disconnect


# --- adb helpers --------------------------------------------------------------------

@pytest.fixture
def scenario(tmp_path, monkeypatch):
    def make(**cfg) -> Path:
        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        cfg.setdefault("state", str(state))
        f = tmp_path / "scenario.json"
        f.write_text(json.dumps(cfg), encoding="utf-8")
        monkeypatch.setenv("FAKE_ADB_SCENARIO", str(f))
        monkeypatch.delenv("ANDROID_SERIAL", raising=False)
        return state
    return make


def test_parse_adb_devices():
    got = parse_adb_devices((FIX / "devices_many.txt").read_text())
    assert got == [
        ("R5CT1234ABC", "device", "SM S911B"),
        ("192.168.1.50:37845", "device", "Pixel 7"),
        ("0123456789ABCDEF", "unauthorized", ""),
        ("emulator-5554", "offline", ""),
        ("ZY22XXXXXX", "no permissions", ""),
        ("adb-R5CT1234ABC-AbCdEf._adb-tls-connect._tcp", "device", "SM S911B"),
    ]


def test_select_device(scenario):
    scenario(devices="devices_one.txt")
    assert select_device(FAKE) == ("R5CT1234ABC", "SM S911B")
    assert list_adb_devices(FAKE)[0][1] == "device"
    scenario(devices="devices_many.txt")
    with pytest.raises(AdbDeviceError, match="Several"):
        select_device(FAKE)
    assert select_device(FAKE, "192.168.1.50:37845") == ("192.168.1.50:37845", "Pixel 7")
    with pytest.raises(AdbDeviceError, match="Allow USB debugging"):
        select_device(FAKE, "0123456789ABCDEF")
    with pytest.raises(AdbDeviceError, match="udev"):
        select_device(FAKE, "ZY22XXXXXX")
    with pytest.raises(AdbDeviceError, match="not attached"):
        select_device(FAKE, "nope")
    scenario(devices="devices_unauthorized.txt")
    with pytest.raises(AdbDeviceError, match="unauthorized"):
        select_device(FAKE)
    scenario(devices="devices_none.txt")
    with pytest.raises(AdbDeviceError, match="USB debugging"):
        select_device(FAKE)


def test_resolve_adb(monkeypatch, tmp_path):
    assert resolve_adb(FAKE) == FAKE
    monkeypatch.delenv("CONTROLLERLOG_ADB", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(ab, "_adb_candidates", lambda: [tmp_path / "nothing" / "adb"])
    with pytest.raises(AdbNotFoundError, match="platform-tools"):
        resolve_adb()
    with pytest.raises(AdbNotFoundError, match="USB debugging"):
        resolve_adb(str(tmp_path / "missing-adb.exe"))
    monkeypatch.setenv("CONTROLLERLOG_ADB", str(tmp_path / "also-missing"))
    with pytest.raises(AdbNotFoundError, match="CONTROLLERLOG_ADB"):
        resolve_adb()
    exe = tmp_path / ("adb.exe" if sys.platform == "win32" else "adb")
    exe.write_bytes(b"")
    monkeypatch.setenv("CONTROLLERLOG_ADB", str(exe))
    assert [Path(p) for p in resolve_adb()] == [exe]
    monkeypatch.delenv("CONTROLLERLOG_ADB")
    monkeypatch.setattr(ab, "_adb_candidates", lambda: [exe])
    assert [Path(p) for p in resolve_adb()] == [exe]


def test_list_input_devices_and_wireless_helpers(scenario):
    state = scenario(devices="devices_one.txt", info="xbox_bt_info.txt")
    devs = list_input_devices(adb=FAKE)
    assert [d.path for d in devs if d.is_gamepad] == ["/dev/input/event5"]
    assert "connected to 192.168.1.50:5555" in adb_connect("192.168.1.50:5555", FAKE)
    assert "Successfully paired" in adb_pair("192.168.1.50:37001", "123456", FAKE)
    calls = (state / "calls.txt").read_text().splitlines()
    assert "-s R5CT1234ABC shell getevent -i" in calls
    assert calls.count("start-server") >= 3


# --- full runs against the fake adb -------------------------------------------------

def wait_for(pred, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def run_backend(**kw) -> tuple[AdbBackend, Collector]:
    hub = Hub()
    col = Collector()
    hub.add_sink(col)
    be = AdbBackend(hub, adb=FAKE, **kw)
    be.start()
    assert be.ready.wait(15), "backend never became ready"
    return be, col


def test_end_to_end_dualsense_with_switch_hotplug(scenario):
    state = scenario(devices="devices_one.txt", info="dualsense_phone_info.txt",
                     probe=["switchpro_info.txt"], streams=["dualsense_stream.txt"])
    be, col = run_backend()
    try:
        assert be.error is None
        ok = wait_for(lambda: any(e.kind == BUTTON and e.code == B["guide"] and e.value == 1
                                  for e in col.of()))
        assert ok, [e.to_row() for e in col.of()]
    finally:
        be.stop()
    assert not be._thread.is_alive()
    assert be._proc is None
    connects = col.of(kind=CONNECT)
    assert [c.value["name"] for c in connects] == ["DualSense Wireless Controller",
                                                   "Nintendo Switch Pro Controller"]
    ds, sw = connects[0].device, connects[1].device
    info = connects[0].value
    assert info["backend"] == "adb" and info["family"] == "playstation"
    assert (info["vendor_id"], info["product_id"]) == (0x054c, 0x0ce6)
    assert info["connection"] == "wireless" and info["serial"] == "a0:ab:51:12:34:56"
    assert info["path"] == "/dev/input/event4" and info["extra"]["adb_serial"] == "R5CT1234ABC"
    assert info["extra"]["sdl_type_guess"] == "ps5"

    def seq(dev, name, kind=BUTTON):
        idx = B[name] if kind == BUTTON else A[name]
        return [e for e in col.of(dev, kind) if e.code == idx]

    assert [e.value for e in seq(ds, "south")] == [1, 0]
    assert [e.value for e in seq(ds, "right_trigger", AXIS)] == [16448, 32767, 0]
    assert [e.value for e in seq(ds, "dpad_up")] == [1, 0]
    assert [e.value for e in seq(ds, "touchpad")] == [1, 0]
    assert [e.value for e in seq(ds, "left_x", AXIS)] == [32767, 0]     # + release at stop
    assert [e.value for e in seq(ds, "left_y", AXIS)] == [-256, 0]
    assert seq(ds, "right_shoulder") == []                               # 0x139 is R2, not R1
    assert [e.value for e in seq(sw, "east")] == [1, 0]
    assert [e.value for e in seq(sw, "left_trigger", AXIS)] == [32767, 0]
    assert [e.value for e in seq(sw, "left_x", AXIS)] == [-32768, 0]
    assert connects[1].value["family"] == "switch"
    disc = col.of(kind=DISCONNECT)
    assert [e.device for e in disc] == [sw, ds]                          # hot-unplug, then stop
    for dev in (ds, sw):
        ts = [e.t_ns for e in col.of(dev)]
        assert ts == sorted(ts)
    press, release = seq(ds, "south")
    assert 100 * MS < release.t_ns - press.t_ns < 220 * MS               # 150 ms on the phone
    calls = (state / "calls.txt").read_text().splitlines()
    assert "-s R5CT1234ABC shell getevent -i /dev/input/event8" in calls
    assert "-s R5CT1234ABC shell getevent -i /dev/input/event4" not in calls
    # the stream runs under a PTY (fake adb then emits "\r\n" like a real one); without it
    # getevent's stdout is block-buffered on the phone and events arrive in bursts
    assert "-s R5CT1234ABC shell -tt getevent -t" in calls
    assert be.stats()["frames"] > 10 and be.clock.samples > 10


def test_restart_after_stream_crash_keeps_device(scenario):
    scenario(devices="devices_one.txt", info="dualsense_phone_info.txt",
             streams=["crash_stream_a.txt", "crash_stream_b.txt"])
    be, col = run_backend()
    try:
        ok = wait_for(lambda: any(e.code == B["north"] and e.kind == BUTTON and e.value == 0
                                  for e in col.of()))
        assert ok, [e.to_row() for e in col.of()]
        assert col.of(kind=DISCONNECT) == []
        assert be.restarts >= 1
    finally:
        be.stop()
    ds = col.of(kind=CONNECT)[0].device
    assert len(col.of(kind=CONNECT)) == 1
    east = [e.value for e in col.of(ds, BUTTON) if e.code == B["east"]]
    assert east == [1, 0]                                   # released when the stream died
    north = [e.value for e in col.of(ds, BUTTON) if e.code == B["north"]]
    assert north == [1, 0]


def test_device_unplugged_disconnects_and_keeps_retrying(scenario):
    scenario(devices="devices_one.txt", info="dualsense_phone_info.txt",
             streams=["crash_stream_a.txt"], fail_after_streams=1)
    be, col = run_backend()
    try:
        assert wait_for(lambda: len(col.of(kind=DISCONNECT)) == 1)
        assert be.error is None and be._thread.is_alive()
    finally:
        be.stop()
    assert not be._thread.is_alive()


def test_device_filter_selects_by_name(scenario):
    scenario(devices="devices_one.txt", info="dualsense_phone_info.txt",
             probe=["switchpro_info.txt"], streams=["dualsense_stream.txt"])
    be, col = run_backend(device_filter="pro controller", nintendo_swap=True)
    try:
        assert wait_for(lambda: len(col.of(kind=DISCONNECT)) == 1)
    finally:
        be.stop()
    connects = col.of(kind=CONNECT)
    assert [c.value["name"] for c in connects] == ["Nintendo Switch Pro Controller"]
    sw = connects[0].device
    assert [e.value for e in col.of(sw, BUTTON) if e.code == B["south"]] == [1, 0]  # A, swapped


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
@pytest.mark.parametrize("devices_file, exc, text", [
    ("devices_none.txt", AdbDeviceError, "No Android device"),
    ("devices_unauthorized.txt", AdbDeviceError, "Allow USB debugging"),
])
def test_startup_errors_surface_in_backend_error(scenario, devices_file, exc, text):
    scenario(devices=devices_file)
    hub = Hub()
    be = AdbBackend(hub, adb=FAKE)
    be.start()
    assert be.ready.wait(15)
    be._thread.join(5)
    assert isinstance(be.error, exc) and text in str(be.error)
    assert hub.devices == {}


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_missing_adb_surfaces_in_backend_error(tmp_path):
    be = AdbBackend(Hub(), adb=str(tmp_path / "no-adb.exe"))
    be.start()
    assert be.ready.wait(5)
    be._thread.join(5)
    assert isinstance(be.error, AdbNotFoundError) and "platform-tools" in str(be.error)


# --- review regressions ---------------------------------------------------------------

def test_current_adb_error_prefix_is_an_error(scenario):
    import subprocess
    for err in ("adb: device unauthorized.\nThis adb server's $ADB_VENDOR_KEYS is not set\n",
                "adb: no devices/emulators found\n", "error: device offline\n"):
        with pytest.raises(ab.AdbError, match="device"):
            ab._check_getevent(subprocess.CompletedProcess([], 1, "", err), "X")
    # getevent's own messages are not adb failures
    assert ab._adb_failure("could not open /dev/input/event9, No such file or directory") is None
    scenario(devices="devices_one.txt", fail_after_streams=0,
             device_error="adb: device unauthorized.")
    with pytest.raises(ab.AdbError, match="unauthorized"):   # used to return [] silently
        list_input_devices(adb=FAKE)


def test_probe_refuses_node_paths_that_are_not_plain_nodes():
    be = AdbBackend(Hub(), serial="S", adb=FAKE)
    be._cmd = FAKE
    for path in ("/dev/input/event8;reboot", "/dev/input/$(id)", "/sdcard/x"):
        with pytest.raises(ab.AdbError, match="unexpected"):
            be._probe(path)


def test_invalid_override_rejected_when_backend_is_built():
    with pytest.raises(ValueError, match="jump"):
        AdbBackend(Hub(), adb=FAKE, profile_override={"buttons": {"BTN_C": "jump"}})
    with pytest.raises(ValueError, match="unknown key"):
        AdbBackend(Hub(), adb=FAKE, profile_override={"face_convention": "xbox"})
    AdbBackend(Hub(), adb=FAKE, device_filter="x", profile_override={"force_gamepad": True})


def test_keyboard_key_names():
    kc = ab.KEY_CODES
    assert (kc["KEY_ESC"], kc["KEY_1"], kc["KEY_0"], kc["KEY_Q"], kc["KEY_A"], kc["KEY_C"],
            kc["KEY_Z"], kc["KEY_M"], kc["KEY_SPACE"], kc["KEY_F1"], kc["KEY_F10"],
            kc["KEY_KPDOT"], kc["KEY_F12"], kc["KEY_UP"]) == (
        1, 2, 11, 16, 30, 46, 44, 50, 57, 59, 68, 83, 88, 103)
    assert ab.evdev_code(1, "key_c") == 46


def test_force_gamepad_keyboard_mode_pad_but_not_sensor_nodes():
    be, col = offline_backend(device_filter="dualsense", profile_override={"force_gamepad": True})
    assert list(be.pads) == ["/dev/input/event4"]            # not touchpad / motion / jack
    kb = EvdevDevice("/dev/input/event9", name="8BitDo Zero 2 gamepad Keyboard", bus=5,
                     vendor=0x2dc8, product=0x3230, keys={46, 32, 18, 33, 34, 36})
    hub = Hub()
    col = Collector()
    hub.add_sink(col)
    be = AdbBackend(hub, serial="S", adb=FAKE, device_filter="zero 2", profile_override={
        "force_gamepad": True, "buttons": {"KEY_C": "dpad_up", "KEY_D": "dpad_down",
                                           "KEY_G": "east", "KEY_J": "south"}})
    be._probe = lambda path: [kb]
    be._reconcile([kb], 1 * S)
    assert list(be.pads) == ["/dev/input/event9"]
    feed(be, ["[ 10.000000] /dev/input/event9: 0001 002e 00000001",
              "[ 10.000000] /dev/input/event9: 0000 0000 00000000"], 2 * S)
    assert [(e.kind, e.code, e.value) for e in col.of(kind=BUTTON)] == [(BUTTON, B["dpad_up"], 1)]


def test_consumer_control_node_merges_home_and_back():
    be, col = offline_backend("generic_bt_info.txt")
    dev = col.of(kind=CONNECT)[0].device
    assert be._companions == {"/dev/input/event7": "/dev/input/event6"}
    feed(be, ["[ 100.000000] /dev/input/event7: 0001 00ac 00000001",    # AC Home
              "[ 100.000000] /dev/input/event7: 0001 0073 00000001",    # volume: not mapped
              "[ 100.000000] /dev/input/event7: 0000 0000 00000000"], 2 * S)
    assert [(e.kind, e.code, e.value) for e in col.of(dev, BUTTON)] == [(BUTTON, B["guide"], 1)]
    # re-reading the gamepad node (resync) keeps the key held on the sibling node
    pad = be._pads["/dev/input/event6"]
    assert pad.mapper.load(node("generic_bt_info.txt", "/dev/input/event6")) == []
    feed(be, ["[ 100.200000] /dev/input/event7: 0001 009e 00000001",    # AC Back
              "[ 100.200000] /dev/input/event7: 0000 0000 00000000",
              "[ 100.300000] /dev/input/event7: 0001 00ac 00000000",
              "[ 100.300000] /dev/input/event7: 0001 009e 00000000",
              "[ 100.300000] /dev/input/event7: 0000 0000 00000000"], 4 * S)
    got = [(e.code, e.value) for e in col.of(dev, BUTTON)]
    assert got == [(B["guide"], 1), (B["back"], 1), (B["back"], 0), (B["guide"], 0)]


def test_consumer_control_of_another_controller_is_not_merged():
    descs = devices("generic_bt_info.txt")
    other = next(d for d in descs if d.path == "/dev/input/event7")
    other.uniq = "11:22:33:44:55:66"
    be = AdbBackend(Hub(), serial="S", adb=FAKE)
    be._probe = lambda path: descs
    be._reconcile(descs, 1 * S)
    assert list(be.pads) == ["/dev/input/event6"] and be._companions == {}


def test_hotplugged_pad_frames_keep_their_own_time(monkeypatch):
    be, col = offline_backend()
    sp = devices("switchpro_info.txt")
    base = be._probe
    fake_now = [0]

    def slow_probe(path):
        fake_now[0] = 10_400 * MS                           # the probe returns at 10.4 s
        return [d for d in sp if d.path == path] or base(path)

    be._probe = slow_probe
    monkeypatch.setattr(ab, "now_ns", lambda: fake_now[0])
    feed(be, ["[    5000.000000] /dev/input/event4: 0000 0000 00000000"], 10 * S)
    feed(be, ["add device 8: /dev/input/event8"], 10_100 * MS)
    feed(be, ['  name:     "Nintendo Switch Pro Controller"'], 10_100 * MS)
    # a frame from 5000.150 queued behind the probe and is read at 10.5 s
    feed(be, ["[    5000.150000] /dev/input/event8: 0001 0131 00000001",
              "[    5000.150000] /dev/input/event8: 0000 0000 00000000"], 10_500 * MS)
    sw = col.of(kind=CONNECT)[1]
    assert sw.t_ns == 10_100 * MS                           # when the add line arrived
    press = col.of(sw.device, BUTTON)[-1]
    assert press.code == B["east"] and press.t_ns == 10_150 * MS   # was clamped to 10.4 s
    feed(be, ["remove device 8: /dev/input/event8"], 11 * S)
    assert col.of(kind=DISCONNECT)[-1].t_ns == 11 * S


def test_adb_connect_and_pair_failures(monkeypatch):
    import subprocess
    monkeypatch.setattr(ab, "_start_server", lambda cmd: None)

    def answer(text: str, rc: int = 0) -> None:
        monkeypatch.setattr(ab, "_run", lambda cmd, timeout: subprocess.CompletedProcess(
            cmd, rc, text + "\n", ""))

    for text in ("unable to connect to 192.168.1.50:5555: Connection refused",   # older adb, rc 0
                 "failed to connect to '192.168.1.50:5555': Connection refused",
                 "failed to authenticate to 192.168.1.50:5555", ""):
        answer(text)
        with pytest.raises(ab.AdbError):
            adb_connect("192.168.1.50:5555", ["adb"])
    answer("already connected to 192.168.1.50:5555")
    assert adb_connect("192.168.1.50:5555", ["adb"]).startswith("already")
    answer("Failed: Wrong password or connection was dropped.")
    with pytest.raises(ab.AdbError):
        adb_pair("192.168.1.50:37001", "000000", ["adb"])
    answer("adb: usage: unknown command pair")
    with pytest.raises(ab.AdbError):
        adb_pair("192.168.1.50:37001", "000000", ["adb"])


def test_stop_before_the_stream_starts_still_sets_ready(scenario):
    scenario(devices="devices_one.txt", info="dualsense_phone_info.txt")
    hub = Hub()
    be = AdbBackend(hub, adb=FAKE)
    be.start()
    be.stop()
    be._thread.join(15)
    assert not be._thread.is_alive()
    assert be.ready.is_set() and be.error is None
    assert hub.devices == {}


class RecordingEvent(threading.Event):
    def __init__(self) -> None:
        super().__init__()
        self.waits: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        return super().wait(timeout)


def test_restart_is_immediate_after_a_healthy_session(scenario):
    scenario(devices="devices_one.txt", info="dualsense_phone_info.txt",
             streams=["crash_stream_a.txt", "crash_stream_b.txt"])
    hub = Hub()
    col = Collector()
    hub.add_sink(col)
    be = AdbBackend(hub, adb=FAKE)
    be.healthy_session_s = 0.05          # stream A runs ~0.2 s: counts as healthy here
    be._stop = RecordingEvent()
    be.start()
    try:
        assert wait_for(lambda: any(e.code == B["north"] and e.kind == BUTTON and e.value == 0
                                    for e in col.of()))
    finally:
        be.stop()
    assert be._stop.waits[0] == 0.0
    assert len(col.of(kind=CONNECT)) == 1


def test_line_handler_errors_do_not_end_capture(scenario, monkeypatch):
    scenario(devices="devices_one.txt", info="dualsense_phone_info.txt",
             probe=["switchpro_info.txt"], streams=["dualsense_stream.txt"])
    orig = AdbBackend._device_added

    def broken(self, path, name, t):
        if path == "/dev/input/event8":
            raise RuntimeError("simulated bug")
        return orig(self, path, name, t)

    monkeypatch.setattr(AdbBackend, "_device_added", broken)
    be, col = run_backend()
    try:
        assert wait_for(lambda: any(e.kind == BUTTON and e.code == B["guide"] and e.value == 1
                                    for e in col.of())), "capture died after the error"
        assert be._thread.is_alive() and be.error is None
    finally:
        be.stop()
    assert be.errors == 1 and be.stats()["errors"] == 1
    assert [c.value["name"] for c in col.of(kind=CONNECT)] == ["DualSense Wireless Controller"]


def test_clock_carries_the_minimum_across_idle_gaps():
    rng = random.Random(3)
    off = 777 * S
    c = ClockSync()                       # 10 s window, 100 ppm drift allowance
    first_errs = []
    t = 0
    for burst in range(20):               # 3 s of play, then 30 s with no frame at all
        for i in range(180):
            remote = t + i * 16_666_667
            local = remote + off + 2 * MS + int(rng.expovariate(1 / (15 * MS)))   # Wi-Fi jitter
            c.update(remote, local)
            assert c.to_local(remote) <= local                  # never later than receipt
            if i == 0 and burst:
                first_errs.append(c.to_local(remote) - (remote + off + 2 * MS))
        t += 33 * S
    # without the carry the first frame after a gap had only its own delay: p90 ~40 ms
    assert max(first_errs) < 4 * MS


def test_clock_carry_is_dropped_on_a_jump_across_a_gap():
    c = ClockSync(window_ns=10 * S)
    for k in range(10):
        c.update(1000 * S + k * S, 5 * S + k * S + MS)         # offset -995 s + 1 ms
    c.update(100 * S, 60 * S + 3 * MS)                          # idle 45 s, remote went back
    assert c.offset_ns == -40 * S + 3 * MS                      # new clock, not the old bound
    c2 = ClockSync(window_ns=10 * S)
    for k in range(10):
        c2.update(k * S, k * S + MS)
    c2.update(60 * S, 60 * S + 30 * MS)                         # after the gap: delayed sample
    age = (60 * S + 30 * MS) - (9 * S + MS)                     # carried sample was at 9.001 s
    assert c2.offset_ns == MS + int(age * 100e-6)               # carried bound wins (6.1 ms)
    for k in range(1, 12):                                      # refills; carry expires
        c2.update(60 * S + k * S, 60 * S + k * S + 2 * MS)
    assert c2.offset_ns == 2 * MS
