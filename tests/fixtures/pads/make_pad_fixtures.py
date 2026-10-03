from __future__ import annotations

import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent
STICK_PRESS = 0.24

GC_LEFT = 0.598
GC_RIGHT = 0.547
PRO2 = 0.786

THOR_SOURCE = ("thor-evidence/thor-dumpsys-input.txt:18-29,178-231; thor-getevent-lp-pad.txt:1-21; "
               "thor-press-test2.txt; Vendor_2020_Product_0111.kl (AYN Thor, Android 13, 2026-10-02)")

AYN_AXES = [
    {"axis": "X", "min": -1, "max": 1, "flat": 0.00046},
    {"axis": "Y", "min": -1, "max": 1, "flat": 0.00046},
    {"axis": "Z", "min": -1, "max": 1, "flat": 0.00046},
    {"axis": "RZ", "min": -1, "max": 1, "flat": 0.00046},
    {"axis": "GAS", "min": 0, "max": 1, "flat": 0},
    {"axis": "BRAKE", "min": 0, "max": 1, "flat": 0},
    {"axis": "HAT_X", "min": -1, "max": 1, "flat": 0},
    {"axis": "HAT_Y", "min": -1, "max": 1, "flat": 0},
]
AYN_HAS_KEYS = ["BUTTON_A", "BUTTON_B", "BUTTON_C", "BUTTON_X", "BUTTON_Y", "BUTTON_Z", "BUTTON_L1",
                "BUTTON_R1", "BUTTON_L2", "BUTTON_R2", "BUTTON_THUMBL", "BUTTON_THUMBR", "BUTTON_SELECT",
                "BUTTON_START", "BUTTON_MODE", "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT"]
AYN_KL = {0x130: "BUTTON_A", 0x131: "BUTTON_B", 0x132: "BUTTON_C", 0x133: "BUTTON_X", 0x134: "BUTTON_Y",
          0x135: "BUTTON_Z", 0x136: "BUTTON_L1", 0x137: "BUTTON_R1", 0x138: "BUTTON_L2", 0x139: "BUTTON_R2",
          0x13a: "BUTTON_SELECT", 0x13b: "BUTTON_START", 0x13c: "BUTTON_MODE", 0x13d: "BUTTON_THUMBL",
          0x13e: "BUTTON_THUMBR", 0x220: "DPAD_UP", 0x221: "DPAD_DOWN", 0x222: "DPAD_LEFT",
          0x223: "DPAD_RIGHT"}
GENERIC_KL = {0x130: "BUTTON_A", 0x131: "BUTTON_B", 0x132: "BUTTON_C", 0x133: "BUTTON_X", 0x134: "BUTTON_Y",
              0x135: "BUTTON_Z", 0x136: "BUTTON_L1", 0x137: "BUTTON_R1", 0x138: "BUTTON_L2",
              0x139: "BUTTON_R2", 0x13a: "BUTTON_SELECT", 0x13b: "BUTTON_START", 0x13c: "BUTTON_MODE",
              0x13d: "BUTTON_THUMBL", 0x13e: "BUTTON_THUMBR"}

