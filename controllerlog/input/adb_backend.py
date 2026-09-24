"""Android input capture over ADB: any controller paired to a phone or tablet, no app needed.

Android's own Bluetooth/USB stack drives DualShock 4, DualSense, Switch Pro,
Joy-Con, Xbox, 8BitDo and generic HID pads natively. The ``adb shell`` user is
in the ``input`` group, so ``getevent`` can read ``/dev/input/event*``
system-wide, even while a game or emulator has focus. This backend runs
``adb shell getevent -t`` on the PC, parses the stream, maps Linux evdev codes
onto the canonical :mod:`controllerlog.model` layout and publishes into the
:class:`~controllerlog.hub.Hub` like any other backend.

Design notes (formats verified against AOSP ``system/core/toolbox/getevent.c``):

* ``getevent`` accepts at most ONE device argument, and only prints
  ``add device``/``remove device`` hotplug lines when run without one. We
  therefore stream every node (``getevent -t``, lines prefixed with the node
  path) and drop foreign nodes (touchscreen, motion sensors) by path. One
  ordered stream gives hotplug for free and a single clock; the cost is some
  unused bandwidth, which is small next to adb's capacity.
* Discovery uses ``getevent -i`` (``-p`` plus bus/vendor/product/version,
  ``location`` and ``id`` = uniq/BT MAC). Label output (``-l``) and one-line id
  formats from other builds are parsed too.
* Events are batched per node and published at ``SYN_REPORT`` with that
  frame's kernel timestamp; ``SYN_DROPPED`` discards the partial frame and
  re-reads the node state.
* getevent exits ("could not get evdev event") when a node vanishes with
  events still queued, which is common when a controller with a streaming
  IMU turns off. The stream is restarted at once after a healthy session.
* Sibling nodes of one controller are merged into its pad: hid-playstation's
  "<name> Touchpad" click, and the "<name> Consumer Control" node that
  hid-generic creates per HID application (Linux 4.18+), which carries
  Home/Back (AC Home/AC Back) of Android-certified pads.
* ``getevent`` asks for ``CLOCK_MONOTONIC`` (``EVIOCSCLOCKID``); older builds
  report wall-clock time. :class:`ClockSync` maps either onto
  ``perf_counter_ns`` with a sliding-window minimum of ``local_rx - remote``,
  which rejects transport jitter and tracks crystal drift; across idle gaps
  longer than the window the last minimum is kept as a drift-bounded
  estimate. The result carries a constant bias equal to the smallest
  observed transport delay (about 1 ms over USB, a few ms over Wi-Fi).
  Kernel timestamps mark when the phone received the HID report, not the
  physical press.
* ``getevent`` sees raw evdev codes, below Android's ``.kl`` key layouts, so
  mapping follows the kernel driver. Two face-button conventions exist:
  ``positional`` (Documentation/input/gamepad.rst: 0x133 = BTN_NORTH = top),
  used by hid-playstation, hid-sony and hid-nintendo; and ``xbox`` (0x133 =
  BTN_X = left), used by xpad and by every hid-generic pad that follows
  Android's HID gamepad usages (buttons 1..15 = A B C X Y Z L1 R1 L2 R2 Select
  Start Mode L3 R3; right stick Z/Rz; triggers Brake/Accelerator). Xbox
  controllers over Bluetooth, 8BitDo and most generic BT pads are in this
  group. See :func:`build_profile`.
* hid-nintendo reports buttons by position in every mainline version (checked
  v5.16, 6.0, 6.6, 6.8, 6.12 and master, plus the out-of-tree dkms driver):
  A -> BTN_EAST, B -> BTN_SOUTH, X -> BTN_NORTH, Y -> BTN_WEST, which matches
  the canonical model (south = Nintendo B). The versions differ in other ways.
  Up to 6.7 the node is named "Nintendo Switch Pro Controller"; from 6.8 it
  takes the HID name. 6.8 also moved to mapping tables and added the NSO
  controllers, and later kernels fixed swapped X/Y bits on licensed (HORI) Pro
  Controllers. ``nintendo_swap=True`` swaps south<->east and west<->north, for
  vendor kernels or pads that report by label.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..hub import Hub, now_ns
from ..model import (AXES, AXIS, AXIS_INDEX, AXIS_MAX, AXIS_MIN, BUTTON, BUTTON_INDEX,
                     FAMILY_GAMECUBE, FAMILY_GENERIC, FAMILY_PLAYSTATION, FAMILY_SWITCH, FAMILY_XBOX,
                     NUM_AXES, NUM_BUTTONS, DeviceInfo, InputEvent)
from .base import Backend

log = logging.getLogger(__name__)

# --- evdev constants (linux/input-event-codes.h) -------------------------------

EV_SYN, EV_KEY, EV_REL, EV_ABS, EV_MSC, EV_SW = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05
SYN_REPORT, SYN_DROPPED = 0, 3

BTN_LEFT = 0x110
BTN_SOUTH, BTN_EAST, BTN_C, BTN_NORTH, BTN_WEST, BTN_Z = 0x130, 0x131, 0x132, 0x133, 0x134, 0x135
BTN_TL, BTN_TR, BTN_TL2, BTN_TR2 = 0x136, 0x137, 0x138, 0x139
BTN_SELECT, BTN_START, BTN_MODE, BTN_THUMBL, BTN_THUMBR = 0x13a, 0x13b, 0x13c, 0x13d, 0x13e
BTN_DPAD_UP, BTN_DPAD_DOWN, BTN_DPAD_LEFT, BTN_DPAD_RIGHT = 0x220, 0x221, 0x222, 0x223
BTN_GRIPL, BTN_GRIPR, BTN_GRIPL2, BTN_GRIPR2 = 0x224, 0x225, 0x226, 0x227
BTN_TRIGGER_HAPPY1 = 0x2c0
KEY_MENU, KEY_BACK, KEY_RECORD, KEY_HOMEPAGE = 139, 158, 167, 172

ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05
ABS_GAS, ABS_BRAKE = 0x09, 0x0a
ABS_HAT0X, ABS_HAT0Y, ABS_HAT2X, ABS_HAT2Y = 0x10, 0x11, 0x14, 0x15
ABS_MT_SLOT, ABS_MT_POSITION_X = 0x2f, 0x35

INPUT_PROP_POINTER, INPUT_PROP_DIRECT, INPUT_PROP_BUTTONPAD = 0, 1, 2
INPUT_PROP_SEMI_MT, INPUT_PROP_POINTING_STICK, INPUT_PROP_ACCELEROMETER = 3, 5, 6

BUS_USB, BUS_BLUETOOTH, BUS_VIRTUAL, BUS_HOST = 0x03, 0x05, 0x06, 0x19

VENDOR_SONY, VENDOR_NINTENDO, VENDOR_MICROSOFT = 0x054c, 0x057e, 0x045e
VENDOR_VALVE, VENDOR_8BITDO = 0x28de, 0x2dc8

# Name -> code tables for ``getevent -l`` output. Aliases are included because
# getevent prints the first-defined name (0x130 is "BTN_GAMEPAD", 0x110 "BTN_MOUSE").
EV_CODES: dict[str, int] = {
    "EV_SYN": 0x00, "EV_KEY": 0x01, "EV_REL": 0x02, "EV_ABS": 0x03, "EV_MSC": 0x04,
    "EV_SW": 0x05, "EV_LED": 0x11, "EV_SND": 0x12, "EV_REP": 0x14, "EV_FF": 0x15,
    "EV_PWR": 0x16, "EV_FF_STATUS": 0x17,
}
SYN_CODES = {"SYN_REPORT": 0, "SYN_CONFIG": 1, "SYN_MT_REPORT": 2, "SYN_DROPPED": 3}
MSC_CODES = {"MSC_SERIAL": 0, "MSC_PULSELED": 1, "MSC_GESTURE": 2, "MSC_RAW": 3,
             "MSC_SCAN": 4, "MSC_TIMESTAMP": 5}
ABS_CODES: dict[str, int] = {
    "ABS_X": 0x00, "ABS_Y": 0x01, "ABS_Z": 0x02, "ABS_RX": 0x03, "ABS_RY": 0x04, "ABS_RZ": 0x05,
    "ABS_THROTTLE": 0x06, "ABS_RUDDER": 0x07, "ABS_WHEEL": 0x08, "ABS_GAS": 0x09,
    "ABS_BRAKE": 0x0a, **{f"ABS_HAT{i // 2}{'XY'[i % 2]}": 0x10 + i for i in range(8)},
    "ABS_PRESSURE": 0x18, "ABS_DISTANCE": 0x19, "ABS_TILT_X": 0x1a, "ABS_TILT_Y": 0x1b,
    "ABS_TOOL_WIDTH": 0x1c, "ABS_VOLUME": 0x20, "ABS_PROFILE": 0x21, "ABS_MISC": 0x28,
    "ABS_RESERVED": 0x2e, "ABS_MT_SLOT": 0x2f, "ABS_MT_TOUCH_MAJOR": 0x30,
    "ABS_MT_TOUCH_MINOR": 0x31, "ABS_MT_WIDTH_MAJOR": 0x32, "ABS_MT_WIDTH_MINOR": 0x33,
    "ABS_MT_ORIENTATION": 0x34, "ABS_MT_POSITION_X": 0x35, "ABS_MT_POSITION_Y": 0x36,
    "ABS_MT_TOOL_TYPE": 0x37, "ABS_MT_BLOB_ID": 0x38, "ABS_MT_TRACKING_ID": 0x39,
    "ABS_MT_PRESSURE": 0x3a, "ABS_MT_DISTANCE": 0x3b, "ABS_MT_TOOL_X": 0x3c, "ABS_MT_TOOL_Y": 0x3d,
}
# Keyboard block KEY_ESC (1) .. KEY_KPDOT (83), for pads in keyboard mode (overrides by name).
_KEYBOARD_NAMES = (
    "ESC 1 2 3 4 5 6 7 8 9 0 MINUS EQUAL BACKSPACE TAB Q W E R T Y U I O P LEFTBRACE RIGHTBRACE "
    "ENTER LEFTCTRL A S D F G H J K L SEMICOLON APOSTROPHE GRAVE LEFTSHIFT BACKSLASH Z X C V B N M "
    "COMMA DOT SLASH RIGHTSHIFT KPASTERISK LEFTALT SPACE CAPSLOCK F1 F2 F3 F4 F5 F6 F7 F8 F9 F10 "
    "NUMLOCK SCROLLLOCK KP7 KP8 KP9 KPMINUS KP4 KP5 KP6 KPPLUS KP1 KP2 KP3 KP0 KPDOT").split()
KEY_CODES: dict[str, int] = {
    **{f"KEY_{n}": i for i, n in enumerate(_KEYBOARD_NAMES, 1)},
    "KEY_F11": 87, "KEY_F12": 88, "KEY_KPENTER": 96, "KEY_RIGHTCTRL": 97, "KEY_KPSLASH": 98,
    "KEY_SYSRQ": 99, "KEY_RIGHTALT": 100, "KEY_PAGEUP": 104, "KEY_END": 107,
    "KEY_PAGEDOWN": 109, "KEY_INSERT": 110, "KEY_DELETE": 111, "KEY_PAUSE": 119,
    "KEY_FORWARD": 159, "KEY_NEXTSONG": 163, "KEY_PREVIOUSSONG": 165, "KEY_STOPCD": 166,
    "KEY_CAMERA": 212,
    "KEY_ESC": 1, "KEY_ENTER": 28, "KEY_SPACE": 57, "KEY_HOME": 102, "KEY_UP": 103,
    "KEY_LEFT": 105, "KEY_RIGHT": 106, "KEY_DOWN": 108, "KEY_MUTE": 113, "KEY_VOLUMEDOWN": 114,
    "KEY_VOLUMEUP": 115, "KEY_POWER": 116, "KEY_MENU": 139, "KEY_BACK": 158,
    "KEY_PLAYPAUSE": 164, "KEY_RECORD": 167, "KEY_HOMEPAGE": 172, "KEY_SEARCH": 217,
    "KEY_MICMUTE": 248, "KEY_SELECT": 0x161, "KEY_OK": 0x160,
    "BTN_MISC": 0x100, **{f"BTN_{i}": 0x100 + i for i in range(10)},
    "BTN_MOUSE": 0x110, "BTN_LEFT": 0x110, "BTN_RIGHT": 0x111, "BTN_MIDDLE": 0x112,
    "BTN_SIDE": 0x113, "BTN_EXTRA": 0x114, "BTN_FORWARD": 0x115, "BTN_BACK": 0x116,
    "BTN_TASK": 0x117, "BTN_JOYSTICK": 0x120, "BTN_TRIGGER": 0x120, "BTN_THUMB": 0x121,
    "BTN_THUMB2": 0x122, "BTN_TOP": 0x123, "BTN_TOP2": 0x124, "BTN_PINKIE": 0x125,
    "BTN_BASE": 0x126, **{f"BTN_BASE{i}": 0x125 + i for i in range(2, 7)}, "BTN_DEAD": 0x12f,
    "BTN_GAMEPAD": 0x130, "BTN_SOUTH": 0x130, "BTN_A": 0x130, "BTN_EAST": 0x131, "BTN_B": 0x131,
    "BTN_C": 0x132, "BTN_NORTH": 0x133, "BTN_X": 0x133, "BTN_WEST": 0x134, "BTN_Y": 0x134,
    "BTN_Z": 0x135, "BTN_TL": 0x136, "BTN_TR": 0x137, "BTN_TL2": 0x138, "BTN_TR2": 0x139,
    "BTN_SELECT": 0x13a, "BTN_START": 0x13b, "BTN_MODE": 0x13c, "BTN_THUMBL": 0x13d,
    "BTN_THUMBR": 0x13e, "BTN_DIGI": 0x140, "BTN_TOOL_PEN": 0x140, "BTN_TOOL_FINGER": 0x145,
    "BTN_TOUCH": 0x14a, "BTN_TOOL_DOUBLETAP": 0x14d, "BTN_WHEEL": 0x150, "BTN_GEAR_DOWN": 0x150,
    "BTN_GEAR_UP": 0x151, "BTN_DPAD_UP": 0x220, "BTN_DPAD_DOWN": 0x221, "BTN_DPAD_LEFT": 0x222,
    "BTN_DPAD_RIGHT": 0x223, "BTN_GRIPL": 0x224, "BTN_GRIPR": 0x225, "BTN_GRIPL2": 0x226,
    "BTN_GRIPR2": 0x227, "BTN_TRIGGER_HAPPY": 0x2c0,
    **{f"BTN_TRIGGER_HAPPY{i}": 0x2bf + i for i in range(1, 41)},
}
PROP_CODES = {"INPUT_PROP_POINTER": 0, "INPUT_PROP_DIRECT": 1, "INPUT_PROP_BUTTONPAD": 2,
              "INPUT_PROP_SEMI_MT": 3, "INPUT_PROP_TOPBUTTONPAD": 4,
              "INPUT_PROP_POINTING_STICK": 5, "INPUT_PROP_ACCELEROMETER": 6,
              "INPUT_PROP_PRESSUREPAD": 7}
_VALUE_LABELS = {"UP": 0, "DOWN": 1, "REPEAT": 2, "MT_TOOL_FINGER": 0, "MT_TOOL_PEN": 1,
                 "MT_TOOL_PALM": 2, "MT_TOOL_DIAL": 0x0a}
_CODE_TABLES = {EV_SYN: SYN_CODES, EV_KEY: KEY_CODES, EV_ABS: ABS_CODES, EV_MSC: MSC_CODES}


def evdev_code(etype: int, token: str | int) -> int | None:
    """Resolve an evdev code: int, ``"0x130"``, 4-digit hex as getevent prints it
    (``"0130"``), other digit strings as decimal (``"317"``), or a name (``"BTN_THUMBL"``)."""
    if isinstance(token, int):
        return token
    tok = token.strip()
    try:
        if tok[:2].lower() == "0x":
            return int(tok, 16)
        if len(tok) == 4 and re.fullmatch(r"[0-9a-fA-F]{4}", tok):
            return int(tok, 16)
        if tok.isdigit():
            return int(tok)
    except ValueError:
        return None
    return _CODE_TABLES.get(etype, {}).get(tok.upper())


def _s32(hex8: str) -> int:
    v = int(hex8, 16)
    return v - (1 << 32) if v & 0x80000000 else v


def _frac_ns(frac: str) -> int:
    return int(frac) * 10 ** (9 - len(frac)) if len(frac) <= 9 else int(frac[:9])


# --- device descriptors (getevent -i / -p) ---------------------------------------

@dataclass
class AbsInfo:
    """``struct input_absinfo`` as printed by ``getevent -p``."""

    value: int = 0
    min: int = 0
    max: int = 0
    fuzz: int = 0
    flat: int = 0
    resolution: int = 0


_FAMILY_BY_VENDOR = {VENDOR_SONY: FAMILY_PLAYSTATION, VENDOR_NINTENDO: FAMILY_SWITCH,
                     VENDOR_MICROSOFT: FAMILY_XBOX}
_FAMILY_BY_NAME = ((re.compile(r"xbox", re.I), FAMILY_XBOX),
                   (re.compile(r"dualsense|dualshock|playstation|\bps[345]\b", re.I), FAMILY_PLAYSTATION),
                   (re.compile(r"nintendo|joy-?con|pro controller", re.I), FAMILY_SWITCH))
_SDL_TYPE_GUESS = {
    (VENDOR_SONY, 0x0268): "ps3", (VENDOR_SONY, 0x05c4): "ps4", (VENDOR_SONY, 0x09cc): "ps4",
    (VENDOR_SONY, 0x0ba0): "ps4", (VENDOR_SONY, 0x0ce6): "ps5", (VENDOR_SONY, 0x0df2): "ps5",
    (VENDOR_NINTENDO, 0x2009): "switchpro", (VENDOR_NINTENDO, 0x2006): "joyconleft",
    (VENDOR_NINTENDO, 0x2007): "joyconright", (VENDOR_NINTENDO, 0x200e): "joyconpair",
    (VENDOR_NINTENDO, 0x2069): "switchpro", (VENDOR_NINTENDO, 0x2073): "gamecube",
    (VENDOR_MICROSOFT, 0x028e): "xbox360", (VENDOR_MICROSOFT, 0x028f): "xbox360",
    (VENDOR_MICROSOFT, 0x0291): "xbox360", (VENDOR_MICROSOFT, 0x0719): "xbox360",
}
_NOT_GAMEPAD_PROPS = frozenset({INPUT_PROP_POINTER, INPUT_PROP_DIRECT, INPUT_PROP_BUTTONPAD,
                                INPUT_PROP_SEMI_MT, INPUT_PROP_POINTING_STICK,
                                INPUT_PROP_ACCELEROMETER})
# Sibling nodes of one controller: driver-created (hid-playstation "Touchpad") and hid-generic's
# one-node-per-HID-application split (Linux 4.18+), which moves Home/Back (AC Home, AC Back)
# of Android-certified pads to "<name> Consumer Control".
COMPANION_SUFFIXES = ("Touchpad", "Consumer Control", "System Control", "Keyboard")


@dataclass
class EvdevDevice:
    """One ``/dev/input/eventN`` node as described by ``getevent -i``."""

    path: str
    name: str = ""
    bus: int = 0
    vendor: int = 0
    product: int = 0
    version: int = 0
    location: str = ""
    uniq: str = ""
    driver_version: str = ""
    keys: set[int] = field(default_factory=set)
    pressed: set[int] = field(default_factory=set)
    abs: dict[int, AbsInfo] = field(default_factory=dict)
    events: dict[int, set[int]] = field(default_factory=dict)  # other types: REL, MSC, SW, FF...
    props: set[int] = field(default_factory=set)
    has_ids: bool = False

    @property
    def is_touch_or_sensor(self) -> bool:
        """Touchscreen, touchpad, pointing stick or motion sensor node."""
        return bool(self.props & _NOT_GAMEPAD_PROPS
                    or ABS_MT_POSITION_X in self.abs or ABS_MT_SLOT in self.abs)

    @property
    def is_gamepad(self) -> bool:
        """Gamepad/joystick heuristic; excludes touch, motion sensors, keyboards and keys."""
        if self.is_touch_or_sensor:
            return False
        if any(0x130 <= k <= 0x13e or 0x220 <= k <= 0x223 for k in self.keys):
            return True
        if ABS_X in self.abs and ABS_Y in self.abs:
            if ABS_HAT0X in self.abs:
                return True
            if any(0x120 <= k <= 0x12f for k in self.keys):  # BTN_JOYSTICK class
                return True
        return False

    @property
    def family(self) -> str:
        if (self.vendor, self.product) == (VENDOR_NINTENDO, SWITCH2_PID_GAMECUBE):
            return FAMILY_GAMECUBE
        fam = _FAMILY_BY_VENDOR.get(self.vendor)
        if fam:
            return fam
        for rx, fam in _FAMILY_BY_NAME:
            if rx.search(self.name):
                return fam
        return FAMILY_GENERIC

    @property
    def sdl_type_guess(self) -> str:
        t = _SDL_TYPE_GUESS.get((self.vendor, self.product))
        if t:
            return t
        return "xboxone" if self.vendor == VENDOR_MICROSOFT else ""

    @property
    def connection(self) -> str:
        if self.bus == BUS_BLUETOOTH:
            return "wireless"
        if self.bus in (BUS_USB, BUS_HOST):
            return "wired"
        return "unknown"

    def summary(self) -> str:
        ids = f"{self.vendor:04x}:{self.product:04x}" if self.has_ids else "????:????"
        kind = "gamepad" if self.is_gamepad else "-"
        return f"{self.path:<20} {ids}  {kind:<7}  {self.name}"

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path, "name": self.name, "bus": self.bus, "vendor": self.vendor,
            "product": self.product, "version": self.version, "location": self.location,
            "uniq": self.uniq, "is_gamepad": self.is_gamepad, "family": self.family,
            "connection": self.connection, "keys": sorted(self.keys),
            "pressed": sorted(self.pressed),
            "abs": {c: [a.value, a.min, a.max, a.fuzz, a.flat, a.resolution]
                    for c, a in sorted(self.abs.items())},
            "props": sorted(self.props),
        }


_ADD_RE = re.compile(r"^add device\s+(\d+):\s+(\S+)\s*$")
_REMOVE_RE = re.compile(r"^remove device\s+(\d+):\s+(\S+)\s*$")
_NAME_RE = re.compile(r'^\s+name:\s+"(.*)"\s*$')
_QUOTED_RE = re.compile(r'^\s+(location|id):\s+"(.*)"\s*$')
_IDS_RE = re.compile(r"\b(bus|vendor|product|version)\b\s*:?\s*([0-9a-fA-F]{4})\b")
_DRV_RE = re.compile(r"^\s+version:\s+(\d+\.\d+\.\d+)\s*$")
_TYPE_RE = re.compile(r"^\s+([A-Z?]{2,3})\s*\(([0-9a-fA-F]{4})\):(.*)$")
_ABS_ITEM_RE = re.compile(
    r"^\s*(\S+?)\*?\s*:\s*value\s+(-?\d+),\s*min\s+(-?\d+),\s*max\s+(-?\d+),"
    r"\s*fuzz\s+(-?\d+),\s*flat\s+(-?\d+)(?:,\s*resolution\s+(-?\d+))?")


def parse_getevent_info(text: str) -> list[EvdevDevice]:
    """Parse ``getevent -i`` / ``-p`` output (numeric or ``-l`` labels) into devices."""
    devices: list[EvdevDevice] = []
    cur: EvdevDevice | None = None
    section = "header"
    etype: int | None = None
    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        m = _ADD_RE.match(line)
        if m:
            cur = EvdevDevice(path=m.group(2))
            devices.append(cur)
            section, etype = "header", None
            continue
        if cur is None or not line.strip():
            continue
        stripped = line.strip()
        if stripped == "events:":
            section, etype = "events", None
            continue
        if stripped == "input props:":
            section = "props"
            continue
        if stripped.startswith("HID descriptor:"):
            section = "hid"
            continue
        if section == "header":
            m = _NAME_RE.match(line)
            if m:
                cur.name = m.group(1)
                continue
            m = _QUOTED_RE.match(line)
            if m:
                if m.group(1) == "location":
                    cur.location = m.group(2)
                else:
                    cur.uniq = m.group(2)
                continue
            m = _DRV_RE.match(line)
            if m:
                cur.driver_version = m.group(1)
                continue
            for key, val in _IDS_RE.findall(line):
                setattr(cur, key, int(val, 16))
                cur.has_ids = True
        elif section == "events":
            m = _TYPE_RE.match(line)
            if m:
                etype = int(m.group(2), 16)
                rest = m.group(3)
            elif etype is not None and line.startswith(" "):
                rest = line
            else:
                continue
            _parse_event_items(cur, etype, rest)
        elif section == "props":
            if stripped.startswith("<"):
                continue
            code = PROP_CODES.get(stripped)
            if code is None and re.fullmatch(r"[0-9a-fA-F]{4}", stripped):
                code = int(stripped, 16)
            if code is not None:
                cur.props.add(code)
    return devices


def _parse_event_items(dev: EvdevDevice, etype: int, rest: str) -> None:
    if etype == EV_ABS:
        m = _ABS_ITEM_RE.match(rest)
        if m:
            code = evdev_code(EV_ABS, m.group(1))
            if code is not None:
                dev.abs[code] = AbsInfo(*(int(g) if g is not None else 0 for g in m.groups()[1:]))
        else:
            for tok in rest.split():
                code = evdev_code(EV_ABS, tok.rstrip("*"))
                if code is not None:
                    dev.abs.setdefault(code, AbsInfo())
        return
    for tok in rest.split():
        pressed = tok.endswith("*")
        code = evdev_code(etype, tok.rstrip("*"))
        if code is None:
            continue
        if etype == EV_KEY:
            dev.keys.add(code)
            if pressed:
                dev.pressed.add(code)
        else:
            dev.events.setdefault(etype, set()).add(code)


# --- stream lines (getevent -t) --------------------------------------------------

@dataclass(slots=True)
class RawEvent:
    """One ``struct input_event``; ``t_ns`` is the phone's kernel time (None without -t)."""

    t_ns: int | None
    path: str | None
    type: int
    code: int
    value: int


