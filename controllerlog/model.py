"""Canonical controller model shared by every backend, recorder, overlay and exporter.

Button and axis indices deliberately match SDL3's ``SDL_GamepadButton`` /
``SDL_GamepadAxis`` enums (SDL 3.4), so SDL events can be stored without
translation and other backends (Android evdev, bk2 import, GSE logs) map onto
the same positional layout ("south" = bottom face button = Xbox A / PS Cross /
Nintendo B).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --- Buttons (index == SDL_GamepadButton value) -----------------------------

BUTTONS: tuple[str, ...] = (
    "south",           # 0  Xbox A, PS Cross, Nintendo B
    "east",            # 1  Xbox B, PS Circle, Nintendo A
    "west",            # 2  Xbox X, PS Square, Nintendo Y
    "north",           # 3  Xbox Y, PS Triangle, Nintendo X
    "back",            # 4  View / Share / Select / Minus
    "guide",           # 5  Xbox / PS / Home
    "start",           # 6  Menu / Options / Start / Plus
    "left_stick",      # 7  L3
    "right_stick",     # 8  R3
    "left_shoulder",   # 9  LB / L1 / L
    "right_shoulder",  # 10 RB / R1 / R
    "dpad_up",         # 11
    "dpad_down",       # 12
    "dpad_left",       # 13
    "dpad_right",      # 14
    "misc1",           # 15 Share / Mic / Capture
    "right_paddle1",   # 16
    "left_paddle1",    # 17
    "right_paddle2",   # 18
    "left_paddle2",    # 19
    "touchpad",        # 20
    "misc2",           # 21
    "misc3",           # 22 GameCube L click
    "misc4",           # 23 GameCube R click
    "misc5",           # 24
    "misc6",           # 25
)
BUTTON_INDEX: dict[str, int] = {name: i for i, name in enumerate(BUTTONS)}
NUM_BUTTONS = len(BUTTONS)

# --- Axes (index == SDL_GamepadAxis value) ----------------------------------

AXES: tuple[str, ...] = (
    "left_x",         # 0  -32768..32767, negative = left
    "left_y",         # 1  -32768..32767, negative = up
    "right_x",        # 2
    "right_y",        # 3
    "left_trigger",   # 4  0..32767
    "right_trigger",  # 5  0..32767
)
AXIS_INDEX: dict[str, int] = {name: i for i, name in enumerate(AXES)}
NUM_AXES = len(AXES)
AXIS_MIN = -32768
AXIS_MAX = 32767
TRIGGER_AXES = frozenset({4, 5})

# A trigger past this value counts as "pressed" for digital consumers
# (frame tables, bk2 export, GB/GBA mapping, overlay highlight). Only
# gm2.state_to_mask's default, which emulates GSE's own host mapping of a live
# controller, uses GSE's lower threshold instead.
TRIGGER_PRESS_THRESHOLD = 16384

# --- Controller families (drive default overlay layout + button labels) -----

# Values are the strings returned by SDL_GetGamepadStringForType() plus a few
# of our own ("gameboy", "gba", "generic").
FAMILY_XBOX = "xbox"
FAMILY_PLAYSTATION = "playstation"
FAMILY_SWITCH = "switch"
FAMILY_GAMECUBE = "gamecube"
FAMILY_GAMEBOY = "gameboy"
FAMILY_GBA = "gba"
FAMILY_GENERIC = "generic"

_SDL_TYPE_TO_FAMILY = {
    "xbox360": FAMILY_XBOX,
    "xboxone": FAMILY_XBOX,
    "ps3": FAMILY_PLAYSTATION,
    "ps4": FAMILY_PLAYSTATION,
    "ps5": FAMILY_PLAYSTATION,
    "switchpro": FAMILY_SWITCH,
    "joyconleft": FAMILY_SWITCH,
    "joyconright": FAMILY_SWITCH,
    "joyconpair": FAMILY_SWITCH,
    "gamecube": FAMILY_GAMECUBE,
}


def family_for_sdl_type(sdl_type: str | None) -> str:
    """Map an SDL gamepad type string (e.g. ``"ps5"``) to a layout family."""
    if not sdl_type:
        return FAMILY_GENERIC
    return _SDL_TYPE_TO_FAMILY.get(sdl_type.lower(), FAMILY_GENERIC)


# Face-button display labels per family, keyed by canonical button name.
FACE_LABELS: dict[str, dict[str, str]] = {
    FAMILY_XBOX: {"south": "A", "east": "B", "west": "X", "north": "Y",
                  "back": "View", "start": "Menu", "guide": "Xbox",
                  "left_shoulder": "LB", "right_shoulder": "RB",
                  "left_trigger": "LT", "right_trigger": "RT", "misc1": "Share"},
    FAMILY_PLAYSTATION: {"south": "✕", "east": "○", "west": "□", "north": "△",
                         "back": "Share", "start": "Options", "guide": "PS",
                         "left_shoulder": "L1", "right_shoulder": "R1",
                         "left_trigger": "L2", "right_trigger": "R2", "misc1": "Mic",
                         "touchpad": "Pad"},
    # SDL reports Nintendo controllers positionally: south is the physical
    # bottom button, which Nintendo labels "B".
    FAMILY_SWITCH: {"south": "B", "east": "A", "west": "Y", "north": "X",
                    "back": "−", "start": "+", "guide": "Home",
                    "left_shoulder": "L", "right_shoulder": "R",
                    "left_trigger": "ZL", "right_trigger": "ZR", "misc1": "Cap",
                    "misc2": "C", "left_paddle1": "GL", "right_paddle1": "GR"},
    FAMILY_GAMECUBE: {"south": "A", "east": "X", "west": "B", "north": "Y",
                      "start": "Start", "right_shoulder": "Z", "left_shoulder": "ZL",
                      "left_trigger": "L", "right_trigger": "R", "guide": "Home",
                      "misc1": "Cap", "misc2": "C", "misc3": "L·", "misc4": "R·"},
    FAMILY_GAMEBOY: {"south": "B", "east": "A", "back": "Select", "start": "Start"},
    FAMILY_GBA: {"south": "B", "east": "A", "back": "Select", "start": "Start",
                 "left_shoulder": "L", "right_shoulder": "R"},
    FAMILY_GENERIC: {"south": "1", "east": "2", "west": "3", "north": "4",
                     "back": "Sel", "start": "Start", "guide": "Home",
                     "left_shoulder": "L1", "right_shoulder": "R1",
                     "left_trigger": "L2", "right_trigger": "R2"},
}


# --- Devices ------------------------------------------------------------------

@dataclass
class DeviceInfo:
    """Static description of a connected controller."""

    id: int                      # stable id inside one recording (0, 1, 2...)
    name: str = "Unknown controller"
    backend: str = "sdl3"        # "sdl3", "adb", "virtual", "replay"...
    sdl_type: str = ""           # SDL_GetGamepadStringForType(), e.g. "ps5"
    family: str = FAMILY_GENERIC
    vendor_id: int = 0
    product_id: int = 0
    connection: str = "unknown"  # "wired" | "wireless" | "unknown"
    serial: str = ""
    path: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        d = {
            "id": self.id, "name": self.name, "backend": self.backend,
            "sdl_type": self.sdl_type, "family": self.family,
            "vendor_id": self.vendor_id, "product_id": self.product_id,
            "connection": self.connection,
        }
        if self.serial:
            d["serial"] = self.serial
        if self.path:
            d["path"] = self.path
        if self.extra:
            d["extra"] = self.extra
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "DeviceInfo":
        return cls(
            id=int(d["id"]), name=d.get("name", "Unknown controller"),
            backend=d.get("backend", "sdl3"), sdl_type=d.get("sdl_type", ""),
            family=d.get("family", FAMILY_GENERIC),
            vendor_id=int(d.get("vendor_id", 0)), product_id=int(d.get("product_id", 0)),
            connection=d.get("connection", "unknown"), serial=d.get("serial", ""),
            path=d.get("path", ""), extra=dict(d.get("extra", {})),
        )


# --- Events -------------------------------------------------------------------

# Event kinds.
BUTTON = "b"      # code = button index, value = 1 (down) / 0 (up)
AXIS = "a"        # code = axis index, value = int16
CONNECT = "+"     # code = None, value = DeviceInfo json
DISCONNECT = "-"  # code = None, value = None
MARK = "m"        # code = None, value = label string (reset, split, run start...)

EVENT_KINDS = frozenset({BUTTON, AXIS, CONNECT, DISCONNECT, MARK})


@dataclass(slots=True)
class InputEvent:
    """One input change.

    ``t_ns`` is a monotonic timestamp in nanoseconds. Backends report their own
    clock (SDL_GetTicksNS, kernel time...); the recorder rebases everything to
    "nanoseconds since recording start" when it writes the log.
    """

    t_ns: int
    device: int | None
    kind: str
    code: int | None = None
    value: Any = None

    def to_row(self) -> list[Any]:
        """Compact JSON row: ``[t_ns, device, kind, code?, value?]``."""
        if self.kind in (BUTTON, AXIS):
            return [self.t_ns, self.device, self.kind, self.code, self.value]
        if self.kind == DISCONNECT:
            return [self.t_ns, self.device, self.kind]
        return [self.t_ns, self.device, self.kind, None, self.value]

    @classmethod
    def from_row(cls, row: list[Any]) -> "InputEvent":
        t, dev, kind = int(row[0]), row[1], row[2]
        code = row[3] if len(row) > 3 else None
        value = row[4] if len(row) > 4 else None
        return cls(t, dev, kind, code, value)


# --- State --------------------------------------------------------------------

@dataclass
class PadState:
    """Full instantaneous state of one controller."""

    buttons: list[int] = field(default_factory=lambda: [0] * NUM_BUTTONS)
    axes: list[int] = field(default_factory=lambda: [0] * NUM_AXES)

    def copy(self) -> "PadState":
        return PadState(list(self.buttons), list(self.axes))

    def apply(self, ev: InputEvent) -> bool:
        """Apply a button/axis event. Returns True if the state changed."""
        if ev.value is None or not isinstance(ev.code, int):
            return False
        if ev.kind == BUTTON and 0 <= ev.code < NUM_BUTTONS:
            v = 1 if ev.value else 0
            if self.buttons[ev.code] != v:
                self.buttons[ev.code] = v
                return True
        elif ev.kind == AXIS and 0 <= ev.code < NUM_AXES:
            v = int(ev.value)
            if self.axes[ev.code] != v:
                self.axes[ev.code] = v
                return True
        return False

    def pressed(self, name: str) -> bool:
        """Digital view of a button or trigger by canonical name."""
        if name in BUTTON_INDEX:
            return bool(self.buttons[BUTTON_INDEX[name]])
        if name in ("left_trigger", "right_trigger"):
            return self.axes[AXIS_INDEX[name]] >= TRIGGER_PRESS_THRESHOLD
        raise KeyError(name)

    def button_mask(self) -> int:
        """Bitmask of pressed buttons (bit i == BUTTONS[i])."""
        m = 0
        for i, v in enumerate(self.buttons):
            if v:
                m |= 1 << i
        return m

    def to_json(self) -> dict[str, Any]:
        return {"buttons": list(self.buttons), "axes": list(self.axes)}


def stick_direction(x: int, y: int, deadzone: int = 12000) -> tuple[bool, bool, bool, bool]:
    """(up, down, left, right) of an analog stick past ``deadzone``."""
    return (y <= -deadzone, y >= deadzone, x <= -deadzone, x >= deadzone)