S2_GC = {
    1: ("B", "west", "B", "BUTTON_B"),
    2: ("A", "south", "A", "BUTTON_A"),
    3: ("Y", "north", "Y", "BUTTON_Y"),
    4: ("X", "east", "X", "BUTTON_X"),
    5: ("R", "right_trigger", "RightTrigger", "BUTTON_R2"),
    6: ("Z", "right_shoulder", "RightShoulder", "BUTTON_R1"),
    7: ("Start", "start", "Start", "BUTTON_START"),
    8: ("RStick", "right_stick", "RightStick", "BUTTON_THUMBR"),
    9: ("DDown", "dpad_down", "DpadDown", "DPAD_DOWN"),
    10: ("DRight", "dpad_right", "DpadRight", "DPAD_RIGHT"),
    11: ("DLeft", "dpad_left", "DpadLeft", "DPAD_LEFT"),
    12: ("DUp", "dpad_up", "DpadUp", "DPAD_UP"),
    13: ("L", "left_trigger", "LeftTrigger", "BUTTON_L2"),
    14: ("ZL", "left_shoulder", "LeftShoulder", "BUTTON_L1"),
    15: ("Minus", "back", "Back", "BUTTON_SELECT"),
    16: ("LStick", "left_stick", "LeftStick", "BUTTON_THUMBL"),
    17: ("Home", "guide", "Guide", "BUTTON_MODE"),
    18: ("Capture", "misc1", "Misc1", "MEDIA_RECORD"),
    19: ("GR", "right_paddle1", "Paddle1", None),
    20: ("GL", "left_paddle1", "Paddle2", None),
    21: ("C", "misc2", "Misc2", None),
}
S2_PRO = {
    1: ("B", "south", "B", "BUTTON_B"),
    2: ("A", "east", "A", "BUTTON_A"),
    3: ("Y", "west", "Y", "BUTTON_Y"),
    4: ("X", "north", "X", "BUTTON_X"),
    5: ("R", "right_shoulder", "RightShoulder", "BUTTON_R1"),
    6: ("ZR", "right_trigger", "RightTrigger", "BUTTON_R2"),
    7: ("Plus", "start", "Start", "BUTTON_START"),
    9: ("DDown", "dpad_down", "DpadDown", "DPAD_DOWN"),
    12: ("DUp", "dpad_up", "DpadUp", "DPAD_UP"),
    13: ("L", "left_shoulder", "LeftShoulder", "BUTTON_L1"),
    14: ("ZL", "left_trigger", "LeftTrigger", "BUTTON_L2"),
    15: ("Minus", "back", "Back", "BUTTON_SELECT"),
}

THOR_PRESSES = [("A", 0x131), ("B", 0x130), ("X", 0x133), ("Y", 0x132), ("Z", 0x135), ("ZL", 0x13d),
                ("L", 0x13c), ("R", 0x134), ("Start", 0x136), ("DUp", 0x13b), ("DRight", 0x139),
                ("DDown", 0x138), ("DLeft", 0x13a)]

TOKENS = {"LEFT_X": ("LeftStickLeft", "LeftStickRight"), "LEFT_Y": ("LeftStickUp", "LeftStickDown"),
          "RIGHT_X": ("RightStickLeft", "RightStickRight"), "RIGHT_Y": ("RightStickUp", "RightStickDown")}


def slot_of(scan: int) -> int:
    if 0x130 <= scan <= 0x13f:
        return scan - 0x130 + 1
    if 0x2c0 <= scan <= 0x2c4:
        return scan - 0x2c0 + 17
    return 0


def report_press(table: dict, scan: int, key_code: str, label: str | None = None) -> dict:
    slot = slot_of(scan)
    lab, gcb, is2l, isw = table[slot]
    return {"label": label or lab, "slot": slot, "scan": scan, "keyCode": key_code,
            "expect": {"gcbridge": gcb, "is2l": is2l, "iswitch2late": isw}}


def r4(v: float) -> float:
    return round(v, 4)


def clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def stick_sample(axis: str, raw: float, canonical: str, scale: float, invert: bool) -> dict:
    v = clamp((-raw if invert else raw) / scale)
    neg, pos = TOKENS[canonical.upper()]
    token = pos if v >= STICK_PRESS else neg if v <= -STICK_PRESS else None
    return {"axis": axis, "raw": raw, "canonical": canonical, "value": r4(v), "expect": token}


def trigger_sample(axis: str, raw: float, canonical: str, centred: bool = False) -> dict:
    v = clamp((raw + 1) / 2 if centred else raw, 0.0, 1.0)
    token = ("LeftTrigger" if canonical == "left_trigger" else "RightTrigger") if v >= 0.5 else None
    return {"axis": axis, "raw": raw, "canonical": canonical, "value": r4(v), "expect": token}


def dropped_sample(axis: str, raw: float) -> dict:
    return {"axis": axis, "raw": raw, "canonical": None, "value": None, "expect": None}