@dataclass(slots=True)
class DeviceAdded:
    index: int
    path: str


@dataclass(slots=True)
class DeviceRemoved:
    index: int
    path: str


@dataclass(slots=True)
class DeviceName:
    name: str


StreamLine = RawEvent | DeviceAdded | DeviceRemoved | DeviceName

_TS_RE = re.compile(r"^(?:\[\s*(\d+)\.(\d+)\]|(\d+)-(\d+):)\s*")
_EV_NUM_RE = re.compile(r"^(?:(/\S+?):\s+)?([0-9a-fA-F]{4})\s+([0-9a-fA-F]{4})\s+([0-9a-fA-F]{8})\s*$")
_EV_LBL_RE = re.compile(r"^(?:(/\S+?):\s+)?(EV_\w+|[0-9a-fA-F]{4})\s+(\w+)\s+(\w+)\s*$")


def parse_stream_line(line: str) -> StreamLine | None:
    """Parse one line of ``getevent [-t] [-l]`` output; unknown lines give None.

    Handles ``[   12345.678901] /dev/input/event4: 0001 0130 00000001`` (the
    path prefix appears when no device argument is given), the label form
    (``EV_KEY BTN_GAMEPAD DOWN``), the old ``sec-usec:`` timestamp and the
    ``add device``/``remove device``/``name:`` hotplug lines. Values are
    32-bit two's complement, so ``ffffffff`` is -1.
    """
    line = line.rstrip("\r\n")
    if not line:
        return None
    if line[0] == "a" or line[0] == "r":
        m = _ADD_RE.match(line)
        if m:
            return DeviceAdded(int(m.group(1)), m.group(2))
        m = _REMOVE_RE.match(line)
        if m:
            return DeviceRemoved(int(m.group(1)), m.group(2))
        return None
    if line[0] == " ":
        m = _NAME_RE.match(line)
        return DeviceName(m.group(1)) if m else None
    t_ns = None
    m = _TS_RE.match(line)
    if m:
        if m.group(1) is not None:
            t_ns = int(m.group(1)) * 1_000_000_000 + _frac_ns(m.group(2))
        else:
            t_ns = int(m.group(3)) * 1_000_000_000 + int(m.group(4)) * 1000
        line = line[m.end():]
    m = _EV_NUM_RE.match(line)
    if m:
        return RawEvent(t_ns, m.group(1), int(m.group(2), 16), int(m.group(3), 16), _s32(m.group(4)))
    m = _EV_LBL_RE.match(line)
    if m:
        tok = m.group(2)
        etype = EV_CODES.get(tok) if tok.startswith("EV_") else int(tok, 16)
        if etype is None:
            return None
        code = evdev_code(etype, m.group(3))
        vtok = m.group(4)
        if vtok in _VALUE_LABELS:
            value = _VALUE_LABELS[vtok]
        elif re.fullmatch(r"[0-9a-fA-F]{8}", vtok):
            value = _s32(vtok)
        else:
            return None
        if code is None:
            return None
        return RawEvent(t_ns, m.group(1), etype, code, value)
    return None


