"""Linux virtual controllers through ``/dev/uinput`` (no extra packages).

:class:`UinputPad` creates an evdev device that copies what the kernel's own
drivers expose for the real pads, so SDL (and every other evdev reader) treats
it exactly like a wired one:

* ``x360``: ``xpad``'s "Microsoft X-Box 360 pad" (045e:028e): A B X Y LB RB
  Back Start Guide LS RS as keys (no trigger keys), sticks -32768..32767 with
  Y down = positive, triggers ``ABS_Z``/``ABS_RZ`` 0..255, d-pad as a hat.
* ``ds4``: ``hid-playstation``'s "Sony Interactive Entertainment Wireless
  Controller" (054c:05c4): the same keys plus L2/R2 keys, sticks and triggers
  0..255 (128 = centre), d-pad as a hat. The touchpad click is dropped (the
  kernel puts it on a separate touchpad device).

The input is the native report tuple from :func:`~.virtual_pad.x360_report`
(with ``invert_y=False``) or :func:`~.virtual_pad.ds4_report`.
"""

from __future__ import annotations

import os
import struct
from typing import Callable

UINPUT_PATH = "/dev/uinput"

# <linux/uinput.h>: _IO('U', n) / _IOW('U', n, size)
_U = ord("U")


def _io(nr: int) -> int:
    return (_U << 8) | nr


def _iow(nr: int, size: int) -> int:
    return (1 << 30) | (size << 16) | (_U << 8) | nr


_SETUP = struct.Struct("<HHHH80sI")          # struct uinput_setup
_ABS_SETUP = struct.Struct("<HxxiiiiiI")     # struct uinput_abs_setup (code, input_absinfo)
_EVENT = struct.Struct("@llHHi")             # struct input_event (64-bit timeval)

UI_DEV_CREATE = _io(1)
UI_DEV_DESTROY = _io(2)
UI_DEV_SETUP = _iow(3, _SETUP.size)
UI_ABS_SETUP = _iow(4, _ABS_SETUP.size)
UI_SET_EVBIT = _iow(100, 4)
UI_SET_KEYBIT = _iow(101, 4)
UI_SET_ABSBIT = _iow(103, 4)

# <linux/input-event-codes.h>
EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
SYN_REPORT = 0
BUS_USB = 0x03
BTN_SOUTH, BTN_EAST, BTN_NORTH, BTN_WEST = 0x130, 0x131, 0x133, 0x134
BTN_TL, BTN_TR, BTN_TL2, BTN_TR2 = 0x136, 0x137, 0x138, 0x139
BTN_SELECT, BTN_START, BTN_MODE, BTN_THUMBL, BTN_THUMBR = 0x13A, 0x13B, 0x13C, 0x13D, 0x13E
ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05
ABS_HAT0X, ABS_HAT0Y = 0x10, 0x11

# XUSB wButtons bit -> key, as xpad reports them (BTN_X = BTN_NORTH, BTN_Y = BTN_WEST).
X360_KEYS: dict[int, int] = {
    0x1000: BTN_SOUTH, 0x2000: BTN_EAST, 0x4000: BTN_NORTH, 0x8000: BTN_WEST,
    0x0100: BTN_TL, 0x0200: BTN_TR, 0x0020: BTN_SELECT, 0x0010: BTN_START,
    0x0400: BTN_MODE, 0x0040: BTN_THUMBL, 0x0080: BTN_THUMBR,
}
# DS4 wButtons bit -> key, as hid-playstation reports them (Square = WEST, Triangle = NORTH).
DS4_KEYS: dict[int, int] = {
    1 << 4: BTN_WEST, 1 << 5: BTN_SOUTH, 1 << 6: BTN_EAST, 1 << 7: BTN_NORTH,
    1 << 8: BTN_TL, 1 << 9: BTN_TR, 1 << 10: BTN_TL2, 1 << 11: BTN_TR2,
    1 << 12: BTN_SELECT, 1 << 13: BTN_START, 1 << 14: BTN_THUMBL, 1 << 15: BTN_THUMBR,
}
DS4_SPECIAL_KEYS: dict[int, int] = {1 << 0: BTN_MODE}
# DS4 hat value (N = 0, clockwise, 8 = centre) -> (HAT0X, HAT0Y), up = -1.
DS4_HAT_XY: dict[int, tuple[int, int]] = {
    0: (0, -1), 1: (1, -1), 2: (1, 0), 3: (1, 1), 4: (0, 1),
    5: (-1, 1), 6: (-1, 0), 7: (-1, -1), 8: (0, 0),
}

_STICK_ABS = (ABS_X, ABS_Y, ABS_RX, ABS_RY)


def _absinfo(lo: int, hi: int, fuzz: int = 0, flat: int = 0) -> tuple[int, int, int, int]:
    return (lo, hi, fuzz, flat)