def expect(rule: str, model, family: str, key: str, table: str, flip: list, left: tuple, right: tuple,
           scale, triggers: dict, drop: list, analog: bool, unavailable: list, raw_report: bool) -> dict:
    return {"rule": rule, "model": model, "family": family, "deviceKey": key, "table": table,
            "flip": flip, "leftX": left[0], "leftY": left[1], "rightX": right[0], "rightY": right[1],
            "fullScale": scale, "triggers": triggers, "dropAxes": drop, "analogTriggers": analog,
            "unavailable": unavailable, "rawReport": raw_report}


def fixture(fid: str, source: str, measured: bool, device: dict, exp: dict, presses: list,
            samples: list, demotions: list | None = None, context: dict | None = None,
            derived: list | None = None) -> dict:
    out = {"format": "pad-fixture", "version": 1, "id": fid, "source": source, "measured": measured}
    if derived:
        out["derived"] = derived
    out["stickPress"] = STICK_PRESS
    out["device"] = device
    if context:
        out["context"] = context
    out["expect"] = exp
    out["presses"] = presses
    out["axisSamples"] = samples
    if demotions is not None:
        out["demotions"] = demotions
    return out


def ayn_device(name: str, descriptor: str, controller: int) -> dict:
    return {"name": name, "vendor": 0x2020, "product": 0x0111, "descriptor": descriptor,
            "sources": ["KEYBOARD", "GAMEPAD", "JOYSTICK"], "external": True, "controllerNumber": controller,
            "axes": AYN_AXES, "hasKeys": AYN_HAS_KEYS}


GC_KEY = "sig:2020:0111:nintendo nintendo gamecube controller"
THOR_DERIVED = ["device.hasKeys", "presses.keyCode"]
GC_UNAVAILABLE = ["Home", "Capture", "C", "CStickLeft", "CStickRight"]

THOR_PROFILE = {
    "v": 1,
    "match": {"key": GC_KEY, "vendor": "2020", "product": "0111", "name": "Nintendo Nintendo GameCube Controller",
              "axes": ["X", "Y", "Z", "RZ", "GAS", "BRAKE", "HAT_X", "HAT_Y"]},
    "model": "gamecube", "family": "gamecube", "rule": "profile",
    "buttons": {"scan:0x131": "A", "scan:0x130": "B", "scan:0x133": "X", "scan:0x132": "Y", "scan:0x135": "Z",
                "scan:0x13c": "L", "scan:0x134": "R", "scan:0x136": "Start", "scan:0x13b": "DUp",
                "scan:0x138": "DDown", "scan:0x13a": "DLeft", "scan:0x139": "DRight", "scan:0x13d": "ZL"},
    "axes": {"left_x": {"axis": "X", "neg": 0.557, "pos": 0.619},
             "left_y": {"axis": "Y", "invert": True, "neg": 0.562, "pos": 0.589},
             "right_y": {"axis": "RZ", "invert": True, "neg": 0.543, "pos": 0.517}},
    "unavailable": GC_UNAVAILABLE,
    "made": "2026-10-03 on AYN Thor",
}