# --- mapping evdev -> canonical ----------------------------------------------------

@dataclass
class AxisMap:
    """Target of one EV_ABS code: a canonical axis name, or ``hat_x``/``hat_y`` (d-pad)."""

    target: str
    invert: bool = False


@dataclass
class Profile:
    """evdev -> canonical mapping for one device."""

    name: str
    family: str
    buttons: dict[int, str]          # EV_KEY code -> button name, or "left_trigger"/"right_trigger"
    axes: dict[int, AxisMap]         # EV_ABS code -> target
    apply_flat: bool = False         # zero sticks/triggers inside the driver's ``flat`` zone

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "family": self.family, "apply_flat": self.apply_flat,
                "buttons": {f"0x{c:03x}": t for c, t in sorted(self.buttons.items())},
                "axes": {f"0x{c:02x}": ({"to": a.target, "invert": True} if a.invert else a.target)
                         for c, a in sorted(self.axes.items())}}


_TRIGGERS = ("left_trigger", "right_trigger")
_AXIS_TARGETS = frozenset(AXES) | {"hat_x", "hat_y"}
_BUTTON_TARGETS = frozenset(BUTTON_INDEX) | set(_TRIGGERS)

FACE_POSITIONAL = {BTN_SOUTH: "south", BTN_EAST: "east", BTN_NORTH: "north", BTN_WEST: "west"}
FACE_XBOX = {BTN_SOUTH: "south", BTN_EAST: "east", BTN_NORTH: "west", BTN_WEST: "north"}
COMMON_KEYS: dict[int, str] = {
    BTN_TL: "left_shoulder", BTN_TR: "right_shoulder",
    BTN_TL2: "left_trigger", BTN_TR2: "right_trigger",  # dropped when the trigger is analog
    BTN_SELECT: "back", BTN_START: "start", BTN_MODE: "guide",
    BTN_THUMBL: "left_stick", BTN_THUMBR: "right_stick",
    BTN_C: "misc2", BTN_Z: "misc1",                     # hid-nintendo: BTN_Z = Capture
    BTN_DPAD_UP: "dpad_up", BTN_DPAD_DOWN: "dpad_down",
    BTN_DPAD_LEFT: "dpad_left", BTN_DPAD_RIGHT: "dpad_right",
    BTN_GRIPL: "left_paddle1", BTN_GRIPR: "right_paddle1",
    BTN_GRIPL2: "left_paddle2", BTN_GRIPR2: "right_paddle2",
    KEY_BACK: "back", KEY_HOMEPAGE: "guide", KEY_MENU: "start", KEY_RECORD: "misc1",
    BTN_TRIGGER_HAPPY1: "right_paddle1", BTN_TRIGGER_HAPPY1 + 1: "left_paddle1",
    BTN_TRIGGER_HAPPY1 + 2: "right_paddle2", BTN_TRIGGER_HAPPY1 + 3: "left_paddle2",
    BTN_TRIGGER_HAPPY1 + 4: "misc3", BTN_TRIGGER_HAPPY1 + 5: "misc4",
    BTN_TRIGGER_HAPPY1 + 6: "misc5", BTN_TRIGGER_HAPPY1 + 7: "misc6",
}
# DS4/DualSense on hid-generic (kernel without hid-sony/hid-playstation):
# raw HID button order, cf. AOSP Vendor_054c_Product_09cc.kl / 0ce6_fallback.kl.
SONY_HID_KEYS: dict[int, str] = {
    0x130: "west", 0x131: "south", 0x132: "east", 0x133: "north",
    0x134: "left_shoulder", 0x135: "right_shoulder", 0x136: "left_trigger", 0x137: "right_trigger",
    0x138: "back", 0x139: "start", 0x13a: "left_stick", 0x13b: "right_stick",
    0x13c: "guide", 0x13d: "touchpad",
}
# hid-playstation: touchpad click arrives on the "<name> Touchpad" node as BTN_LEFT;
# DualSense Edge Fn1/Fn2/left paddle/right paddle are BTN_TRIGGER_HAPPY1..4.
SONY_KERNEL_EXTRA: dict[int, str] = {
    BTN_LEFT: "touchpad", BTN_TRIGGER_HAPPY1: "left_paddle2", BTN_TRIGGER_HAPPY1 + 1: "right_paddle2",
    BTN_TRIGGER_HAPPY1 + 2: "left_paddle1", BTN_TRIGGER_HAPPY1 + 3: "right_paddle1",
}
_SWAP = {"south": "east", "east": "south", "west": "north", "north": "west"}
_COMPANION_KEYS = frozenset({BTN_LEFT, KEY_BACK, KEY_HOMEPAGE, KEY_MENU, KEY_RECORD})

