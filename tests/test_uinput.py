"""Tests for the Linux uinput virtual pad, without the kernel (fake ioctl, a pipe for writes).

The real device is exercised by ``test_replay.py::test_live_roundtrip_through_sdl``
on Linux (uinput, read back through SDL3).
"""

from __future__ import annotations

import os
import struct

import pytest

from controllerlog.model import AXIS_INDEX, BUTTON_INDEX, PadState
from controllerlog.output import uinput_pad as up
from controllerlog.output import virtual_pad as vp


def mkstate(*buttons: str, **axes: int) -> PadState:
    st = PadState()
    for b in buttons:
        st.buttons[BUTTON_INDEX[b]] = 1
    for a, v in axes.items():
        st.axes[AXIS_INDEX[a]] = v
    return st


class FakeKernel:
    def __init__(self) -> None:
        self.calls: list[tuple[int, object]] = []
        self.r, self.w = os.pipe()
        os.set_blocking(self.r, False)

    def ioctl(self, fd, req, arg=0):
        self.calls.append((req, arg))
        return 0

    def opener(self, path):
        return os.dup(self.w)

    def events(self) -> list[tuple[int, int, int]]:
        try:
            data = os.read(self.r, 1 << 16)
        except BlockingIOError:
            return []
        size = up._EVENT.size
        return [up._EVENT.unpack(data[i:i + size])[2:] for i in range(0, len(data), size)]

    def close(self) -> None:
        os.close(self.r)
        os.close(self.w)


@pytest.fixture
def kernel():
    k = FakeKernel()
    yield k
    k.close()


def make(kernel, target):
    return up.UinputPad(target, ioctl=kernel.ioctl, opener=kernel.opener)


def test_ioctl_numbers_match_linux_headers():
    # Values from <linux/uinput.h> on x86_64 / aarch64.
    assert up.UI_DEV_CREATE == 0x5501
    assert up.UI_DEV_DESTROY == 0x5502
    assert up.UI_DEV_SETUP == 0x405C5503
    assert up.UI_ABS_SETUP == 0x401C5504
    assert up.UI_SET_EVBIT == 0x40045564
    assert up.UI_SET_KEYBIT == 0x40045565
    assert up.UI_SET_ABSBIT == 0x40045567
    assert up._EVENT.size == struct.calcsize("@llHHi")


@pytest.mark.parametrize("target,name,ids", [
    ("x360", "Microsoft X-Box 360 pad", (0x045E, 0x028E)),
    ("ds4", "Sony Interactive Entertainment Wireless Controller", (0x054C, 0x05C4)),
])
def test_device_setup(kernel, target, name, ids):
    pad = make(kernel, target)
    reqs = [r for r, _ in kernel.calls]
    assert reqs[-1] == up.UI_DEV_CREATE and reqs[-2] == up.UI_DEV_SETUP
    bus, vid, pid, _ver, raw, _ff = up._SETUP.unpack(kernel.calls[-2][1])
    assert (bus, vid, pid) == (up.BUS_USB, *ids)
    assert raw.rstrip(b"\0").decode() == name
    keys = {a for r, a in kernel.calls if r == up.UI_SET_KEYBIT}
    assert {up.BTN_SOUTH, up.BTN_EAST, up.BTN_NORTH, up.BTN_WEST, up.BTN_MODE} <= keys
    # xpad has no trigger keys (the triggers are axes only); hid-playstation has both.
    assert (up.BTN_TL2 in keys) == (target == "ds4")
    absinfo = {c: v for c, v, *_ in (up._ABS_SETUP.unpack(a) for r, a in kernel.calls
                                     if r == up.UI_ABS_SETUP)}
    assert set(absinfo) == {up.ABS_X, up.ABS_Y, up.ABS_Z, up.ABS_RX, up.ABS_RY, up.ABS_RZ,
                            up.ABS_HAT0X, up.ABS_HAT0Y}
    pad.close()
    assert kernel.calls[-1][0] == up.UI_DEV_DESTROY
    pad.close()   # twice is fine
    assert sum(1 for r, _ in kernel.calls if r == up.UI_DEV_DESTROY) == 1


def test_x360_events_from_canonical(kernel):
    pad = make(kernel, "x360")
    st = mkstate("south", "west", "guide", "dpad_up", "dpad_right",
                 left_x=-32768, left_y=32767, right_trigger=32767)
    pad.send(vp.x360_report(st, invert_y=False))
    ev = {(t, c): v for t, c, v in kernel.events()}
    assert ev[(up.EV_KEY, up.BTN_SOUTH)] == 1
    assert ev[(up.EV_KEY, up.BTN_NORTH)] == 1     # xpad: X = BTN_X = BTN_NORTH
    assert ev[(up.EV_KEY, up.BTN_MODE)] == 1
    assert ev[(up.EV_KEY, up.BTN_EAST)] == 0
    assert ev[(up.EV_ABS, up.ABS_X)] == -32768
    assert ev[(up.EV_ABS, up.ABS_Y)] == 32767     # down stays positive on evdev
    assert ev[(up.EV_ABS, up.ABS_RZ)] == 255
    assert (ev[(up.EV_ABS, up.ABS_HAT0X)], ev[(up.EV_ABS, up.ABS_HAT0Y)]) == (1, -1)
    assert ev[(up.EV_SYN, up.SYN_REPORT)] == 0