def thor_copy() -> dict:
    presses = [report_press(S2_GC, scan, AYN_KL[scan], label) for label, scan in THOR_PRESSES]
    samples = [
        stick_sample("X", r4(20271 / 32767), "left_x", GC_LEFT, False),
        stick_sample("X", r4(-18255 / 32767), "left_x", GC_LEFT, False),
        stick_sample("Y", r4(19295 / 32767), "left_y", GC_LEFT, True),
        stick_sample("Y", r4(-18415 / 32767), "left_y", GC_LEFT, True),
        stick_sample("RZ", r4(16927 / 32767), "right_y", GC_RIGHT, True),
        stick_sample("RZ", r4(-17791 / 32767), "right_y", GC_RIGHT, True),
        stick_sample("X", 0.2, "left_x", GC_LEFT, False),
        stick_sample("Y", 0.1, "left_y", GC_LEFT, True),
        dropped_sample("Z", 0.5),
        dropped_sample("GAS", 1.0),
        dropped_sample("BRAKE", 1.0),
    ]
    demotions = [
        {"scan": 0x220, "keyCode": "DPAD_UP", "demotes": True},
        {"scan": 0x110, "keyCode": "UNKNOWN", "demotes": True},
        {"scan": 0x131, "keyCode": "BUTTON_B", "demotes": False},
        {"scan": 0x13f, "keyCode": "UNKNOWN", "demotes": False},
        {"scan": 0x2c4, "keyCode": "UNKNOWN", "demotes": False},
        {"scan": 114, "keyCode": "VOLUME_DOWN", "demotes": False},
        {"scan": 115, "keyCode": "VOLUME_UP", "demotes": False},
        {"scan": 102, "keyCode": "HOME", "demotes": False},
        {"scan": 158, "keyCode": "BACK", "demotes": False},
        {"scan": 580, "keyCode": "APP_SWITCH", "demotes": False},
        {"hat": "HAT_X", "value": 1.0, "demotes": True},
        {"hat": "HAT_Y", "value": 0.0, "demotes": False},
    ]
    exp = expect("ayn-copy-gamecube", "gamecube", "gamecube", GC_KEY, "S2-GC", ["Y", "RZ"], ("X", "Y"),
                 (None, "RZ"), {"left": GC_LEFT, "right": GC_RIGHT},
                 {"left": None, "right": None, "centred": False}, ["Z", "GAS", "BRAKE", "HAT_X", "HAT_Y"],
                 False, GC_UNAVAILABLE, True)
    return fixture("thor-ayn-gc-copy", THOR_SOURCE, True,
                   ayn_device("Nintendo Nintendo GameCube Controller", "45336d6f85bed0c9ed60722b2e9865f72fccd5bf", 3),
                   exp, presses, samples, demotions, derived=THOR_DERIVED)


def thor_copy_profile() -> dict:
    base = thor_copy()
    spans = {"left_x": (0.557, 0.619, False), "left_y": (0.562, 0.589, True), "right_y": (0.543, 0.517, True)}
    samples = []
    for axis, raw, canonical in (("X", 0.619, "left_x"), ("X", -0.557, "left_x"), ("Y", 0.589, "left_y"),
                                 ("Y", -0.562, "left_y"), ("RZ", 0.517, "right_y"), ("RZ", -0.543, "right_y"),
                                 ("X", 0.2, "left_x")):
        neg, pos, inv = spans[canonical]
        v = clamp((-raw if inv else raw) / (neg if raw < 0 else pos))
        n, p = TOKENS[canonical.upper()]
        samples.append({"axis": axis, "raw": raw, "canonical": canonical, "value": r4(v),
                        "expect": p if v >= STICK_PRESS else n if v <= -STICK_PRESS else None})
    samples += [dropped_sample("Z", 0.5), dropped_sample("GAS", 1.0)]
    exp = expect("profile", "gamecube", "gamecube", GC_KEY, "profile", ["Y", "RZ"], ("X", "Y"), (None, "RZ"),
                 None, {"left": None, "right": None, "centred": False},
                 ["Z", "GAS", "BRAKE", "HAT_X", "HAT_Y"], False, GC_UNAVAILABLE, True)
    exp["spans"] = {k: {"neg": v[0], "pos": v[1]} for k, v in spans.items()}
    return fixture("thor-ayn-gc-copy-profile", THOR_SOURCE + "; profile = DESIGN-THOR-FIXES 2.5 example", True,
                   base["device"], exp, base["presses"], samples,
                   context={"profiles": {GC_KEY: THOR_PROFILE}, "ignore": []}, derived=THOR_DERIVED)