# Switch 2 controllers in their standard HID gamepad mode (report 0x0A, what the GC Bridge app
# switches on over USB-C): no kernel driver knows them, so hid-generic numbers the report's 21
# buttons in the report's own order, 1-16 as BTN_SOUTH.. (0x130..) and 17-21 as
# BTN_TRIGGER_HAPPY1.. (0x2c0..). Confirmed by pressing on the NSO GameCube controller, whose
# R / L triggers report their click in the R / L slots and whose Z is in the ZR slot. The
# report's stick Y grows upwards (Android and evdev expect down), hence the inverted Y axes.
SWITCH2_PID_PRO, SWITCH2_PID_GAMECUBE = 0x2069, 0x2073
_SWITCH2_STANDARD_ORDER = ("B", "A", "Y", "X", "R", "ZR", "PLUS", "RSTICK", "DOWN", "RIGHT",
                           "LEFT", "UP", "L", "ZL", "MINUS", "LSTICK", "HOME", "CAPTURE", "GR",
                           "GL", "C")
_SWITCH2_STANDARD_COMMON = {
    "PLUS": "start", "RSTICK": "right_stick", "DOWN": "dpad_down", "RIGHT": "dpad_right",
    "LEFT": "dpad_left", "UP": "dpad_up", "MINUS": "back", "LSTICK": "left_stick",
    "HOME": "guide", "CAPTURE": "misc1", "GR": "right_paddle1", "GL": "left_paddle1", "C": "misc2",
}
_SWITCH2_STANDARD_TARGETS = {
    # positional: the GameCube's A is the bottom button, B the left one; its trigger clicks are
    # the only trigger information in this report
    SWITCH2_PID_GAMECUBE: {"B": "west", "A": "south", "Y": "north", "X": "east",
                           "R": "right_trigger", "ZR": "right_shoulder",
                           "L": "left_trigger", "ZL": "left_shoulder"},
    SWITCH2_PID_PRO: {"B": "south", "A": "east", "Y": "west", "X": "north",
                      "R": "right_shoulder", "ZR": "right_trigger",
                      "L": "left_shoulder", "ZL": "left_trigger"},
}


def switch2_standard_keys(product: int) -> dict[int, str]:
    """evdev key code -> canonical button for a Switch 2 pad in standard HID mode."""
    targets = {**_SWITCH2_STANDARD_COMMON, **_SWITCH2_STANDARD_TARGETS[product]}
    out = {}
    for i, label in enumerate(_SWITCH2_STANDARD_ORDER):
        code = BTN_SOUTH + i if i < 16 else BTN_TRIGGER_HAPPY1 + (i - 16)
        out[code] = targets[label]
    return out


def is_switch2_standard(dev: "EvdevDevice") -> bool:
    """A Switch 2 GameCube / Pro pad handled by hid-generic (BTN_C = report button 3)."""
    return (dev.vendor == VENDOR_NINTENDO and dev.product in _SWITCH2_STANDARD_TARGETS
            and BTN_C in dev.keys)


def _stickiness(a: AbsInfo) -> int:
    """>0: looks like a centred stick axis, <0: looks like a trigger resting at min."""
    span = a.max - a.min
    if span <= 0:
        return 0
    score = 2 if a.min < 0 else 0
    c = (a.min + a.max + 1) // 2
    if abs(a.value - c) <= span // 5:
        score += 1
    elif a.value - a.min <= span // 10:
        score -= 1
    return score


def _assign_axes(dev: EvdevDevice, face: str) -> dict[int, AxisMap]:
    a = dev.abs
    has = lambda *codes: all(c in a for c in codes)  # noqa: E731
    out: dict[int, AxisMap] = {}
    if has(ABS_X, ABS_Y):
        out[ABS_X], out[ABS_Y] = AxisMap("left_x"), AxisMap("left_y")
    if ABS_HAT0X in a:
        out[ABS_HAT0X] = AxisMap("hat_x")
    if ABS_HAT0Y in a:
        out[ABS_HAT0Y] = AxisMap("hat_y")
    right: tuple[int, int] | None = None
    trig: tuple[int, int] | None = None
    if face == "sony_hid":
        right, trig = (ABS_Z, ABS_RZ), (ABS_RX, ABS_RY)
    elif dev.vendor == VENDOR_VALVE and has(ABS_RX, ABS_RY):
        right, trig = (ABS_RX, ABS_RY), (ABS_HAT2Y, ABS_HAT2X)   # hid-steam
    elif dev.vendor in (VENDOR_SONY, VENDOR_NINTENDO) and has(ABS_RX, ABS_RY):
        right = (ABS_RX, ABS_RY)
        trig = (ABS_Z, ABS_RZ) if has(ABS_Z, ABS_RZ) else None
    elif has(ABS_RX, ABS_RY) and has(ABS_Z, ABS_RZ):
        s_rxy = _stickiness(a[ABS_RX]) + _stickiness(a[ABS_RY])
        s_zrz = _stickiness(a[ABS_Z]) + _stickiness(a[ABS_RZ])
        if s_zrz > s_rxy:
            right, trig = (ABS_Z, ABS_RZ), (ABS_RX, ABS_RY)
        else:  # Linux driver convention (xpad, hid-sony, hid-playstation)
            right, trig = (ABS_RX, ABS_RY), (ABS_Z, ABS_RZ)
        if has(ABS_BRAKE, ABS_GAS) and s_zrz >= s_rxy:
            right, trig = (ABS_Z, ABS_RZ), (ABS_BRAKE, ABS_GAS)
    elif has(ABS_Z, ABS_RZ):
        right = (ABS_Z, ABS_RZ)    # Android HID gamepad: right stick = Z/Rz
    elif has(ABS_RX, ABS_RY):
        right = (ABS_RX, ABS_RY)
    elif has(ABS_RX, ABS_RZ):
        right = (ABS_RX, ABS_RZ)   # Switch 2 standard HID report: X/Y + Rx/Rz
    if trig is None:
        if has(ABS_BRAKE, ABS_GAS):
            trig = (ABS_BRAKE, ABS_GAS)      # Android HID: Brake = LT, Accelerator = RT
        elif has(ABS_HAT2Y, ABS_HAT2X) and a[ABS_HAT2X].min >= 0:
            trig = (ABS_HAT2Y, ABS_HAT2X)
    if trig is not None and not has(*trig):
        trig = None
    if right:
        out[right[0]], out[right[1]] = AxisMap("right_x"), AxisMap("right_y")
    if trig:
        out[trig[0]], out[trig[1]] = AxisMap("left_trigger"), AxisMap("right_trigger")
    return out


def _parse_axis_override(spec: Any) -> AxisMap | None:
    if spec is None:
        return None
    if isinstance(spec, str):
        target, invert = spec, False
        if target.startswith("-"):
            target, invert = target[1:], True
    elif isinstance(spec, dict) and "to" in spec:
        target, invert = spec["to"], bool(spec.get("invert", False))
    else:
        raise ValueError(f"profile override: bad axis spec {spec!r} (use a name, '-name', "
                         "{'to': name, 'invert': bool} or null)")
    if target is None:
        return None
    if not isinstance(target, str) or target not in _AXIS_TARGETS:
        raise ValueError(f"profile override: unknown axis target {target!r}")
    return AxisMap(target, invert)


_OVERRIDE_KEYS = frozenset({"face", "swap_ab", "buttons", "axes", "apply_flat", "force_gamepad"})


