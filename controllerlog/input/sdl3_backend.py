"""SDL3 gamepad backend: every controller SDL can see, including Bluetooth HID pads."""

from __future__ import annotations

import ctypes
import logging
import time
from typing import Callable

from ..hub import Hub, now_ns
from ..model import (AXIS, BUTTON, NUM_AXES, NUM_BUTTONS, DeviceInfo,
                     InputEvent, family_for_sdl_type)
from .base import Backend
from .sdl3 import (SDL3, SDL_EVENT_GAMEPAD_ADDED, SDL_EVENT_GAMEPAD_AXIS_MOTION,
                   SDL_EVENT_GAMEPAD_BUTTON_DOWN, SDL_EVENT_GAMEPAD_BUTTON_UP,
                   SDL_EVENT_GAMEPAD_REMOVED, SDLEvent)

log = logging.getLogger(__name__)

# Serials that many devices share, so they can't identify one controller: every ViGEm
# (vgamepad) virtual DualShock 4 reports this MAC. Such pads get a per-instance key, as
# serial-less virtual X360 pads do, so a restarted DS4 bridge sees its new pad connect.
SHARED_SERIALS = {"c0-13-37-66-3c-55"}


def device_info_from_sdl(desc: dict) -> DeviceInfo:
    return DeviceInfo(
        id=-1, name=desc["name"], backend="sdl3", sdl_type=desc["sdl_type"],
        family=family_for_sdl_type(desc["sdl_type"]),
        vendor_id=desc["vendor_id"], product_id=desc["product_id"],
        connection=desc["connection"], serial=desc["serial"], path=desc["path"],
        extra={k: desc[k] for k in ("instance_id", "real_sdl_type") if desc.get(k) is not None},
    )


