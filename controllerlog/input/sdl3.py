"""Minimal ctypes binding to the SDL3 gamepad API (SDL 3.2+; developed against 3.4.16).

Only what the input backend needs is bound. Struct layouts come from
SDL3/SDL_events.h: every event starts with ``Uint32 type; Uint32 reserved;
Uint64 timestamp`` and ``SDL_Event`` is a 128-byte union.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
from ctypes import (POINTER, Structure, Union, byref, c_bool, c_char_p, c_int,
                    c_int16, c_uint8, c_uint16, c_uint32, c_uint64, c_void_p)
from pathlib import Path

SDL_INIT_JOYSTICK = 0x00000200
SDL_INIT_GAMEPAD = 0x00002000
SDL_INIT_EVENTS = 0x00004000

SDL_EVENT_QUIT = 0x100
SDL_EVENT_JOYSTICK_ADDED = 0x605
SDL_EVENT_JOYSTICK_REMOVED = 0x606
SDL_EVENT_GAMEPAD_AXIS_MOTION = 0x650
SDL_EVENT_GAMEPAD_BUTTON_DOWN = 0x651
SDL_EVENT_GAMEPAD_BUTTON_UP = 0x652
SDL_EVENT_GAMEPAD_ADDED = 0x653
SDL_EVENT_GAMEPAD_REMOVED = 0x654
SDL_EVENT_GAMEPAD_REMAPPED = 0x655

SDL_JOYSTICK_CONNECTION_NAMES = {-1: "unknown", 0: "unknown", 1: "wired", 2: "wireless"}

# Hints that turn on SDL's own HID drivers for controllers Windows doesn't
# handle natively (or that SDL leaves off by default on Windows). This is the
# "software settings only" part of making odd controllers work.
DEFAULT_HINTS: dict[str, str] = {
    "SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS": "1",   # keep logging while the game has focus
    "SDL_JOYSTICK_HIDAPI": "1",
    "SDL_JOYSTICK_HIDAPI_PS3": "1",                # DualShock 3 / Sixaxis (off by default on Windows)
    "SDL_JOYSTICK_HIDAPI_PS3_SIXAXIS_DRIVER": "1", # DS3 via Sony sixaxis.sys / DsHidMini "SXS" mode
    "SDL_JOYSTICK_HIDAPI_WII": "1",                # Wii Remote / Wii U Pro (off by default on Windows)
    "SDL_JOYSTICK_HIDAPI_STEAM": "1",              # Steam Controller over BT (off by default)
    "SDL_JOYSTICK_HIDAPI_SWITCH": "1",
    "SDL_JOYSTICK_HIDAPI_SWITCH2": "1",
    "SDL_JOYSTICK_HIDAPI_JOY_CONS": "1",
    "SDL_JOYSTICK_HIDAPI_COMBINE_JOY_CONS": "1",   # L+R Joy-Con become one pad
    "SDL_JOYSTICK_HIDAPI_PS4_REPORT_INTERVAL": "1",  # DS4 over BT: 1 ms reports (SDL default 4 ms)
    # Enhanced reports give full-rate DS4/DS5/Switch reports, but on PlayStation pads they
    # break DirectInput for non-SDL games until the pad is power-cycled. See enhanced_reports().
    "SDL_JOYSTICK_ENHANCED_REPORTS": "1",
    "SDL_JOYSTICK_HIDAPI_NINTENDO_CLASSIC": "1",   # NSO NES/SNES/N64/Genesis controllers
    "SDL_JOYSTICK_HIDAPI_GAMECUBE": "1",
    "SDL_JOYSTICK_HIDAPI_PS4": "1",
    "SDL_JOYSTICK_HIDAPI_PS5": "1",
    "SDL_JOYSTICK_HIDAPI_STADIA": "1",
    "SDL_JOYSTICK_HIDAPI_LUNA": "1",
    "SDL_JOYSTICK_HIDAPI_SHIELD": "1",
    "SDL_JOYSTICK_HIDAPI_8BITDO": "1",
    "SDL_JOYSTICK_HIDAPI_XBOX": "1",
    "SDL_JOYSTICK_RAWINPUT": "1",
}


def hints_with(enhanced_reports: bool = True, extra: dict[str, str] | None = None) -> dict[str, str]:
    """DEFAULT_HINTS with the enhanced-report mode chosen.

    Use ``enhanced_reports=False`` when a non-SDL game reads the same
    PlayStation/Switch controller through DirectInput at the same time.
    """
    hints = dict(DEFAULT_HINTS)
    hints["SDL_JOYSTICK_ENHANCED_REPORTS"] = "1" if enhanced_reports else "auto"
    if extra:
        hints.update(extra)
    return hints


class SDLCommonEvent(Structure):
    _fields_ = [("type", c_uint32), ("reserved", c_uint32), ("timestamp", c_uint64)]


class SDLGamepadAxisEvent(Structure):
    _fields_ = [("type", c_uint32), ("reserved", c_uint32), ("timestamp", c_uint64),
                ("which", c_uint32), ("axis", c_uint8), ("padding1", c_uint8),
                ("padding2", c_uint8), ("padding3", c_uint8), ("value", c_int16),
                ("padding4", c_uint16)]


class SDLGamepadButtonEvent(Structure):
    _fields_ = [("type", c_uint32), ("reserved", c_uint32), ("timestamp", c_uint64),
                ("which", c_uint32), ("button", c_uint8), ("down", c_bool),
                ("padding1", c_uint8), ("padding2", c_uint8)]


class SDLGamepadDeviceEvent(Structure):
    _fields_ = [("type", c_uint32), ("reserved", c_uint32), ("timestamp", c_uint64),
                ("which", c_uint32)]


class SDLEvent(Union):
    _fields_ = [("type", c_uint32), ("common", SDLCommonEvent),
                ("gaxis", SDLGamepadAxisEvent), ("gbutton", SDLGamepadButtonEvent),
                ("gdevice", SDLGamepadDeviceEvent), ("padding", c_uint8 * 128)]


assert ctypes.sizeof(SDLEvent) == 128
assert SDLGamepadAxisEvent.axis.offset == 20 and SDLGamepadAxisEvent.value.offset == 24
assert SDLGamepadButtonEvent.down.offset == 21


class SDLError(RuntimeError):
    pass


def _candidate_paths() -> list[str]:
    names = {"win32": ["SDL3.dll"], "darwin": ["libSDL3.dylib", "libSDL3.0.dylib"]}.get(
        sys.platform, ["libSDL3.so.0", "libSDL3.so"])
    out: list[str] = []
    env = os.environ.get("CONTROLLERLOG_SDL3")
    if env:
        out.append(env)
    root = Path(__file__).resolve().parents[2]
    for n in names:
        out.append(str(root / "vendor" / n))
        out.append(str(Path(sys.prefix) / "vendor" / n))
    found = ctypes.util.find_library("SDL3")
    if found:
        out.append(found)
    out.extend(names)
    return out


def load_library() -> ctypes.CDLL:
    errors = []
    for cand in _candidate_paths():
        if os.sep in cand or "/" in cand:
            if not Path(cand).exists():
                continue
        try:
            return ctypes.CDLL(cand)
        except OSError as e:
            errors.append(f"{cand}: {e}")
    raise SDLError("Could not load SDL3. Run `python scripts/fetch_sdl3.py` (Windows) or install "
                   "SDL3 (Linux/macOS), or set CONTROLLERLOG_SDL3 to the library path.\n"
                   + "\n".join(errors))


class SDL3:
    """Thin wrapper with typed function prototypes."""

    def __init__(self, lib: ctypes.CDLL | None = None) -> None:
        self.lib = lib or load_library()
        L = self.lib

        def proto(name: str, restype, *argtypes):
            fn = getattr(L, name)
            fn.restype = restype
            fn.argtypes = list(argtypes)
            return fn

        self.GetVersion = proto("SDL_GetVersion", c_int)
        self.SetHint = proto("SDL_SetHint", c_bool, c_char_p, c_char_p)
        self.Init = proto("SDL_Init", c_bool, c_uint32)
        self.Quit = proto("SDL_Quit", None)
        self.GetError = proto("SDL_GetError", c_char_p)
        self.free = proto("SDL_free", None, c_void_p)
        self.GetTicksNS = proto("SDL_GetTicksNS", c_uint64)
        self.PollEvent = proto("SDL_PollEvent", c_bool, POINTER(SDLEvent))
        self.WaitEventTimeout = proto("SDL_WaitEventTimeout", c_bool, POINTER(SDLEvent), c_int)
        self.GetGamepads = proto("SDL_GetGamepads", POINTER(c_uint32), POINTER(c_int))
        self.IsGamepad = proto("SDL_IsGamepad", c_bool, c_uint32)
        self.OpenGamepad = proto("SDL_OpenGamepad", c_void_p, c_uint32)
        self.CloseGamepad = proto("SDL_CloseGamepad", None, c_void_p)
        self.GetGamepadID = proto("SDL_GetGamepadID", c_uint32, c_void_p)
        self.GetGamepadName = proto("SDL_GetGamepadName", c_char_p, c_void_p)
        self.GetGamepadPath = proto("SDL_GetGamepadPath", c_char_p, c_void_p)
        self.GetGamepadSerial = proto("SDL_GetGamepadSerial", c_char_p, c_void_p)
        self.GetGamepadType = proto("SDL_GetGamepadType", c_int, c_void_p)
        self.GetRealGamepadType = proto("SDL_GetRealGamepadType", c_int, c_void_p)
        self.GetGamepadStringForType = proto("SDL_GetGamepadStringForType", c_char_p, c_int)
        self.GetGamepadVendor = proto("SDL_GetGamepadVendor", c_uint16, c_void_p)
        self.GetGamepadProduct = proto("SDL_GetGamepadProduct", c_uint16, c_void_p)
        self.GetGamepadConnectionState = proto("SDL_GetGamepadConnectionState", c_int, c_void_p)
        self.GetGamepadButton = proto("SDL_GetGamepadButton", c_bool, c_void_p, c_int)
        self.GetGamepadAxis = proto("SDL_GetGamepadAxis", c_int16, c_void_p, c_int)
        self.GetGamepadMapping = proto("SDL_GetGamepadMapping", c_void_p, c_void_p)
        self.AddGamepadMapping = proto("SDL_AddGamepadMapping", c_int, c_char_p)

    @property
    def version(self) -> str:
        v = self.GetVersion()
        return f"{v // 1000000}.{(v // 1000) % 1000}.{v % 1000}"

    def error(self) -> str:
        e = self.GetError()
        return e.decode("utf-8", "replace") if e else ""

    def set_hints(self, hints: dict[str, str]) -> None:
        for k, v in hints.items():
            self.SetHint(k.encode(), str(v).encode())

    def init_gamepads(self, hints: dict[str, str] | None = None) -> None:
        self.set_hints(DEFAULT_HINTS if hints is None else hints)
        if not self.Init(SDL_INIT_GAMEPAD | SDL_INIT_EVENTS):
            raise SDLError(f"SDL_Init failed: {self.error()}")

    def gamepad_ids(self) -> list[int]:
        count = c_int(0)
        ptr = self.GetGamepads(byref(count))
        if not ptr:
            return []
        try:
            return [ptr[i] for i in range(count.value)]
        finally:
            self.free(ctypes.cast(ptr, c_void_p))

    @staticmethod
    def _s(b: bytes | None) -> str:
        return b.decode("utf-8", "replace") if b else ""

    def describe(self, pad: int) -> dict:
        t = self.GetGamepadType(pad)
        real = self.GetRealGamepadType(pad)
        mapping_ptr = self.GetGamepadMapping(pad)
        mapping = ""
        if mapping_ptr:
            mapping = ctypes.string_at(mapping_ptr).decode("utf-8", "replace")
            self.free(mapping_ptr)
        return {
            "instance_id": self.GetGamepadID(pad),
            "name": self._s(self.GetGamepadName(pad)) or "Unknown controller",
            "sdl_type": self._s(self.GetGamepadStringForType(t)),
            "real_sdl_type": self._s(self.GetGamepadStringForType(real)),
            "vendor_id": self.GetGamepadVendor(pad),
            "product_id": self.GetGamepadProduct(pad),
            "connection": SDL_JOYSTICK_CONNECTION_NAMES.get(self.GetGamepadConnectionState(pad),
                                                            "unknown"),
            "serial": self._s(self.GetGamepadSerial(pad)),
            "path": self._s(self.GetGamepadPath(pad)),
            "mapping": mapping,
        }