def build_profile(dev: EvdevDevice, override: dict[str, Any] | None = None, *,
                  nintendo_swap: bool = False, apply_flat: bool = False) -> Profile:
    """Choose the evdev -> canonical mapping for ``dev``.

    Face convention: vendor 054c/057e (hid-playstation, hid-sony, hid-nintendo)
    -> ``positional``; a Sony pad exposing BTN_C (hid-generic raw DS4/DS5)
    -> ``sony_hid``; everything else -> ``xbox`` (xpad and the Android HID
    gamepad layout). Axes: left stick X/Y, d-pad HAT0X/Y; Sony/Nintendo right
    stick RX/RY (Sony triggers Z/RZ), raw Sony Z/RZ + RX/RY triggers, Valve
    RX/RY + HAT2Y/HAT2X; otherwise Z/RZ vs RX/RY is decided by which pair looks
    like a centred stick (signed range, resting value), and triggers come from
    BRAKE/GAS, HAT2Y/HAT2X or the leftover pair. BTN_TL2/TR2 drive the trigger
    axes only when the device has no analog trigger. A d-pad on
    BTN_TRIGGER_HAPPY1..4 (old xpad) is recognised.

    ``override`` (merged last) keys:
      ``face``: ``"positional"|"xbox"|"sony_hid"``; ``swap_ab``: bool;
      ``buttons``: ``{code: target|None}``; ``axes``: ``{code: target|"-target"|
      {"to": target, "invert": bool}|None}``; ``apply_flat``: bool.
    Codes: ints, ``"0x130"``, 4-digit getevent hex (``"0130"``), decimal
    strings, or evdev names (``"BTN_C"``, ``"ABS_Z"``). Button targets are
    canonical button names or ``left_trigger``/``right_trigger``; axis targets
    are canonical axis names or ``hat_x``/``hat_y``. ``None`` removes a mapping.
    ``force_gamepad`` is accepted (it is read by :class:`AdbBackend`); any other
    key raises ValueError. Keys a sibling node can deliver (touchpad click,
    ``KEY_BACK``/``KEY_HOMEPAGE``/``KEY_MENU``/``KEY_RECORD`` from a
    "Consumer Control" node) stay mapped even when this node lacks them.
    """
    override = override or {}
    if not isinstance(override, dict):
        raise ValueError(f"profile override must be a dict, not {type(override).__name__}")
    unknown = set(override) - _OVERRIDE_KEYS
    if unknown:
        raise ValueError(f"profile override: unknown key(s) {sorted(map(str, unknown))}; "
                         f"expected {sorted(_OVERRIDE_KEYS)}")
    for key in ("buttons", "axes"):
        if not isinstance(override.get(key) or {}, dict):
            raise ValueError(f"profile override: {key!r} must be a dict of code -> target")
    fam = dev.family
    face = override.get("face")
    if face is None:
        if dev.vendor == VENDOR_SONY and BTN_C in dev.keys:
            face = "sony_hid"
        elif dev.vendor in (VENDOR_SONY, VENDOR_NINTENDO):
            face = "positional"
        else:
            face = "xbox"
    if face not in ("positional", "xbox", "sony_hid"):
        raise ValueError(f"profile override: unknown face convention {face!r}")
    buttons = dict(COMMON_KEYS)
    if face == "sony_hid":
        buttons.update(SONY_HID_KEYS)
    else:
        buttons.update(FACE_POSITIONAL if face == "positional" else FACE_XBOX)
    if dev.vendor == VENDOR_SONY and face == "positional":
        buttons.update(SONY_KERNEL_EXTRA)
    elif dev.vendor == VENDOR_MICROSOFT:
        buttons[KEY_MENU] = "guide"          # AOSP Vendor_045e_Product_02e0.kl
    elif dev.vendor == VENDOR_8BITDO:
        buttons[BTN_C] = "guide"             # AOSP Vendor_2dc8_Product_6101.kl
    happy = [BTN_TRIGGER_HAPPY1 + i for i in range(4)]
    dpad_keys = {BTN_DPAD_UP, BTN_DPAD_DOWN, BTN_DPAD_LEFT, BTN_DPAD_RIGHT}
    if (ABS_HAT0X not in dev.abs and not dev.keys & dpad_keys
            and all(k in dev.keys for k in happy) and dev.vendor != VENDOR_SONY):
        buttons.update(zip(happy, ("dpad_left", "dpad_right", "dpad_up", "dpad_down")))
    if is_switch2_standard(dev):
        buttons.update(switch2_standard_keys(dev.product))
    axes = _assign_axes(dev, face)
    if is_switch2_standard(dev):
        for m in axes.values():
            if m.target in ("left_y", "right_y"):
                m.invert = not m.invert
    swapped = bool((nintendo_swap and fam == FAMILY_SWITCH) or override.get("swap_ab"))
    if swapped:
        buttons = {c: _SWAP.get(t, t) for c, t in buttons.items()}
    for key, target in (override.get("buttons") or {}).items():
        code = evdev_code(EV_KEY, key)
        if code is None:
            raise ValueError(f"profile override: unknown key code {key!r}")
        if target is None:
            buttons.pop(code, None)
        elif not isinstance(target, str) or target not in _BUTTON_TARGETS:
            raise ValueError(f"profile override: unknown button target {target!r}")
        else:
            buttons[code] = target
    for key, spec in (override.get("axes") or {}).items():
        code = evdev_code(EV_ABS, key)
        if code is None:
            raise ValueError(f"profile override: unknown axis code {key!r}")
        m = _parse_axis_override(spec)
        if m is None:
            axes.pop(code, None)
        else:
            axes[code] = m
    axes = {c: m for c, m in axes.items() if c in dev.abs}
    analog = {m.target for m in axes.values()}
    buttons = {c: t for c, t in buttons.items()
               if (c in dev.keys or c in _COMPANION_KEYS) and not (t in _TRIGGERS and t in analog)}
    kinds = []
    for pair, label in (((ABS_RX, ABS_RY), "rxry"), ((ABS_Z, ABS_RZ), "zrz")):
        if axes.get(pair[0]) and axes[pair[0]].target == "right_x":
            kinds.append(f"right={label}")
    trig = next((c for c, m in axes.items() if m.target == "left_trigger"), None)
    kinds.append(f"lt=0x{trig:02x}" if trig is not None else "lt=digital")
    name = "/".join([face, *kinds]) + ("/swapped" if swapped else "")
    return Profile(name=name, family=fam, buttons=buttons, axes=axes,
                   apply_flat=bool(override.get("apply_flat", apply_flat)))