class SDL3Backend(Backend):
    """Polls SDL3 for gamepad events and publishes them to the hub.

    SDL event timestamps (``SDL_GetTicksNS``) are converted to the hub's
    ``perf_counter_ns`` domain with an offset measured at start-up; on Windows
    both clocks are QueryPerformanceCounter-based, so the offset is stable.
    """

    name = "sdl3"

    def __init__(self, hub: Hub, hints: dict[str, str] | None = None,
                 poll_interval_s: float = 0.0001,  # Windows sleep(0.0001) ~= 0.5 ms; SDL stamps at drain time
                 accept: Callable[[DeviceInfo], bool] | None = None) -> None:
        super().__init__(hub)
        self.hints = hints
        self.poll_interval_s = poll_interval_s
        self.accept = accept
        self.sdl: SDL3 | None = None
        self._pads: dict[int, int] = {}        # instance id -> SDL_Gamepad*
        self._resync: list[tuple[float, int]] = []  # (monotonic due time, instance id)
        self._keys: dict[int, tuple] = {}            # instance id -> hub source key
        self._clock_offset = 0
        self.version = ""

    def _to_hub_time(self, sdl_ns: int) -> int:
        return sdl_ns + self._clock_offset if sdl_ns else now_ns()

    def _calibrate(self) -> None:
        # Bracket SDL_GetTicksNS between two perf_counter reads; keep the tightest.
        best = None
        for _ in range(16):
            a = now_ns()
            s = self.sdl.GetTicksNS()
            b = now_ns()
            if best is None or b - a < best[0]:
                best = (b - a, (a + b) // 2 - s)
        self._clock_offset = best[1]

    def _open(self, instance_id: int, t_ns: int) -> None:
        if instance_id in self._pads:
            return
        pad = self.sdl.OpenGamepad(instance_id)
        if not pad:
            log.warning("SDL_OpenGamepad(%s) failed: %s", instance_id, self.sdl.error())
            return
        info = device_info_from_sdl(self.sdl.describe(pad))
        if self.accept is not None and not self.accept(info):
            self.sdl.CloseGamepad(pad)
            return
        self._pads[instance_id] = pad
        # A controller that drops and reconnects (Bluetooth!) gets a new SDL instance id; key it
        # by its serial (usually the BT MAC) so it keeps its hub id and a run isn't split in two.
        key: tuple = ("sdl3", instance_id)
        if info.serial and info.serial.lower() not in SHARED_SERIALS:
            stable = ("sdl3-serial", info.vendor_id, info.product_id, info.serial)
            if stable not in self._keys.values():
                key = stable
        self._keys[instance_id] = key
        dev = self.hub.connect(key, info, t_ns)
        # Publish the initial state so sticks that rest off-centre are known. Some drivers
        # (RawInput X360) have no report yet at open time, and SDL emits nothing later for a
        # state that doesn't change, so re-read it shortly after too.
        self._publish_state(instance_id, dev, t_ns)
        now = time.monotonic()
        self._resync += [(now + 0.1, instance_id), (now + 0.5, instance_id)]
        log.info("connected #%d %s (%s, %s)", dev, info.name, info.sdl_type or "?",
                 info.connection)

    def _publish_state(self, instance_id: int, dev: int, t_ns: int) -> None:
        """Publish the polled state of every control (the hub drops unchanged values)."""
        pad = self._pads.get(instance_id)
        if not pad:
            return
        for b in range(NUM_BUTTONS):
            self.hub.publish(InputEvent(t_ns, dev, BUTTON, b,
                                        1 if self.sdl.GetGamepadButton(pad, b) else 0))
        for a in range(NUM_AXES):
            self.hub.publish(InputEvent(t_ns, dev, AXIS, a, int(self.sdl.GetGamepadAxis(pad, a))))

    def _run_resyncs(self) -> None:
        now = time.monotonic()
        due = [iid for t, iid in self._resync if t <= now]
        if not due:
            return
        self._resync = [(t, iid) for t, iid in self._resync if t > now]
        for iid in due:
            dev = self._device(iid)
            if dev is not None:
                self._publish_state(iid, dev, now_ns())

    def _device(self, instance_id: int) -> int | None:
        key = self._keys.get(instance_id)
        return self.hub.device_id(key) if key is not None else None

    def _close(self, instance_id: int, t_ns: int) -> None:
        pad = self._pads.pop(instance_id, None)
        dev = self._device(instance_id)
        self._keys.pop(instance_id, None)
        if dev is not None:
            self.hub.disconnect(dev, t_ns)
        if pad:
            self.sdl.CloseGamepad(pad)

    def run(self) -> None:
        # SDL must be initialised and polled on the same thread.
        self.sdl = SDL3()
        self.sdl.init_gamepads(self.hints)
        self.version = self.sdl.version
        self._calibrate()
        for iid in self.sdl.gamepad_ids():
            self._open(iid, now_ns())
        self.ready.set()
        ev = SDLEvent()
        evp = ctypes.byref(ev)
        last_cal = time.monotonic()
        try:
            while not self._stop.is_set():
                got = False
                while self.sdl.PollEvent(evp):
                    got = True
                    self._handle(ev)
                if self._resync:
                    self._run_resyncs()
                if time.monotonic() - last_cal > 30:
                    self._calibrate()
                    last_cal = time.monotonic()
                if not got:
                    time.sleep(self.poll_interval_s)
        finally:
            for iid in list(self._pads):
                self._close(iid, now_ns())
            self.sdl.Quit()

    def _handle(self, ev: SDLEvent) -> None:
        et = ev.type
        if et == SDL_EVENT_GAMEPAD_BUTTON_DOWN or et == SDL_EVENT_GAMEPAD_BUTTON_UP:
            e = ev.gbutton
            dev = self._device(e.which)
            if dev is not None and e.button < NUM_BUTTONS:
                self.hub.publish(InputEvent(self._to_hub_time(e.timestamp), dev, BUTTON,
                                            int(e.button), 1 if e.down else 0))
        elif et == SDL_EVENT_GAMEPAD_AXIS_MOTION:
            e = ev.gaxis
            dev = self._device(e.which)
            if dev is not None and e.axis < NUM_AXES:
                self.hub.publish(InputEvent(self._to_hub_time(e.timestamp), dev, AXIS,
                                            int(e.axis), int(e.value)))
        elif et == SDL_EVENT_GAMEPAD_ADDED:
            self._open(ev.gdevice.which, self._to_hub_time(ev.gdevice.timestamp))
        elif et == SDL_EVENT_GAMEPAD_REMOVED:
            self._close(ev.gdevice.which, self._to_hub_time(ev.gdevice.timestamp))


def list_gamepads(hints: dict[str, str] | None = None, settle_s: float = 1.0) -> tuple[str, list[dict]]:
    """One-shot enumeration for ``controllerlog devices``. Returns (sdl_version, descriptions)."""
    sdl = SDL3()
    sdl.init_gamepads(hints)
    try:
        # Give HIDAPI drivers a moment to finish their handshakes (Switch/PS need it).
        deadline = time.monotonic() + settle_s
        ev = SDLEvent()
        while time.monotonic() < deadline:
            while sdl.PollEvent(ctypes.byref(ev)):
                pass
            time.sleep(0.01)
        out = []
        for iid in sdl.gamepad_ids():
            pad = sdl.OpenGamepad(iid)
            if not pad:
                out.append({"instance_id": iid, "name": "(could not open)",
                            "error": sdl.error()})
                continue
            d = sdl.describe(pad)
            d["family"] = family_for_sdl_type(d["sdl_type"])
            out.append(d)
            sdl.CloseGamepad(pad)
        return sdl.version, out
    finally:
        sdl.Quit()