def thor_copy_ignored() -> dict:
    base = thor_copy()
    presses = [{"label": p["label"], "slot": p["slot"], "scan": p["scan"], "keyCode": p["keyCode"],
                "expect": {"gcbridge": None, "is2l": None, "iswitch2late": None}} for p in base["presses"][:3]]
    exp = {"rule": "ignored", "ignored": True, "model": "gamecube", "family": "gamecube", "deviceKey": GC_KEY}
    return fixture("thor-ayn-gc-copy-ignored", THOR_SOURCE, True, base["device"], exp, presses, [],
                   context={"profiles": {}, "ignore": [GC_KEY]}, derived=THOR_DERIVED)


def thor_odin() -> dict:
    presses = []
    for scan, iswitch, gcb, is2l in ((0x130, "BUTTON_A", "south", "A"), (0x131, "BUTTON_B", "east", "B"),
                                     (0x133, "BUTTON_X", "west", "X"), (0x134, "BUTTON_Y", "north", "Y"),
                                     (0x136, "BUTTON_L1", "left_shoulder", "LeftShoulder"),
                                     (0x138, "BUTTON_L2", None, "LeftTrigger"),
                                     (0x13b, "BUTTON_START", "start", "Start"),
                                     (0x13c, "BUTTON_MODE", "guide", "Guide"),
                                     (0x220, "DPAD_UP", "dpad_up", "DpadUp")):
        presses.append({"label": iswitch, "slot": 0, "scan": scan, "keyCode": AYN_KL[scan],
                        "expect": {"gcbridge": gcb, "is2l": is2l, "iswitch2late": iswitch}})
    samples = [
        stick_sample("Y", 0.5, "left_y", 1.0, False),
        stick_sample("X", -0.5, "left_x", 1.0, False),
        stick_sample("Z", 0.5, "right_x", 1.0, False),
        stick_sample("RZ", 0.5, "right_y", 1.0, False),
        stick_sample("X", 0.2, "left_x", 1.0, False),
        trigger_sample("GAS", 1.0, "right_trigger"),
        trigger_sample("BRAKE", 1.0, "left_trigger"),
    ]
    demotions = [{"scan": 0x220, "keyCode": "DPAD_UP", "demotes": False},
                 {"hat": "HAT_X", "value": 1.0, "demotes": False}]
    exp = expect("builtin-untouched", None, "generic", "sig:2020:0111:odin controller", "none", [], ("X", "Y"),
                 ("Z", "RZ"), {"left": 1.0, "right": 1.0}, {"left": "BRAKE", "right": "GAS", "centred": False},
                 [], True, [], False)
    return fixture("thor-odin-builtin", "thor-evidence/thor-dumpsys-input.txt:44-55,232-285; "
                   "thor-getevent-lp-pad.txt:23-45; presses are synthetic from the shared .kl (the Odin was "
                   "not pressed tonight)", True,
                   ayn_device("Odin Controller", "8e1073ea5832500672194344d81498833991c43c", 1),
                   exp, presses, samples, demotions, derived=["device.hasKeys", "presses"])


def thor_mouse() -> dict:
    device = {"name": "ODIN Station Virtual Mouse", "vendor": 0x2020, "product": 0x0111,
              "descriptor": "215005827afddf4e97a68b3679bcdfdf7635af91", "sources": ["MOUSE"], "external": True,
              "controllerNumber": 0, "axes": [], "hasKeys": []}
    exp = expect("builtin-untouched", None, "generic", "sig:2020:0111:odin station virtual mouse", "none", [],
                 (None, None), (None, None), {"left": 1.0, "right": 1.0},
                 {"left": None, "right": None, "centred": False}, [], False, [], False)
    return fixture("thor-odin-station-mouse", "thor-evidence/thor-dumpsys-input.txt (device 10/11, CURSOR_ICON); "
                   "thor-getevent-lp-pad.txt:46-52", True, device, exp, [], [])