def test_only_changes_are_written(kernel):
    pad = make(kernel, "x360")
    pad.send(vp.x360_report(mkstate("south"), invert_y=False))
    kernel.events()
    pad.send(vp.x360_report(mkstate("south"), invert_y=False))
    assert kernel.events() == []
    pad.send(vp.x360_report(mkstate("east"), invert_y=False))
    assert kernel.events() == [(up.EV_KEY, up.BTN_SOUTH, 0), (up.EV_KEY, up.BTN_EAST, 1),
                               (up.EV_SYN, up.SYN_REPORT, 0)]


def test_x360_opposite_dpad_cancels(kernel):
    pad = make(kernel, "x360")
    pad.send(vp.x360_report(mkstate("dpad_up", "dpad_down", "dpad_left"), invert_y=False))
    ev = {(t, c): v for t, c, v in kernel.events()}
    assert (ev[(up.EV_ABS, up.ABS_HAT0X)], ev[(up.EV_ABS, up.ABS_HAT0Y)]) == (-1, 0)


def test_ds4_events_from_canonical(kernel):
    pad = make(kernel, "ds4")
    st = mkstate("west", "north", "guide", "dpad_down", "dpad_left",
                 left_trigger=32767, right_x=32767)
    pad.send(vp.ds4_report(st))
    ev = {(t, c): v for t, c, v in kernel.events()}
    assert ev[(up.EV_KEY, up.BTN_WEST)] == 1      # Square
    assert ev[(up.EV_KEY, up.BTN_NORTH)] == 1     # Triangle
    assert ev[(up.EV_KEY, up.BTN_TL2)] == 1       # L2 digital past the threshold
    assert ev[(up.EV_KEY, up.BTN_MODE)] == 1
    assert ev[(up.EV_ABS, up.ABS_Z)] == 255
    assert ev[(up.EV_ABS, up.ABS_RX)] == 255
    assert ev[(up.EV_ABS, up.ABS_X)] == 128       # centre
    assert (ev[(up.EV_ABS, up.ABS_HAT0X)], ev[(up.EV_ABS, up.ABS_HAT0Y)]) == (-1, 1)


@pytest.mark.parametrize("hat", range(9))
def test_ds4_hat_round_trip(hat):
    expected = {0: (0, -1), 2: (1, 0), 4: (0, 1), 6: (-1, 0), 8: (0, 0)}
    x, y = up.DS4_HAT_XY[hat]
    if hat in expected:
        assert (x, y) == expected[hat]
    up_, down = y < 0, y > 0
    assert vp.ds4_hat(up_, down, x < 0, x > 0) == hat


def test_send_after_close_fails(kernel):
    pad = make(kernel, "x360")
    pad.close()
    with pytest.raises(OSError):
        pad.send(vp.x360_report(PadState(), invert_y=False))


def test_setup_failure_closes_fd(kernel):
    opened = []

    def opener(path):
        fd = os.dup(kernel.w)
        opened.append(fd)
        return fd

    def ioctl(fd, req, arg=0):
        if req == up.UI_DEV_CREATE:
            raise OSError(22, "Invalid argument")

    with pytest.raises(OSError):
        up.UinputPad("x360", ioctl=ioctl, opener=opener)
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_virtual_pad_uses_uinput_on_linux(monkeypatch, kernel):
    monkeypatch.setattr(vp, "USE_UINPUT", True)
    monkeypatch.setattr(vp, "X360_INVERT_Y", False)
    monkeypatch.setattr(vp.VirtualPad, "RELEASE_DELAY_S", 0.0)
    real = up.UinputPad
    monkeypatch.setattr(up, "UinputPad",
                        lambda target: real(target, ioctl=kernel.ioctl, opener=kernel.opener))
    pad = vp.create_virtual_pad("x360")
    pad.apply(mkstate("south", left_y=-32768))
    ev = {(t, c): v for t, c, v in kernel.events()}
    assert ev[(up.EV_KEY, up.BTN_SOUTH)] == 1
    assert ev[(up.EV_ABS, up.ABS_Y)] == -32768    # up stays negative (no XInput flip)
    pad.close()
    ev = kernel.events()
    assert (up.EV_KEY, up.BTN_SOUTH, 0) in ev      # released before unplugging
    assert kernel.calls[-1][0] == up.UI_DEV_DESTROY


def test_virtual_pad_uinput_permission_error(monkeypatch):
    monkeypatch.setattr(vp, "USE_UINPUT", True)

    def denied(target):
        raise PermissionError(13, "Permission denied", "/dev/uinput")
    monkeypatch.setattr(up, "UinputPad", denied)
    with pytest.raises(vp.VirtualPadUnavailable, match="/dev/uinput") as ei:
        vp.create_virtual_pad("x360")
    assert "install-udev-rules" in str(ei.value)


def test_unavailable_reason_checks_uinput(monkeypatch):
    monkeypatch.setattr(vp, "USE_UINPUT", True)
    monkeypatch.setattr(vp.os, "access", lambda p, m: True)
    assert vp.virtual_pad_unavailable_reason() is None
    monkeypatch.setattr(vp.os, "access", lambda p, m: False)
    monkeypatch.setattr(vp.os.path, "exists", lambda p: True)
    assert "not writable" in vp.virtual_pad_unavailable_reason()
