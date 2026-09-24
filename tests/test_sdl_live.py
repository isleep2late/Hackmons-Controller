"""Live SDL3 capture tests using ViGEm virtual pads (no physical controller needed)."""
from __future__ import annotations

import time

import pytest

pytestmark = [pytest.mark.sdl, pytest.mark.vigem]

vg = pytest.importorskip("vgamepad")

from controllerlog.hub import Hub, Recorder  # noqa: E402
from controllerlog.input.sdl3 import SDLError  # noqa: E402
from controllerlog.input.sdl3_backend import SDL3Backend  # noqa: E402
from controllerlog.logfile import read_log  # noqa: E402
from controllerlog.model import AXIS, BUTTON, BUTTON_INDEX, CONNECT, DISCONNECT  # noqa: E402


def wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def backend():
    hub = Hub()
    events = []
    hub.add_sink(events.append)
    be = SDL3Backend(hub)
    be.start()
    if not be.ready.wait(10) or be.error:
        if isinstance(be.error, SDLError):
            pytest.skip(f"SDL3 unavailable: {be.error}")
        raise AssertionError(be.error)
    try:
        yield hub, be, events
    finally:
        be.stop()


def _new_pad():
    try:
        return vg.VX360Gamepad()
    except Exception as e:  # driver missing
        pytest.skip(f"ViGEmBus unavailable: {e}")


def test_two_pads_hotplug_press_and_disconnect(backend, tmp_path):
    hub, be, events = backend
    before = set(hub.devices)
    p1 = _new_pad()
    assert wait_for(lambda: len(set(hub.devices) - before) >= 1)
    p2 = _new_pad()
    assert wait_for(lambda: len(set(hub.devices) - before) >= 2)
    new = sorted(set(hub.devices) - before)
    d1, d2 = new[0], new[1]
    assert hub.devices[d1].family == "xbox" and hub.devices[d1].sdl_type == "xbox360"
    rec = Recorder(hub, tmp_path / "live.ctlog", meta={"test": "sdl"})

    p1.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_A); p1.update()
    p2.left_trigger(value=255); p2.update()
    assert wait_for(lambda: hub.states[d1].buttons[BUTTON_INDEX["south"]] == 1)
    assert wait_for(lambda: hub.states[d2].axes[4] == 32767)
    assert hub.states[d2].buttons[BUTTON_INDEX["south"]] == 0  # inputs stay per-device

    # Unplug pad 1 while A is held: the hub must release it before the disconnect.
    del p1
    assert wait_for(lambda: d1 not in hub.devices)
    d1_events = [(e.kind, e.code, e.value) for e in events if e.device == d1]
    i = d1_events.index((DISCONNECT, None, None))
    assert (BUTTON, BUTTON_INDEX["south"], 0) in d1_events[:i]
    rec.close()
    del p2
    log = read_log(tmp_path / "live.ctlog")
    kinds = [e.kind for e in log.events]
    assert kinds.count(CONNECT) >= 2 and AXIS in kinds and BUTTON in kinds


def test_reconnect_with_serial_keeps_device_id(backend, monkeypatch):
    """A pad that drops and comes back (same serial) keeps its hub id, so a run isn't split."""
    from controllerlog.input import sdl3_backend
    # Every ViGEm DS4 reports the same serial, so the backend normally doesn't key it by
    # serial (see test_vigem_ds4_serial_is_not_an_identity). Here it stands in for a
    # physical pad with a unique serial.
    monkeypatch.setattr(sdl3_backend, "SHARED_SERIALS", set())
    hub, be, events = backend
    before = set(hub.devices)
    try:
        pad = vg.VDS4Gamepad()  # ViGEm DS4 reports a serial through SDL's HIDAPI driver
    except Exception as e:
        pytest.skip(f"ViGEmBus unavailable: {e}")
    assert wait_for(lambda: len(set(hub.devices) - before) >= 1)
    dev = sorted(set(hub.devices) - before)[0]
    if not hub.devices[dev].serial:
        del pad
        pytest.skip("this driver exposes no serial")
    del pad
    assert wait_for(lambda: dev not in hub.devices)
    pad = vg.VDS4Gamepad()
    assert wait_for(lambda: dev in hub.devices)
    del pad


def test_vigem_ds4_serial_is_not_an_identity(backend):
    """Each new virtual DS4 is a new device, so a restarted DS4 bridge can claim its new pad."""
    from controllerlog.input.sdl3_backend import SHARED_SERIALS
    hub, be, events = backend
    before = set(hub.devices)
    try:
        pad = vg.VDS4Gamepad()
    except Exception as e:
        pytest.skip(f"ViGEmBus unavailable: {e}")
    assert wait_for(lambda: len(set(hub.devices) - before) >= 1)
    dev = sorted(set(hub.devices) - before)[0]
    serial = (hub.devices[dev].serial or "").lower()
    if serial not in SHARED_SERIALS:
        del pad
        pytest.skip(f"this driver reports serial {serial!r}")
    del pad
    assert wait_for(lambda: dev not in hub.devices)
    pad = vg.VDS4Gamepad()
    assert wait_for(lambda: len(set(hub.devices) - before - {dev}) >= 1)
    assert dev not in hub.devices
    del pad


def test_latency_under_5ms(backend):
    hub, be, events = backend
    before = set(hub.devices)
    pad = _new_pad()
    assert wait_for(lambda: len(set(hub.devices) - before) >= 1)
    dev = sorted(set(hub.devices) - before)[0]
    lat = []
    for i in range(20):
        want = i % 2 == 0
        t0 = time.perf_counter_ns()
        if want:
            pad.press_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_B)
        else:
            pad.release_button(vg.XUSB_BUTTON.XUSB_GAMEPAD_B)
        pad.update()
        assert wait_for(lambda: hub.states[dev].buttons[BUTTON_INDEX["east"]] == int(want), 1.0)
        ev = [e for e in events if e.device == dev and e.kind == BUTTON and e.code == BUTTON_INDEX["east"]][-1]
        lat.append((ev.t_ns - t0) / 1e6)
        time.sleep(0.02)
    del pad
    lat.sort()
    assert lat[len(lat) // 2] < 5.0, lat
