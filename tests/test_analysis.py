from __future__ import annotations

import random

import pytest

from controllerlog.analysis import align, shifted
from controllerlog.formats import gm2
from controllerlog.formats.gm2 import GB_FRAME_SAMPLES, Gm2Button, Gm2Header, Gm2Movie
from controllerlog.logfile import Recording
from controllerlog.model import BUTTON, BUTTON_INDEX, CONNECT, DeviceInfo, InputEvent
from controllerlog.timeline import FPS, NS_PER_S

FRAME_NS = NS_PER_S / FPS["gb"]
GB_BIT = {"east": Gm2Button.A, "south": Gm2Button.B, "start": Gm2Button.START,
          "dpad_right": Gm2Button.RIGHT, "dpad_up": Gm2Button.UP}


def simulate(seed=1, offset_s=12.345, scale=1.0002, swap_ab=False, n=300):
    """Physical presses -> host log (host clock) and the frames an emulator would latch."""
    rng = random.Random(seed)
    presses = []  # (button, down_s, dur_s)
    t = 1.0
    for _ in range(n):
        t += rng.uniform(0.4, 0.9)  # > max hold + 1 frame, so presses never merge
        b = rng.choice(list(GB_BIT))
        dur = rng.uniform(0.004, 0.012) if rng.random() < 0.1 else rng.uniform(0.05, 0.3)
        presses.append((b, t, dur))
    # host recording (what ControllerLog captured from the pad)
    host = Recording(header={"meta": {}})
    host.devices[0] = DeviceInfo(0, "pad", family="xbox")
    host.events.append(InputEvent(0, 0, CONNECT, None, host.devices[0].to_json()))
    host_name = {"east": "south", "south": "east"} if swap_ab else {}
    for b, d, dur in presses:
        name = host_name.get(b, b)
        host.events.append(InputEvent(round(d * NS_PER_S), 0, BUTTON, BUTTON_INDEX[name], 1))
        host.events.append(InputEvent(round((d + dur) * NS_PER_S), 0, BUTTON, BUTTON_INDEX[name], 0))
    host.sort()
    # emulator: latches input at the start of each frame, on its own clock
    end_emu = (t + 2) * scale + offset_s
    n_frames = int(end_emu * NS_PER_S / FRAME_NS)
    masks = [0] * n_frames
    registered = 0
    for b, d, dur in presses:
        a = (d * scale + offset_s) * NS_PER_S / FRAME_NS
        z = ((d + dur) * scale + offset_s) * NS_PER_S / FRAME_NS
        first, last = int(a) + 1, int(z)  # frames whose start falls inside the press
        if first <= last:
            registered += 1
        for f in range(first, min(last, n_frames - 1) + 1):
            masks[f] |= GB_BIT[b]
    mv = Gm2Movie(Gm2Header(rom_name="sim"), records=[(GB_FRAME_SAMPLES, m) for m in masks])
    return host, gm2.gm2_to_recording(mv), registered, presses


def test_recovers_offset_drift_and_dropped_taps():
    host, emu, registered, presses = simulate()
    al = align(host, emu, fps=FPS["gb"])
    s = al.summary(FPS["gb"])
    # constant offset recovered to within a frame (the half-frame latch delay is absorbed)
    assert al.offset_ns / NS_PER_S == pytest.approx(12.345, abs=FRAME_NS / NS_PER_S)
    assert al.scale == pytest.approx(1.0002, abs=2e-5)
    assert len(al.matched) == registered
    # every unmatched host press is a sub-frame tap the emulator couldn't see
    assert all(hold < FRAME_NS for _, _, hold in al.host_only)
    assert len(al.host_only) == len(presses) - registered
    assert s["residual_frames"]["spread_p5_p95"] <= 1.1
    assert not al.emu_only


def test_detects_ab_swapped_bindings():
    host, emu, registered, _ = simulate(seed=7, swap_ab=True)
    al = align(host, emu, fps=FPS["gb"])
    assert al.mapping.get("south") == "east"
    assert len(al.matched) == registered


def test_shifted_puts_host_on_emulator_clock():
    host, emu, _, _ = simulate(seed=3, scale=1.0)
    al = align(host, emu, fps=FPS["gb"])
    moved = shifted(host, al)
    first_host = next(e for e in moved.events if e.kind == BUTTON and e.value)
    first_emu = next(e for e in emu.events if e.kind == BUTTON and e.value)
    assert abs(first_host.t_ns - first_emu.t_ns) < 2 * FRAME_NS
    assert moved.meta["aligned_to_emulator"]


def test_no_common_input_raises():
    host = Recording(header={"meta": {}})
    emu = Recording(header={"meta": {}})
    with pytest.raises(ValueError):
        align(host, emu)


def simulate_long(seed, offset_s, scale, duration_s):
    """Like simulate(), for a long run with a press every 0.4-0.9 s."""
    rng = random.Random(seed)
    presses, t = [], 1.0
    while t < duration_s:
        t += rng.uniform(0.4, 0.9)
        presses.append((rng.choice(list(GB_BIT)), t, rng.uniform(0.05, 0.3)))
    host = Recording(header={"meta": {}})
    host.devices[0] = DeviceInfo(0, "pad", family="xbox")
    host.events.append(InputEvent(0, 0, CONNECT, None, host.devices[0].to_json()))
    for b, d, dur in presses:
        host.events.append(InputEvent(round(d * NS_PER_S), 0, BUTTON, BUTTON_INDEX[b], 1))
        host.events.append(InputEvent(round((d + dur) * NS_PER_S), 0, BUTTON, BUTTON_INDEX[b], 0))
    host.sort()
    n_frames = int(((t + 2) * scale + offset_s) * NS_PER_S / FRAME_NS)
    masks = [0] * n_frames
    for b, d, dur in presses:
        first = int((d * scale + offset_s) * NS_PER_S / FRAME_NS) + 1
        last = int(((d + dur) * scale + offset_s) * NS_PER_S / FRAME_NS)
        for f in range(first, min(last, n_frames - 1) + 1):
            masks[f] |= GB_BIT[b]
    mv = Gm2Movie(Gm2Header(rom_name="sim"), records=[(GB_FRAME_SAMPLES, m) for m in masks])
    return host, gm2.gm2_to_recording(mv)


@pytest.mark.parametrize("scale,duration", [(1.0003, 3600), (1.0001, 7200)])
def test_align_recovers_drift_over_long_runs(scale, duration):
    host, emu = simulate_long(seed=5, offset_s=2.0, scale=scale, duration_s=duration)
    al = align(host, emu, fps=FPS["gb"])
    assert al.match_rate > 0.9
    assert al.scale == pytest.approx(scale, abs=2e-5)
    assert al.warnings == []


def test_align_warns_when_it_cannot_be_trusted():
    host, _, _, _ = simulate(seed=3)
    _, other, _, _ = simulate(seed=4)    # a different run: presses don't line up
    al = align(host, other, fps=FPS["gb"])
    assert al.match_rate < 0.5 and al.warnings
    assert al.summary(FPS["gb"])["warnings"] == al.warnings