def trifold_device(vendor: int, product: int, name: str) -> dict:
    flat = r4(255 / 2047.5)
    return {"name": name, "vendor": vendor, "product": product, "descriptor": "",
            "sources": ["KEYBOARD", "GAMEPAD", "JOYSTICK"], "external": True, "controllerNumber": 1,
            "axes": [{"axis": a, "min": -1, "max": 1, "flat": flat} for a in ("X", "Y", "RX", "RZ")],
            "hasKeys": ["BUTTON_A", "BUTTON_B", "BUTTON_C", "BUTTON_X", "BUTTON_Y", "BUTTON_Z", "BUTTON_L1",
                        "BUTTON_R1", "BUTTON_L2", "BUTTON_R2", "BUTTON_THUMBL", "BUTTON_THUMBR",
                        "BUTTON_SELECT", "BUTTON_START", "BUTTON_MODE"]}


def trifold_presses(table: dict) -> list:
    out = []
    for slot in sorted(table):
        scan = 0x130 + slot - 1 if slot <= 16 else 0x2c0 + slot - 17
        out.append(report_press(table, scan, GENERIC_KL.get(scan, "UNKNOWN")))
    return out


def trifold() -> dict:
    samples = [
        stick_sample("X", 0.4, "left_x", 1.0, False),
        stick_sample("X", 0.2, "left_x", 1.0, False),
        stick_sample("Y", 0.5, "left_y", 1.0, True),
        stick_sample("RX", 0.5, "right_x", 1.0, False),
        stick_sample("RZ", 0.5, "right_y", 1.0, True),
    ]
    exp = expect("s2-hidgeneric", "gamecube", "gamecube", "sig:057e:2073:nintendo co., ltd. nso gamecube controller",
                 "S2-GC", ["Y", "RZ"], ("X", "Y"), ("RX", "RZ"), {"left": 1.0, "right": 1.0},
                 {"left": None, "right": None, "centred": False}, [], False, [], True)
    return fixture("trifold-gc-hidgeneric", "tests/fixtures/adb/switch2_gc_standard_info.txt (TriFold, getevent -i); "
                   "Android axes and key codes derived from Generic.kl", True,
                   trifold_device(0x057e, 0x2073, "Nintendo Co., Ltd. NSO GameCube Controller"),
                   exp, trifold_presses(S2_GC), samples,
                   [{"scan": 0x220, "keyCode": "DPAD_UP", "demotes": True},
                    {"scan": 0x2c0, "keyCode": "UNKNOWN", "demotes": False}],
                   derived=["device.axes", "device.hasKeys", "presses.keyCode"])


def pro2_hidgeneric() -> dict:
    samples = [stick_sample("Y", 0.5, "left_y", 1.0, True), stick_sample("RZ", -0.5, "right_y", 1.0, True)]
    exp = expect("s2-hidgeneric", "pro2", "switch", "sig:057e:2069:nintendo co., ltd. switch 2 pro controller",
                 "S2-Pro", ["Y", "RZ"], ("X", "Y"), ("RX", "RZ"), {"left": 1.0, "right": 1.0},
                 {"left": None, "right": None, "centred": False}, [], False, [], True)
    return fixture("s2-pro2-hidgeneric", "synthetic: the TriFold node with the Pro 2 product id", False,
                   trifold_device(0x057e, 0x2069, "Nintendo Co., Ltd. Switch 2 Pro Controller"),
                   exp, trifold_presses(S2_PRO), samples)


def gc_clone() -> dict:
    samples = [stick_sample("Y", 0.5, "left_y", 1.0, True), stick_sample("X", 0.2, "left_x", 1.0, False)]
    exp = expect("name-gamecube-clone", "gamecube", "gamecube",
                 "sig:0000:0000:nintendo co., ltd. nso gamecube controller", "S2-GC", ["Y", "RZ"], ("X", "Y"),
                 ("RX", "RZ"), {"left": 1.0, "right": 1.0}, {"left": None, "right": None, "centred": False},
                 [], False, [], True)
    return fixture("gc-clone-vendor0", "synthetic: the TriFold node re-identified with vendor/product 0000", False,
                   trifold_device(0, 0, "Nintendo Co., Ltd. NSO GameCube Controller"), exp,
                   [report_press(S2_GC, 0x131, "BUTTON_B"), report_press(S2_GC, 0x130, "BUTTON_A"),
                    report_press(S2_GC, 0x13b, "BUTTON_START")], samples)


