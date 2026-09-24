"""Virtual game controller output (Xbox 360 or DualShock 4) through ViGEmBus.

Wraps `vgamepad <https://github.com/yannbouteiller/vgamepad>`_, which talks to
the ViGEmBus kernel driver on Windows (and to uinput on Linux). Games see the
virtual pad as a genuine wired controller, so anything expressed in the
canonical model (:mod:`controllerlog.model`) can be "played" into a game:
recorded runs, TAS frame tables, or a live physical controller (the bridge).

Canonical -> native conversion (verified by reading the pads back through SDL3):

* Xbox 360 (XUSB): positional face buttons (south = A ...), triggers
  0..32767 -> 0..255, sticks int16 with **Y inverted** (canonical Y is
  negative = up, XInput Y is positive = up; -32768 clamps to 32767). Opposite
  d-pad directions are passed through unchanged (XInput allows them). On
  Linux vgamepad writes the Y value straight into evdev ``ABS_Y`` (down =
  positive, like canonical), so Y is not inverted there (see
  :data:`X360_INVERT_Y`; read from vgamepad's source, untested).
* DualShock 4: Cross/Circle/Square/Triangle, Share = back, Options = start,
  PS = guide, touchpad click = touchpad, L2/R2 digital bits set past
  :data:`~controllerlog.model.TRIGGER_PRESS_THRESHOLD`, sticks 0..255 with
  128 = centre and Y down = positive (same direction as canonical, no
  inversion), d-pad as an 8-way hat (opposite directions cancel).

Canonical buttons with no native equivalent (paddles, misc*, and touchpad on
X360) are dropped unless a ``remap`` sends them somewhere.
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from typing import Any, Mapping

from ..layouts import DIGITAL_INPUTS
from ..model import (AXIS_INDEX, AXIS_MAX, AXIS_MIN, BUTTON_INDEX, BUTTONS,
                     NUM_AXES, NUM_BUTTONS, TRIGGER_PRESS_THRESHOLD, PadState)

log = logging.getLogger(__name__)

VIGEMBUS_URL = "https://github.com/nefarius/ViGEmBus/releases"


class VirtualPadError(RuntimeError):
    """A virtual controller operation failed."""


class VirtualPadUnavailable(VirtualPadError):
    """Virtual controllers cannot be created on this machine (driver/package missing)."""


@dataclass(frozen=True)
class TargetInfo:
    """What a virtual controller looks like to the OS (and to our own SDL backend)."""

    name: str        # "x360" | "ds4"
    label: str
    sdl_type: str    # SDL_GetGamepadStringForType() of the virtual device
    vendor_id: int
    product_id: int


TARGETS: dict[str, TargetInfo] = {
    "x360": TargetInfo("x360", "Xbox 360 Controller", "xbox360", 0x045E, 0x028E),
    "ds4": TargetInfo("ds4", "DualShock 4", "ps4", 0x054C, 0x05C4),
}
_TARGET_ALIASES = {"x360": "x360", "xbox360": "x360", "xbox": "x360", "xinput": "x360",
                   "ds4": "ds4", "ps4": "ds4", "dualshock4": "ds4", "dualshock": "ds4"}

# Swap face buttons so Nintendo *labels* match Xbox labels (Nintendo A -> Xbox A)
# instead of the default positional mapping (Nintendo A is "east" -> Xbox B).
NINTENDO_LABEL_REMAP: dict[str, str] = {"south": "east", "east": "south",
                                        "west": "north", "north": "west"}

# --- Native constants (ViGEm XUSB_BUTTON / DS4_BUTTONS, see vgamepad.win.vigem_commons)

XUSB_BUTTONS: dict[str, int] = {
    "dpad_up": 0x0001, "dpad_down": 0x0002, "dpad_left": 0x0004, "dpad_right": 0x0008,
    "start": 0x0010, "back": 0x0020, "left_stick": 0x0040, "right_stick": 0x0080,
    "left_shoulder": 0x0100, "right_shoulder": 0x0200, "guide": 0x0400,
    "south": 0x1000, "east": 0x2000, "west": 0x4000, "north": 0x8000,
}
DS4_BUTTONS: dict[str, int] = {
    "west": 1 << 4, "south": 1 << 5, "east": 1 << 6, "north": 1 << 7,  # Square Cross Circle Triangle
    "left_shoulder": 1 << 8, "right_shoulder": 1 << 9,
    "back": 1 << 12, "start": 1 << 13, "left_stick": 1 << 14, "right_stick": 1 << 15,
}
DS4_TRIGGER_LEFT = 1 << 10
DS4_TRIGGER_RIGHT = 1 << 11
DS4_SPECIAL: dict[str, int] = {"guide": 1 << 0, "touchpad": 1 << 1}
DS4_DPAD_NONE = 0x8
# (vertical, horizontal) with up/right positive -> DS4 hat value (N=0, clockwise).
_DS4_HAT = {(1, 0): 0, (1, 1): 1, (0, 1): 2, (-1, 1): 3, (-1, 0): 4,
            (-1, -1): 5, (0, -1): 6, (1, -1): 7, (0, 0): DS4_DPAD_NONE}

# vgamepad's Windows backend takes XInput sticks (Y up = positive); its Linux backend
# copies sThumbLY into evdev ABS_Y unchanged, where down is positive like canonical.
X360_INVERT_Y = sys.platform == "win32"

_TRIGGERS = ("left_trigger", "right_trigger")
_LT, _RT = AXIS_INDEX["left_trigger"], AXIS_INDEX["right_trigger"]
_LX, _LY, _RX, _RY = (AXIS_INDEX[a] for a in ("left_x", "left_y", "right_x", "right_y"))
_B = BUTTON_INDEX


def normalize_target(target: str) -> str:
    """``"xbox360"``/``"xbox"``/``"x360"`` -> ``"x360"``; ``"ps4"``/``"ds4"`` -> ``"ds4"``."""
    key = _TARGET_ALIASES.get(str(target).lower())
    if key is None:
        raise ValueError(f"unknown virtual controller target {target!r} (use 'x360' or 'ds4')")
    return key


# --- Value conversion (pure functions, exported for tests and other outputs) -----

def _clamp(v: int, lo: int, hi: int) -> int:
    return lo if v < lo else hi if v > hi else v


def trigger_to_u8(value: int) -> int:
    """Canonical trigger 0..32767 -> 0..255 (rounded; 32767 -> 255)."""
    return _clamp((int(value) * 255 + 16383) // 32767, 0, 255)


def stick_to_xinput(value: int, invert: bool = False) -> int:
    """Canonical stick int16 -> XInput int16; ``invert`` flips Y (canonical down+ -> XInput up+)."""
    v = -int(value) if invert else int(value)
    return _clamp(v, AXIS_MIN, AXIS_MAX)


def stick_to_u8(value: int) -> int:
    """Canonical stick int16 -> DS4 byte (0 = left/up, 128 = centre, 255 = right/down).

    Exact inverse of SDL's ``byte * 257 - 32768`` decoding, so 0 -> 128.
    """
    return _clamp((_clamp(int(value), AXIS_MIN, AXIS_MAX) + 32768 + 128) // 257, 0, 255)


def ds4_hat(up: bool, down: bool, left: bool, right: bool) -> int:
    """Combine d-pad buttons into a DS4 hat value; opposite directions cancel."""
    return _DS4_HAT[(int(bool(up)) - int(bool(down)), int(bool(right)) - int(bool(left)))]


def validate_remap(remap: Mapping[str, str] | None) -> dict[str, str]:
    """Check a canonical->canonical remap (buttons and triggers only)."""
    out: dict[str, str] = {}
    for src, dst in (remap or {}).items():
        for v in (src, dst):
            if v not in DIGITAL_INPUTS:
                raise ValueError(f"bad remap {src!r}->{dst!r}: unknown input {v!r} "
                                 f"(use canonical button names or left/right_trigger)")
        out[src] = dst
    return out


def remap_state(state: PadState, remap: Mapping[str, str] | None) -> PadState:
    """Apply a canonical->canonical remap (e.g. ``{"south": "east", "east": "south"}``).

    Buttons and triggers can be mapped onto each other: a button mapped to a
    trigger pulls it fully, a trigger mapped to a button presses it past
    :data:`TRIGGER_PRESS_THRESHOLD`. Unmapped inputs pass through; a remapped
    source no longer drives its original control.
    """
    if not remap:
        return state
    buttons = [0] * NUM_BUTTONS
    axes = list(state.axes)
    for t in _TRIGGERS:
        if remap.get(t, t) != t:
            axes[AXIS_INDEX[t]] = 0
    for i, v in enumerate(state.buttons):
        if not v:
            continue
        dst = remap.get(BUTTONS[i], BUTTONS[i])
        if dst in BUTTON_INDEX:
            buttons[BUTTON_INDEX[dst]] = 1
        else:
            axes[AXIS_INDEX[dst]] = AXIS_MAX
    for t in _TRIGGERS:
        dst = remap.get(t, t)
        if dst == t:
            continue
        v = state.axes[AXIS_INDEX[t]]
        if dst in BUTTON_INDEX:
            if v >= TRIGGER_PRESS_THRESHOLD:
                buttons[BUTTON_INDEX[dst]] = 1
        else:
            axes[AXIS_INDEX[dst]] = max(axes[AXIS_INDEX[dst]], v)
    return PadState(buttons, axes)


def x360_report(state: PadState,
                invert_y: bool = True) -> tuple[int, int, int, int, int, int, int]:
    """``(wButtons, LT, RT, LX, LY, RX, RY)`` of an XUSB report for a canonical state.

    ``invert_y``: XInput convention (Y up = positive). :class:`VirtualPad`
    passes :data:`X360_INVERT_Y` (False on Linux, where vgamepad feeds evdev).
    """
    b = state.buttons
    mask = 0
    for name, bit in XUSB_BUTTONS.items():
        if b[_B[name]]:
            mask |= bit
    a = state.axes
    return (mask, trigger_to_u8(a[_LT]), trigger_to_u8(a[_RT]),
            stick_to_xinput(a[_LX]), stick_to_xinput(a[_LY], invert=invert_y),
            stick_to_xinput(a[_RX]), stick_to_xinput(a[_RY], invert=invert_y))


def _x360_native_report(state: PadState) -> tuple[int, int, int, int, int, int, int]:
    return x360_report(state, invert_y=X360_INVERT_Y)


def ds4_report(state: PadState) -> tuple[int, int, int, int, int, int, int, int, int]:
    """``(wButtons without hat, bSpecial, hat, L2, R2, LX, LY, RX, RY)`` for a DS4 report."""
    b = state.buttons
    a = state.axes
    mask = 0
    for name, bit in DS4_BUTTONS.items():
        if b[_B[name]]:
            mask |= bit
    if a[_LT] >= TRIGGER_PRESS_THRESHOLD:
        mask |= DS4_TRIGGER_LEFT
    if a[_RT] >= TRIGGER_PRESS_THRESHOLD:
        mask |= DS4_TRIGGER_RIGHT
    special = 0
    for name, bit in DS4_SPECIAL.items():
        if b[_B[name]]:
            special |= bit
    hat = ds4_hat(b[_B["dpad_up"]], b[_B["dpad_down"]], b[_B["dpad_left"]], b[_B["dpad_right"]])
    return (mask, special, hat, trigger_to_u8(a[_LT]), trigger_to_u8(a[_RT]),
            stick_to_u8(a[_LX]), stick_to_u8(a[_LY]), stick_to_u8(a[_RX]), stick_to_u8(a[_RY]))


# --- vgamepad loading ---------------------------------------------------------------

def _driver_help(err: BaseException) -> str:
    if sys.platform == "win32":
        return ("Virtual controllers need the ViGEmBus driver. Install the latest "
                f"ViGEmBus_*.exe from {VIGEMBUS_URL} (the project is retired but its last "
                "release still works on Windows 10/11), reboot if asked, then retry. "
                f"Underlying error: {type(err).__name__}: {err}")
    return ("Virtual controllers need vgamepad's Linux dependencies: libevdev "
            "(pip install libevdev) and write access to /dev/uinput (sudo modprobe uinput "
            "and a udev rule or the 'input' group). "
            f"Underlying error: {type(err).__name__}: {err}")


def load_vgamepad() -> Any:
    """Import vgamepad, turning its import-time driver errors into :class:`VirtualPadUnavailable`.

    vgamepad connects to ViGEmBus when imported, so a missing driver surfaces here.
    """
    try:
        import vgamepad  # noqa: PLC0415
    except ImportError as e:
        if e.name == "vgamepad":
            raise VirtualPadUnavailable(
                "The 'vgamepad' package is not installed (pip install vgamepad).") from e
        raise VirtualPadUnavailable(_driver_help(e)) from e
    except Exception as e:  # vgamepad raises a bare Exception('VIGEM_ERROR_BUS_NOT_FOUND')
        raise VirtualPadUnavailable(_driver_help(e)) from e
    return vgamepad


def virtual_pad_unavailable_reason() -> str | None:
    """``None`` if virtual controllers can be created, else a human-readable reason."""
    try:
        load_vgamepad()
    except VirtualPadUnavailable as e:
        return str(e)
    return None


# --- VirtualPad -------------------------------------------------------------------

def _button_index(button: int | str) -> int:
    idx = BUTTON_INDEX[button] if isinstance(button, str) else int(button)
    if not 0 <= idx < NUM_BUTTONS:
        raise ValueError(f"bad button {button!r}")
    return idx


def _axis_index(axis: int | str) -> int:
    idx = AXIS_INDEX[axis] if isinstance(axis, str) else int(axis)
    if not 0 <= idx < NUM_AXES:
        raise ValueError(f"bad axis {axis!r}")
    return idx


class VirtualPad:
    """A virtual Xbox 360 or DualShock 4 controller driven by canonical state.

    Use :meth:`apply` to push a full :class:`PadState`, or stage changes with
    :meth:`set_button` / :meth:`set_axis` and push them with :meth:`update`.
    Identical consecutive reports are skipped unless ``force=True``.
    :meth:`close` (or leaving the ``with`` block) releases everything and
    unplugs the device. Not thread-safe: drive it from one thread.
    """

    RELEASE_DELAY_S = 0.05

    def __init__(self, target: str = "x360", remap: Mapping[str, str] | None = None) -> None:
        self.target: TargetInfo = TARGETS[normalize_target(target)]
        self.remap = validate_remap(remap)
        vg = load_vgamepad()
        cls = vg.VX360Gamepad if self.target.name == "x360" else vg.VDS4Gamepad
        try:
            self._dev: Any = cls()
        except Exception as e:  # AssertionError / Exception('VIGEM_ERROR_...')
            raise VirtualPadUnavailable(_driver_help(e)) from e
        self._staged = PadState()
        self._last: tuple[int, ...] | None = None
        self.updates = 0
        self.closed = False
        self._report = _x360_native_report if self.target.name == "x360" else ds4_report
        self._send = self._send_x360 if self.target.name == "x360" else self._send_ds4
        log.info("virtual %s connected", self.target.label)

    # -- identity --------------------------------------------------------------
    @property
    def vendor_id(self) -> int:
        return self.target.vendor_id

    @property
    def product_id(self) -> int:
        return self.target.product_id

    @property
    def state(self) -> PadState:
        """Copy of the canonical state staged for (or last pushed to) the device."""
        return self._staged.copy()

    # -- input -----------------------------------------------------------------
    def apply(self, state: PadState) -> bool:
        """Set every control from a full canonical state and push one report.

        Returns False if the resulting report equals the last one (not resent).
        """
        self._staged = PadState(list(state.buttons), list(state.axes))
        return self.update()

    def set_button(self, button: int | str, pressed: bool | int) -> None:
        """Stage a button (canonical index or name); call :meth:`update` to send."""
        self._staged.buttons[_button_index(button)] = 1 if pressed else 0

    def set_axis(self, axis: int | str, value: int) -> None:
        """Stage an axis (canonical index or name, canonical range); call :meth:`update`."""
        self._staged.axes[_axis_index(axis)] = int(value)

    def update(self, force: bool = False) -> bool:
        """Push the staged state. Returns False if the report was unchanged and skipped."""
        if self.closed:
            raise VirtualPadError("virtual controller is closed")
        report = self._report(remap_state(self._staged, self.remap))
        if not force and report == self._last:
            return False
        try:
            self._send(report)
        except Exception as e:
            # Drop the driver traceback: its frames reference the vgamepad device, and an
            # error kept by the caller (e.g. Bridge.error) would stop close() unplugging it.
            raise VirtualPadError(
                f"virtual {self.target.label} update failed: {e}") from e.with_traceback(None)
        self._last = report
        self.updates += 1
        return True

    def reset(self) -> None:
        """Release every button, centre sticks and triggers, and push the report."""
        self._staged = PadState()
        self.update(force=True)

    def close(self, release_delay_s: float | None = None) -> None:
        """Release everything, then unplug the virtual device. Safe to call twice.

        Waits ``release_delay_s`` (default :attr:`RELEASE_DELAY_S`) between the
        release report and unplugging, so games polling once per frame see the
        release instead of the device vanishing mid-press.
        """
        if self.closed:
            return
        delay = self.RELEASE_DELAY_S if release_delay_s is None else release_delay_s
        try:
            self.reset()
            if delay > 0:
                time.sleep(delay)
        except Exception as e:
            log.warning("could not release virtual %s: %s", self.target.label, e)
        finally:
            self.closed = True
            dev, self._dev = self._dev, None
            del dev  # vgamepad unplugs the target in __del__ (last reference)
            log.info("virtual %s disconnected", self.target.label)

    def __enter__(self) -> "VirtualPad":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- native reports ----------------------------------------------------------
    def _send_x360(self, r: tuple[int, ...]) -> None:
        d = self._dev
        mask, lt, rt, lx, ly, rx, ry = r
        d.release_button(0xFFFF & ~mask)
        d.press_button(mask)
        d.left_trigger(lt)
        d.right_trigger(rt)
        d.left_joystick(lx, ly)
        d.right_joystick(rx, ry)
        d.update()

    def _send_ds4(self, r: tuple[int, ...]) -> None:
        d = self._dev
        mask, special, hat, lt, rt, lx, ly, rx, ry = r
        d.release_button(0xFFF0 & ~mask)   # low nibble of wButtons is the hat
        d.press_button(mask)
        d.release_special_button(0x03 & ~special)
        d.press_special_button(special)
        d.directional_pad(hat)
        d.left_trigger(lt)
        d.right_trigger(rt)
        d.left_joystick(lx, ly)
        d.right_joystick(rx, ry)
        d.update()

    def __repr__(self) -> str:
        return f"<VirtualPad {self.target.name}{' closed' if self.closed else ''}>"


def create_virtual_pad(target: str = "x360", remap: Mapping[str, str] | None = None) -> VirtualPad:
    """Plug in a virtual controller. ``target``: ``"x360"`` (XInput) or ``"ds4"``.

    Raises :class:`VirtualPadUnavailable` with install instructions when the
    ViGEmBus driver (or the vgamepad package) is missing.
    """
    return VirtualPad(target, remap=remap)


__all__ = ["VirtualPad", "VirtualPadError", "VirtualPadUnavailable", "TargetInfo", "TARGETS",
           "NINTENDO_LABEL_REMAP", "VIGEMBUS_URL", "create_virtual_pad", "normalize_target",
           "remap_state", "validate_remap", "x360_report", "ds4_report", "X360_INVERT_Y",
           "trigger_to_u8",
           "stick_to_xinput", "stick_to_u8", "ds4_hat", "load_vgamepad",
           "virtual_pad_unavailable_reason"]