# name, vendor, product, version, keys, {abs code: (min, max, fuzz, flat)}
_LAYOUTS: dict[str, tuple[str, int, int, int, tuple[int, ...], dict[int, tuple[int, int, int, int]]]] = {
    "x360": ("Microsoft X-Box 360 pad", 0x045E, 0x028E, 0x0110,
             tuple(X360_KEYS.values()),
             {**{c: _absinfo(-32768, 32767, 16, 128) for c in _STICK_ABS},
              ABS_Z: _absinfo(0, 255), ABS_RZ: _absinfo(0, 255),
              ABS_HAT0X: _absinfo(-1, 1), ABS_HAT0Y: _absinfo(-1, 1)}),
    "ds4": ("Sony Interactive Entertainment Wireless Controller", 0x054C, 0x05C4, 0x8111,
            tuple(DS4_KEYS.values()) + tuple(DS4_SPECIAL_KEYS.values()),
            {**{c: _absinfo(0, 255) for c in _STICK_ABS},
             ABS_Z: _absinfo(0, 255), ABS_RZ: _absinfo(0, 255),
             ABS_HAT0X: _absinfo(-1, 1), ABS_HAT0Y: _absinfo(-1, 1)}),
}


def x360_events(report: tuple[int, ...]) -> list[tuple[int, int, int]]:
    """``(type, code, value)`` events for an XUSB report (Y already evdev-down-positive)."""
    mask, lt, rt, lx, ly, rx, ry = report
    ev = [(EV_KEY, key, int(bool(mask & bit))) for bit, key in X360_KEYS.items()]
    hx = int(bool(mask & 0x0008)) - int(bool(mask & 0x0004))
    hy = int(bool(mask & 0x0002)) - int(bool(mask & 0x0001))
    ev += [(EV_ABS, ABS_X, lx), (EV_ABS, ABS_Y, ly), (EV_ABS, ABS_RX, rx), (EV_ABS, ABS_RY, ry),
           (EV_ABS, ABS_Z, lt), (EV_ABS, ABS_RZ, rt), (EV_ABS, ABS_HAT0X, hx), (EV_ABS, ABS_HAT0Y, hy)]
    return ev


def ds4_events(report: tuple[int, ...]) -> list[tuple[int, int, int]]:
    """``(type, code, value)`` events for a :func:`~.virtual_pad.ds4_report` tuple."""
    mask, special, hat, lt, rt, lx, ly, rx, ry = report
    ev = [(EV_KEY, key, int(bool(mask & bit))) for bit, key in DS4_KEYS.items()]
    ev += [(EV_KEY, key, int(bool(special & bit))) for bit, key in DS4_SPECIAL_KEYS.items()]
    hx, hy = DS4_HAT_XY.get(hat, (0, 0))
    ev += [(EV_ABS, ABS_X, lx), (EV_ABS, ABS_Y, ly), (EV_ABS, ABS_RX, rx), (EV_ABS, ABS_RY, ry),
           (EV_ABS, ABS_Z, lt), (EV_ABS, ABS_RZ, rt), (EV_ABS, ABS_HAT0X, hx), (EV_ABS, ABS_HAT0Y, hy)]
    return ev


class UinputPad:
    """One virtual pad on ``/dev/uinput``. :meth:`send` takes a native report tuple.

    Only changed values are written (one ``SYN_REPORT`` per report). ``ioctl``
    and ``opener`` exist so tests can run without the kernel.
    """

    def __init__(self, target: str, path: str = UINPUT_PATH,
                 ioctl: Callable[..., object] | None = None,
                 opener: Callable[[str], int] | None = None) -> None:
        if ioctl is None:
            import fcntl  # noqa: PLC0415 - POSIX only
            ioctl = fcntl.ioctl
        self._ioctl = ioctl
        name, vendor, product, version, keys, absinfo = _LAYOUTS[target]
        self.name = name
        self._events = x360_events if target == "x360" else ds4_events
        self._fd = (opener or (lambda p: os.open(p, os.O_WRONLY | os.O_NONBLOCK)))(path)
        try:
            ioctl(self._fd, UI_SET_EVBIT, EV_KEY)
            for k in keys:
                ioctl(self._fd, UI_SET_KEYBIT, k)
            ioctl(self._fd, UI_SET_EVBIT, EV_ABS)
            for code, (lo, hi, fuzz, flat) in absinfo.items():
                ioctl(self._fd, UI_SET_ABSBIT, code)
                value = (lo + hi + 1) // 2 if target == "ds4" and code in _STICK_ABS else 0
                ioctl(self._fd, UI_ABS_SETUP, _ABS_SETUP.pack(code, value, lo, hi, fuzz, flat, 0))
            ioctl(self._fd, UI_DEV_SETUP,
                  _SETUP.pack(BUS_USB, vendor, product, version, name.encode(), 0))
            ioctl(self._fd, UI_DEV_CREATE)
        except BaseException:
            os.close(self._fd)
            raise
        self._last: dict[tuple[int, int], int] = {}

    def send(self, report: tuple[int, ...]) -> None:
        if self._fd < 0:
            raise OSError("uinput device is closed")
        out = bytearray()
        for etype, code, value in self._events(report):
            if self._last.get((etype, code)) != value:
                out += _EVENT.pack(0, 0, etype, code, value)
        if not out:
            return
        out += _EVENT.pack(0, 0, EV_SYN, SYN_REPORT, 0)
        os.write(self._fd, out)
        for etype, code, value in self._events(report):
            self._last[(etype, code)] = value

    def close(self) -> None:
        """Unplug the device. Safe to call twice."""
        fd, self._fd = self._fd, -1
        if fd < 0:
            return
        try:
            self._ioctl(fd, UI_DEV_DESTROY)
        finally:
            os.close(fd)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


__all__ = ["UinputPad", "UINPUT_PATH", "x360_events", "ds4_events"]