def kernel_driver() -> dict:
    device = {"name": "Nintendo Switch 2 GameCube Controller", "vendor": 0x057e, "product": 0x2073,
              "descriptor": "", "sources": ["KEYBOARD", "GAMEPAD", "JOYSTICK"], "external": True,
              "controllerNumber": 1,
              "axes": [{"axis": a, "min": -1, "max": 1, "flat": 0.0} for a in
                       ("X", "Y", "RX", "RY", "Z", "RZ", "HAT_X", "HAT_Y")],
              "hasKeys": ["BUTTON_A", "BUTTON_B", "BUTTON_X", "BUTTON_Y", "BUTTON_L1", "BUTTON_R1",
                          "BUTTON_START", "BUTTON_SELECT", "BUTTON_MODE"]}
    presses = [{"label": "A", "slot": 0, "scan": 0x130, "keyCode": "BUTTON_A",
                "expect": {"gcbridge": "south", "is2l": "A", "iswitch2late": "BUTTON_A"}},
               {"label": "B", "slot": 0, "scan": 0x131, "keyCode": "BUTTON_B",
                "expect": {"gcbridge": "east", "is2l": "B", "iswitch2late": "BUTTON_B"}},
               {"label": "Y", "slot": 0, "scan": 0x134, "keyCode": "BUTTON_Y",
                "expect": {"gcbridge": "west", "is2l": "Y", "iswitch2late": "BUTTON_Y"}}]
    samples = [
        stick_sample("Y", 0.5, "left_y", 1.0, False),
        stick_sample("RY", 0.5, "right_y", 1.0, False),
        stick_sample("RX", -0.5, "right_x", 1.0, False),
        trigger_sample("Z", -1.0, "left_trigger", True),
        trigger_sample("Z", 0.0, "left_trigger", True),
        trigger_sample("RZ", 1.0, "right_trigger", True),
    ]
    exp = expect("s2-kernel-driver", "gamecube", "gamecube", "sig:057e:2073:nintendo switch 2 gamecube controller",
                 "none", [], ("X", "Y"), ("RX", "RY"), {"left": 1.0, "right": 1.0},
                 {"left": "Z", "right": "RZ", "centred": True}, [], True, [], False)
    return fixture("kernel-driver-gc", "synthetic: no kernel driver for the Switch 2 pads exists yet "
                   "(map 09 section 4.4)", False, device, exp, presses, samples,
                   [{"scan": 0x220, "keyCode": "DPAD_UP", "demotes": False}])


def xbox360() -> dict:
    device = {"name": "Microsoft X-Box 360 pad", "vendor": 0x045e, "product": 0x028e, "descriptor": "",
              "sources": ["KEYBOARD", "GAMEPAD", "JOYSTICK"], "external": True, "controllerNumber": 1,
              "axes": [{"axis": a, "min": 0 if "TRIGGER" in a else -1, "max": 1, "flat": 0.0} for a in
                       ("X", "Y", "Z", "RZ", "LTRIGGER", "RTRIGGER", "HAT_X", "HAT_Y")],
              "hasKeys": ["BUTTON_A", "BUTTON_B", "BUTTON_X", "BUTTON_Y", "BUTTON_L1", "BUTTON_R1",
                          "BUTTON_THUMBL", "BUTTON_THUMBR", "BUTTON_START", "BUTTON_SELECT", "BUTTON_MODE"]}
    presses = [{"label": "A", "slot": 0, "scan": 0x130, "keyCode": "BUTTON_A",
                "expect": {"gcbridge": "south", "is2l": "A", "iswitch2late": "BUTTON_A"}},
               {"label": "X", "slot": 0, "scan": 0x133, "keyCode": "BUTTON_X",
                "expect": {"gcbridge": "west", "is2l": "X", "iswitch2late": "BUTTON_X"}},
               {"label": "Y", "slot": 0, "scan": 0x134, "keyCode": "BUTTON_Y",
                "expect": {"gcbridge": "north", "is2l": "Y", "iswitch2late": "BUTTON_Y"}}]
    samples = [stick_sample("Y", 0.5, "left_y", 1.0, False), stick_sample("RZ", -0.5, "right_y", 1.0, False),
               trigger_sample("RTRIGGER", 1.0, "right_trigger")]
    exp = expect("family", None, "xbox", "sig:045e:028e:microsoft x-box 360 pad", "none", [], ("X", "Y"),
                 ("Z", "RZ"), {"left": 1.0, "right": 1.0}, {"left": "LTRIGGER", "right": "RTRIGGER", "centred": False},
                 [], True, [], False)
    return fixture("xbox360", "synthetic: xpad 045e:028e with AOSP Vendor_045e_Product_028e.kl axes", False,
                   device, exp, presses, samples)


