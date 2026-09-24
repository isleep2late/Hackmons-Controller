"""Nintendo Switch 2 controllers over Bluetooth LE, without a dongle (EXPERIMENTAL).

Switch 2 Pro Controller (057E:2069), Joy-Con 2 R (2066) / L (2067) and the NSO
GameCube controller for Switch 2 (2073) speak a proprietary GATT protocol, not
HID-over-GATT, so Windows exposes no gamepad and SDL 3.4 only drives them over
USB. This module is a user-mode GATT client (``bleak``; WinRT on Windows) that
leaves the Bluetooth adapter to the OS.

Protocol summary (sources: ndeadly/switch2_controller_research docs, BlueRetro
``main/bluetooth/hidp/sw2.c`` + ``main/adapter/wireless/sw2.c`` (Apache-2.0),
SDL ``SDL_hidapi_switch2.c`` 3.4.16 (zlib); this file is an independent
implementation):

* Discovery: the advertisement carries only Flags + manufacturer data for
  company 0x0553 with VID 057E / PID at offsets 3..6 (after the company id); the
  GAP name is literally ``"DeviceName"`` and there is no scan response, so we
  identify by manufacturer data only. Host address bytes 10..15 are all zero in
  pairing ("sync button") mode, and hold the paired console's address in
  reconnect/wake advertisements.
* Pairing: Nintendo exchanges keys through its own command (0x15) instead of
  SMP, and an SMP pairing attempt makes the controller drop the link. Pairing is
  optional for reading input, so we connect *without* pairing and never write
  the controller's pairing table.
* Service ``ab7de9be-89fe-49ad-828f-118f09df7fd0``:
  input report 0x05 (common to all models) notifies on ``...7fd2``; a
  per-model report (0x07 JC-L, 0x08 JC-R, 0x09 Pro, 0x0A GC) notifies on a
  per-model UUID; commands are written (without response) to ``649d4ac9-...``
  and answered on ``c765a961-...`` (0x001A) or, prefixed by 14 zero bytes, on
  the per-model "response #2" characteristic (0x001E); we listen on both.
  Enabling the CCCD is what starts reports (BlueRetro sends nothing else).
* Report 0x05: u32 counter (observed to tick in milliseconds over BLE), u32
  button bitfield at 4, 12-bit packed sticks at 10 and 13, GameCube analog
  triggers at 0x3C/0x3D. BLE reports are the USB HID reports minus the
  report-id byte.
* Calibration lives in controller flash: factory stick blocks at 0x13080+0x28
  (primary) and 0x130C0+0x28 (secondary), user calibration at 0x1FC040 /
  0x1FC060 (magic ``B2 A1``), GameCube trigger zero points at 0x13140.
* Rate: the controller sends one report per connection event. Windows 10 uses
  a fixed 60 ms interval (~16.7 Hz, per ndeadly) with no API to change it;
  Windows 11 ``ThroughputOptimized`` preferred parameters are 12 x 1.25 ms =
  15 ms (~66.7 Hz). The console uses a vendor-specific 5 ms (200 Hz), below
  the 7.5 ms spec minimum, which Windows cannot request.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import math
import statistics
import struct
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable

from ..hub import Hub, now_ns
from ..model import (AXES, AXIS, AXIS_INDEX, AXIS_MAX, AXIS_MIN, BUTTON, BUTTON_INDEX,
                     BUTTONS, FACE_LABELS, FAMILY_GAMECUBE, FAMILY_SWITCH, NUM_AXES,
                     NUM_BUTTONS, DeviceInfo, InputEvent, PadState)
from .base import Backend

log = logging.getLogger(__name__)

# --- identifiers ---------------------------------------------------------------

NINTENDO_COMPANY_ID = 0x0553       # Bluetooth SIG company id in advertisements
NINTENDO_VENDOR_ID = 0x057E        # USB vendor id (also inside the manufacturer data)

SERVICE_UUID = "ab7de9be-89fe-49ad-828f-118f09df7fd0"
INPUT_COMMON_UUID = "ab7de9be-89fe-49ad-828f-118f09df7fd2"       # report 0x05, handle 0x000A
COMMAND_UUID = "649d4ac9-8eb7-4e6c-af44-1ea54fe5f005"            # write w/o response, 0x0014
COMMAND_RESPONSE_UUID = "c765a961-d9d8-4d36-a20a-5315b111836a"   # notify, 0x001A
REPORT_RATE_DESCRIPTOR_UUID = "679d5510-5a24-4dee-9557-95df80486ecb"  # "Set Report Rate?"

REPORT_COMMON = 0x05

DEFAULT_DEADZONE = 0.03            # radial, fraction of full deflection
DEFAULT_FEATURES = 0x27            # buttons | sticks | IMU | rumble (SDL's USB value)
FEATURE_BUTTONS = 0x01
FEATURE_STICKS = 0x02

ORIENTATIONS = ("horizontal", "vertical")


@dataclass(frozen=True)
class ModelSpec:
    key: str
    product_id: int
    name: str
    sdl_type: str
    family: str
    report_id: int        # model-specific input report (handle 0x000E)
    input_uuid: str       # its characteristic UUID
    sticks: str           # "both", "left" or "right" (slot used in report 0x05)
    ext_response_uuid: str = ""   # "Command Response #2" notify characteristic (0x001E)


MODELS: dict[str, ModelSpec] = {
    "pro": ModelSpec("pro", 0x2069, "Nintendo Switch 2 Pro Controller", "switchpro",
                     FAMILY_SWITCH, 0x09, "7492866c-ec3e-4619-8258-32755ffcc0f8", "both",
                     "506d9f7d-4278-4e95-a549-326ba77657e0"),
    "joycon_l": ModelSpec("joycon_l", 0x2067, "Joy-Con 2 (L)", "joyconleft", FAMILY_SWITCH,
                          0x07, "cc1bbbb5-7354-4d32-a716-a81cb241a32a", "left",
                          "63a3810f-aec7-474b-9010-3d52403cb996"),
    "joycon_r": ModelSpec("joycon_r", 0x2066, "Joy-Con 2 (R)", "joyconright", FAMILY_SWITCH,
                          0x08, "d5a9e01e-2ffc-4cca-b20c-8b67142bf442", "right",
                          "640ca58e-0e88-410c-a7f3-426faf2b690b"),
    "gamecube": ModelSpec("gamecube", 0x2073, "Nintendo GameCube Controller (Switch 2)",
                          "gamecube", FAMILY_GAMECUBE, 0x0A,
                          "8261cba1-9435-420c-84d6-f0c75a2c8e4d", "both",
                          "46f6ad29-cdaf-4569-a2fe-339020b94604"),
}
MODEL_BY_PID = {m.product_id: m.key for m in MODELS.values()}
MODEL_BY_INPUT_UUID = {m.input_uuid: m.key for m in MODELS.values()}
REPORT_IDS = {REPORT_COMMON} | {m.report_id for m in MODELS.values()}

# Keys are compacted names: lower case without spaces, "-", "_", "(" and ")".
_MODEL_ALIASES = {
    **dict.fromkeys(("pro", "pro2", "procon", "procon2", "procontroller", "procontroller2",
                     "switch2pro", "switch2procontroller", "2069"), "pro"),
    **dict.fromkeys(("joyconl", "joycon2l", "joyconleft", "joycon2left", "jcl", "jc2l",
                     "left", "l", "2067"), "joycon_l"),
    **dict.fromkeys(("joyconr", "joycon2r", "joyconright", "joycon2right", "jcr", "jc2r",
                     "right", "r", "2066"), "joycon_r"),
    **dict.fromkeys(("gamecube", "gamecubecontroller", "nsogamecube", "gc", "ngc", "2073"),
                    "gamecube"),
}


def normalize_model(name: str | None) -> str | None:
    """Accept ``pro``/``jcl``/``Joy-Con (R)``/``gc``/``0x2069``... and return a MODELS key."""
    if name is None:
        return None
    k = name.strip().lower().removeprefix("0x")
    for ch in " -_()":
        k = k.replace(ch, "")
    if k in _MODEL_ALIASES:
        return _MODEL_ALIASES[k]
    raise ValueError(f"unknown Switch 2 controller model {name!r} "
                     f"(expected one of {', '.join(MODELS)})")


def normalize_address(address: str | None) -> str | None:
    """``98-e2-55-c2-16-88`` -> ``98:E2:55:C2:16:88`` (bleak's form on Windows/Linux)."""
    if not address or not address.strip():
        return None
    return address.strip().upper().replace("-", ":")


# --- button bits (input report 0x05 layout, u32 little-endian at offset 4) -----

class Bit:
    """Bit positions of the report 0x05 button field (ndeadly hid_reports.md)."""

    Y, X, B, A, SR_R, SL_R, R, ZR = range(8)
    MINUS, PLUS, RSTICK, LSTICK, HOME, CAPTURE, C = range(8, 15)
    DOWN, UP, RIGHT, LEFT, SR_L, SL_L, L, ZL = range(16, 24)
    GR, GL = 24, 25
    HEADSET = 28


BIT_NAMES = {v: k for k, v in vars(Bit).items() if not k.startswith("_")}

# Labels printed on the controllers (the NSO GameCube pad reports Z in the ZR bit).
_BIT_LABELS = {
    Bit.Y: "Y", Bit.X: "X", Bit.B: "B", Bit.A: "A", Bit.SR_R: "SR", Bit.SL_R: "SL",
    Bit.R: "R", Bit.ZR: "ZR", Bit.MINUS: "-", Bit.PLUS: "+", Bit.RSTICK: "RS",
    Bit.LSTICK: "LS", Bit.HOME: "Home", Bit.CAPTURE: "Capture", Bit.C: "C",
    Bit.DOWN: "Down", Bit.UP: "Up", Bit.RIGHT: "Right", Bit.LEFT: "Left", Bit.SR_L: "SR",
    Bit.SL_L: "SL", Bit.L: "L", Bit.ZL: "ZL", Bit.GR: "GR", Bit.GL: "GL",
}
_GC_BIT_LABELS = {**_BIT_LABELS, Bit.ZR: "Z", Bit.PLUS: "Start"}


def nintendo_labels(raw_buttons: int, model: str | None = None) -> list[str]:
    """Pressed buttons of a report 0x05 bitfield, named as printed on the controller."""
    labels = _GC_BIT_LABELS if normalize_model(model) == "gamecube" else _BIT_LABELS
    return [labels[b] for b in sorted(labels) if raw_buttons >> b & 1]

# Model-specific reports use their own byte/bit order; these tables translate
# them into the report 0x05 bit layout above: (byte offset, mask, Bit).
_MODEL_REPORT_BITS: dict[int, tuple[tuple[int, int, int], ...]] = {
    0x09: (  # Pro Controller 2
        (2, 0x01, Bit.B), (2, 0x02, Bit.A), (2, 0x04, Bit.Y), (2, 0x08, Bit.X),
        (2, 0x10, Bit.R), (2, 0x20, Bit.ZR), (2, 0x40, Bit.PLUS), (2, 0x80, Bit.RSTICK),
        (3, 0x01, Bit.DOWN), (3, 0x02, Bit.RIGHT), (3, 0x04, Bit.LEFT), (3, 0x08, Bit.UP),
        (3, 0x10, Bit.L), (3, 0x20, Bit.ZL), (3, 0x40, Bit.MINUS), (3, 0x80, Bit.LSTICK),
        (4, 0x01, Bit.HOME), (4, 0x02, Bit.CAPTURE), (4, 0x04, Bit.GR), (4, 0x08, Bit.GL),
        (4, 0x10, Bit.C),
    ),
    0x0A: (  # NSO GameCube: Z sits in the ZR slot, the L/R trigger clicks in L/R
        (2, 0x01, Bit.B), (2, 0x02, Bit.A), (2, 0x04, Bit.Y), (2, 0x08, Bit.X),
        (2, 0x10, Bit.ZR), (2, 0x20, Bit.R), (2, 0x40, Bit.PLUS), (2, 0x80, Bit.RSTICK),
        (3, 0x01, Bit.DOWN), (3, 0x02, Bit.RIGHT), (3, 0x04, Bit.LEFT), (3, 0x08, Bit.UP),
        (3, 0x10, Bit.ZL), (3, 0x20, Bit.L), (3, 0x40, Bit.MINUS), (3, 0x80, Bit.LSTICK),
        (4, 0x01, Bit.HOME), (4, 0x02, Bit.CAPTURE), (4, 0x10, Bit.C),
    ),
    0x07: (  # Joy-Con 2 (L)
        (2, 0x01, Bit.DOWN), (2, 0x02, Bit.RIGHT), (2, 0x04, Bit.LEFT), (2, 0x08, Bit.UP),
        (2, 0x10, Bit.L), (2, 0x20, Bit.ZL), (2, 0x40, Bit.MINUS), (2, 0x80, Bit.LSTICK),
        (3, 0x01, Bit.CAPTURE), (3, 0x40, Bit.SR_L), (3, 0x80, Bit.SL_L),
    ),
    0x08: (  # Joy-Con 2 (R)
        (2, 0x01, Bit.B), (2, 0x02, Bit.A), (2, 0x04, Bit.Y), (2, 0x08, Bit.X),
        (2, 0x10, Bit.R), (2, 0x20, Bit.ZR), (2, 0x40, Bit.PLUS), (2, 0x80, Bit.RSTICK),
        (3, 0x01, Bit.HOME), (3, 0x10, Bit.C), (3, 0x40, Bit.SR_R), (3, 0x80, Bit.SL_R),
    ),
}

# --- canonical mapping tables ----------------------------------------------------
# Positional, matching SDL 3.4's gamepad mappings for these controllers so that a
# pad reads the same over BLE here and over USB through the SDL3 backend.

_PRO_BUTTONS = {
    Bit.B: "south", Bit.A: "east", Bit.Y: "west", Bit.X: "north",
    Bit.MINUS: "back", Bit.HOME: "guide", Bit.PLUS: "start",
    Bit.LSTICK: "left_stick", Bit.RSTICK: "right_stick",
    Bit.L: "left_shoulder", Bit.R: "right_shoulder",
    Bit.UP: "dpad_up", Bit.DOWN: "dpad_down", Bit.LEFT: "dpad_left", Bit.RIGHT: "dpad_right",
    Bit.CAPTURE: "misc1", Bit.GR: "right_paddle1", Bit.GL: "left_paddle1", Bit.C: "misc2",
}
_GC_BUTTONS = {  # labels: A big/centre (south), B left, X right, Y top
    Bit.A: "south", Bit.X: "east", Bit.B: "west", Bit.Y: "north",
    Bit.PLUS: "start", Bit.HOME: "guide",
    Bit.ZL: "left_shoulder", Bit.ZR: "right_shoulder",    # ZR slot == GameCube Z
    Bit.UP: "dpad_up", Bit.DOWN: "dpad_down", Bit.LEFT: "dpad_left", Bit.RIGHT: "dpad_right",
    Bit.CAPTURE: "misc1", Bit.C: "misc2",
    Bit.L: "misc3", Bit.R: "misc4",                       # full-pull trigger clicks
}
# Single Joy-Con held sideways (stick on the left), like SDL's "mini gamepad".
_JCR_HORIZONTAL = {
    Bit.A: "south", Bit.X: "east", Bit.B: "west", Bit.Y: "north",
    Bit.SL_R: "left_shoulder", Bit.SR_R: "right_shoulder",
    Bit.PLUS: "start", Bit.RSTICK: "left_stick", Bit.HOME: "guide", Bit.C: "misc2",
    Bit.R: "right_paddle1", Bit.ZR: "right_paddle2",
}
_JCL_HORIZONTAL = {
    Bit.LEFT: "south", Bit.DOWN: "east", Bit.UP: "west", Bit.RIGHT: "north",
    Bit.SL_L: "left_shoulder", Bit.SR_L: "right_shoulder",
    Bit.MINUS: "start", Bit.LSTICK: "left_stick", Bit.CAPTURE: "guide",
    Bit.L: "left_paddle1", Bit.ZL: "left_paddle2",
}
# Held upright as one half of a pair: the Pro layout restricted to that half.
_JCL_VERTICAL = {b: n for b, n in _PRO_BUTTONS.items()
                 if b in (Bit.MINUS, Bit.LSTICK, Bit.CAPTURE, Bit.L, Bit.UP, Bit.DOWN,
                          Bit.LEFT, Bit.RIGHT, Bit.GL)}
_JCR_VERTICAL = {b: n for b, n in _PRO_BUTTONS.items()
                 if b in (Bit.A, Bit.B, Bit.X, Bit.Y, Bit.PLUS, Bit.RSTICK, Bit.HOME,
                          Bit.C, Bit.R, Bit.GR)}

BUTTON_MAPS: dict[tuple[str, str | None], dict[int, str]] = {
    ("pro", None): _PRO_BUTTONS,
    ("gamecube", None): _GC_BUTTONS,
    ("joycon_r", "horizontal"): _JCR_HORIZONTAL,
    ("joycon_l", "horizontal"): _JCL_HORIZONTAL,
    ("joycon_r", "vertical"): _JCR_VERTICAL,
    ("joycon_l", "vertical"): _JCL_VERTICAL,
}
# Digital ZL/ZR drive the canonical trigger axes (0 or AXIS_MAX).
DIGITAL_TRIGGERS: dict[tuple[str, str | None], dict[int, str]] = {
    ("pro", None): {Bit.ZL: "left_trigger", Bit.ZR: "right_trigger"},
    ("gamecube", None): {},
    ("joycon_r", "horizontal"): {},
    ("joycon_l", "horizontal"): {},
    ("joycon_r", "vertical"): {Bit.ZR: "right_trigger"},
    ("joycon_l", "vertical"): {Bit.ZL: "left_trigger"},
}
# Stick routing: canonical axis <- (report stick slot, component, sign). Canonical y
# is negative = up; raw y grows upwards, hence the -1 on the upright layouts.
STICK_MAPS: dict[tuple[str, str | None], tuple[tuple[str, str, str, int], ...]] = {
    ("pro", None): (("left_x", "left", "x", 1), ("left_y", "left", "y", -1),
                    ("right_x", "right", "x", 1), ("right_y", "right", "y", -1)),
    ("gamecube", None): (("left_x", "left", "x", 1), ("left_y", "left", "y", -1),
                         ("right_x", "right", "x", 1), ("right_y", "right", "y", -1)),
    # Sideways R: rotated 90 deg clockwise, so stick "up" (raw y+) points right.
    ("joycon_r", "horizontal"): (("left_x", "right", "y", 1), ("left_y", "right", "x", 1)),
    # Sideways L: rotated 90 deg counter-clockwise, raw y+ points left.
    ("joycon_l", "horizontal"): (("left_x", "left", "y", -1), ("left_y", "left", "x", -1)),
    ("joycon_r", "vertical"): (("right_x", "right", "x", 1), ("right_y", "right", "y", -1)),
    ("joycon_l", "vertical"): (("left_x", "left", "x", 1), ("left_y", "left", "y", -1)),
}


def mapping_key(model: str, orientation: str | None = None) -> tuple[str, str | None]:
    """Key into the mapping tables; Joy-Cons default to sideways (``horizontal``)."""
    model = normalize_model(model) or ""
    if model not in MODELS:
        raise ValueError(f"unknown model {model!r}")
    if model.startswith("joycon"):
        orientation = orientation or "horizontal"
        if orientation not in ORIENTATIONS:
            raise ValueError(f"orientation must be one of {ORIENTATIONS}, got {orientation!r}")
        return model, orientation
    return model, None


# --- calibration -------------------------------------------------------------------

@dataclass(frozen=True)
class AxisCalibration:
    """One 12-bit stick axis: ``neutral`` plus distances to the extremes (not absolutes)."""

    neutral: int = 2048
    rel_max: int = 1610
    rel_min: int = 1610

    def normalize(self, raw: int) -> float:
        """Map a raw 12-bit value to -1.0..1.0 (positive = raw grows)."""
        d = raw - self.neutral
        span = self.rel_min if d < 0 else self.rel_max
        if not self.neutral or span <= 0:
            d, span = raw - 2048, 2048
        return max(-1.0, min(1.0, d / span))


@dataclass(frozen=True)
class StickCalibration:
    x: AxisCalibration = AxisCalibration()
    y: AxisCalibration = AxisCalibration()

    @classmethod
    def uniform(cls, span: int, neutral: int = 2048) -> "StickCalibration":
        a = AxisCalibration(neutral, span, span)
        return cls(a, a)


def unpack_u12_pair(data: bytes | bytearray, offset: int = 0) -> tuple[int, int]:
    """Two little-endian 12-bit values packed into 3 bytes (sticks, calibration)."""
    b0, b1, b2 = data[offset], data[offset + 1], data[offset + 2]
    return b0 | ((b1 & 0x0F) << 8), (b1 >> 4) | (b2 << 4)


def pack_u12_pair(a: int, b: int) -> bytes:
    a &= 0xFFF
    b &= 0xFFF
    return bytes((a & 0xFF, (a >> 8) | ((b & 0x0F) << 4), b >> 4))


def parse_stick_calibration(data: bytes | bytearray) -> StickCalibration | None:
    """9-byte block: neutral (x, y), max distance (x, y), min distance (x, y).

    Returns None for erased flash (``FF``...) or implausible values.
    """
    if len(data) < 9 or all(b == 0xFF for b in data[:9]):
        return None
    nx, ny = unpack_u12_pair(data, 0)
    mx, my = unpack_u12_pair(data, 3)
    lx, ly = unpack_u12_pair(data, 6)
    vals = (nx, ny, mx, my, lx, ly)
    if any(v in (0, 0xFFF) for v in vals) or not (512 < nx < 3584 and 512 < ny < 3584):
        return None
    return StickCalibration(AxisCalibration(nx, mx, lx), AxisCalibration(ny, my, ly))


def pack_stick_calibration(cal: StickCalibration) -> bytes:
    """Inverse of :func:`parse_stick_calibration` (used for tests and fake devices)."""
    return (pack_u12_pair(cal.x.neutral, cal.y.neutral)
            + pack_u12_pair(cal.x.rel_max, cal.y.rel_max)
            + pack_u12_pair(cal.x.rel_min, cal.y.rel_min))


USER_CALIBRATION_MAGIC = b"\xb2\xa1"


def parse_user_stick_calibration(data: bytes | bytearray) -> StickCalibration | None:
    """User calibration record: magic ``B2 A1`` then the 9-byte block."""
    if len(data) < 11 or bytes(data[:2]) != USER_CALIBRATION_MAGIC:
        return None
    return parse_stick_calibration(data[2:11])


# Defaults when flash cannot be read: BlueRetro's Pro/GameCube ranges; Joy-Con 2
# factory blocks seen so far hold spans of roughly 1180-1210.
_DEFAULT_SPANS = {"pro": (1610, 1610), "joycon_l": (1200, 1200), "joycon_r": (1200, 1200),
                  "gamecube": (1225, 1120)}
TRIGGER_ZERO_DEFAULT = 30     # GameCube trigger at rest (raw)
TRIGGER_FULL_DEFAULT = 232    # raw value treated as fully pressed (SDL)


@dataclass(frozen=True)
class Calibration:
    """Per-controller conversion parameters. ``left``/``right`` are report stick slots."""

    left: StickCalibration = StickCalibration()
    right: StickCalibration = StickCalibration()
    left_trigger_zero: int = TRIGGER_ZERO_DEFAULT
    right_trigger_zero: int = TRIGGER_ZERO_DEFAULT
    trigger_full: int = TRIGGER_FULL_DEFAULT
    deadzone: float = DEFAULT_DEADZONE
    source: str = "default"

    @classmethod
    def default(cls, model: str = "pro", deadzone: float = DEFAULT_DEADZONE) -> "Calibration":
        l_span, r_span = _DEFAULT_SPANS.get(normalize_model(model) or "pro", (1610, 1610))
        return cls(StickCalibration.uniform(l_span), StickCalibration.uniform(r_span),
                   deadzone=deadzone)

    def with_deadzone(self, deadzone: float) -> "Calibration":
        return Calibration(self.left, self.right, self.left_trigger_zero,
                           self.right_trigger_zero, self.trigger_full, deadzone, self.source)


# Flash addresses (ndeadly memory_layout.md).
ADDR_DEVICE_INFO = 0x13000
ADDR_FACTORY_STICK_PRIMARY = 0x13080     # 0x40 block, calibration at +0x28
ADDR_FACTORY_STICK_SECONDARY = 0x130C0
FACTORY_STICK_OFFSET = 0x28
ADDR_TRIGGER_ZERO = 0x13140              # GameCube: left, right trigger rest values
ADDR_USER_STICKS = 0x1FC040              # primary at +0x00, secondary at +0x20
USER_SECONDARY_OFFSET = 0x20


def calibration_from_flash(model: str, factory_primary: bytes | None = None,
                           factory_secondary: bytes | None = None,
                           user_block: bytes | None = None,
                           trigger_zero: bytes | None = None,
                           deadzone: float = DEFAULT_DEADZONE) -> Calibration:
    """Build a :class:`Calibration` from raw flash blocks (any may be None).

    ``factory_*`` are the 0x40-byte blocks read at 0x13080/0x130C0, ``user_block``
    the 0x40 bytes at 0x1FC040 (user calibration overrides factory data) and
    ``trigger_zero`` the 2 bytes at 0x13140. The primary stick is the left slot,
    except on Joy-Con 2 (R) whose only stick reports in the right slot.
    """
    model = normalize_model(model) or "pro"
    base = Calibration.default(model, deadzone)
    prim = sec = None
    source = "default"

    def factory(block: bytes | None) -> StickCalibration | None:
        if block is None or len(block) < FACTORY_STICK_OFFSET + 9:
            return None
        return parse_stick_calibration(block[FACTORY_STICK_OFFSET:FACTORY_STICK_OFFSET + 9])

    prim, sec = factory(factory_primary), factory(factory_secondary)
    if prim or sec:
        source = "factory"
    if user_block is not None:
        up = parse_user_stick_calibration(user_block[:11])
        us = parse_user_stick_calibration(
            user_block[USER_SECONDARY_OFFSET:USER_SECONDARY_OFFSET + 11])
        if up or us:
            source = "user"
        prim, sec = up or prim, us or sec
    left, right = base.left, base.right
    if model == "joycon_r":
        right = prim or right
    else:
        left = prim or left
        right = sec or right
    lz, rz = base.left_trigger_zero, base.right_trigger_zero
    if model == "gamecube" and trigger_zero is not None and len(trigger_zero) >= 2:
        # Accept only plausible rest values; erased flash reads 0xFF.
        if trigger_zero[0] < 128:
            lz = trigger_zero[0]
        if trigger_zero[1] < 128:
            rz = trigger_zero[1]
    return Calibration(left, right, lz, rz, base.trigger_full, deadzone, source)


def apply_radial_deadzone(x: float, y: float, deadzone: float) -> tuple[float, float]:
    """Zero inside ``deadzone``; rescale outside so full deflection stays 1.0.

    The magnitude is not clamped to the unit circle (diagonals keep their
    per-axis values, as SDL reports them); callers clamp each axis.
    """
    if deadzone <= 0:
        return x, y
    if deadzone >= 1:
        raise ValueError(f"deadzone must be in [0, 1), got {deadzone}")
    mag = math.hypot(x, y)
    if mag <= deadzone:
        return 0.0, 0.0
    scale = (mag - deadzone) / (1.0 - deadzone) / mag
    return x * scale, y * scale


def normalize_trigger(raw: int, zero: int, full: int = TRIGGER_FULL_DEFAULT) -> int:
    """GameCube analog trigger (raw byte) -> 0..AXIS_MAX."""
    if full <= zero:
        full = 255
    frac = (raw - zero) / (full - zero)
    return int(round(max(0.0, min(1.0, frac)) * AXIS_MAX))


def _to_axis(v: float) -> int:
    return max(AXIS_MIN, min(AXIS_MAX, int(round(v * AXIS_MAX))))


# --- input report parsing --------------------------------------------------------

@dataclass
class Switch2Report:
    """One decoded input report: canonical values plus the raw fields."""

    model: str
    report_id: int
    buttons: list[int]
    axes: list[int]
    raw_buttons: int                   # report 0x05 bit layout (see :class:`Bit`)
    raw_sticks: tuple[int, int, int, int]  # lx, ly, rx, ry (12-bit, 0 if absent)
    raw_triggers: tuple[int, int] = (0, 0)
    counter: int = 0                   # 0x05: u32 (ms over BLE); model reports: u8
    battery_mv: int | None = None
    battery_level: int | None = None   # 0..9 (model-specific reports)
    charging: bool | None = None
    external_power: bool | None = None

    def pad_state(self) -> PadState:
        return PadState(list(self.buttons), list(self.axes))

    def pressed_bits(self) -> list[str]:
        return [BIT_NAMES[b] for b in sorted(BIT_NAMES) if self.raw_buttons >> b & 1]


def _model_report_buttons(report_id: int, data: bytes | bytearray) -> int:
    out = 0
    for off, mask, bit in _MODEL_REPORT_BITS[report_id]:
        if data[off] & mask:
            out |= 1 << bit
    return out


def parse_input_report(kind: str, data: bytes | bytearray,
                       calibration: Calibration | None = None, *,
                       report: int = REPORT_COMMON,
                       orientation: str | None = None) -> Switch2Report:
    """Decode a Switch 2 BLE input notification into canonical model values.

    ``kind`` is a model key (``pro``, ``joycon_l``, ``joycon_r``, ``gamecube``);
    ``report`` is 0x05 (characteristic ``...7fd2``, all models) or the model's own
    report id (0x07/0x08/0x09/0x0A). ``orientation`` applies to Joy-Cons only:
    ``horizontal`` (default, single Joy-Con held sideways) or ``vertical``.
    Sticks come out as int16 with y negative = up; triggers 0..32767.
    """
    model = normalize_model(kind) or ""
    key = mapping_key(model, orientation)
    spec = MODELS[model]
    cal = calibration or Calibration.default(model)
    data = bytes(data)
    trig = (0, 0)
    mv = level = None
    charging = ext = None
    if report == REPORT_COMMON:
        if len(data) < 16:
            raise ValueError(f"report 0x05 too short: {len(data)} bytes")
        counter, raw_buttons = struct.unpack_from("<II", data, 0)
        lx, ly = unpack_u12_pair(data, 0x0A)
        rx, ry = unpack_u12_pair(data, 0x0D)
        if len(data) >= 0x21:
            mv = struct.unpack_from("<H", data, 0x1F)[0] or None
        if model == "gamecube" and len(data) >= 0x3E:
            trig = (data[0x3C], data[0x3D])
    elif report == spec.report_id:
        if len(data) < 11:
            raise ValueError(f"report 0x{report:02X} too short: {len(data)} bytes")
        counter = data[0]
        power = data[1]
        ext, charging, level = bool(power & 1), bool(power & 2), (power >> 2) & 0x0F
        raw_buttons = _model_report_buttons(report, data)
        if spec.sticks == "both":
            lx, ly = unpack_u12_pair(data, 5)
            rx, ry = unpack_u12_pair(data, 8)
        elif spec.sticks == "left":
            (lx, ly), (rx, ry) = unpack_u12_pair(data, 5), (0, 0)
        else:
            (lx, ly), (rx, ry) = (0, 0), unpack_u12_pair(data, 5)
        if model == "gamecube" and len(data) >= 0x0E:
            trig = (data[0x0C], data[0x0D])
    else:
        raise ValueError(f"report 0x{report:02X} is not valid for model {model!r}")

    buttons = [0] * NUM_BUTTONS
    for bit, name in BUTTON_MAPS[key].items():
        if raw_buttons >> bit & 1:
            buttons[BUTTON_INDEX[name]] = 1
    axes = [0] * NUM_AXES
    for bit, name in DIGITAL_TRIGGERS[key].items():
        if raw_buttons >> bit & 1:
            axes[AXIS_INDEX[name]] = AXIS_MAX
    # Normalize each used stick slot, deadzone it as a 2D vector, then route it.
    slots: dict[str, tuple[float, float]] = {}
    for slot, (x, y), sc in (("left", (lx, ly), cal.left), ("right", (rx, ry), cal.right)):
        if any(src == slot for _, src, _, _ in STICK_MAPS[key]):
            slots[slot] = apply_radial_deadzone(sc.x.normalize(x), sc.y.normalize(y),
                                                cal.deadzone)
    for axis, src, comp, sign in STICK_MAPS[key]:
        v = slots[src][0 if comp == "x" else 1]
        axes[AXIS_INDEX[axis]] = _to_axis(sign * v)
    if model == "gamecube":
        axes[AXIS_INDEX["left_trigger"]] = normalize_trigger(trig[0], cal.left_trigger_zero,
                                                             cal.trigger_full)
        axes[AXIS_INDEX["right_trigger"]] = normalize_trigger(trig[1], cal.right_trigger_zero,
                                                              cal.trigger_full)
    return Switch2Report(model, report, buttons, axes, raw_buttons, (lx, ly, rx, ry), trig,
                         counter, mv, level, charging, ext)


# --- commands ------------------------------------------------------------------------

DIR_REQUEST = 0x91
DIR_RESPONSE = 0x01
TRANSPORT_USB = 0x00
TRANSPORT_BLE = 0x01
CMD_FLASH = 0x02
SUB_MEMORY_READ = 0x04
CMD_LEDS = 0x09
SUB_LED_PATTERN = 0x07
CMD_FEATURES = 0x0C
SUB_FEATURE_SET_MASK = 0x02
SUB_FEATURE_ENABLE = 0x04
MAX_BLE_READ = 0x4F

# Player LED patterns for players 1..8 (as SDL uses them).
PLAYER_LED_PATTERNS = (0x1, 0x3, 0x7, 0xF, 0x9, 0x5, 0xD, 0x6)


def build_command(cmd: int, sub: int, data: bytes = b"", transport: int = TRANSPORT_BLE) -> bytes:
    """8-byte header ``cmd 91 transport sub 00 len 00 00`` followed by ``data``."""
    if len(data) > 0xFF:
        raise ValueError("command data too long")
    return bytes((cmd, DIR_REQUEST, transport, sub, 0x00, len(data), 0x00, 0x00)) + bytes(data)


def build_memory_read(address: int, length: int = 0x40) -> bytes:
    if not 0 < length <= MAX_BLE_READ:
        raise ValueError(f"BLE flash reads are limited to 1..0x{MAX_BLE_READ:X} bytes")
    return build_command(CMD_FLASH, SUB_MEMORY_READ,
                         bytes((length, 0x7E, 0, 0)) + struct.pack("<I", address))


def build_player_leds(player: int | None = 1, pattern: int | None = None) -> bytes:
    """LED command for player 1..8 (or an explicit 4-bit ``pattern``)."""
    if pattern is None:
        pattern = PLAYER_LED_PATTERNS[(player - 1) % 8] if player else 0
    return build_command(CMD_LEDS, SUB_LED_PATTERN, bytes((pattern & 0x0F,)) + bytes(7))


def build_feature_command(sub: int, flags: int) -> bytes:
    return build_command(CMD_FEATURES, sub, bytes((flags & 0xFF, 0, 0, 0)))


@dataclass(frozen=True)
class CommandResponse:
    cmd: int
    sub: int
    transport: int
    ack: int
    payload: bytes


def parse_command_response(data: bytes | bytearray) -> CommandResponse | None:
    """Decode a command response notification.

    Responses on ``c765a961...`` (handle 0x001A) start with the 8-byte header;
    the per-model extended response characteristic (0x001E) prefixes 14 zero
    bytes. Both are accepted.
    """
    data = bytes(data)
    for off in (0, 14):
        if len(data) >= off + 8 and data[off + 1] == DIR_RESPONSE and data[off + 2] in (
                TRANSPORT_USB, TRANSPORT_BLE) and data[off] != 0:
            if off and any(data[:off]):
                continue
            h = data[off:off + 8]
            return CommandResponse(h[0], h[3], h[2], h[5], data[off + 8:])
    return None


def parse_memory_read(resp: CommandResponse) -> tuple[int, bytes]:
    """(address, data) from a flash read response: ``len 00 00 00 addr[4] data``."""
    p = resp.payload
    if resp.cmd != CMD_FLASH or len(p) < 8:
        raise ValueError("not a flash read response")
    length = p[0]
    addr = struct.unpack_from("<I", p, 4)[0]
    return addr, p[8:8 + length]


def parse_device_info_block(block: bytes) -> dict[str, Any]:
    """Factory block at 0x13000: serial, VID/PID and body/button colours."""
    out: dict[str, Any] = {}
    if len(block) >= 0x12:
        out["serial"] = block[2:0x12].split(b"\x00", 1)[0].decode("ascii", "replace")
    if len(block) >= 0x16:
        out["vendor_id"], out["product_id"] = struct.unpack_from("<HH", block, 0x12)
    if len(block) >= 0x25:
        names = ("body", "buttons", "highlight", "grip")
        out["colors"] = {n: "#" + block[0x19 + 3 * i:0x1C + 3 * i].hex()
                         for i, n in enumerate(names)
                         if block[0x19 + 3 * i:0x1C + 3 * i] != b"\xff\xff\xff"}
    return out


# --- advertisements ------------------------------------------------------------------

@dataclass
class Switch2Device:
    """A Switch 2 controller seen advertising."""

    address: str
    model: str | None                # MODELS key, None for unknown Switch 2 PIDs
    product_id: int
    name: str
    rssi: int | None = None
    pairing_mode: bool = True        # host address all zero (sync button held)
    wake: bool = False               # "wake console" advertisement
    host_address: str | None = None  # console it wants to reconnect to
    device: Any = None               # bleak BLEDevice, for connecting without a rescan

    def describe(self) -> str:
        mode = "pairing" if self.pairing_mode else ("wake" if self.wake else "reconnect")
        host = f" -> host {self.host_address}" if self.host_address else ""
        rssi = f"{self.rssi} dBm" if self.rssi is not None else "? dBm"
        return f"{self.address}  {self.name:<40} {rssi:>8}  {mode}{host}"


def parse_manufacturer_data(payload: bytes | bytearray) -> dict[str, Any] | None:
    """Parse Nintendo manufacturer data (bytes after the 0x0553 company id).

    Returns ``{vendor_id, product_id, model, wake, host_address, pairing_mode}``
    or None if it is not a Switch 2 controller advertisement.
    """
    p = bytes(payload)
    if len(p) < 9:
        return None
    vid, pid = struct.unpack_from("<HH", p, 3)
    if vid != NINTENDO_VENDOR_ID or pid < 0x2060 or pid > 0x20FF:
        return None
    host = p[10:16] if len(p) >= 16 else b""
    host_addr = ":".join(f"{b:02X}" for b in reversed(host)) if host and any(host) else None
    return {"vendor_id": vid, "product_id": pid, "model": MODEL_BY_PID.get(pid),
            "wake": len(p) > 9 and p[9] == 0x81, "host_address": host_addr,
            "pairing_mode": host_addr is None}


def _device_from_adv(dev: Any, adv: Any) -> Switch2Device | None:
    md = getattr(adv, "manufacturer_data", None) or {}
    payload = md.get(NINTENDO_COMPANY_ID)
    if payload is None:
        return None
    info = parse_manufacturer_data(payload)
    if info is None:
        return None
    model = info["model"]
    name = MODELS[model].name if model else f"Switch 2 controller (PID {info['product_id']:04X})"
    return Switch2Device(address=str(dev.address).upper(), model=model,
                         product_id=info["product_id"], name=name,
                         rssi=getattr(adv, "rssi", None), pairing_mode=info["pairing_mode"],
                         wake=info["wake"], host_address=info["host_address"], device=dev)


PAIRING_INSTRUCTIONS = (
    "Put the controller in pairing mode: hold its small SYNC button (top edge of the "
    "Pro / GameCube controller, rail side of a Joy-Con 2) until the player LEDs run "
    "back and forth. Do NOT pair it in Windows Settings: Windows' standard (SMP) "
    "pairing makes Switch 2 controllers disconnect; if it is listed there already, "
    "remove it first. Keep the Switch 2 console off or out of range.")


def _bleak_module() -> Any:
    """Import bleak lazily so the parsing layer works without it."""
    try:
        import bleak
    except ImportError as e:  # pragma: no cover - depends on environment
        raise RuntimeError("the 'bleak' package is required for Switch 2 BLE support: "
                           "pip install bleak") from e
    return bleak


async def discover_async(timeout_s: float = 8.0, *, include_reconnecting: bool = True,
                         model: str | None = None) -> list[Switch2Device]:
    """Scan for Switch 2 controllers (strongest signal first)."""
    bleak = _bleak_module()
    want = normalize_model(model)
    found = await bleak.BleakScanner.discover(timeout=timeout_s, return_adv=True)
    out = []
    for dev, adv in found.values():
        d = _device_from_adv(dev, adv)
        if d is None or (want and d.model != want):
            continue
        if not include_reconnecting and not d.pairing_mode:
            continue
        out.append(d)
    out.sort(key=lambda d: -(d.rssi if d.rssi is not None else -999))
    return out


def discover(timeout_s: float = 8.0, *, include_reconnecting: bool = True,
             model: str | None = None) -> list[Switch2Device]:
    """Blocking wrapper around :func:`discover_async` (not for use inside an event loop)."""
    return asyncio.run(discover_async(timeout_s, include_reconnecting=include_reconnecting,
                                      model=model))


# --- Windows connection parameters -------------------------------------------------

CONNECTION_PRIORITIES = ("throughput", "balanced", "power")


def _winrt_device(client: Any) -> Any:
    """bleak's WinRT ``BluetoothLEDevice`` (private attribute, may disappear)."""
    return getattr(getattr(client, "_backend", None), "_requester", None)


def request_connection_priority(client: Any, priority: str = "throughput") -> tuple[Any, str]:
    """Ask Windows 11 for preferred LE connection parameters.

    Returns ``(request, status)``; keep ``request`` alive for as long as the
    parameters should apply (closing it withdraws the request). bleak has no API
    for this, so it reaches into bleak's WinRT backend.
    """
    if sys.platform != "win32":
        return None, "unsupported: not Windows"
    dev = _winrt_device(client)
    if dev is None:
        return None, "unsupported: no WinRT device handle"
    try:
        from winrt.windows.devices.bluetooth import BluetoothLEPreferredConnectionParameters as P
    except ImportError as e:  # pragma: no cover
        return None, f"unsupported: {e}"
    params = {"throughput": "throughput_optimized", "balanced": "balanced",
              "power": "power_optimized"}[priority]
    try:
        req = dev.request_preferred_connection_parameters(getattr(P, params))
        status = getattr(req.status, "name", str(req.status))
        return req, status.lower()
    except Exception as e:  # Windows 10 lacks the API; WinRT raises OSError/RuntimeError
        return None, f"unsupported: {e}"


def connection_interval_ms(client: Any) -> float | None:
    """Current LE connection interval (Windows 11), or None if unknown.

    Never raises: it is polled every second, and a WinRT error on a closing
    link must not tear the session down.
    """
    dev = _winrt_device(client)
    try:
        return dev.get_connection_parameters().connection_interval * 1.25 if dev else None
    except Exception:  # Windows 10 (no API), closed device objects, WinRT RuntimeError...
        return None


# --- backend ---------------------------------------------------------------------------

class RateMeter:
    """Reports per second over a sliding window of arrival times (thread-safe)."""

    def __init__(self, window_s: float = 2.0) -> None:
        self.window_ns = int(window_s * 1e9)
        self._t: deque[int] = deque()
        self._lock = threading.Lock()
        self.count = 0

    def add(self, t_ns: int) -> None:
        with self._lock:
            self._t.append(t_ns)
            self.count += 1
            while len(self._t) > 2 and self._t[-1] - self._t[0] > self.window_ns:
                self._t.popleft()

    def hz(self, now: int | None = None) -> float:
        with self._lock:
            if len(self._t) < 2:
                return 0.0
            if now is not None and now - self._t[-1] > self.window_ns:
                return 0.0
            span = self._t[-1] - self._t[0]
            return (len(self._t) - 1) * 1e9 / span if span > 0 else 0.0

    def reset(self) -> None:
        with self._lock:
            self._t.clear()


class ControllerNotFound(RuntimeError):
    pass


class _Stopped(Exception):
    """stop() was requested while waiting."""


class Switch2BleBackend(Backend):
    """Reads one Switch 2 controller over BLE GATT and publishes it to the hub.

    ``address`` picks a specific controller (any advertisement type); without it
    the first controller seen in pairing mode (optionally of ``model``) is used.
    Runs its own asyncio loop in the backend thread (bleak delivers notifications
    there); timestamps are ``perf_counter_ns`` at notification delivery.
    """

    name = "switch2_ble"

    def __init__(self, hub: Hub, address: str | None = None, model: str | None = None, *,
                 orientation: str = "horizontal", deadzone: float = DEFAULT_DEADZONE,
                 player: int | None = 1, features: int | None = DEFAULT_FEATURES,
                 report: str = "common", connection_priority: str | None = "throughput",
                 reconnect: bool = True, scan_timeout_s: float = 10.0,
                 connect_timeout_s: float = 20.0, command_timeout_s: float = 1.0,
                 read_calibration: bool = True, retry_delay_s: float = 1.0) -> None:
        super().__init__(hub)
        if orientation not in ORIENTATIONS:
            raise ValueError(f"orientation must be one of {ORIENTATIONS}")
        if report not in ("common", "model"):
            raise ValueError("report must be 'common' or 'model'")
        if connection_priority is not None and connection_priority not in CONNECTION_PRIORITIES:
            raise ValueError(f"connection_priority must be one of {CONNECTION_PRIORITIES}")
        if not 0 <= deadzone < 1:
            raise ValueError(f"deadzone must be in [0, 1), got {deadzone}")
        self.address = normalize_address(address)
        self.model = normalize_model(model)
        self.orientation = orientation
        self.deadzone = deadzone
        self.player = player
        self.features = features
        self.report_kind = report
        self.connection_priority = connection_priority
        self.reconnect = reconnect
        self.scan_timeout_s = scan_timeout_s
        self.connect_timeout_s = connect_timeout_s
        self.command_timeout_s = command_timeout_s
        self.read_calibration = read_calibration
        self.retry_delay_s = retry_delay_s
        self.connected = threading.Event()
        self.calibration: Calibration | None = None
        self.last_error: BaseException | None = None
        self.status = "idle"
        self.dev_id: int | None = None
        self.info: DeviceInfo | None = None
        self._rate = RateMeter()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        # (cmd, sub) -> (future, flash address expected in the answer or None)
        self._pending: dict[tuple[int, int], tuple[asyncio.Future, int | None]] = {}
        self._last: PadState | None = None
        self._active_model: str | None = None
        self._active_report = REPORT_COMMON
        self._counters: deque[int] = deque(maxlen=64)
        self._counters_lock = threading.Lock()
        self._last_counter_t = 0
        self._answers = 0          # command responses received this session
        self._unanswered = 0       # commands that timed out this session
        self._last_report: Switch2Report | None = None
        self._conn_request: Any = None
        self._conn_interval_ms: float | None = None
        self._bound_address: str | None = None
        self.reports = 0
        self.dropped = 0
        self.parse_errors = 0

    # -- public helpers ------------------------------------------------------
    @property
    def last_report(self) -> Switch2Report | None:
        """The most recent decoded report (raw bits, battery...), or None."""
        return self._last_report

    def report_rate_hz(self) -> float:
        """Measured notification rate over the last ~2 s (0 when idle)."""
        return round(self._rate.hz(now_ns()), 1)

    def stats(self) -> dict[str, Any]:
        rep = self._last_report
        d: dict[str, Any] = {"status": self.status, "report_rate_hz": self.report_rate_hz(),
                             "reports": self.reports, "dropped_estimate": self.dropped,
                             "parse_errors": self.parse_errors}
        dev_ms = self._device_interval_ms()
        if dev_ms is not None:
            d["device_interval_ms"] = dev_ms
        if self._conn_interval_ms is not None:
            d["conn_interval_ms"] = self._conn_interval_ms
        if rep is not None:
            if rep.battery_mv:
                d["battery_mv"] = rep.battery_mv
            if rep.battery_level is not None:
                d["battery_level"] = rep.battery_level
        return d

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        loop, wake = self._loop, self._wake
        if loop is not None and wake is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(wake.set)
            except RuntimeError:
                pass
        super().stop(timeout)

    # -- thread / loop -------------------------------------------------------
    def run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        if self._stop.is_set():
            return
        _bleak_module()   # fail fast (sets .error) if bleak is missing
        self.ready.set()
        failures = 0      # consecutive errors other than "not found" (adapter off...)
        while not self._stop.is_set():
            try:
                await self._session()
                failures = 0
            except asyncio.CancelledError:
                raise
            except _Stopped:
                break
            except Exception as e:
                self.last_error = e
                self.status = f"error: {e}"
                if not self.reconnect:
                    raise
                if isinstance(e, ControllerNotFound):
                    log.info("switch2: %s", e)
                else:
                    failures += 1
                    log.warning("switch2: %s", e, exc_info=log.isEnabledFor(logging.DEBUG))
            if not self.reconnect:
                break
            # Back off on repeated errors (e.g. Bluetooth turned off), at most 10 s.
            delay = self.retry_delay_s * 2 ** min(max(failures - 1, 0), 4)
            await self._sleep(min(delay, max(self.retry_delay_s, 10.0)))
        self.status = "stopped"

    async def _sleep(self, seconds: float) -> None:
        assert self._wake is not None
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def _guard(self, aw: Awaitable[Any], *abort: asyncio.Event) -> Any:
        """Await ``aw`` unless stop() or one of the ``abort`` events comes first.

        The awaitable is then cancelled; stop() raises :class:`_Stopped`, an
        abort event (the link dropped) raises ``ConnectionError``.
        """
        assert self._wake is not None
        task = asyncio.ensure_future(aw)
        waiters = [asyncio.ensure_future(e.wait()) for e in (self._wake, *abort)]
        try:
            done, _ = await asyncio.wait({task, *waiters}, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            task.cancel()
            raise
        finally:
            for w in waiters:
                w.cancel()
        if task in done:
            return task.result()
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        if self._wake.is_set():
            raise _Stopped()
        raise ConnectionError("controller disconnected during setup")

    # -- one connection ------------------------------------------------------
    async def _find(self) -> tuple[Any, str | None]:
        bleak = _bleak_module()
        seen: dict[str, Any] = {}

        target = self.address or self._bound_address

        def match(dev: Any, adv: Any) -> bool:
            d = _device_from_adv(dev, adv)
            if d is None:
                return False
            if target:
                ok = d.address == target
            else:
                # Only known models: connecting to an unknown Switch 2 PID would fail
                # model resolution and then be retried forever.
                ok = (d.pairing_mode and d.model is not None
                      and (self.model is None or d.model == self.model))
            if ok:
                seen["dev"] = d
            return ok

        self.status = "scanning" + (f" for {target}" if target else "")
        dev = await self._guard(bleak.BleakScanner.find_device_by_filter(
            match, timeout=self.scan_timeout_s))
        if dev is None:
            what = target or "a Switch 2 controller in pairing mode"
            raise ControllerNotFound(f"no advertisement from {what} within "
                                     f"{self.scan_timeout_s:g} s")
        d: Switch2Device = seen["dev"]
        return dev, d.model

    async def _session(self) -> None:
        bleak = _bleak_module()
        dev, adv_model = await self._find()
        address = str(dev.address).upper()
        disconnected = asyncio.Event()
        loop = asyncio.get_running_loop()

        def on_disconnect(_client: Any) -> None:
            loop.call_soon_threadsafe(disconnected.set)

        self.status = f"connecting to {address}"
        client = bleak.BleakClient(dev, disconnected_callback=on_disconnect,
                                   timeout=self.connect_timeout_s,
                                   winrt={"use_cached_services": False})
        # Never pair: Switch 2 controllers drop the link on SMP.
        await self._guard(client.connect())
        dev_id = None
        try:
            prio = "not requested"
            if self.connection_priority:
                self._conn_request, prio = request_connection_priority(
                    client, self.connection_priority)
            model = self._resolve_model(client, adv_model)
            self._active_model = model
            spec = MODELS[model]
            self.status = "initialising"
            cal, flash = await self._guard(self._initialize(client, model), disconnected)
            self.calibration = cal
            input_uuid = INPUT_COMMON_UUID if self.report_kind == "common" else spec.input_uuid
            self._active_report = REPORT_COMMON if self.report_kind == "common" else spec.report_id
            extra: dict[str, Any] = {
                "address": address, "model": model, "report": f"0x{self._active_report:02X}",
                "calibration": cal.source, "conn_request": prio,
            }
            if model.startswith("joycon"):
                extra["orientation"] = self.orientation
            if flash.get("colors"):
                extra["colors"] = flash["colors"]
            info = DeviceInfo(id=-1, name=spec.name, backend=self.name, sdl_type=spec.sdl_type,
                              family=spec.family, vendor_id=NINTENDO_VENDOR_ID,
                              product_id=spec.product_id, connection="wireless",
                              serial=flash.get("serial", ""), path=f"ble:{address}", extra=extra)
            self._last = PadState()
            self._last_report = None
            self._rate.reset()
            with self._counters_lock:
                self._counters.clear()
            dev_id = self.hub.connect((self.name, address), info)
            self.dev_id, self.info = dev_id, info
            self._bound_address = address
            await self._guard(client.start_notify(input_uuid, self._on_report), disconnected)
            self.connected.set()
            self.status = "connected"
            log.info("switch2: connected #%d %s at %s (%s, calibration %s)", dev_id, spec.name,
                     address, prio, cal.source)
            assert self._wake is not None
            while not self._stop.is_set() and not disconnected.is_set():
                waiters = [asyncio.ensure_future(disconnected.wait()),
                           asyncio.ensure_future(self._wake.wait())]
                try:
                    await asyncio.wait(waiters, timeout=1.0,
                                       return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for w in waiters:
                        w.cancel()
                self._conn_interval_ms = connection_interval_ms(client)
                self._refresh_extra()
            if disconnected.is_set():
                log.info("switch2: %s disconnected", address)
        finally:
            self.connected.clear()
            if dev_id is not None:
                self.hub.disconnect(dev_id)
            self.dev_id = None
            for fut, _addr in self._pending.values():
                fut.cancel()
            self._pending.clear()
            # Always disconnect, even after the link dropped: bleak's WinRT backend
            # only releases its GattDeviceService objects in disconnect().
            try:
                await asyncio.wait_for(client.disconnect(), 5.0)
            except Exception as e:  # already gone, adapter off...
                log.debug("switch2: disconnect: %s", e)
            if self._conn_request is not None:
                with contextlib.suppress(Exception):
                    self._conn_request.close()
                self._conn_request = None
            self._conn_interval_ms = None
            self.status = "disconnected"

    def _resolve_model(self, client: Any, adv_model: str | None) -> str:
        found = None
        try:
            uuids = {str(c.uuid).lower() for s in client.services for c in s.characteristics}
            found = next((MODEL_BY_INPUT_UUID[u] for u in uuids if u in MODEL_BY_INPUT_UUID),
                         None)
        except Exception:
            pass
        model = adv_model or found or self.model
        if self.model and model != self.model:
            log.warning("switch2: controller reports model %s, requested %s", model, self.model)
        if model is None:
            raise RuntimeError("could not identify the Switch 2 controller model; pass model=")
        return model

    def _refresh_extra(self) -> None:
        """Copy live stats into hub.devices[dev].extra.

        The dict is replaced, never mutated: the CONNECT event (and any sink
        that queued it) holds the original, and other threads may be
        serialising it right now.
        """
        if self.info is None:
            return
        stats = {k: v for k, v in self.stats().items() if k != "status"}
        self.info.extra = {**self.info.extra, **stats}

    def _count_drops(self, counter: int, t_ns: int) -> bool:
        """Estimate missed notifications from the controller's report counter.

        Returns False when the report should not become the new reference
        (an out-of-order model report).
        """
        if not self._counters:
            return True
        prev = self._counters[-1]
        if self._active_report == REPORT_COMMON:   # u32 milliseconds
            med = self._device_interval_ms()
            delta = (counter - prev) & 0xFFFFFFFF
            elapsed_ms = (t_ns - self._last_counter_t) / 1e6
            # A counter that ran ahead of the wall clock (or backwards) was reset
            # or garbled: resynchronise instead of counting phantom drops.
            if med and 1.5 * med < delta <= elapsed_ms + 1000:
                self.dropped += max(0, round(delta / med) - 1)
            return True
        gap = (counter - prev) & 0xFF              # u8, +1 per report
        if gap >= 0xF0:                            # a small step back: out of order
            return False
        if gap > 1:
            self.dropped += gap - 1
        return True

    def _device_interval_ms(self) -> float | None:
        """Median spacing of the controller's own report counter (report 0x05 only)."""
        with self._counters_lock:
            c = list(self._counters)
        if len(c) < 3:
            return None
        if self._active_report == REPORT_COMMON:   # u32 millisecond-ish counter
            deltas = [(b - a) & 0xFFFFFFFF for a, b in zip(c, c[1:])]
            return float(statistics.median(deltas))
        return None

    # -- init sequence ---------------------------------------------------------
    async def _initialize(self, client: Any, model: str) -> tuple[Calibration, dict[str, Any]]:
        flash: dict[str, Any] = {}
        cal = Calibration.default(model, self.deadzone)
        self._answers = self._unanswered = 0
        # Answers to 0x0014 writes arrive on 0x001A (BlueRetro, the console with
        # Joy-Cons) or, 14 zero bytes first, on the per-model 0x001E (ndeadly's PC
        # host, the console with a Pro Controller). Listen on both; the response
        # matcher ignores duplicates and stale answers.
        channels = 0
        for uuid in (COMMAND_RESPONSE_UUID, MODELS[model].ext_response_uuid):
            try:
                await client.start_notify(uuid, self._on_command_response)
                channels += 1
            except Exception as e:
                log.debug("switch2: no command responses on %s: %s", uuid, e)
        if not channels:
            log.warning("switch2: command channel unavailable; default calibration")
            return cal, flash
        if self.read_calibration:
            blk = await self._read_flash(client, ADDR_DEVICE_INFO, 0x40)
            if blk:
                flash.update(parse_device_info_block(blk))
                pid = flash.get("product_id")
                if pid in MODEL_BY_PID and MODEL_BY_PID[pid] != model:
                    log.warning("switch2: flash says PID %04X, using model %s", pid, model)
            prim = await self._read_flash(client, ADDR_FACTORY_STICK_PRIMARY, 0x40)
            sec = None
            if MODELS[model].sticks == "both":
                sec = await self._read_flash(client, ADDR_FACTORY_STICK_SECONDARY, 0x40)
            user = await self._read_flash(client, ADDR_USER_STICKS, 0x40)
            trig = None
            if model == "gamecube":
                trig = await self._read_flash(client, ADDR_TRIGGER_ZERO, 2)
            cal = calibration_from_flash(model, prim, sec, user, trig, self.deadzone)
        if self.player:
            await self._command(client, build_player_leds(self.player))
        if self.features:
            await self._command(client, build_feature_command(SUB_FEATURE_SET_MASK, self.features))
            await self._command(client, build_feature_command(SUB_FEATURE_ENABLE, self.features))
        await self._write_rate_descriptor(client, model)
        return cal, flash

    async def _write_rate_descriptor(self, client: Any, model: str) -> None:
        """Replicate the console's ``85 00`` write to the model report's 679d5510 descriptor."""
        try:
            for s in client.services:
                for c in s.characteristics:
                    if str(c.uuid).lower() != MODELS[model].input_uuid:
                        continue
                    for dsc in c.descriptors:
                        if str(dsc.uuid).lower() == REPORT_RATE_DESCRIPTOR_UUID:
                            await client.write_gatt_descriptor(dsc.handle, b"\x85\x00")
                            return
        except Exception as e:
            log.debug("switch2: rate descriptor write failed: %s", e)

    async def _command(self, client: Any, packet: bytes,
                       flash_address: int | None = None) -> CommandResponse | None:
        """Write a command (without response) and wait for its answer.

        ``flash_address`` makes the matcher accept only a flash read answer for
        that address, so a late answer to an earlier (timed out) read, or a
        duplicate delivered on the second response channel, cannot complete it.
        """
        key = (packet[0], packet[3])
        fut = asyncio.get_running_loop().create_future()
        self._pending[key] = (fut, flash_address)
        # Once a command went unanswered and nothing ever answered, the response
        # channel is not working: keep sending the rest, but don't stall the
        # connection for a full timeout on each of them.
        timeout = self.command_timeout_s
        if self._answers == 0 and self._unanswered:
            timeout = min(timeout, 0.1)
        try:
            await client.write_gatt_char(COMMAND_UUID, packet, response=False)
            resp = await asyncio.wait_for(fut, timeout)
            self._answers += 1
            return resp
        except asyncio.TimeoutError:
            self._unanswered += 1
            log.debug("switch2: no response to command %02X/%02X", *key)
            return None
        except Exception as e:
            log.debug("switch2: command %02X/%02X failed: %s", key[0], key[1], e)
            return None
        finally:
            entry = self._pending.get(key)
            if entry is not None and entry[0] is fut:
                del self._pending[key]

    async def _read_flash(self, client: Any, address: int, length: int) -> bytes | None:
        resp = await self._command(client, build_memory_read(address, length), address)
        if resp is None:
            return None
        try:
            addr, data = parse_memory_read(resp)
        except ValueError:
            return None
        if addr != address:
            log.debug("switch2: flash read for %X answered for %X", address, addr)
            return None
        if len(data) < length:
            log.debug("switch2: flash read at %X truncated to %d bytes (small ATT MTU?)",
                      address, len(data))
        return data

    # -- notification handlers (run on the backend's event loop) ---------------
    def _on_command_response(self, _sender: Any, data: bytearray) -> None:
        resp = parse_command_response(data)
        if resp is None:
            return
        entry = self._pending.get((resp.cmd, resp.sub))
        if entry is None or entry[0].done():
            return
        fut, want_addr = entry
        if want_addr is not None:
            try:
                addr, _ = parse_memory_read(resp)
            except ValueError:
                return
            if addr != want_addr:
                log.debug("switch2: ignoring stale flash answer for %X (waiting for %X)",
                          addr, want_addr)
                return
        fut.set_result(resp)

    def _on_report(self, _sender: Any, data: bytearray) -> None:
        t = now_ns()
        dev, model, last = self.dev_id, self._active_model, self._last
        if dev is None or model is None or last is None:
            return
        try:
            rep = parse_input_report(model, data, self.calibration, report=self._active_report,
                                     orientation=self.orientation)
        except Exception as e:  # never let a bad packet escape into bleak's callback
            self.parse_errors += 1
            log.debug("switch2: bad report (%d bytes): %s", len(data), e)
            return
        self._rate.add(t)
        self.reports += 1
        if self._count_drops(rep.counter, t):
            with self._counters_lock:
                self._counters.append(rep.counter)
            self._last_counter_t = t
        self._last_report = rep
        for i, v in enumerate(rep.buttons):
            if v != last.buttons[i]:
                last.buttons[i] = v
                self.hub.publish(InputEvent(t, dev, BUTTON, i, v))
        for i, v in enumerate(rep.axes):
            if v != last.axes[i]:
                last.axes[i] = v
                self.hub.publish(InputEvent(t, dev, AXIS, i, v))


# --- CLI -------------------------------------------------------------------------------

def format_state(state: PadState, family: str = FAMILY_SWITCH,
                 names: list[str] | None = None) -> str:
    """Compact one-line view: pressed buttons and all six axis values.

    ``names`` overrides the pressed-button list (e.g. :func:`nintendo_labels`
    of the raw report); otherwise canonical buttons get the family's labels and
    a trigger past the press threshold is listed too (digital ZL/ZR).
    """
    if names is None:
        labels = FACE_LABELS.get(family, {})
        names = [labels.get(n, n) for n, v in zip(BUTTONS, state.buttons) if v]
        names += [labels.get(n, n) for n in ("left_trigger", "right_trigger")
                  if state.pressed(n)]
    short = {"left_x": "LX", "left_y": "LY", "right_x": "RX", "right_y": "RY",
             "left_trigger": "LT", "right_trigger": "RT"}
    axes = " ".join(f"{short[n]}{v:+6d}" for n, v in zip(AXES, state.axes))
    return f"[{' '.join(names) or '-'}]  {axes}"


def _cmd_scan(args: Any) -> int:
    print(PAIRING_INSTRUCTIONS)
    print(f"\nScanning for {args.timeout:g} s...")
    try:
        devices = discover(args.timeout, include_reconnecting=not args.pairing_only,
                           model=args.model)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if not devices:
        print("No Switch 2 controllers found.")
        return 1
    for d in devices:
        print("  " + d.describe())
    print("\nConnect with:  controllerlog switch2 test <ADDRESS>")
    return 0


def _cmd_test(args: Any) -> int:
    hub = Hub()
    backend = Switch2BleBackend(
        hub, address=args.address, model=args.model, orientation=args.orientation,
        deadzone=args.deadzone, player=args.player, report=args.report,
        connection_priority=None if args.no_throughput else "throughput",
        scan_timeout_s=args.timeout)
    if not args.address:
        print(PAIRING_INSTRUCTIONS)
    print("Waiting for the controller... (Ctrl+C to quit)")
    backend.start()
    deadline = time.monotonic() + args.seconds if args.seconds else None
    last = ""
    ever_connected = False
    try:
        while deadline is None or time.monotonic() < deadline:
            if backend.error:
                print(f"\nerror: {backend.error}", file=sys.stderr)
                return 1
            snap = hub.snapshot()
            if backend.connected.is_set() and backend.dev_id in snap:
                ever_connected = True
                info, state = snap[backend.dev_id]
                s = backend.stats()
                rep = backend.last_report
                names = nintendo_labels(rep.raw_buttons, rep.model) if rep else None
                conn = f" conn {s['conn_interval_ms']:.1f} ms" if "conn_interval_ms" in s else ""
                bat = f" {s['battery_mv'] / 1000:.2f} V" if "battery_mv" in s else ""
                line = (f"{info.name}: {s['report_rate_hz']:5.1f} Hz{conn}{bat}  "
                        f"{format_state(state, info.family, names)}")
            else:
                line = backend.status
            if line != last:
                sys.stdout.write("\r" + line.ljust(len(last)))
                sys.stdout.flush()
                last = line
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        print()
        s = backend.stats()
        backend.stop()
        if s.get("reports"):
            print(f"{s['reports']} reports, last measured rate {s['report_rate_hz']} Hz, "
                  f"~{s['dropped_estimate']} dropped")
    if not ever_connected:
        print("No controller connected.")
        return 1
    return 0


def _cmd_switch2(args: Any) -> int:
    # ``switch2 -v`` = info, ``-vv`` (or the global ``controllerlog -v``) = debug. main() has
    # already configured logging, so set the level instead of a second basicConfig (a no-op).
    local = getattr(args, "switch2_verbose", 0)
    if local > 1 or getattr(args, "verbose", False):
        logging.getLogger().setLevel(logging.DEBUG)
    elif local:
        logging.getLogger().setLevel(logging.INFO)
    if args.switch2_cmd == "usb":
        from .switch2_usb import cmd_usb_test
        return cmd_usb_test(args)
    return {"scan": _cmd_scan, "test": _cmd_test}[args.switch2_cmd](args)


def _deadzone_arg(text: str) -> float:
    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not 0 <= v < 1:
        raise argparse.ArgumentTypeError("must be >= 0 and < 1")
    return v


def add_cli(subparsers: Any) -> None:
    """Register ``controllerlog switch2 {scan,test}`` on an argparse subparsers object."""
    sp = subparsers.add_parser(
        "switch2", help="Switch 2 controllers: 'usb' (GameCube/Pro over USB-C) or the "
                        "EXPERIMENTAL Bluetooth reader ('scan', 'test')")
    sp.add_argument("-v", "--verbose", dest="switch2_verbose", action="count", default=0,
                    help="log more (-vv: debug)")
    sub = sp.add_subparsers(dest="switch2_cmd", required=True)

    sc = sub.add_parser("scan", help="list Switch 2 controllers that are advertising")
    sc.add_argument("--timeout", type=float, default=8.0, help="scan time in seconds")
    sc.add_argument("--model", choices=sorted(MODELS), help="only this model")
    sc.add_argument("--pairing-only", action="store_true",
                    help="hide controllers that are trying to reconnect to a console")

    t = sub.add_parser("test", help="connect, print live inputs and the measured report rate")
    t.add_argument("address", nargs="?", help="controller address (default: first one in "
                                              "pairing mode)")
    t.add_argument("--model", choices=sorted(MODELS), help="model hint")
    t.add_argument("--orientation", choices=ORIENTATIONS, default="horizontal",
                   help="single Joy-Con: sideways (default) or upright")
    t.add_argument("--deadzone", type=_deadzone_arg, default=DEFAULT_DEADZONE,
                   help="radial stick deadzone, 0 <= d < 1 (default %(default)s)")
    t.add_argument("--player", type=int, choices=range(0, 9), default=1, metavar="0-8",
                   help="player LED 1-8 (0 = leave as is)")
    t.add_argument("--report", choices=("common", "model"), default="common",
                   help="input report: common 0x05 (default) or the model-specific one")
    t.add_argument("--no-throughput", action="store_true",
                   help="do not request Windows 11 ThroughputOptimized connection parameters")
    t.add_argument("--timeout", type=float, default=10.0, help="scan timeout per attempt")
    t.add_argument("--seconds", type=float, default=0.0, help="stop after N seconds")
    from .switch2_usb import add_usb_parser
    add_usb_parser(sub)
    sp.set_defaults(func=_cmd_switch2)