def scale_stick(v: int, a: AbsInfo, flat: bool = False) -> int:
    """Map ``[min, max]`` to int16 with the integer centre ``(min + max + 1) // 2`` at 0."""
    lo, hi = a.min, a.max
    if hi <= lo:
        return 0
    c = (lo + hi + 1) // 2
    d = v - c
    if flat and abs(d) <= a.flat:
        return 0
    if d >= 0:
        out = (2 * d * AXIS_MAX + (hi - c)) // (2 * (hi - c)) if hi > c else 0
    else:
        out = -((2 * -d * -AXIS_MIN + (c - lo)) // (2 * (c - lo))) if c > lo else 0
    return max(AXIS_MIN, min(AXIS_MAX, out))


def scale_trigger(v: int, a: AbsInfo, flat: bool = False) -> int:
    """Map ``[min, max]`` to ``0..32767``."""
    lo, hi = a.min, a.max
    if hi <= lo:
        return 0
    d = v - lo
    if flat and d <= a.flat:
        return 0
    return max(0, min(AXIS_MAX, (2 * d * AXIS_MAX + (hi - lo)) // (2 * (hi - lo))))


_DPAD_NEG = {"hat_x": BUTTON_INDEX["dpad_left"], "hat_y": BUTTON_INDEX["dpad_up"]}
_DPAD_POS = {"hat_x": BUTTON_INDEX["dpad_right"], "hat_y": BUTTON_INDEX["dpad_down"]}


class EvdevMapper:
    """Per-device evdev state machine: feed events, get canonical changes at SYN_REPORT."""

    def __init__(self, profile: Profile, dev: EvdevDevice) -> None:
        self.profile = profile
        self._raw_keys: dict[int, int] = {}
        self._raw_abs: dict[int, int] = {c: a.value for c, a in dev.abs.items()}
        self._pending: dict[tuple[int, int], int] = {}
        self._cpending: dict[int, int] = {}            # companion node (touchpad) keys
        self._btn: list[tuple[int, int, bool]] = []    # (code, index, is_trigger_axis)
        for code, target in profile.buttons.items():
            if target in _TRIGGERS:
                self._btn.append((code, AXIS_INDEX[target], True))
            else:
                self._btn.append((code, BUTTON_INDEX[target], False))
        self._axes: list[tuple[int, int, AbsInfo, bool, bool]] = []  # code, idx, info, trigger, inv
        self._hats: list[tuple[int, int, int, int]] = []             # code, neg, pos, centre*2
        for code, m in profile.axes.items():
            info = dev.abs.get(code, AbsInfo(0, -1, 1))
            if m.target in _DPAD_NEG:
                neg, pos = _DPAD_NEG[m.target], _DPAD_POS[m.target]
                if m.invert:
                    neg, pos = pos, neg
                self._hats.append((code, neg, pos, info.min + info.max))
            else:
                idx = AXIS_INDEX[m.target]
                self._axes.append((code, idx, info, idx in (4, 5), m.invert))
        self.buttons = [0] * NUM_BUTTONS
        self.axes = [0] * NUM_AXES

    def feed(self, etype: int, code: int, value: int, companion: bool = False) -> None:
        """Queue one event of the current frame (``companion``: from a sibling node such as
        the touchpad or "Consumer Control"; only its keys are used)."""
        if companion:
            if etype == EV_KEY:
                self._cpending[code] = value
        elif etype == EV_KEY or etype == EV_ABS:
            self._pending[(etype, code)] = value

    def discard(self) -> None:
        """Drop the partial frame (after SYN_DROPPED)."""
        self._pending.clear()

    def sync(self, companion: bool = False) -> list[tuple[str, int, int]]:
        """Apply the pending frame (SYN_REPORT); returns ``(kind, index, value)`` changes.

        Nodes are read round-robin, so a companion node's frame can arrive in the
        middle of the gamepad's; each node therefore has its own pending frame.
        """
        if companion:
            if not self._cpending:
                return []
            for code, v in self._cpending.items():
                self._raw_keys[code] = 1 if v else 0
            self._cpending.clear()
            return self._recompute()
        if not self._pending:
            return []
        for (etype, code), v in self._pending.items():
            if etype == EV_KEY:
                self._raw_keys[code] = 1 if v else 0
            else:
                self._raw_abs[code] = v
        self._pending.clear()
        return self._recompute()

    def load(self, dev: EvdevDevice) -> list[tuple[str, int, int]]:
        """Replace the raw state with a ``getevent -i`` snapshot (pressed keys, abs values).

        Keys this node does not have came from sibling nodes and are kept."""
        self._pending.clear()
        companion = {k: v for k, v in self._raw_keys.items() if k not in dev.keys}
        self._raw_keys = {k: 1 for k in dev.pressed} | companion
        self._raw_abs.update({c: a.value for c, a in dev.abs.items()})
        return self._recompute()

    def reset(self) -> list[tuple[str, int, int]]:
        """Forget raw key state and return the changes that neutralise the pad."""
        self._pending.clear()
        self._cpending.clear()
        self._raw_keys.clear()
        out = [(BUTTON, i, 0) for i, v in enumerate(self.buttons) if v]
        out += [(AXIS, i, 0) for i, v in enumerate(self.axes) if v]
        self.buttons = [0] * NUM_BUTTONS
        self.axes = [0] * NUM_AXES
        return out

    def _recompute(self) -> list[tuple[str, int, int]]:
        b = [0] * NUM_BUTTONS
        ax = [0] * NUM_AXES
        keys, absv, flat = self._raw_keys, self._raw_abs, self.profile.apply_flat
        for code, idx, is_axis in self._btn:
            if keys.get(code):
                if is_axis:
                    ax[idx] = AXIS_MAX
                else:
                    b[idx] = 1
        for code, idx, info, trigger, inv in self._axes:
            v = absv.get(code)
            if v is None:
                continue
            if trigger:
                out = scale_trigger(v, info, flat)
                out = AXIS_MAX - out if inv else out
                ax[idx] = max(ax[idx], out)
            else:
                out = scale_stick(v, info, flat)
                ax[idx] = min(AXIS_MAX, -out) if inv else out
        for code, neg, pos, c2 in self._hats:
            v = absv.get(code)
            if v is None:
                continue
            if 2 * v < c2:
                b[neg] = 1
            elif 2 * v > c2:
                b[pos] = 1
        out = [(BUTTON, i, v) for i, v in enumerate(b) if v != self.buttons[i]]
        out += [(AXIS, i, v) for i, v in enumerate(ax) if v != self.axes[i]]
        self.buttons, self.axes = b, ax
        return out


# --- clock ------------------------------------------------------------------------

class ClockSync:
    """Maps the phone's kernel timestamps onto the local ``perf_counter_ns`` clock.

    ``offset = min(local_rx_ns - remote_ns)`` over a sliding window. The
    minimum is the sample with the least transport delay, so USB, Wi-Fi and
    adb buffering jitter is rejected. The window slides, so drift between the
    two crystals (tens of ppm) is followed. A forward jump of the remote clock
    is adopted immediately (the minimum drops). A backward jump (old builds
    that report wall-clock time, NTP steps) resets the window once every
    sample has stayed ``jump_ns`` above the minimum for ``jump_hold_ns``.

    Idle gaps: a pad without a motion-sensor node (Xbox, generic) can send
    nothing for longer than the window, which would leave the first frames
    after the gap with a single, possibly badly delayed sample (tens of ms
    over Wi-Fi). The last minimum is therefore carried over the gap as the
    upper bound ``sample + max_drift_ppm * age`` and used while it beats the
    refilling window (at most one window, dropped on a clock jump). Both are
    upper bounds of the true offset + transport delay while the drift stays
    below ``max_drift_ppm``, so the smaller one is never the worse estimate.
    """

    def __init__(self, window_ns: int = 10_000_000_000, jump_ns: int = 1_000_000_000,
                 jump_hold_ns: int = 1_000_000_000, max_drift_ppm: float = 100.0) -> None:
        self.window_ns = window_ns
        self.jump_ns = jump_ns
        self.jump_hold_ns = jump_hold_ns
        self.max_drift_ppm = max_drift_ppm
        self._q: deque[tuple[int, int]] = deque()   # (local_ns, sample), samples ascending
        self._above_since: int | None = None
        self._carry: tuple[int, int, int] | None = None  # (local_ns, sample, gap end)
        self._offset: int | None = None
        self.samples = 0
        self.resets = 0

    def update(self, remote_ns: int, local_ns: int) -> int:
        """Add one (remote, local receive time) pair; returns the current offset."""
        s = local_ns - remote_ns
        q = self._q
        if q and q[-1][0] < local_ns - self.window_ns:     # idle gap: the whole window expires
            self._carry = (q[0][0], q[0][1], local_ns)
        if q and s - q[0][1] > self.jump_ns:
            if self._above_since is None:
                self._above_since = local_ns
            elif local_ns - self._above_since >= self.jump_hold_ns:
                q.clear()
                self._above_since = None
                self._carry = None
                self.resets += 1
        else:
            self._above_since = None
        while q and q[-1][1] >= s:
            q.pop()
        q.append((local_ns, s))
        cutoff = local_ns - self.window_ns
        while len(q) > 1 and q[0][0] < cutoff:
            q.popleft()
        best = q[0][1]
        if self._carry is not None:
            c_local, c_s, gap_end = self._carry
            bound = c_s + int((local_ns - c_local) * self.max_drift_ppm * 1e-6)
            if bound >= best or local_ns - gap_end > self.window_ns or s - c_s > self.jump_ns:
                self._carry = None
            else:
                best = bound
        self._offset = best
        self.samples += 1
        return best

    @property
    def offset_ns(self) -> int | None:
        return self._offset

    def to_local(self, remote_ns: int) -> int:
        """Convert a remote timestamp; falls back to "now" before the first sample."""
        return remote_ns + self._offset if self._offset is not None else now_ns()


# --- adb plumbing -------------------------------------------------------------------

class AdbError(RuntimeError):
    """adb failed or the device cannot be used."""


class AdbNotFoundError(AdbError):
    """The adb executable could not be located."""


class AdbDeviceError(AdbError):
    """No usable (attached and authorized) device."""


ADB_HELP = (
    "Install Android SDK Platform-Tools (https://developer.android.com/tools/releases/platform-tools), "
    "unzip it and add the folder to PATH, or set CONTROLLERLOG_ADB to the full path of adb. "
    "On the phone: Settings > About phone > tap 'Build number' 7 times, then Developer options > "
    "enable 'USB debugging' (cable) or 'Wireless debugging' (Android 11+: 'adb pair IP:PORT' "
    "with the pairing code, then 'adb connect IP:PORT')."
)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
AdbCommand = str | Sequence[str]


def _adb_candidates() -> list[Path]:
    exe = "adb.exe" if sys.platform == "win32" else "adb"
    roots = [os.environ.get(k) for k in ("ANDROID_HOME", "ANDROID_SDK_ROOT")]
    home = Path.home()
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        roots += [str(Path(local) / "Android" / "Sdk") if local else None]
        extra = [home / "Android" / "Sdk" / "platform-tools" / exe,
                 Path("C:/platform-tools") / exe, home / "platform-tools" / exe,
                 home / "scoop" / "apps" / "adb" / "current" / "platform-tools" / exe]
    elif sys.platform == "darwin":
        roots += [str(home / "Library" / "Android" / "sdk")]
        extra = [Path("/opt/homebrew/bin/adb"), Path("/usr/local/bin/adb")]
    else:
        roots += [str(home / "Android" / "Sdk")]
        extra = [Path("/usr/bin/adb"), Path("/usr/lib/android-sdk/platform-tools/adb")]
    project = Path(__file__).resolve().parents[2] / "vendor" / "platform-tools" / exe
    return [Path(r) / "platform-tools" / exe for r in roots if r] + extra + [project]


def resolve_adb(adb: AdbCommand = "adb") -> list[str]:
    """Return the adb command prefix.

    ``adb`` may be a command list (used as-is, e.g. ``[python, fake_adb.py]``),
    a path or name, or ``"adb"`` for auto-detection: ``$CONTROLLERLOG_ADB``,
    PATH, then the usual SDK locations. Raises :class:`AdbNotFoundError`.
    """
    if not isinstance(adb, str):
        cmd = list(adb)
        if not cmd:
            raise AdbNotFoundError("empty adb command")
        return cmd
    if adb == "adb":
        env = os.environ.get("CONTROLLERLOG_ADB")
        if env:
            p = shutil.which(env) or (env if Path(env).is_file() else None)
            if not p:
                raise AdbNotFoundError(f"CONTROLLERLOG_ADB={env!r} does not exist. {ADB_HELP}")
            return [p]
        p = shutil.which("adb")
        if p:
            return [p]
        for cand in _adb_candidates():
            if cand.is_file():
                return [str(cand)]
        raise AdbNotFoundError(f"adb (Android platform-tools) was not found. {ADB_HELP}")
    p = shutil.which(adb) or (adb if Path(adb).is_file() else None)
    if not p:
        raise AdbNotFoundError(f"adb not found at {adb!r}. {ADB_HELP}")
    return [p]


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              creationflags=_NO_WINDOW)
    except FileNotFoundError as e:
        raise AdbNotFoundError(f"cannot run {cmd[0]!r}: {e}. {ADB_HELP}") from e
    except subprocess.TimeoutExpired as e:
        raise AdbError(f"'{' '.join(cmd[-3:])}' timed out after {timeout:.0f} s") from e


def _start_server(cmd: list[str]) -> None:
    # Start the adb daemon with no pipes attached: on Windows a daemon forked by
    # a piped 'adb devices' can inherit the pipe and keep it open forever.
    try:
        subprocess.run([*cmd, "start-server"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=20, creationflags=_NO_WINDOW)
    except FileNotFoundError as e:
        raise AdbNotFoundError(f"cannot run {cmd[0]!r}: {e}. {ADB_HELP}") from e
    except subprocess.TimeoutExpired as e:
        raise AdbError("'adb start-server' timed out") from e


def parse_adb_devices(text: str) -> list[tuple[str, str, str]]:
    """Parse ``adb devices -l`` into ``[(serial, state, model)]``."""
    out = []
    started = False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        if line.startswith("List of devices"):
            started = True
            continue
        if not started:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, rest = parts[0], line[len(parts[0]):].strip()
        state = "no permissions" if rest.startswith("no permissions") else parts[1]
        model = next((p[6:] for p in parts[2:] if p.startswith("model:")), "")
        out.append((serial, state, model.replace("_", " ")))
    return out


def list_adb_devices(adb: AdbCommand = "adb", timeout: float = 20.0) -> list[tuple[str, str, str]]:
    """Devices known to adb: ``[(serial, state, model)]``; state is ``device`` when usable."""
    cmd = resolve_adb(adb)
    _start_server(cmd)
    r = _run([*cmd, "devices", "-l"], timeout)
    if r.returncode != 0:
        raise AdbError(f"'adb devices' failed: {(r.stderr or r.stdout).strip()}")
    return parse_adb_devices(r.stdout)


_STATE_HELP = {
    "unauthorized": "unlock the phone and accept the 'Allow USB debugging?' prompt (tick 'Always "
                    "allow from this computer'); if no prompt appears, use Developer options > "
                    "'Revoke USB debugging authorizations' and reconnect",
    "offline": "reconnect the cable or re-enable debugging; 'adb kill-server' can help",
    "no permissions": "the OS denies USB access to adb (Linux: install udev rules for Android "
                      "devices / add yourself to the plugdev group)",
    "authorizing": "wait a moment for the phone to finish authorizing, then retry",
    "connecting": "wait a moment for the connection to finish, then retry",
}


def _state_error(serial: str, state: str) -> AdbDeviceError:
    hint = _STATE_HELP.get(state, "boot the device into Android with debugging enabled")
    return AdbDeviceError(f"Android device {serial!r} is '{state}': {hint}.")


def select_device(adb: AdbCommand = "adb", serial: str | None = None) -> tuple[str, str]:
    """Pick the device to use; returns ``(serial, model)`` or raises :class:`AdbDeviceError`."""
    devices = list_adb_devices(adb)
    serial = serial or os.environ.get("ANDROID_SERIAL") or None
    if serial:
        for s, state, model in devices:
            if s == serial:
                if state != "device":
                    raise _state_error(s, state)
                return s, model
        attached = ", ".join(f"{s} ({st})" for s, st, _ in devices) or "none"
        raise AdbDeviceError(f"Android device {serial!r} is not attached (attached: {attached}).")
    ready = [(s, m) for s, st, m in devices if st == "device"]
    if len(ready) == 1:
        return ready[0]
    if not devices:
        raise AdbDeviceError(
            "No Android device found by adb. Connect the phone by USB with USB debugging "
            "enabled (accept the authorization prompt), or pair over Wi-Fi with 'adb pair "
            "IP:PORT' + 'adb connect IP:PORT' (Android 11+ Wireless debugging). "
            "Check with 'adb devices'.")
    if not ready:
        raise _state_error(devices[0][0], devices[0][1])
    names = ", ".join(f"{s} ({m or '?'})" for s, m in ready)
    raise AdbDeviceError(f"Several Android devices are attached ({names}); choose one with "
                         "serial=... (adb -s).")


def list_input_devices(serial: str | None = None, adb: AdbCommand = "adb",
                       timeout: float = 15.0) -> list[EvdevDevice]:
    """Run ``getevent -i`` on the device and parse every input node (see ``is_gamepad``)."""
    cmd = resolve_adb(adb)
    if serial is None:
        serial, _ = select_device(cmd)
    r = _run([*cmd, "-s", serial, "shell", "getevent", "-i"], timeout)
    return _check_getevent(r, serial)


def _adb_failure(err: str | None) -> str | None:
    """The adb client's own error line, if any: ``error: ...`` (older adb) or ``adb: ...``
    (current platform-tools, e.g. ``adb: device unauthorized.``). getevent never prints these."""
    for line in (err or "").splitlines():
        line = line.strip()
        if line.startswith(("error:", "adb: ")):
            return line
    return None


def _check_getevent(r: subprocess.CompletedProcess[str], serial: str) -> list[EvdevDevice]:
    devices = parse_getevent_info(r.stdout)
    err = (r.stderr or "").strip()
    if not devices:
        failure = _adb_failure(err)
        if failure or r.returncode not in (0, 1):
            raise AdbError(f"adb -s {serial} shell getevent failed: "
                           f"{failure or err or r.stdout.strip()}")
        if "ermission denied" in err or "ermission denied" in r.stdout:
            raise AdbError("The adb shell user cannot read /dev/input on this device (vendor "
                           f"restriction?): {err}")
        if "not found" in err or "not found" in r.stdout:
            raise AdbError(f"getevent is not available on this device: {err or r.stdout.strip()}")
    return devices


def adb_connect(address: str, adb: AdbCommand = "adb", timeout: float = 20.0) -> str:
    """``adb connect HOST:PORT`` (Wireless debugging / tcpip); returns adb's message."""
    cmd = resolve_adb(adb)
    _start_server(cmd)
    r = _run([*cmd, "connect", address], timeout)
    msg = (r.stdout + r.stderr).strip()
    # adb often exits 0 on failure ("unable to connect to ...", "failed to connect to ...",
    # "failed to authenticate ..."); success is "connected to X" / "already connected to X".
    if (r.returncode != 0 or "connected to" not in msg
            or any(w in msg for w in ("cannot", "failed", "unable"))):
        raise AdbError(f"adb connect {address} failed: {msg}")
    return msg


def adb_pair(address: str, code: str, adb: AdbCommand = "adb", timeout: float = 30.0) -> str:
    """``adb pair HOST:PORT CODE`` (Android 11+ Wireless debugging pairing)."""
    cmd = resolve_adb(adb)
    _start_server(cmd)
    r = _run([*cmd, "pair", address, code], timeout)
    msg = (r.stdout + r.stderr).strip()
    if r.returncode != 0 or "Failed" in msg or "Successfully paired" not in msg:
        raise AdbError(f"adb pair {address} failed: {msg}")
    return msg


def device_info(dev: EvdevDevice, profile: Profile, adb_serial: str = "",
                phone: str = "") -> DeviceInfo:
    """Hub :class:`DeviceInfo` for an Android input node."""
    extra: dict[str, Any] = {"adb_serial": adb_serial, "bus": f"{dev.bus:04x}",
                             "version": f"{dev.version:04x}", "profile": profile.name}
    if phone:
        extra["phone"] = phone
    if dev.sdl_type_guess:
        extra["sdl_type_guess"] = dev.sdl_type_guess
    return DeviceInfo(id=-1, name=dev.name or dev.path, backend="adb", family=profile.family,
                      vendor_id=dev.vendor, product_id=dev.product, connection=dev.connection,
                      serial=dev.uniq, path=dev.path, extra=extra)


# --- backend --------------------------------------------------------------------------

@dataclass
class _Pad:
    dev: EvdevDevice
    profile: Profile
    mapper: EvdevMapper
    dev_id: int
    last_t: int = 0
    dropping: bool = False


class AdbBackend(Backend):
    """Streams every controller attached to an Android device into the hub via adb.

    ``adb``: command/path (``"adb"`` auto-detects, see :func:`resolve_adb`).
    ``serial``: device to use (default: ``$ANDROID_SERIAL`` or the only one).
    ``device_filter``: case-insensitive substring of the controller name.
    ``profile_override``: merged into every captured device's profile (see
    :func:`build_profile`; invalid overrides raise ValueError here); with
    ``device_filter`` set, ``{"force_gamepad": True}`` also captures matching
    nodes that the classifier rejects (keyboard-mode pads), except touch and
    motion-sensor nodes.
    ``nintendo_swap``: swap A/B and X/Y on Nintendo pads. ``apply_flat``: honour
    the driver's flat (deadzone). ``touchpad``: merge the PlayStation touchpad
    node's click into the pad. Keys of a pad's "<name> Consumer Control" /
    "System Control" / "Keyboard" sibling node (Home/Back on hid-generic pads)
    are always merged. ``reconnect``: restart the stream when it dies (Wi-Fi
    drops, unplug, getevent exiting when a controller vanishes) and re-attach
    when the phone returns; after a session that ran ``healthy_session_s`` the
    first restart is immediate.

    Errors that happen before the stream starts (adb missing, no/unauthorized
    device) end :meth:`run` with an :class:`AdbError` in ``self.error``.
    ``ready`` is set once the stream runs, on such an error, or when stopped.
    """

    name = "adb"
    healthy_session_s = 10.0

    def __init__(self, hub: Hub, serial: str | None = None, adb: AdbCommand = "adb",
                 device_filter: str | None = None, profile_override: dict[str, Any] | None = None,
                 *, nintendo_swap: bool = False, apply_flat: bool = False, touchpad: bool = True,
                 reconnect: bool = True, clock_window_s: float = 10.0,
                 probe_timeout_s: float = 15.0) -> None:
        super().__init__(hub)
        self.adb = adb
        self.serial = serial
        self.model = ""
        self.device_filter = device_filter.lower() if device_filter else None
        self.profile_override = dict(profile_override or {})
        self._override = {k: v for k, v in self.profile_override.items() if k != "force_gamepad"}
        # Validate now: a bad override found when a pad hot-plugs would end capture mid-stream.
        build_profile(EvdevDevice("/dev/input/event0"), self._override)
        self.nintendo_swap = nintendo_swap
        self.apply_flat = apply_flat
        self.touchpad = touchpad
        self.reconnect = reconnect
        self.probe_timeout_s = probe_timeout_s
        self.clock = ClockSync(window_ns=int(clock_window_s * 1e9))
        self.lines = 0
        self.frames = 0
        self.restarts = 0
        self.errors = 0
        self._cmd: list[str] = []
        self._auto_serial = serial is None
        self._known: dict[str, EvdevDevice] = {}
        self._pads: dict[str, _Pad] = {}
        self._companions: dict[str, str] = {}     # sibling node -> gamepad node
        self._pending_add: tuple[str, int] | None = None   # (path, rx of the add line)
        self._proc: subprocess.Popen[str] | None = None
        self._aux: subprocess.Popen[str] | None = None
        self._plock = threading.Lock()
        self._stderr_tail: deque[str] = deque(maxlen=20)

    # -- public helpers ------------------------------------------------------------
    @property
    def pads(self) -> dict[str, tuple[int, EvdevDevice, Profile]]:
        """Captured nodes: ``{path: (hub_id, descriptor, profile)}``."""
        return {p: (pad.dev_id, pad.dev, pad.profile) for p, pad in list(self._pads.items())}

    def stats(self) -> dict[str, Any]:
        return {"serial": self.serial, "model": self.model, "lines": self.lines,
                "frames": self.frames, "restarts": self.restarts, "pads": len(self._pads),
                "clock_offset_ns": self.clock.offset_ns, "clock_samples": self.clock.samples,
                "errors": self.errors}

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._kill_children()
        super().stop(timeout)

    # -- lifecycle -----------------------------------------------------------------
    def run(self) -> None:
        self._cmd = resolve_adb(self.adb)
        self.serial, self.model = select_device(self._cmd, self.serial)
        log.info("adb: using %s (%s)", self.serial, self.model or "?")
        first = True
        backoff = 0.5
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                try:
                    if not first and self._auto_serial:
                        self._reselect()
                    if self._stop.is_set():
                        break
                    self._session(first)
                except AdbError as e:
                    if self._stop.is_set():
                        break
                    if first:
                        raise
                    log.warning("adb: %s", e)
                    self._disconnect_all(now_ns())
                else:
                    if not self._stop.is_set():
                        tail = "; ".join(self._stderr_tail) or "no message"
                        log.warning("adb: getevent stream ended (%s); restarting", tail)
                        self._release_all(now_ns())
                first = False
                if self._stop.is_set() or not self.reconnect:
                    break
                if time.monotonic() - started > self.healthy_session_s:
                    # getevent exits when a controller vanishes with events queued; restart at
                    # once so the other pads lose as little input as possible.
                    backoff = 0.0
                self._stop.wait(backoff)
                backoff = min(max(backoff * 2, 0.5), 5.0)
                self.restarts += 1
        finally:
            self._kill_children()
            self._disconnect_all(now_ns())
        # Stopped before the stream ran: never leave ready.wait() callers hanging. (On an
        # error, Backend._run_guarded sets ready after self.error, so waiters see the error.)
        self.ready.set()

    def _reselect(self) -> None:
        # Wireless-debugging serials (ip:port) change when debugging is re-enabled.
        try:
            serial, model = select_device(self._cmd, None)
        except AdbDeviceError:
            if any(s == self.serial and st == "device" for s, st, _ in list_adb_devices(self._cmd)):
                return
            raise
        if serial != self.serial:
            log.info("adb: switching to %s (%s)", serial, model or "?")
        self.serial, self.model = serial, model

    def _session(self, first: bool) -> None:
        self._pending_add = None
        descs = self._probe(None)
        self._reconcile(descs, now_ns())
        if self._stop.is_set():
            return
        # getevent never fflush()es, so through a plain pipe its stdout is block-buffered on
        # the phone and events arrive in multi-KB bursts. "-tt" forces a PTY: line-buffered.
        proc = self._spawn([*self._cmd, "-s", self.serial, "shell", "-tt", "getevent", "-t"])
        self._stderr_tail.clear()
        drain = threading.Thread(target=self._drain, args=(proc,), name="adb-stderr", daemon=True)
        drain.start()
        self.ready.set()
        t0 = time.monotonic()
        got = 0
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                rx = now_ns()
                got += 1
                try:
                    self._handle_line(line, rx)
                except Exception:  # one bad line or device must never end capture
                    self.errors += 1
                    if self.errors <= 5:
                        log.exception("adb: failed to handle %r", line.rstrip())
                if self._stop.is_set():
                    break
        finally:
            self._kill_children()
            try:
                rc = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = proc.wait()
            drain.join(1.0)
            # never close a pipe another thread may still be blocked reading
            for pipe in (proc.stdout, None if drain.is_alive() else proc.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass
            with self._plock:
                if self._proc is proc:
                    self._proc = None
        if (first and not self._stop.is_set() and got == 0 and rc != 0
                and time.monotonic() - t0 < 2.0):
            raise AdbError("getevent exited immediately: "
                           + ("; ".join(self._stderr_tail) or f"exit code {rc}"))

    # -- subprocesses ----------------------------------------------------------------
    def _spawn(self, cmd: list[str]) -> subprocess.Popen[str]:
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                    errors="replace", creationflags=_NO_WINDOW)
        except FileNotFoundError as e:
            raise AdbNotFoundError(f"cannot run {cmd[0]!r}: {e}. {ADB_HELP}") from e
        with self._plock:
            self._proc = proc
        if self._stop.is_set():
            self._kill_children()
        return proc

    def _drain(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            line = line.strip()
            if line:
                self._stderr_tail.append(line)
                log.debug("adb stderr: %s", line)

    def _kill_children(self) -> None:
        with self._plock:
            procs = [p for p in (self._proc, self._aux) if p is not None]
        for p in procs:
            if p.poll() is None:
                try:
                    p.kill()
                except OSError:
                    pass

    def _probe(self, path: str | None) -> list[EvdevDevice]:
        """``getevent -i [path]``; raises :class:`AdbError` when adb itself fails."""
        cmd = [*self._cmd, "-s", self.serial, "shell", "getevent", "-i"]
        if path:
            # adb shell joins its arguments into a remote shell command line
            if not re.fullmatch(r"/dev/input/[\w.-]+", path):
                raise AdbError(f"unexpected input node path {path!r}")
            cmd.append(path)
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                    errors="replace", creationflags=_NO_WINDOW)
        except FileNotFoundError as e:
            raise AdbNotFoundError(f"cannot run {cmd[0]!r}: {e}. {ADB_HELP}") from e
        with self._plock:
            self._aux = proc
        if self._stop.is_set():  # stop() ran before _aux was visible to it
            self._kill_children()
        try:
            out, err = proc.communicate(timeout=self.probe_timeout_s)
        except subprocess.TimeoutExpired as e:
            proc.kill()
            proc.communicate()
            raise AdbError(f"'getevent -i' timed out after {self.probe_timeout_s:.0f} s") from e
        finally:
            with self._plock:
                self._aux = None
        r = subprocess.CompletedProcess(cmd, proc.returncode, out, err)
        if path:
            devs = parse_getevent_info(out)
            failure = _adb_failure(err)
            if not devs and failure:
                raise AdbError(failure)
            return devs
        return _check_getevent(r, self.serial or "")

    # -- device bookkeeping ---------------------------------------------------------
    def _accept(self, dev: EvdevDevice) -> bool:
        if self.device_filter:
            if self.device_filter not in dev.name.lower():
                return False
            if self.profile_override.get("force_gamepad"):
                # keyboard-mode pads yes; a matching touchpad / IMU / headset-jack node no
                return not dev.is_touch_or_sensor and bool(dev.keys or dev.abs)
        return dev.is_gamepad

    def _source_key(self, dev: EvdevDevice) -> tuple[str, Any]:
        # uniq (BT MAC / USB serial) keeps the hub id across power cycles, new eventN
        # numbers and wireless-debugging serial changes; the node path is the fallback.
        if dev.uniq and not any(p.dev.uniq == dev.uniq and p.dev.path != dev.path
                                for p in self._pads.values()):
            return ("adb", f"{dev.uniq}|{dev.name}")
        return ("adb", f"{self.serial}|{dev.path}|{dev.name}")

    def _connect(self, dev: EvdevDevice, t: int) -> None:
        profile = build_profile(dev, self._override, nintendo_swap=self.nintendo_swap,
                                apply_flat=self.apply_flat)
        mapper = EvdevMapper(profile, dev)
        info = device_info(dev, profile, self.serial or "", self.model)
        dev_id = self.hub.connect(self._source_key(dev), info, t)
        pad = _Pad(dev, profile, mapper, dev_id, last_t=t)
        self._pads[dev.path] = pad
        self._publish(pad, mapper.load(dev), t)
        log.info("adb: connected #%d %s [%04x:%04x %s] profile %s", dev_id, dev.name,
                 dev.vendor, dev.product, dev.path, profile.name)

    def _disconnect(self, path: str, t: int) -> None:
        pad = self._pads.pop(path, None)
        if pad is None:
            return
        t = max(t, pad.last_t)
        self.hub.disconnect(pad.dev_id, t)
        self._companions = {c: g for c, g in self._companions.items() if g != path}
        log.info("adb: disconnected #%d %s", pad.dev_id, pad.dev.name)

    def _disconnect_all(self, t: int) -> None:
        for path in list(self._pads):
            self._disconnect(path, t)

    def _release_all(self, t: int) -> None:
        for pad in self._pads.values():
            self._publish(pad, pad.mapper.reset(), max(t, pad.last_t))

    def _link_companions(self) -> None:
        """Attach sibling nodes ("<pad name> Touchpad", "... Consumer Control"...) to their pad."""
        self._companions = {}
        for path, dev in self._known.items():
            if path in self._pads or not dev.keys:
                continue
            for gpath, pad in self._pads.items():
                if self._is_companion(dev, pad):
                    self._companions[path] = gpath
                    break

    def _is_companion(self, dev: EvdevDevice, pad: _Pad) -> bool:
        g = pad.dev
        if not dev.name.startswith(g.name + " "):
            return False
        suffix = dev.name[len(g.name) + 1:]
        if suffix not in COMPANION_SUFFIXES or (suffix == "Touchpad" and not self.touchpad):
            return False
        if not dev.keys & pad.profile.buttons.keys():
            return False
        # same controller: uniq (BT MAC) and phys must agree whenever both are known
        if any(a and b and a != b for a, b in ((dev.uniq, g.uniq), (dev.location, g.location))):
            return False
        return not (dev.has_ids and g.has_ids
                    and (dev.vendor, dev.product) != (g.vendor, g.product))

    def _reconcile(self, descs: list[EvdevDevice], t: int) -> None:
        self._known = {d.path: d for d in descs}
        for path, pad in list(self._pads.items()):
            d = self._known.get(path)
            if d is None or d.name != pad.dev.name or not self._accept(d):
                self._disconnect(path, t)
        for d in descs:
            pad = self._pads.get(d.path)
            if pad is not None:
                pad.dev = d
                pad.dropping = False
                self._publish(pad, pad.mapper.load(d), max(t, pad.last_t))
            elif self._accept(d):
                self._connect(d, t)
        self._link_companions()
        if not self._pads:
            log.info("adb: no controller on %s yet; waiting for one to connect", self.serial)

    def _device_added(self, path: str, name: str | None, t: int) -> None:
        """Hot-plug; ``t`` is when the ``add device`` line arrived. Frames that queue up
        while ``getevent -i`` runs keep their own (earlier) times instead of being
        clamped to the end of the probe."""
        known = self._known.get(path)
        if known is not None and (name is None or known.name == name):
            return
        try:
            descs = self._probe(path)
        except AdbError as e:
            log.warning("adb: cannot describe new device %s: %s", path, e)
            return
        for d in descs:
            if d.path != path:
                continue
            self._known[path] = d
            if path in self._pads:
                self._disconnect(path, t)
            if self._accept(d):
                self._connect(d, t)
            self._link_companions()

    def _device_removed(self, path: str, t: int) -> None:
        self._known.pop(path, None)
        if path in self._pads:
            self._disconnect(path, t)
        self._companions.pop(path, None)

    # -- stream ----------------------------------------------------------------------
    def _handle_line(self, line: str, rx: int) -> None:
        self.lines += 1
        item = parse_stream_line(line)
        if item is None:
            return
        if isinstance(item, DeviceName):
            if self._pending_add is not None:
                (path, t), self._pending_add = self._pending_add, None
                self._device_added(path, item.name, t)
            return
        if self._pending_add is not None:
            (path, t), self._pending_add = self._pending_add, None
            self._device_added(path, None, t)
        if isinstance(item, RawEvent):
            self._handle_event(item, rx)
        elif isinstance(item, DeviceAdded):
            self._pending_add = (item.path, rx)
        elif isinstance(item, DeviceRemoved):
            self._device_removed(item.path, rx)

    def _handle_event(self, ev: RawEvent, rx: int) -> None:
        is_report = ev.type == EV_SYN and ev.code == SYN_REPORT
        if is_report and ev.t_ns is not None:
            self.clock.update(ev.t_ns, rx)
        path = ev.path or ""
        pad = self._pads.get(path)
        if pad is None:
            gpath = self._companions.get(path)
            pad = self._pads.get(gpath) if gpath is not None else None
            if pad is None:
                return
            if is_report:
                t = self.clock.to_local(ev.t_ns) if ev.t_ns is not None else rx
                self._publish(pad, pad.mapper.sync(companion=True), t)
            elif ev.type == EV_KEY and ev.code in pad.profile.buttons:
                pad.mapper.feed(EV_KEY, ev.code, ev.value, companion=True)
            return
        if ev.type == EV_SYN:
            if ev.code == SYN_REPORT:
                t = self.clock.to_local(ev.t_ns) if ev.t_ns is not None else rx
                if pad.dropping:
                    pad.dropping = False
                    pad.mapper.discard()
                    self._resync(pad, t)
                else:
                    self._publish(pad, pad.mapper.sync(), t)
            elif ev.code == SYN_DROPPED:
                pad.mapper.discard()
                pad.dropping = True
        else:
            pad.mapper.feed(ev.type, ev.code, ev.value)

    def _resync(self, pad: _Pad, t: int) -> None:
        log.warning("adb: events dropped on %s; re-reading state", pad.dev.path)
        try:
            descs = self._probe(pad.dev.path)
        except AdbError as e:
            log.warning("adb: resync failed: %s", e)
            return
        for d in descs:
            if d.path == pad.dev.path:
                self._publish(pad, pad.mapper.load(d), t)

    def _publish(self, pad: _Pad, changes: list[tuple[str, int, int]], t: int) -> None:
        if not changes:
            return
        if t < pad.last_t:
            t = pad.last_t
        pad.last_t = t
        self.frames += 1
        for kind, idx, value in changes:
            self.hub.publish(InputEvent(t, pad.dev_id, kind, idx, value))