def ayn_pro2() -> dict:
    samples = [stick_sample("Y", 0.5, "left_y", PRO2, True), stick_sample("RZ", 0.5, "right_y", PRO2, True)]
    presses = [report_press(S2_PRO, 0x130, "BUTTON_A"), report_press(S2_PRO, 0x131, "BUTTON_B"),
               report_press(S2_PRO, 0x135, "BUTTON_Z")]
    exp = expect("ayn-copy-pro2", "pro2", "switch", "sig:2020:0111:nintendo switch pro controller", "S2-Pro",
                 ["Y", "RZ"], ("X", "Y"), (None, "RZ"), {"left": PRO2, "right": PRO2},
                 {"left": None, "right": None, "centred": False}, ["Z", "GAS", "BRAKE", "HAT_X", "HAT_Y"], False,
                 ["Home", "Capture", "GR", "GL", "C", "RightStickLeft", "RightStickRight"], True)
    return fixture("ayn-copy-unknown-nintendo", "synthetic: the Thor copy's template with a Pro Controller name "
                   "(no Pro 2 was measured on the Thor)", False,
                   ayn_device("Nintendo Switch Pro Controller", "", 3), exp, presses, samples)


def ayn_nintendo_other() -> dict:
    exp = expect("ayn-copy-nintendo", None, "switch", "sig:2020:0111:nintendo co., ltd. joy-con 2 (r)", "none", [],
                 ("X", "Y"), ("Z", "RZ"), {"left": 1.0, "right": 1.0},
                 {"left": "BRAKE", "right": "GAS", "centred": False}, [], True, [], False)
    presses = [{"label": "BUTTON_A", "slot": 0, "scan": 0x130, "keyCode": "BUTTON_A",
                "expect": {"gcbridge": "south", "is2l": "A", "iswitch2late": "BUTTON_A"}}]
    return fixture("ayn-copy-nintendo-other", "synthetic: the Thor copy's template with a Nintendo name of no "
                   "known model", False, ayn_device("Nintendo Co., Ltd. Joy-Con 2 (R)", "", 3), exp, presses,
                   [stick_sample("Y", 0.5, "left_y", 1.0, False)])


def ayn_word_boundary() -> dict:
    exp = expect("builtin-untouched", None, "generic", "sig:2020:0111:odin console gamepad", "none", [],
                 ("X", "Y"), ("Z", "RZ"), {"left": 1.0, "right": 1.0},
                 {"left": "BRAKE", "right": "GAS", "centred": False}, [], True, [], False)
    return fixture("ayn-ids-word-boundary", "synthetic: \"nso\" inside \"console\" is not the nso token", False,
                   ayn_device("Odin Console Gamepad", "", 2), exp, [], [])


ALL = [thor_copy, thor_copy_profile, thor_copy_ignored, thor_odin, thor_mouse, trifold, pro2_hidgeneric,
       gc_clone, kernel_driver, xbox360, ayn_pro2, ayn_nintendo_other, ayn_word_boundary]


def main() -> int:
    for make in ALL:
        f = make()
        path = OUT / (f["id"] + ".json")
        path.write_text(json.dumps(f, indent=1) + "\n", encoding="utf-8")
        print(path.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
