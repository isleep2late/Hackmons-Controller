"""Switch 2 BLE backend: report parsing, calibration, commands, discovery, backend."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import struct
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from controllerlog.hub import Hub
from controllerlog.input import switch2_ble as sw2
from controllerlog.model import (AXIS, AXIS_INDEX, BUTTON, BUTTON_INDEX, BUTTONS, CONNECT,
                                 DISCONNECT, FAMILY_GAMECUBE, FAMILY_SWITCH, PadState)

FIXTURES = Path(__file__).parent / "fixtures" / "switch2"
DOC = json.loads((FIXTURES / "doc_vectors.json").read_text(encoding="utf-8"))


def _hex(s: str) -> bytes:
    return bytes.fromhex(s)


# --- report builders written straight from ndeadly's hid_reports.md tables ----------

# Report 0x05 button bitfield: byte*8 + bit index (Byte 0: Y X B A SR SL R ZR ...).
DOC_BITS_05 = {
    "Y": 0, "X": 1, "B": 2, "A": 3, "SR_R": 4, "SL_R": 5, "R": 6, "ZR": 7,
    "MINUS": 8, "PLUS": 9, "RSTICK": 10, "LSTICK": 11, "HOME": 12, "CAPTURE": 13, "C": 14,
    "DOWN": 16, "UP": 17, "RIGHT": 18, "LEFT": 19, "SR_L": 20, "SL_L": 21, "L": 22, "ZL": 23,
    "GR": 24, "GL": 25,
}
# Model-specific reports: name -> (byte offset in the BLE report, mask).
DOC_BITS_MODEL = {
    0x09: {"B": (2, 0x01), "A": (2, 0x02), "Y": (2, 0x04), "X": (2, 0x08), "R": (2, 0x10),
           "ZR": (2, 0x20), "PLUS": (2, 0x40), "RSTICK": (2, 0x80), "DOWN": (3, 0x01),
           "RIGHT": (3, 0x02), "LEFT": (3, 0x04), "UP": (3, 0x08), "L": (3, 0x10),
           "ZL": (3, 0x20), "MINUS": (3, 0x40), "LSTICK": (3, 0x80), "HOME": (4, 0x01),
           "CAPTURE": (4, 0x02), "GR": (4, 0x04), "GL": (4, 0x08), "C": (4, 0x10)},
    # GameCube: the doc's "Z" is the 0x05 layout's ZR slot, its "R"/"L" are the clicks.
    0x0A: {"B": (2, 0x01), "A": (2, 0x02), "Y": (2, 0x04), "X": (2, 0x08), "ZR": (2, 0x10),
           "R": (2, 0x20), "PLUS": (2, 0x40), "DOWN": (3, 0x01), "RIGHT": (3, 0x02),
           "LEFT": (3, 0x04), "UP": (3, 0x08), "ZL": (3, 0x10), "L": (3, 0x20),
           "HOME": (4, 0x01), "CAPTURE": (4, 0x02), "C": (4, 0x10)},
    0x07: {"DOWN": (2, 0x01), "RIGHT": (2, 0x02), "LEFT": (2, 0x04), "UP": (2, 0x08),
           "L": (2, 0x10), "ZL": (2, 0x20), "MINUS": (2, 0x40), "LSTICK": (2, 0x80),
           "CAPTURE": (3, 0x01), "SR_L": (3, 0x40), "SL_L": (3, 0x80)},
    0x08: {"B": (2, 0x01), "A": (2, 0x02), "Y": (2, 0x04), "X": (2, 0x08), "R": (2, 0x10),
           "ZR": (2, 0x20), "PLUS": (2, 0x40), "RSTICK": (2, 0x80), "HOME": (3, 0x01),
           "C": (3, 0x10), "SR_R": (3, 0x40), "SL_R": (3, 0x80)},
}
MODEL_REPORT = {"pro": 0x09, "gamecube": 0x0A, "joycon_l": 0x07, "joycon_r": 0x08}


def pack12(a: int, b: int) -> bytes:
    return bytes((a & 0xFF, ((a >> 8) & 0x0F) | ((b & 0x0F) << 4), (b >> 4) & 0xFF))


def common_report(buttons=(), left=(2048, 2048), right=(2048, 2048), triggers=(0, 0),
                  counter=0, battery_mv=3700) -> bytes:
    r = bytearray(63)
    mask = 0
    for name in buttons:
        mask |= 1 << DOC_BITS_05[name]
    struct.pack_into("<II", r, 0, counter, mask)
    r[0x0A:0x0D] = pack12(*left)
    r[0x0D:0x10] = pack12(*right)
    struct.pack_into("<H", r, 0x1F, battery_mv)
    r[0x3C], r[0x3D] = triggers
    return bytes(r)


def model_report(report_id: int, buttons=(), left=(2048, 2048), right=(2048, 2048),
                 triggers=(0, 0), counter=0, power=0x18) -> bytes:
    """Model-specific report; single-stick Joy-Con reports put their stick at 5."""
    r = bytearray(63)
    r[0], r[1] = counter & 0xFF, power
    for name in buttons:
        off, mask = DOC_BITS_MODEL[report_id][name]
        r[off] |= mask
    if report_id in (0x07, 0x08):
        r[4] = 0x07
        r[5:8] = pack12(*(left if report_id == 0x07 else right))
    else:
        r[5:8] = pack12(*left)
        r[8:11] = pack12(*right)
    if report_id == 0x0A:
        r[0x0C], r[0x0D] = triggers
    return bytes(r)


NO_DZ = sw2.Calibration(deadzone=0.0)                # 1610 spans (Pro default)
JCL = sw2.Calibration.default("joycon_l", deadzone=0.0)  # 1200 spans
JCR = sw2.Calibration.default("joycon_r", deadzone=0.0)


def pressed(rep) -> set[str]:
    return {BUTTONS[i] for i, v in enumerate(rep.buttons) if v}


def axis(rep, name: str) -> int:
    return rep.axes[AXIS_INDEX[name]]


# --- canonical button mapping ---------------------------------------------------------

DPAD = {"UP": "dpad_up", "DOWN": "dpad_down", "LEFT": "dpad_left", "RIGHT": "dpad_right"}
EXPECT = {
    ("pro", None): {"B": "south", "A": "east", "Y": "west", "X": "north", "MINUS": "back",
                    "HOME": "guide", "PLUS": "start", "LSTICK": "left_stick",
                    "RSTICK": "right_stick", "L": "left_shoulder", "R": "right_shoulder",
                    "CAPTURE": "misc1", "C": "misc2", "GR": "right_paddle1",
                    "GL": "left_paddle1", **DPAD},
    ("gamecube", None): {"A": "south", "B": "west", "X": "east", "Y": "north",
                         "PLUS": "start", "HOME": "guide", "ZR": "right_shoulder",
                         "ZL": "left_shoulder", "L": "misc3", "R": "misc4",
                         "CAPTURE": "misc1", "C": "misc2", **DPAD},
    ("joycon_r", "horizontal"): {"A": "south", "X": "east", "B": "west", "Y": "north",
                                 "SL_R": "left_shoulder", "SR_R": "right_shoulder",
                                 "PLUS": "start", "RSTICK": "left_stick", "HOME": "guide",
                                 "C": "misc2", "R": "right_paddle1", "ZR": "right_paddle2"},
    ("joycon_l", "horizontal"): {"LEFT": "south", "DOWN": "east", "UP": "west",
                                 "RIGHT": "north", "SL_L": "left_shoulder",
                                 "SR_L": "right_shoulder", "MINUS": "start",
                                 "LSTICK": "left_stick", "CAPTURE": "guide",
                                 "L": "left_paddle1", "ZL": "left_paddle2"},
    ("joycon_r", "vertical"): {"B": "south", "A": "east", "Y": "west", "X": "north",
                               "PLUS": "start", "RSTICK": "right_stick", "HOME": "guide",
                               "C": "misc2", "R": "right_shoulder", "GR": "right_paddle1"},
    ("joycon_l", "vertical"): {"MINUS": "back", "LSTICK": "left_stick", "CAPTURE": "misc1",
                               "L": "left_shoulder", "GL": "left_paddle1", **DPAD},
}
EXPECT_TRIGGERS = {
    ("pro", None): {"ZL": "left_trigger", "ZR": "right_trigger"},
    ("joycon_r", "vertical"): {"ZR": "right_trigger"},
    ("joycon_l", "vertical"): {"ZL": "left_trigger"},
}
BUTTON_CASES = [(m, o, name, target) for (m, o), table in EXPECT.items()
                for name, target in table.items()]


@pytest.mark.parametrize("model,orientation,name,target", BUTTON_CASES)
def test_common_report_button_mapping(model, orientation, name, target):
    rep = sw2.parse_input_report(model, common_report([name]), NO_DZ, orientation=orientation)
    assert pressed(rep) == {target}
    assert rep.raw_buttons == 1 << DOC_BITS_05[name]
    assert all(v == 0 for v in rep.axes)


@pytest.mark.parametrize("key,name,target", [(k, n, t) for k, tbl in EXPECT_TRIGGERS.items()
                                             for n, t in tbl.items()])
def test_digital_zl_zr_drive_trigger_axes(key, name, target):
    model, orientation = key
    rep = sw2.parse_input_report(model, common_report([name]), NO_DZ, orientation=orientation)
    assert pressed(rep) == set()
    assert axis(rep, target) == 32767
    assert sum(1 for v in rep.axes if v) == 1


def test_unmapped_bits_are_kept_raw_only():
    # SL/SR on a Pro controller report do not exist; Headset is informational.
    rep = sw2.parse_input_report("pro", common_report(["SL_R", "SR_L"]), NO_DZ)
    assert pressed(rep) == set()
    assert set(rep.pressed_bits()) == {"SL_R", "SR_L"}


@pytest.mark.parametrize("model", ["pro", "gamecube", "joycon_l", "joycon_r"])
def test_model_specific_reports_match_common_report(model):
    rid = MODEL_REPORT[model]
    for orientation in ((None,) if model in ("pro", "gamecube") else sw2.ORIENTATIONS):
        for name in DOC_BITS_MODEL[rid]:
            a = sw2.parse_input_report(model, common_report([name]), NO_DZ,
                                       orientation=orientation)
            b = sw2.parse_input_report(model, model_report(rid, [name]), NO_DZ,
                                       report=rid, orientation=orientation)
            assert b.raw_buttons == a.raw_buttons, (model, name)
            assert (b.buttons, b.axes) == (a.buttons, a.axes), (model, orientation, name)


def test_neutral_report_is_all_zero():
    for model in sw2.MODELS:
        rep = sw2.parse_input_report(model, common_report(triggers=(30, 30)))
        assert not any(rep.buttons) and not any(rep.axes), model
        assert rep.pad_state() == PadState()


# --- sticks -----------------------------------------------------------------------------

def test_twelve_bit_stick_unpacking():
    rep = sw2.parse_input_report("pro", common_report(left=(0x123, 0xABC), right=(0xFED, 0x001)))
    assert rep.raw_sticks == (0x123, 0xABC, 0xFED, 0x001)
    assert sw2.unpack_u12_pair(pack12(0x7B3, 0x836)) == (0x7B3, 0x836)
    assert sw2.pack_u12_pair(0x7B3, 0x836) == pack12(0x7B3, 0x836)


def test_pro_stick_directions_default_calibration():
    full = 1610
    p = lambda **kw: sw2.parse_input_report("pro", common_report(**kw), NO_DZ)  # noqa: E731
    r = p(left=(2048 + full, 2048))
    assert (axis(r, "left_x"), axis(r, "left_y")) == (32767, 0)
    r = p(left=(2048, 2048 + full))                      # raw y up -> canonical negative
    assert (axis(r, "left_x"), axis(r, "left_y")) == (0, -32767)
    r = p(left=(2048, 2048 - full))
    assert axis(r, "left_y") == 32767
    r = p(right=(2048 - full, 2048 + full))
    assert (axis(r, "right_x"), axis(r, "right_y")) == (-32767, -32767)
    r = p(left=(2048 + full // 2, 2048))
    assert abs(axis(r, "left_x") - 16384) <= 12
    r = p(left=(4095, 0))                                # beyond range clamps
    assert (axis(r, "left_x"), axis(r, "left_y")) == (32767, 32767)


def test_radial_deadzone_zeroes_centre_and_rescales():
    cal = sw2.Calibration(deadzone=0.03)
    r = sw2.parse_input_report("pro", common_report(left=(2048 + 30, 2048 - 20)), cal)
    assert axis(r, "left_x") == 0 and axis(r, "left_y") == 0
    r = sw2.parse_input_report("pro", common_report(left=(2048 + 1610, 2048)), cal)
    assert axis(r, "left_x") == 32767
    r = sw2.parse_input_report("pro", common_report(left=(2048 + 805, 2048)), cal)
    assert abs(axis(r, "left_x") - round((0.5 - 0.03) / 0.97 * 32767)) <= 12
    assert sw2.apply_radial_deadzone(0.02, 0.02, 0.03) == (0.0, 0.0)
    x, y = sw2.apply_radial_deadzone(0.6, 0.8, 0.0)
    assert (x, y) == (0.6, 0.8)


def test_joycon_horizontal_stick_rotation():
    s = 1200   # Joy-Con default span
    # Right Joy-Con sideways (rotated clockwise): its stick reports in the right slot.
    r = sw2.parse_input_report("joycon_r", common_report(right=(2048, 2048 + s)), JCR)
    assert (axis(r, "left_x"), axis(r, "left_y")) == (32767, 0)      # "up" points right
    r = sw2.parse_input_report("joycon_r", common_report(right=(2048 + s, 2048)), JCR)
    assert (axis(r, "left_x"), axis(r, "left_y")) == (0, 32767)      # "right" points down
    # The unused slot is ignored.
    r = sw2.parse_input_report("joycon_r", common_report(left=(0, 4095)), JCR)
    assert not any(r.axes)
    # Left Joy-Con sideways (rotated counter-clockwise).
    r = sw2.parse_input_report("joycon_l", common_report(left=(2048, 2048 + s)), JCL)
    assert (axis(r, "left_x"), axis(r, "left_y")) == (-32767, 0)     # "up" points left
    r = sw2.parse_input_report("joycon_l", common_report(left=(2048 + s, 2048)), JCL)
    assert (axis(r, "left_x"), axis(r, "left_y")) == (0, -32767)     # "right" points up


def test_joycon_vertical_sticks_keep_their_side():
    s = 1200
    r = sw2.parse_input_report("joycon_r", common_report(right=(2048 + s, 2048 + s)), JCR,
                               orientation="vertical")
    assert (axis(r, "right_x"), axis(r, "right_y")) == (32767, -32767)
    assert axis(r, "left_x") == 0
    r = sw2.parse_input_report("joycon_l", common_report(left=(2048 - s, 2048 - s)), JCL,
                               orientation="vertical")
    assert (axis(r, "left_x"), axis(r, "left_y")) == (-32767, 32767)


def test_joycon_model_report_stick_matches_common():
    for model, slot in (("joycon_l", "left"), ("joycon_r", "right")):
        stick = (2048 + 600, 2048 - 300)
        kw = {slot: stick}
        a = sw2.parse_input_report(model, common_report(**kw), NO_DZ)
        b = sw2.parse_input_report(model, model_report(MODEL_REPORT[model], **kw), NO_DZ,
                                   report=MODEL_REPORT[model])
        assert a.axes == b.axes and any(a.axes)


# --- GameCube triggers ---------------------------------------------------------------------

def test_gamecube_analog_triggers():
    p = lambda t, cal=None: sw2.parse_input_report(  # noqa: E731
        "gamecube", common_report(triggers=t), cal or NO_DZ)
    assert axis(p((30, 30)), "left_trigger") == 0
    r = p((232, 131))
    assert axis(r, "left_trigger") == 32767
    assert abs(axis(r, "right_trigger") - 16384) <= 1
    assert axis(p((5, 255)), "left_trigger") == 0
    assert axis(p((5, 255)), "right_trigger") == 32767
    cal = sw2.Calibration(left_trigger_zero=40, right_trigger_zero=20, deadzone=0)
    r = p((40, 20), cal)
    assert axis(r, "left_trigger") == 0 and axis(r, "right_trigger") == 0
    # Analog value and the digital full-pull click are independent.
    r = sw2.parse_input_report("gamecube", common_report(["L"], triggers=(232, 30)), NO_DZ)
    assert pressed(r) == {"misc3"} and axis(r, "left_trigger") == 32767
    # Model-specific report 0x0A carries the triggers at 0x0C/0x0D.
    r = sw2.parse_input_report("gamecube", model_report(0x0A, triggers=(232, 131)), NO_DZ,
                               report=0x0A)
    assert r.raw_triggers == (232, 131) and axis(r, "left_trigger") == 32767


def test_status_fields():
    r = sw2.parse_input_report("pro", common_report(counter=0x01020304, battery_mv=3374))
    assert r.counter == 0x01020304 and r.battery_mv == 3374
    r = sw2.parse_input_report("joycon_r", model_report(0x08, counter=0x42, power=0x1B),
                               report=0x08)
    assert (r.counter, r.external_power, r.charging, r.battery_level) == (0x42, True, True, 6)


def test_parse_errors():
    with pytest.raises(ValueError):
        sw2.parse_input_report("pro", b"\x00" * 10)
    with pytest.raises(ValueError):
        sw2.parse_input_report("joycon_r", model_report(0x09), report=0x09)
    with pytest.raises(ValueError):
        sw2.parse_input_report("snes", common_report())
    with pytest.raises(ValueError):
        sw2.parse_input_report("joycon_l", common_report(), orientation="diagonal")


def test_model_names_and_aliases():
    assert sw2.normalize_model("Joy-Con L") == "joycon_l"
    assert sw2.normalize_model("jcr") == "joycon_r"
    assert sw2.normalize_model("0x2069") == "pro"
    assert sw2.normalize_model("GC") == "gamecube"
    assert sw2.normalize_model(None) is None
    assert sw2.MODEL_BY_PID == {0x2069: "pro", 0x2066: "joycon_r", 0x2067: "joycon_l",
                                0x2073: "gamecube"}
    assert sw2.MODELS["gamecube"].family == FAMILY_GAMECUBE
    assert sw2.MODELS["pro"].family == FAMILY_SWITCH


def test_uuids_match_research_notes():
    assert sw2.SERVICE_UUID == "ab7de9be-89fe-49ad-828f-118f09df7fd0"
    assert sw2.INPUT_COMMON_UUID == "ab7de9be-89fe-49ad-828f-118f09df7fd2"
    assert sw2.MODEL_BY_INPUT_UUID["7492866c-ec3e-4619-8258-32755ffcc0f8"] == "pro"


# --- calibration ------------------------------------------------------------------------------

@pytest.mark.parametrize("vec", DOC["stick_calibration"], ids=lambda v: v["what"])
def test_stick_calibration_doc_examples(vec):
    cal = sw2.parse_stick_calibration(_hex(vec["hex"]))
    assert cal is not None
    assert [cal.x.neutral, cal.x.rel_max, cal.x.rel_min] == vec["x"]
    assert [cal.y.neutral, cal.y.rel_max, cal.y.rel_min] == vec["y"]
    assert sw2.pack_stick_calibration(cal) == _hex(vec["hex"])


def test_calibration_rejects_erased_or_invalid_flash():
    assert sw2.parse_stick_calibration(b"\xff" * 9) is None
    assert sw2.parse_stick_calibration(b"\x00" * 9) is None
    assert sw2.parse_stick_calibration(b"\x01" * 4) is None
    good = _hex(DOC["stick_calibration"][0]["hex"])
    assert sw2.parse_user_stick_calibration(b"\xb2\xa1" + good) is not None
    assert sw2.parse_user_stick_calibration(b"\xff\xff" + good) is None


def _factory_block(stick_hex: str | None) -> bytes:
    body = bytearray(b"\x01" + b"\xaa" * (sw2.FACTORY_STICK_OFFSET - 1))
    body += _hex(stick_hex) if stick_hex else b"\xff" * 9
    return bytes(body + b"\xff" * (0x40 - len(body)))


def test_calibration_from_flash_and_its_effect():
    prim_hex, sec_hex = (v["hex"] for v in DOC["stick_calibration"])
    cal = sw2.calibration_from_flash("pro", _factory_block(prim_hex), _factory_block(sec_hex),
                                     b"\xff" * 0x40, deadzone=0.0)
    assert cal.source == "factory"
    assert cal.left.x.neutral == 1971 and cal.right.x.neutral == 2092
    # Rest position reads zero, extremes reach full scale in the right direction.
    r = sw2.parse_input_report("pro", common_report(left=(1971, 2102), right=(2092, 2112)), cal)
    assert not any(r.axes)
    r = sw2.parse_input_report("pro", common_report(left=(1971 + 1582, 2102 + 1510)), cal)
    assert (axis(r, "left_x"), axis(r, "left_y")) == (32767, -32767)
    r = sw2.parse_input_report("pro", common_report(left=(1971 - 1594, 2102 - 1520)), cal)
    assert (axis(r, "left_x"), axis(r, "left_y")) == (-32767, 32767)

    # User calibration (magic B2 A1) overrides factory data per stick.
    user = sw2.StickCalibration(sw2.AxisCalibration(2000, 1400, 1450),
                                sw2.AxisCalibration(2050, 1500, 1350))
    blk = bytearray(b"\xff" * 0x40)
    blk[0:11] = sw2.USER_CALIBRATION_MAGIC + sw2.pack_stick_calibration(user)
    cal = sw2.calibration_from_flash("pro", _factory_block(prim_hex), _factory_block(sec_hex),
                                     bytes(blk))
    assert cal.source == "user" and cal.left == user and cal.right.x.neutral == 2092

    # Joy-Con 2 (R): its single (primary) calibration applies to the right slot.
    cal = sw2.calibration_from_flash("joycon_r", _factory_block(prim_hex))
    assert cal.right.x.neutral == 1971 and cal.left == sw2.StickCalibration.uniform(1200)

    # GameCube trigger zero points; erased flash keeps the defaults.
    cal = sw2.calibration_from_flash("gamecube", trigger_zero=b"\x28\x2a")
    assert (cal.left_trigger_zero, cal.right_trigger_zero) == (40, 42)
    cal = sw2.calibration_from_flash("gamecube", trigger_zero=b"\xff\xff")
    assert (cal.left_trigger_zero, cal.right_trigger_zero) == (30, 30)
    assert cal.left.x.rel_max == 1225 and cal.right.x.rel_max == 1120 and cal.source == "default"


# --- commands and responses ----------------------------------------------------------------------

@pytest.mark.parametrize("vec", DOC["commands"], ids=lambda v: v["what"])
def test_commands_match_documented_bytes(vec):
    kind, *a = vec["build"]
    pkt = {"memory_read": lambda: sw2.build_memory_read(*a),
           "player_leds": lambda: sw2.build_player_leds(*a),
           "feature": lambda: sw2.build_feature_command(*a)}[kind]()
    assert pkt == _hex(vec["hex"])


def test_command_limits():
    with pytest.raises(ValueError):
        sw2.build_memory_read(0x13000, 0x50)       # BLE maximum is 0x4F
    assert sw2.build_player_leds(pattern=0b1001)[8] == 0b1001
    assert sw2.build_player_leds(5)[8] == sw2.PLAYER_LED_PATTERNS[4]


@pytest.mark.parametrize("vec", DOC["responses"], ids=lambda v: v["what"])
def test_command_responses(vec):
    resp = sw2.parse_command_response(_hex(vec["hex"]))
    assert resp is not None and (resp.cmd, resp.sub) == (vec["cmd"], vec["sub"])
    if "address" in vec:
        addr, data = sw2.parse_memory_read(resp)
        assert addr == vec["address"] and data == _hex(vec["data"])


def test_response_parser_rejects_garbage():
    assert sw2.parse_command_response(b"") is None
    assert sw2.parse_command_response(b"\x00" * 22) is None
    assert sw2.parse_command_response(bytes(63)) is None


def test_device_info_block():
    vec = DOC["device_info_block"]
    blk = _hex(vec["hex"]) + b"\xff" * (0x40 - len(_hex(vec["hex"])))
    info = sw2.parse_device_info_block(blk)
    assert info["serial"] == vec["serial"]
    assert (info["vendor_id"], info["product_id"]) == (vec["vendor_id"], vec["product_id"])
    assert info["colors"] == vec["colors"]


# --- advertisements ------------------------------------------------------------------------------

@pytest.mark.parametrize("vec", DOC["advertisements"], ids=lambda v: v["name"])
def test_manufacturer_data(vec):
    raw = _hex(vec["manufacturer_data"])
    assert struct.unpack_from("<H", raw)[0] == sw2.NINTENDO_COMPANY_ID
    info = sw2.parse_manufacturer_data(raw[2:])      # bleak strips the company id
    assert info is not None
    for k in ("model", "product_id", "pairing_mode", "wake", "host_address"):
        assert info[k] == vec[k], k


def test_manufacturer_data_rejects_other_devices():
    assert sw2.parse_manufacturer_data(b"\x01\x00\x03\x7e\x05\x09\x20\x00\x01") is None  # Switch 1
    assert sw2.parse_manufacturer_data(b"\x01\x00\x03\x5e\x04\x69\x20\x00\x01") is None  # not 057E
    assert sw2.parse_manufacturer_data(b"\x01\x00") is None


def test_rate_meter():
    m = sw2.RateMeter(window_s=1.0)
    for i in range(50):
        m.add(i * 10_000_000)                      # 10 ms apart
    assert m.hz() == pytest.approx(100.0)
    assert m.hz(now=49 * 10_000_000 + 5_000_000_000) == 0.0
    for i in range(50, 200):
        m.add(i * 10_000_000)
    assert m.hz() == pytest.approx(100.0) and m.count == 200


def test_format_state():
    st = PadState()
    st.buttons[BUTTON_INDEX["east"]] = 1
    st.axes[AXIS_INDEX["left_x"]] = -32767
    line = sw2.format_state(st, FAMILY_SWITCH)
    assert "[A]" in line and "LX-32767" in line


# --- fake bleak ---------------------------------------------------------------------------------

PRO_ADDRESS = "98:E2:55:C2:16:88"

# "Command Response #2" characteristics (handle 0x001E), per bluetooth_interface.md.
DOC_EXT_RESPONSE_UUIDS = {
    "joycon_l": "63a3810f-aec7-474b-9010-3d52403cb996",
    "joycon_r": "640ca58e-0e88-410c-a7f3-426faf2b690b",
    "pro": "506d9f7d-4278-4e95-a549-326ba77657e0",
    "gamecube": "46f6ad29-cdaf-4569-a2fe-339020b94604",
}


def _mfr(pid: int, host: bytes = bytes(6), wake: bool = False) -> bytes:
    return (bytes((0x01, 0x00, 0x03)) + struct.pack("<HH", 0x057E, pid)
            + bytes((0x00, 0x01, 0x81 if wake else 0x00)) + host + b"\x0f" + bytes(7))


class FakeBleak:
    """Stand-in for the ``bleak`` module simulating one Switch 2 controller."""

    def __init__(self, model: str = "pro", address: str = PRO_ADDRESS, pairing: bool = True,
                 flash: dict[int, bytes] | None = None, extra_ads=()):
        spec = sw2.MODELS[model]
        self.model = model
        self.flash = flash if flash is not None else {}
        self.clients: list = []
        self.fail_connect = 0
        self.respond_on = "plain"        # "plain" (0x001A), "extended" (0x001E) or "both"
        self.delays: dict[int, float] = {}   # flash address -> response delay (s)
        self.drop_on_command = False     # lose the link on the first command write
        ext_uuid = DOC_EXT_RESPONSE_UUIDS[model]
        self.ext_uuid = ext_uuid
        host = bytes(6) if pairing else bytes.fromhex("5f1185ebf148")
        self.device = SimpleNamespace(address=address, name=None)
        self.ads = [(self.device, SimpleNamespace(manufacturer_data={0x0553: _mfr(
            spec.product_id, host)}, rssi=-48)), *extra_ads]
        ch = lambda uuid, handle, descs=(): SimpleNamespace(  # noqa: E731
            uuid=uuid, handle=handle, descriptors=[SimpleNamespace(uuid=u, handle=h)
                                                   for u, h in descs])
        self.services = [SimpleNamespace(uuid=sw2.SERVICE_UUID, characteristics=[
            ch(sw2.INPUT_COMMON_UUID, 0x0A, [("00002902-0000-1000-8000-00805f9b34fb", 0x0B),
                                            (sw2.REPORT_RATE_DESCRIPTOR_UUID, 0x0C)]),
            ch(spec.input_uuid, 0x0E, [("00002902-0000-1000-8000-00805f9b34fb", 0x0F),
                                       (sw2.REPORT_RATE_DESCRIPTOR_UUID, 0x10)]),
            ch(sw2.COMMAND_UUID, 0x14), ch(sw2.COMMAND_RESPONSE_UUID, 0x1A),
            ch(ext_uuid, 0x1E)])]
        outer = self

        class Scanner:
            @staticmethod
            async def discover(timeout: float = 5.0, return_adv: bool = False):
                await asyncio.sleep(0)
                return {d.address: (d, a) for d, a in outer.ads}

            @staticmethod
            async def find_device_by_filter(fn, timeout: float = 10.0):
                end = time.monotonic() + timeout
                while True:
                    for d, a in outer.ads:
                        if fn(d, a):
                            return d
                    if time.monotonic() >= end:
                        return None
                    await asyncio.sleep(0.01)

        class Client:
            def __init__(self, dev, disconnected_callback=None, **kwargs):
                self.dev, self.on_disc, self.kwargs = dev, disconnected_callback, kwargs
                self.writes: list = []
                self.desc_writes: list = []
                self.notify: dict = {}
                self.loop = None
                self.services = outer.services
                self.disconnected = False
                self.dead = False
                outer.clients.append(self)

            async def connect(self, **kw):
                if outer.fail_connect:
                    outer.fail_connect -= 1
                    raise RuntimeError("fake connect failure")
                self.loop = asyncio.get_running_loop()

            async def disconnect(self):
                self.disconnected = True

            async def start_notify(self, uuid, callback):
                uuid = str(uuid).lower()
                if uuid not in {c.uuid for s in self.services for c in s.characteristics}:
                    raise KeyError(f"characteristic {uuid} not found")   # like bleak
                self.notify[uuid] = callback

            async def write_gatt_char(self, uuid, data, response=None):
                self.writes.append((str(uuid).lower(), bytes(data), response))
                if str(uuid).lower() != sw2.COMMAND_UUID or self.dead:
                    return
                if outer.drop_on_command:
                    outer.drop_on_command = False
                    self.dead = True
                    self.loop.call_soon(self.on_disc, self)
                    return
                resp = outer.respond(bytes(data))
                delay = 0.0
                if data[0] == 0x02 and data[3] == 0x04:
                    delay = outer.delays.get(struct.unpack_from("<I", data, 12)[0], 0.0)
                targets = []
                if outer.respond_on in ("plain", "both"):
                    targets.append((self.notify.get(sw2.COMMAND_RESPONSE_UUID), resp))
                if outer.respond_on in ("extended", "both"):
                    targets.append((self.notify.get(outer.ext_uuid), bytes(14) + resp))
                for cb, payload in targets:
                    if cb is not None:
                        self.loop.call_later(delay, cb, None, bytearray(payload))

            async def write_gatt_descriptor(self, handle, data):
                self.desc_writes.append((handle, bytes(data)))

            # test-side controls (called from the test thread)
            def push(self, data: bytes, uuid: str = sw2.INPUT_COMMON_UUID) -> None:
                self.loop.call_soon_threadsafe(self.notify[uuid], None, bytearray(data))

            def drop(self) -> None:
                self.loop.call_soon_threadsafe(self.on_disc, self)

        self.BleakScanner, self.BleakClient = Scanner, Client

    def read(self, addr: int, n: int) -> bytes:
        out = bytearray(b"\xff" * n)
        for base, blob in self.flash.items():
            for i in range(n):
                j = addr + i - base
                if 0 <= j < len(blob):
                    out[i] = blob[j]
        return bytes(out)

    def respond(self, req: bytes) -> bytes:
        cmd, sub = req[0], req[3]
        if cmd == 0x02 and sub == 0x04:
            n, addr = req[8], struct.unpack_from("<I", req, 12)[0]
            return (bytes((0x02, 0x01, 0x01, 0x04, 0x10, 0x78, 0, 0, n, 0, 0, 0))
                    + struct.pack("<I", addr) + self.read(addr, n))
        return bytes((cmd, 0x01, 0x01, sub, 0x10, 0x78, 0, 0)) + bytes(4)


def _pro_flash() -> dict[int, bytes]:
    info = _hex(DOC["device_info_block"]["hex"])
    prim, sec = (v["hex"] for v in DOC["stick_calibration"])
    return {0x13000: info, 0x13080: _factory_block(prim), 0x130C0: _factory_block(sec)}


def wait_for(pred, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.005)
    return bool(pred())


@pytest.fixture
def fake(monkeypatch):
    fb = FakeBleak(flash=_pro_flash())
    monkeypatch.setattr(sw2, "_bleak_module", lambda: fb)
    return fb


def _backend(hub, **kw):
    kw.setdefault("scan_timeout_s", 0.3)
    kw.setdefault("retry_delay_s", 0.02)
    kw.setdefault("command_timeout_s", 0.5)
    return sw2.Switch2BleBackend(hub, **kw)


def test_backend_connects_initialises_and_publishes(fake):
    hub = Hub()
    events = []
    hub.add_sink(events.append)
    b = _backend(hub)
    b.start()
    try:
        assert b.connected.wait(5), b.status
        client = fake.clients[-1]
        assert "pair" not in client.kwargs or not client.kwargs["pair"]
        assert client.kwargs["winrt"] == {"use_cached_services": False}
        conn = [e for e in events if e.kind == CONNECT]
        assert len(conn) == 1
        info = conn[0].value
        assert info["backend"] == "switch2_ble" and info["product_id"] == 0x2069
        assert info["family"] == FAMILY_SWITCH and info["serial"] == "HEJ71001121247"
        assert info["extra"]["calibration"] == "factory" and info["extra"]["report"] == "0x05"
        assert info["extra"]["address"] == PRO_ADDRESS
        cmds = [w[1] for w in client.writes if w[0] == sw2.COMMAND_UUID]
        assert all(w[2] is False for w in client.writes)            # write without response
        assert sw2.build_player_leds(1) in cmds
        assert sw2.build_feature_command(sw2.SUB_FEATURE_SET_MASK, sw2.DEFAULT_FEATURES) in cmds
        assert sw2.build_feature_command(sw2.SUB_FEATURE_ENABLE, sw2.DEFAULT_FEATURES) in cmds
        assert sw2.build_memory_read(0x130C0, 0x40) in cmds
        assert (0x10, b"\x85\x00") in client.desc_writes
        assert b.calibration.left.x.neutral == 1971
        dev = b.dev_id

        # Rest position (factory neutral) publishes nothing; A + full left does.
        client.push(common_report(left=(1971, 2102), right=(2092, 2112), counter=100))
        t0 = time.perf_counter_ns()
        client.push(common_report(["A"], left=(1971 - 1594, 2102), right=(2092, 2112),
                                  counter=115))
        assert wait_for(lambda: any(e.kind == AXIS for e in events))
        t1 = time.perf_counter_ns()
        btn = [e for e in events if e.kind == BUTTON]
        ax = [e for e in events if e.kind == AXIS]
        assert [(e.code, e.value) for e in btn] == [(BUTTON_INDEX["east"], 1)]
        assert [(e.code, e.value) for e in ax] == [(AXIS_INDEX["left_x"], -32767)]
        assert all(e.device == dev and t0 <= e.t_ns <= t1 for e in btn + ax)
        client.push(common_report(left=(1971, 2102), right=(2092, 2112), counter=130))
        assert wait_for(lambda: hub.states[dev].buttons[BUTTON_INDEX["east"]] == 0)
        assert hub.states[dev].axes[AXIS_INDEX["left_x"]] == 0

        # Report rate from arrival times; device counter spacing in ms; drop estimate.
        for i in range(25):
            client.push(common_report(left=(1971, 2102), counter=145 + 15 * i))
            time.sleep(0.01)
        assert wait_for(lambda: b.reports >= 28)
        assert 20 < b.report_rate_hz() < 200
        client.push(common_report(left=(1971, 2102), counter=145 + 15 * 24 + 60))  # 3 lost
        assert wait_for(lambda: b.reports >= 29)
        st = b.stats()
        assert st["device_interval_ms"] == 15 and st["dropped_estimate"] == 3
        assert wait_for(lambda: "report_rate_hz" in hub.devices[dev].extra, 3)

        # Hold a button, lose the link: the hub releases it and marks the disconnect.
        client.push(common_report(["ZL"], left=(1971, 2102), counter=1000))
        assert wait_for(lambda: hub.states[dev].axes[AXIS_INDEX["left_trigger"]] == 32767)
        n = len(events)
        client.drop()
        assert wait_for(lambda: any(e.kind == DISCONNECT for e in events[n:]))
        tail = events[n:]
        assert (AXIS, AXIS_INDEX["left_trigger"], 0) in [(e.kind, e.code, e.value) for e in tail]

        # It reconnects to the same address and keeps the same hub id.
        assert wait_for(lambda: len(fake.clients) >= 2 and b.connected.is_set())
        assert [e.device for e in events if e.kind == CONNECT] == [dev, dev]
    finally:
        b.stop()
    assert not b._thread.is_alive()
    assert b.error is None


def test_backend_model_report_gamecube(monkeypatch):
    fb = FakeBleak(model="gamecube", address="98:E2:55:00:00:01",
                   flash={0x13140: b"\x28\x28"})
    monkeypatch.setattr(sw2, "_bleak_module", lambda: fb)
    hub = Hub()
    b = _backend(hub, report="model", player=None, features=None)
    b.start()
    try:
        assert b.connected.wait(5)
        client = fb.clients[-1]
        assert not any(w[1][0] in (0x09, 0x0C) for w in client.writes)   # no LED/features
        info = hub.devices[b.dev_id]
        assert info.family == FAMILY_GAMECUBE and info.extra["report"] == "0x0A"
        assert b.calibration.left_trigger_zero == 40
        client.push(model_report(0x0A, ["A", "ZR"], triggers=(232, 40)),
                    uuid=sw2.MODELS["gamecube"].input_uuid)
        dev = b.dev_id
        assert wait_for(lambda: hub.states[dev].axes[AXIS_INDEX["left_trigger"]] == 32767)
        st = hub.states[dev]
        assert st.buttons[BUTTON_INDEX["south"]] == 1               # GameCube A = bottom
        assert st.buttons[BUTTON_INDEX["right_shoulder"]] == 1      # GameCube Z
        assert st.axes[AXIS_INDEX["right_trigger"]] == 0
    finally:
        b.stop()
    assert not b._thread.is_alive()


def test_backend_address_filter_and_pairing_mode(monkeypatch):
    other = (SimpleNamespace(address="AA:BB:CC:DD:EE:FF", name=None),
             SimpleNamespace(manufacturer_data={0x0553: _mfr(0x2066)}, rssi=-30))
    # The Pro controller is only advertising "reconnect" (not pairing mode).
    fb = FakeBleak(pairing=False, extra_ads=[other])
    monkeypatch.setattr(sw2, "_bleak_module", lambda: fb)
    hub = Hub()
    b = _backend(hub, address=PRO_ADDRESS.lower(), player=None, features=None,
                 read_calibration=False)
    b.start()
    try:
        assert b.connected.wait(5)
        assert fb.clients[-1].dev.address == PRO_ADDRESS
        assert hub.devices[b.dev_id].extra["model"] == "pro"
        assert b.calibration.source == "default"
    finally:
        b.stop()
    # Without an address only pairing-mode advertisements are accepted.
    hub2 = Hub()
    b2 = _backend(hub2, model="pro", reconnect=False, scan_timeout_s=0.2)
    b2.start()
    b2._thread.join(5)
    assert isinstance(b2.error, sw2.ControllerNotFound)


def test_backend_retries_after_connect_failure_and_stops_while_scanning(monkeypatch):
    fb = FakeBleak()
    fb.fail_connect = 2
    monkeypatch.setattr(sw2, "_bleak_module", lambda: fb)
    hub = Hub()
    b = _backend(hub, player=None, features=None, read_calibration=False)
    b.start()
    try:
        assert b.connected.wait(5)
        assert len(fb.clients) == 3 and "fake connect failure" in str(b.last_error)
    finally:
        b.stop()
    # stop() interrupts a long scan promptly.
    fb2 = FakeBleak()
    fb2.ads = []
    monkeypatch.setattr(sw2, "_bleak_module", lambda: fb2)
    b = _backend(Hub(), scan_timeout_s=30)
    b.start()
    assert wait_for(lambda: b.status.startswith("scanning"))
    t = time.monotonic()
    b.stop()
    assert time.monotonic() - t < 2 and not b._thread.is_alive()


def test_discover_with_fake_scanner(monkeypatch):
    ads = [(SimpleNamespace(address="aa:00:00:00:00:01", name=None),
            SimpleNamespace(manufacturer_data={0x0553: _mfr(0x2073)}, rssi=-70)),
           (SimpleNamespace(address="aa:00:00:00:00:02", name="Mouse"),
            SimpleNamespace(manufacturer_data={0x0006: b"\x01\x02"}, rssi=-20)),
           (SimpleNamespace(address="aa:00:00:00:00:03", name=None),
            SimpleNamespace(manufacturer_data={0x0553: _mfr(0x2067, bytes(range(1, 7)), True)},
                            rssi=-40))]
    fb = FakeBleak()
    fb.ads = ads
    monkeypatch.setattr(sw2, "_bleak_module", lambda: fb)
    found = sw2.discover(0.01)
    assert [(d.address, d.model) for d in found] == [("AA:00:00:00:00:03", "joycon_l"),
                                                     ("AA:00:00:00:00:01", "gamecube")]
    jc = found[0]
    assert not jc.pairing_mode and jc.wake and jc.host_address == "06:05:04:03:02:01"
    assert "wake" in jc.describe()
    assert [d.model for d in sw2.discover(0.01, include_reconnecting=False)] == ["gamecube"]
    assert [d.model for d in sw2.discover(0.01, model="jcl")] == ["joycon_l"]


# --- CLI ---------------------------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="controllerlog")
    sw2.add_cli(p.add_subparsers(dest="cmd", required=True))
    return p


def test_add_cli_registers_scan_and_test():
    a = _parser().parse_args(["switch2", "scan", "--timeout", "2", "--model", "pro"])
    assert a.switch2_cmd == "scan" and a.timeout == 2.0 and callable(a.func)
    a = _parser().parse_args(["switch2", "test", "98:E2:55:C2:16:88", "--orientation",
                              "vertical", "--deadzone", "0", "--no-throughput"])
    assert (a.switch2_cmd, a.address, a.orientation, a.deadzone, a.no_throughput) == (
        "test", "98:E2:55:C2:16:88", "vertical", 0.0, True)
    with pytest.raises(SystemExit):
        _parser().parse_args(["switch2"])


def test_cli_scan_and_test_with_fake(fake, capsys):
    a = _parser().parse_args(["switch2", "scan", "--timeout", "0.01"])
    assert a.func(a) == 0
    out = capsys.readouterr().out
    assert PRO_ADDRESS in out and "Pro Controller" in out and "pairing" in out
    a = _parser().parse_args(["switch2", "test", "--seconds", "0.6"])
    assert a.func(a) == 0
    out = capsys.readouterr().out
    assert "Nintendo Switch 2 Pro Controller" in out


# --- review: edge cases found while breaking the backend ------------------------------------------

def test_connect_event_snapshot_is_not_mutated_by_live_stats(fake):
    """The CONNECT event must keep the extra dict it was sent with; live stats go elsewhere."""
    hub = Hub()
    events = []
    hub.add_sink(events.append)
    b = _backend(hub)
    b.start()
    try:
        assert b.connected.wait(5)
        dev = b.dev_id
        conn_extra = [e for e in events if e.kind == CONNECT][0].value["extra"]
        before = dict(conn_extra)
        client = fake.clients[-1]
        for i in range(5):
            client.push(common_report(counter=100 + 15 * i))
        assert wait_for(lambda: "report_rate_hz" in hub.devices[dev].extra, 3)
        assert hub.devices[dev].extra["model"] == "pro"
        assert conn_extra == before and "report_rate_hz" not in conn_extra
    finally:
        b.stop()


def test_connection_interval_swallows_winrt_errors():
    class Dev:
        def get_connection_parameters(self):
            raise RuntimeError("WinRT: the object has been closed")

    client = SimpleNamespace(_backend=SimpleNamespace(_requester=Dev()))
    assert sw2.connection_interval_ms(client) is None
    ok = SimpleNamespace(get_connection_parameters=lambda: SimpleNamespace(connection_interval=12))
    assert sw2.connection_interval_ms(SimpleNamespace(_backend=SimpleNamespace(_requester=ok))) == 15.0
    assert sw2.connection_interval_ms(SimpleNamespace()) is None


def test_late_flash_response_does_not_hijack_the_next_read(monkeypatch):
    # 0x13080 answers after its command timed out, just before 0x130C0's own answer.
    fb = FakeBleak(flash=_pro_flash())
    fb.delays = {0x13080: 0.30, 0x130C0: 0.15}
    monkeypatch.setattr(sw2, "_bleak_module", lambda: fb)
    b = _backend(Hub(), command_timeout_s=0.2, player=None, features=None)
    b.start()
    try:
        assert b.connected.wait(5)
        assert b.calibration.right.x.neutral == 2092          # secondary block still read
        assert b.calibration.left.x.neutral == 2048           # primary timed out: default
    finally:
        b.stop()


@pytest.mark.parametrize("mode", ["extended", "both"])
def test_responses_on_extended_characteristic(monkeypatch, mode):
    # ndeadly's PC host got its 0x0014 command answers on 0x001E (14 zero bytes first).
    fb = FakeBleak(flash=_pro_flash())
    fb.respond_on = mode
    monkeypatch.setattr(sw2, "_bleak_module", lambda: fb)
    b = _backend(Hub())
    b.start()
    try:
        assert b.connected.wait(5)
        assert b.calibration.source == "factory"
        assert (b.calibration.left.x.neutral, b.calibration.right.x.neutral) == (1971, 2092)
        assert hub_serial(b) == "HEJ71001121247"
    finally:
        b.stop()
    assert sw2.MODELS["pro"].ext_response_uuid == DOC_EXT_RESPONSE_UUIDS["pro"]
    assert {m: s.ext_response_uuid for m, s in sw2.MODELS.items()} == DOC_EXT_RESPONSE_UUIDS


def hub_serial(b) -> str:
    return b.hub.devices[b.dev_id].serial


def test_link_loss_during_init_is_noticed_immediately(monkeypatch):
    fb = FakeBleak(flash=_pro_flash())
    fb.drop_on_command = True
    monkeypatch.setattr(sw2, "_bleak_module", lambda: fb)
    b = _backend(Hub(), command_timeout_s=1.0)
    t = time.monotonic()
    b.start()
    try:
        assert b.connected.wait(5)
        # ~7 init commands x 1 s timeout would take > 5 s without the abort.
        assert time.monotonic() - t < 2.0
        assert len(fb.clients) == 2 and fb.clients[0].disconnected
    finally:
        b.stop()


def test_silent_command_channel_does_not_stall_the_connection(monkeypatch):
    fb = FakeBleak(flash=_pro_flash())
    fb.respond_on = "none"
    monkeypatch.setattr(sw2, "_bleak_module", lambda: fb)
    b = _backend(Hub(), command_timeout_s=1.0)
    t = time.monotonic()
    b.start()
    try:
        assert b.connected.wait(5)
        assert time.monotonic() - t < 2.0          # not 8 x 1 s
        client = fb.clients[-1]
        cmds = [w[1] for w in client.writes if w[0] == sw2.COMMAND_UUID]
        assert sw2.build_player_leds(1) in cmds    # still sent
        assert b.calibration.source == "default" and (0x10, b"\x85\x00") in client.desc_writes
    finally:
        b.stop()


def test_old_client_is_closed_after_an_unexpected_disconnect(fake):
    b = _backend(Hub(), player=None, features=None, read_calibration=False)
    b.start()
    try:
        assert b.connected.wait(5)
        first = fake.clients[-1]
        first.drop()
        assert wait_for(lambda: len(fake.clients) >= 2 and b.connected.is_set())
        assert first.disconnected           # WinRT services are released, not leaked
    finally:
        b.stop()


def test_drop_estimate_survives_counter_resets(fake):
    b = _backend(Hub(), player=None, features=None, read_calibration=False)
    b.start()
    try:
        assert b.connected.wait(5)
        client = fake.clients[-1]
        for i in range(6):
            client.push(common_report(counter=10_000 + 15 * i))
        client.push(common_report(counter=7))                 # counter reset / garbage
        client.push(common_report(counter=22))
        client.push(common_report(counter=10_000 + 15 * 6 + 45))   # jump far ahead again
        assert wait_for(lambda: b.reports >= 9)
        assert b.dropped == 0
        client.push(common_report(counter=10_000 + 15 * 6 + 45 + 60))   # 3 genuinely lost
        assert wait_for(lambda: b.reports >= 10)
        assert b.dropped == 3
    finally:
        b.stop()


def test_model_report_counter_out_of_order_is_not_a_drop(monkeypatch):
    fb = FakeBleak(model="gamecube", address="98:E2:55:00:00:02")
    monkeypatch.setattr(sw2, "_bleak_module", lambda: fb)
    b = _backend(Hub(), report="model", player=None, features=None, read_calibration=False)
    b.start()
    try:
        assert b.connected.wait(5)
        client = fb.clients[-1]
        uuid = sw2.MODELS["gamecube"].input_uuid
        for c in (10, 11, 12, 11, 13, 16):                    # one reordered, two lost
            client.push(model_report(0x0A, counter=c), uuid=uuid)
        assert wait_for(lambda: b.reports >= 6)
        assert b.dropped == 2
    finally:
        b.stop()


def test_auto_pick_skips_unknown_switch2_pids(monkeypatch):
    unknown = (SimpleNamespace(address="AA:BB:CC:00:00:68", name=None),
               SimpleNamespace(manufacturer_data={0x0553: _mfr(0x2068)}, rssi=-20))
    fb = FakeBleak(flash=_pro_flash())
    fb.ads.insert(0, unknown)            # seen first, in pairing mode, unknown PID
    monkeypatch.setattr(sw2, "_bleak_module", lambda: fb)
    b = _backend(Hub(), player=None, features=None, read_calibration=False)
    b.start()
    try:
        assert b.connected.wait(5)
        assert [c.dev.address for c in fb.clients] == [PRO_ADDRESS]
    finally:
        b.stop()
    assert [d.product_id for d in sw2.discover(0.01)] == [0x2068, 0x2069]   # still listed


def test_deadzone_is_validated():
    with pytest.raises(ValueError):
        sw2.Switch2BleBackend(Hub(), deadzone=1.0)
    with pytest.raises(ValueError):
        sw2.Switch2BleBackend(Hub(), deadzone=-0.1)
    with pytest.raises(ValueError):
        sw2.apply_radial_deadzone(1.0, 1.0, 1.0)
    with pytest.raises(SystemExit):
        _parser().parse_args(["switch2", "test", "--deadzone", "1.5"])


def test_bad_report_never_escapes_the_callback(fake):
    b = _backend(Hub(), player=None, features=None, read_calibration=False)
    b.start()
    try:
        assert b.connected.wait(5)
        cb = fake.clients[-1].notify[sw2.INPUT_COMMON_UUID]
        # Forced invalid states; the callback is called directly and must not raise.
        b.calibration = sw2.Calibration(deadzone=1.0)     # used to be a ZeroDivisionError
        cb(None, bytearray(common_report(left=(4000, 4000))))
        b.calibration = SimpleNamespace()                 # AttributeError
        cb(None, bytearray(common_report()))
        assert b.parse_errors == 2
    finally:
        b.stop()


def test_model_aliases_and_address_forms(monkeypatch):
    assert sw2.normalize_model("Joy-Con (R)") == "joycon_r"
    assert sw2.normalize_model("Joy-Con 2 (L)") == "joycon_l"
    assert sw2.normalize_model("Pro Controller") == "pro"
    b = sw2.Switch2BleBackend(Hub(), address="98-e2-55-c2-16-88")
    assert b.address == PRO_ADDRESS


def test_nintendo_labels():
    rep = sw2.parse_input_report("joycon_l", common_report(["SL_L", "LEFT", "CAPTURE"]))
    assert sw2.nintendo_labels(rep.raw_buttons, "joycon_l") == ["Capture", "Left", "SL"]
    rep = sw2.parse_input_report("gamecube", common_report(["ZR", "A", "PLUS"]))
    assert sw2.nintendo_labels(rep.raw_buttons, "gamecube") == ["A", "Z", "Start"]
    line = sw2.format_state(rep.pad_state(), FAMILY_GAMECUBE,
                            names=sw2.nintendo_labels(rep.raw_buttons, "gamecube"))
    assert line.startswith("[A Z Start]")
    st = PadState()
    st.axes[AXIS_INDEX["right_trigger"]] = 32767
    assert "[ZR]" in sw2.format_state(st, FAMILY_SWITCH)      # digital ZR shows as pressed


def test_cli_test_exit_code_when_no_controller(monkeypatch, capsys):
    fb = FakeBleak()
    fb.ads = []
    monkeypatch.setattr(sw2, "_bleak_module", lambda: fb)
    a = _parser().parse_args(["switch2", "test", "--seconds", "0.5", "--timeout", "0.1"])
    assert a.func(a) == 1
    assert "no controller" in capsys.readouterr().out.lower()


# --- optional: ndeadly's real nRF52840 captures ---------------------------------------------------

def _captures() -> Path:
    root = os.environ.get("SWITCH2_RESEARCH_DIR")
    path = Path(root) / "captures" / "nrf52840" if root else None
    if not path or not path.is_dir():
        pytest.skip("set SWITCH2_RESEARCH_DIR to a clone of ndeadly/switch2_controller_research")
    return path


def _pcap():
    spec = importlib.util.spec_from_file_location("ble_pcap", FIXTURES / "ble_pcap.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _flash_blocks(pcap, path: Path) -> dict[int, bytes]:
    """Flash reads answered in a capture (plain 0x001A or extended 0x001E responses)."""
    blocks = {}
    for handle in (0x001A, 0x001E):
        for _, v in pcap.notifications(path, handle):
            r = sw2.parse_command_response(v)
            if r and r.cmd == sw2.CMD_FLASH and len(r.payload) >= 8:
                addr, data = sw2.parse_memory_read(r)
                if len(data) == r.payload[0]:      # the sniffer garbles a few
                    blocks[addr] = data
    return blocks


def test_real_capture_pro_common_report():
    cap, pcap = _captures(), _pcap()
    motion = cap / "btle_procon2_motion_0x000A.pcapng"
    # Same controller (serial) in both captures; the wake capture has the primary block.
    blocks = {**_flash_blocks(pcap, cap / "btle_procon2_wake_console_decrypted.pcapng"),
              **_flash_blocks(pcap, motion)}
    info = sw2.parse_device_info_block(blocks[sw2.ADDR_DEVICE_INFO])
    assert info["product_id"] == 0x2069 and info["serial"] == "HEJ71001121247"
    cal = sw2.calibration_from_flash("pro", blocks[sw2.ADDR_FACTORY_STICK_PRIMARY],
                                     blocks[sw2.ADDR_FACTORY_STICK_SECONDARY],
                                     blocks.get(sw2.ADDR_USER_STICKS))
    assert cal.source == "factory"          # user calibration block is erased
    assert cal.left == sw2.parse_stick_calibration(_hex(DOC["stick_calibration"][0]["hex"]))
    assert cal.right == sw2.parse_stick_calibration(_hex(DOC["stick_calibration"][1]["hex"]))

    reps = pcap.notifications(motion, 0x000A)
    assert len(reps) > 1000 and {len(v) for _, v in reps} == {63}
    parsed = [sw2.parse_input_report("pro", v, cal) for _, v in reps]
    for p in parsed:     # untouched sticks rest at the factory neutral: no output
        assert all(1900 < s < 2200 for s in p.raw_sticks)
        assert not any(p.axes)
    deltas = [(b.counter - a.counter) & 0xFFFFFFFF for a, b in zip(parsed, parsed[1:])]
    assert sorted(deltas)[len(deltas) // 2] in (22, 23)   # ms counter at a 22.5 ms interval
    gr = [p for p in parsed if p.raw_buttons]
    assert gr and all(p.raw_buttons == 1 << sw2.Bit.GR for p in gr)
    assert all(p.buttons[BUTTON_INDEX["right_paddle1"]] for p in gr)
    assert all(3000 < p.battery_mv < 4400 for p in parsed)


def test_real_capture_model_specific_reports():
    cap, pcap = _captures(), _pcap()
    reps = pcap.notifications(cap / "btle_procon2_motion_0x000E.pcapng", 0x000E)
    parsed = [sw2.parse_input_report("pro", v, report=0x09) for _, v in reps]
    assert len(parsed) > 1000
    assert all(p.battery_level == 6 and all(1900 < s < 2200 for s in p.raw_sticks)
               for p in parsed)
    gaps = [(b.counter - a.counter) & 0xFF for a, b in zip(parsed, parsed[1:])]
    assert gaps.count(1) > 0.9 * len(gaps)
    wake = [sw2.parse_input_report("pro", v, report=0x09) for _, v in
            pcap.notifications(cap / "btle_procon2_wake_console_decrypted.pcapng", 0x000E)]
    assert wake[0].raw_buttons == 1 << sw2.Bit.HOME          # woke the console with HOME
    assert wake[0].buttons[BUTTON_INDEX["guide"]] == 1
    # Joy-Con 2 (R) (PID 0x2066 in its flash) clicking R / ZR in mouse mode.
    path = cap / "btle_joycon2_mouse_mode_decrypted.pcapng"
    assert sw2.parse_device_info_block(
        _flash_blocks(pcap, path)[sw2.ADDR_DEVICE_INFO])["product_id"] == 0x2066
    jc = [sw2.parse_input_report("joycon_r", v, report=0x08) for _, v in
          pcap.notifications(path, 0x000E)]
    assert {p.raw_buttons for p in jc} == {0, 1 << sw2.Bit.R, 1 << sw2.Bit.ZR}
    assert any(p.buttons[BUTTON_INDEX["right_paddle1"]] for p in jc)   # R, held sideways
    assert any(p.buttons[BUTTON_INDEX["right_paddle2"]] for p in jc)   # ZR
