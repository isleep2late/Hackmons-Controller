"""Switch 2 controllers over USB-C, without a libusb-enabled SDL build.

Supports the NSO GameCube controller for Switch 2 (057E:2073) and the Switch 2
Pro Controller (057E:2069). SDL 3.4 can only drive these through libusb, which
the official SDL3.dll doesn't include, so ControllerLog talks to them directly:

* Interface 1 is a vendor interface with bulk OUT/IN endpoints (Windows binds
  it to WinUSB automatically, so libusb opens it without Zadig). Commands go
  there: flash reads for calibration, then an init sequence that starts the
  input stream.
* Interface 0 is a normal HID interface. Once started, it streams 64-byte
  input reports (report id 0x05) at 250 Hz.
* The command interface is released right after init, so another program
  (GSE, a libusb SDL) can still claim and initialise the controller.

Report layout and init sequence follow SDL 3.4.16 ``SDL_hidapi_switch2.c``;
verified against a real NSO GameCube controller over USB. Needs
``pip install pyusb libusb-package hidapi`` (the ``usb`` extra).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..hub import Hub, now_ns
from ..model import (AXIS, BUTTON, BUTTON_INDEX, FAMILY_GAMECUBE, FAMILY_SWITCH,
                     DeviceInfo, InputEvent, PadState)
from .base import Backend

log = logging.getLogger(__name__)

NINTENDO_VID = 0x057E
MODELS = {0x2073: "gamecube", 0x2069: "pro"}
NAMES = {"gamecube": "Nintendo GameCube Controller (Switch 2)",
         "pro": "Nintendo Switch 2 Pro Controller"}
REPORT_ID = 0x05
REPORT_LEN = 64

# SDL_hidapi_switch2.c HIDAPI_DriverSwitch2_InitUSB, in order.
INIT_SEQUENCE: tuple[bytes, ...] = tuple(bytes(c) for c in (
    [0x07, 0x91, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00],                          # unknown
    [0x0C, 0x91, 0x00, 0x02, 0x00, 0x04, 0x00, 0x00, 0x27, 0x00, 0x00, 0x00],  # feature mask
    [0x11, 0x91, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00],                          # unknown
    [0x0A, 0x91, 0x00, 0x08, 0x00, 0x14, 0x00, 0x00, 0x01, 0xFF, 0xFF, 0xFF,  # rumble data
     0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x35, 0x00, 0x46, 0x00, 0x00, 0x00, 0x00,
     0x00, 0x00, 0x00, 0x00],
    [0x0C, 0x91, 0x00, 0x04, 0x00, 0x04, 0x00, 0x00, 0x27, 0x00, 0x00, 0x00],  # enable features
    [0x01, 0x91, 0x00, 0x0C, 0x00, 0x00, 0x00, 0x00],                          # unknown
    [0x01, 0x91, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00],                          # enable rumble
    [0x08, 0x91, 0x00, 0x02, 0x00, 0x04, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00],  # grip buttons
    [0x03, 0x91, 0x00, 0x0A, 0x00, 0x04, 0x00, 0x00, 0x05, 0x00, 0x00, 0x00],  # report format
    [0x03, 0x91, 0x00, 0x0D, 0x00, 0x08, 0x00, 0x00, 0x01, 0x00, 0xFF, 0xFF,  # start output
     0xFF, 0xFF, 0xFF, 0xFF],
))
PLAYER_LED = [0x01, 0x03, 0x07, 0x0F, 0x09, 0x05, 0x0D, 0x06]


def flash_read_command(address: int) -> bytes:
    return bytes([0x02, 0x91, 0x00, 0x01, 0x00, 0x08, 0x00, 0x00, 0x40, 0x00, 0x00, 0x00,
                  address & 0xFF, address >> 8 & 0xFF, address >> 16 & 0xFF, address >> 24 & 0xFF])


def led_command(player: int) -> bytes:
    pattern = PLAYER_LED[(player - 1) % 8] if player > 0 else 0
    return bytes([0x09, 0x91, 0x00, 0x07, 0x00, 0x08, 0x00, 0x00, pattern, 0, 0, 0, 0, 0, 0, 0])


# --- calibration --------------------------------------------------------------------------

@dataclass(frozen=True)
class AxisCal:
    neutral: int = 2048
    below: int = 1610   # raw span from neutral down to full deflection
    above: int = 1610   # raw span from neutral up to full deflection


@dataclass(frozen=True)
class StickCal:
    x: AxisCal = field(default_factory=AxisCal)
    y: AxisCal = field(default_factory=AxisCal)


# Used when the controller's flash can't be read (another program owns the command interface).
DEFAULT_STICKS = {"gamecube": (StickCal(AxisCal(2048, 1225, 1225), AxisCal(2048, 1225, 1225)),
                               StickCal(AxisCal(2048, 1120, 1120), AxisCal(2048, 1120, 1120))),
                  "pro": (StickCal(), StickCal())}
DEFAULT_TRIGGER_ZERO = 30
TRIGGER_FULL = 232
USER_CAL_MAGIC = b"\xb2\xa1"


def _u12(lo: int, hi: int, high_nibble: bool) -> int:
    return (lo >> 4) | (hi << 4) if high_nibble else lo | ((hi & 0x0F) << 8)


def parse_stick_calibration(b: bytes) -> StickCal | None:
    """9 bytes: neutral x/y, max x/y, min x/y as packed 12-bit pairs (SDL ParseStickCalibration)."""
    if len(b) < 9 or b[:9] == b"\xff" * 9:
        return None
    nx, ny = _u12(b[0], b[1], False), _u12(b[1], b[2], True)
    ax, ay = _u12(b[3], b[4], False), _u12(b[4], b[5], True)
    bx, by = _u12(b[6], b[7], False), _u12(b[7], b[8], True)
    if not all((nx, ny, ax, ay, bx, by)):
        return None
    return StickCal(AxisCal(nx, bx, ax), AxisCal(ny, by, ay))


@dataclass
class Calibration:
    left: StickCal
    right: StickCal
    trigger_zero: tuple[int, int] = (DEFAULT_TRIGGER_ZERO, DEFAULT_TRIGGER_ZERO)
    serial: str = ""
    source: str = "defaults"

    @classmethod
    def defaults(cls, model: str) -> "Calibration":
        left, right = DEFAULT_STICKS[model]
        return cls(left, right)


def read_calibration(read_flash: Callable[[int], bytes | None], model: str) -> Calibration:
    """Factory stick calibration, user calibration if present, GameCube trigger rest values."""
    cal = Calibration.defaults(model)
    blk = read_flash(0x13000)
    if blk:
        cal.serial = blk[2:18].split(b"\0", 1)[0].decode("ascii", "replace").strip()
    got = []
    for addr, side in ((0x13080, "left"), (0x130C0, "right")):
        blk = read_flash(addr)
        sc = parse_stick_calibration(blk[0x28:0x31]) if blk else None
        if sc:
            setattr(cal, side, sc)
            got.append(f"factory {side}")
    # User recalibration (System Settings): SDL reads the right stick at 0x1FC080,
    # ndeadly/BlueRetro at 0x1FC060 - take whichever carries the magic.
    for addrs, side in (((0x1FC040,), "left"), ((0x1FC060, 0x1FC080), "right")):
        for addr in addrs:
            blk = read_flash(addr)
            if blk and blk[:2] == USER_CAL_MAGIC:
                sc = parse_stick_calibration(blk[2:11])
                if sc:
                    setattr(cal, side, sc)
                    got.append(f"user {side}")
                    break
    if model == "gamecube":
        blk = read_flash(0x13140)
        if blk and blk[0] != 0xFF and blk[1] != 0xFF:
            cal.trigger_zero = (blk[0], blk[1])
            got.append("triggers")
    if got:
        cal.source = ", ".join(got)
    return cal


# --- report parsing ------------------------------------------------------------------------

def map_axis(raw: int, cal: AxisCal, invert: bool = False) -> int:
    """SDL MapJoystickAxis: scale by the span on each side of neutral; Y is inverted."""
    v = raw - cal.neutral
    f = v / cal.below if v < 0 else v / cal.above
    out = int(max(-32768, min(32767, f * 32767)))
    return ~out if invert else out


def map_trigger(raw: int, zero: int) -> int:
    """GameCube analog L/R: rest value -> 0, 232 -> full (SDL MapTriggerAxis), as 0..32767."""
    f = (raw - zero) / max(1, TRIGGER_FULL - zero)
    return round(max(0.0, min(1.0, f)) * 32767)


B = BUTTON_INDEX
# (report byte, bit mask) -> canonical button, per model. Byte 5 holds the Y, X, B, A bits (0x01,
# 0x02, 0x04, 0x08): on the Pro Controller they are positional (Y left, X top, B bottom, A right)
# and match SDL's HandleSwitchProState. The GameCube controller puts its *own* Y, X, B and A in
# those bits (its standard HID report is laid out the same way, confirmed by pressing on the
# real controller), so positionally Y = north, X = east, B = west, A = south. Z is in the ZR bit
# (0x80), the R / L trigger clicks in the R / L bits (0x40), as in SDL's HandleGameCubeState.
BUTTON_BITS: dict[str, tuple[tuple[int, int, int], ...]] = {
    "gamecube": (
        (5, 0x01, B["north"]), (5, 0x02, B["east"]), (5, 0x04, B["west"]), (5, 0x08, B["south"]),
        (5, 0x40, B["misc4"]),            # R fully pressed (click)
        (5, 0x80, B["right_shoulder"]),   # Z
        (6, 0x02, B["start"]), (6, 0x10, B["guide"]), (6, 0x20, B["misc1"]), (6, 0x40, B["misc2"]),
        (7, 0x01, B["dpad_down"]), (7, 0x02, B["dpad_up"]), (7, 0x04, B["dpad_right"]),
        (7, 0x08, B["dpad_left"]),
        (7, 0x40, B["misc3"]),            # L fully pressed (click)
        (7, 0x80, B["left_shoulder"]),    # ZL
    ),
    "pro": (
        (5, 0x01, B["west"]), (5, 0x02, B["north"]), (5, 0x04, B["south"]), (5, 0x08, B["east"]),
        (5, 0x40, B["right_shoulder"]),
        (6, 0x01, B["back"]), (6, 0x02, B["start"]), (6, 0x04, B["right_stick"]),
        (6, 0x08, B["left_stick"]), (6, 0x10, B["guide"]), (6, 0x20, B["misc1"]),
        (6, 0x40, B["misc2"]),
        (7, 0x01, B["dpad_down"]), (7, 0x02, B["dpad_up"]), (7, 0x04, B["dpad_right"]),
        (7, 0x08, B["dpad_left"]), (7, 0x40, B["left_shoulder"]),
        (8, 0x01, B["right_paddle1"]), (8, 0x02, B["left_paddle1"]),
    ),
}


DEFAULT_DEADZONE = 0.03  # same default as the Bluetooth reader; resting sensor noise -> exactly 0


def radial_deadzone(x: int, y: int, dz: float) -> tuple[int, int]:
    """Zero a stick inside ``dz`` (fraction of full scale) and rescale the rest to full range.

    The magnitude isn't capped, so diagonals still reach full scale on both axes
    (each axis is clamped instead), as with SDL's unfiltered values.
    """
    if dz <= 0:
        return x, y
    mag = (x * x + y * y) ** 0.5 / 32767
    if mag <= dz:
        return 0, 0
    k = (mag - dz) / (1 - dz) / mag
    return (int(max(-32768, min(32767, x * k))), int(max(-32768, min(32767, y * k))))


def parse_report(model: str, data: bytes, cal: Calibration,
                 deadzone: float = DEFAULT_DEADZONE) -> PadState | None:
    """64-byte input report (report id first) -> canonical state, or None if not an input report."""
    if len(data) < REPORT_LEN or data[0] != REPORT_ID:
        return None
    st = PadState()
    for byte, mask, idx in BUTTON_BITS[model]:
        if data[byte] & mask:
            st.buttons[idx] = 1
    lx = map_axis(_u12(data[11], data[12], False), cal.left.x)
    ly = map_axis(_u12(data[12], data[13], True), cal.left.y, invert=True)
    rx = map_axis(_u12(data[14], data[15], False), cal.right.x)
    ry = map_axis(_u12(data[15], data[16], True), cal.right.y, invert=True)
    st.axes[0], st.axes[1] = radial_deadzone(lx, ly, deadzone)
    st.axes[2], st.axes[3] = radial_deadzone(rx, ry, deadzone)
    if model == "gamecube":
        for ax, raw, zero in ((4, data[61], cal.trigger_zero[0]), (5, data[62], cal.trigger_zero[1])):
            v = map_trigger(raw, zero)
            st.axes[ax] = 0 if v <= deadzone * 32767 else v
    else:  # Pro: ZL/ZR are digital
        st.axes[4] = 32767 if data[7] & 0x80 else 0
        st.axes[5] = 32767 if data[5] & 0x80 else 0
    return st


# --- transport ----------------------------------------------------------------------------

class Switch2UsbUnavailable(RuntimeError):
    pass


def _import_usb():
    try:
        import hid  # noqa: F401
        import libusb_package  # noqa: F401
        import usb.core  # noqa: F401
        import usb.util  # noqa: F401
    except ImportError as e:
        raise Switch2UsbUnavailable(
            f"the Switch 2 USB reader needs pyusb, libusb-package and hidapi ({e}); "
            "run: pip install pyusb libusb-package hidapi") from None


@dataclass
class FoundController:
    product_id: int
    model: str
    hid_path: bytes


def find_controllers() -> list[FoundController]:
    """Plugged-in Switch 2 GameCube / Pro controllers (one HID input interface each)."""
    _import_usb()
    import hid
    out = []
    for d in hid.enumerate(NINTENDO_VID, 0):
        model = MODELS.get(d.get("product_id", 0))
        if model and d.get("interface_number", 0) == 0:
            out.append(FoundController(d["product_id"], model, d["path"]))
    return out


class UsbTransport:
    """Bulk command channel (interface 1, pyusb/libusb) + HID input reports (interface 0, hidapi)."""

    def __init__(self, found: FoundController) -> None:
        _import_usb()
        import hid
        import libusb_package
        import usb.core
        self.found = found
        self._usb = None
        self._out = self._in = None
        self._claimed = False
        self._hid = hid.device()
        self._hid.open_path(found.hid_path)
        dev = usb.core.find(idVendor=NINTENDO_VID, idProduct=found.product_id,
                            backend=libusb_package.get_libusb1_backend())
        if dev is not None:
            try:
                for intf in dev.get_active_configuration():
                    if intf.bInterfaceNumber != 1:
                        continue
                    for ep in intf:
                        if ep.bmAttributes & 3 == 2:  # bulk
                            if ep.bEndpointAddress & 0x80:
                                self._in = ep.bEndpointAddress
                            else:
                                self._out = ep.bEndpointAddress
                self._usb = dev
            except Exception as e:  # noqa: BLE001 - access problems just mean no command channel
                log.info("switch2-usb: command interface not accessible: %s", e)

    def claim(self) -> bool:
        """Claim the command interface; False if another program holds it."""
        if self._usb is None or self._out is None or self._in is None:
            return False
        import usb.util
        try:
            usb.util.claim_interface(self._usb, 1)
            self._claimed = True
        except Exception as e:  # noqa: BLE001
            log.info("switch2-usb: can't claim the command interface (%s); another program "
                     "may be using the controller", e)
            return False
        return True

    def command(self, data: bytes, reply_len: int = 64) -> bytes | None:
        import usb.core
        try:
            self._usb.write(self._out, data, timeout=1000)
            reply = b""
            while len(reply) < reply_len:
                chunk = bytes(self._usb.read(self._in, 64, timeout=200))
                reply += chunk
                if len(chunk) < 64:
                    break
            return reply
        except usb.core.USBTimeoutError:
            return None

    def read_flash(self, address: int) -> bytes | None:
        reply = self.command(flash_read_command(address), 0x50)
        return reply[0x10:0x50] if reply and len(reply) >= 0x50 else None

    def release(self) -> None:
        """Release the command interface AND close the libusb handle.

        On Windows WinUSB allows one open handle per interface, so merely
        releasing the claim would still block other programs (GSE, a libusb
        SDL) from starting the controller.
        """
        if self._usb is None:
            return
        import usb.util
        if self._claimed:
            try:
                usb.util.release_interface(self._usb, 1)
            except Exception:  # noqa: BLE001
                pass
            self._claimed = False
        try:
            usb.util.dispose_resources(self._usb)
        except Exception:  # noqa: BLE001
            pass

    def read_report(self, timeout_ms: int = 100) -> bytes | None:
        """One input report, None on timeout. Raises OSError when the controller is gone."""
        data = self._hid.read(REPORT_LEN, timeout_ms)
        return bytes(data) if data else None

    def close(self) -> None:
        self.release()
        try:
            self._hid.close()
        except Exception:  # noqa: BLE001
            pass


def start_controller(t: UsbTransport, model: str, player: int = 1) -> Calibration:
    """Read calibration and send the init sequence (releases the command interface after)."""
    if not t.claim():
        log.warning("switch2-usb: using default calibration (command interface busy)")
        return Calibration.defaults(model)
    try:
        cal = read_calibration(t.read_flash, model)
        for cmd in INIT_SEQUENCE:
            t.command(cmd)
        if player:
            t.command(led_command(player))
        return cal
    finally:
        t.release()


def usb_available() -> bool:
    try:
        _import_usb()
        return True
    except Switch2UsbUnavailable:
        return False


def cmd_usb_test(args: Any) -> int:
    """``controllerlog switch2 usb``: live buttons, sticks and report rate of a USB controller."""
    import sys
    from ..model import AXES, BUTTONS, FACE_LABELS
    try:
        found = find_controllers()
    except Switch2UsbUnavailable as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not found:
        print("No Switch 2 GameCube / Pro controller found on USB. Plug it in with a data "
              "USB-C cable (it doesn't need to be paired).")
        return 1
    hub = Hub()
    be = Switch2UsbBackend(hub, player=args.player, deadzone=args.deadzone)
    be.start()
    be.ready.wait(5)
    t_end = time.monotonic() + args.seconds if args.seconds else None
    last = ""
    try:
        while t_end is None or time.monotonic() < t_end:
            if be.error:
                print(f"\nerror: {be.error}", file=sys.stderr)
                return 1
            snap = hub.snapshot()
            if not snap:
                line = "connecting..."
            else:
                info, st = next(iter(snap.values()))
                labels = FACE_LABELS.get(info.family, {})
                held = [labels.get(BUTTONS[i], BUTTONS[i]) for i, v in enumerate(st.buttons) if v]
                axes = " ".join(f"{a}={v / 32767:+.2f}" for a, v in zip(AXES, st.axes))
                line = f"{be.report_rate_hz:5.1f} Hz | {' '.join(held) or '-':20s} | {axes}"
            if line != last:
                sys.stdout.write("\r" + line.ljust(len(last)))
                sys.stdout.flush()
                last = line
            time.sleep(0.03)
    except KeyboardInterrupt:
        pass
    finally:
        be.stop()
        print()
    for info in [NAMES[f.model] for f in found]:
        print(f"{info}: {be.reports} reports, calibration read OK" if be.reports else info)
    return 0


def add_usb_parser(sub: Any) -> None:
    """Register ``switch2 usb`` under the switch2 subcommand."""
    u = sub.add_parser("usb", help="show live input from a USB-connected Switch 2 GameCube / "
                                   "Pro controller")
    u.add_argument("--seconds", type=float, default=0.0, help="stop after N seconds")
    u.add_argument("--player", type=int, choices=range(0, 9), default=1, metavar="0-8",
                   help="player LED 1-8 (0 = leave as is)")
    u.add_argument("--deadzone", type=float, default=DEFAULT_DEADZONE,
                   help="stick/trigger deadzone, 0 <= d < 1 (default %(default)s)")


# --- backend ------------------------------------------------------------------------------

class Switch2UsbBackend(Backend):
    """Publishes plugged-in Switch 2 GameCube / Pro controllers into the hub; handles replugs."""

    name = "switch2-usb"

    def __init__(self, hub: Hub, player: int = 1, rescan_s: float = 1.0,
                 deadzone: float = DEFAULT_DEADZONE,
                 transport_factory: Callable[[FoundController], Any] = UsbTransport,
                 finder: Callable[[], list[FoundController]] = find_controllers) -> None:
        super().__init__(hub)
        if not 0 <= deadzone < 1:
            raise ValueError("deadzone must be >= 0 and < 1")
        self.deadzone = deadzone
        self.player = player
        self.rescan_s = rescan_s
        self._factory = transport_factory
        self._finder = finder
        self.reports = 0
        self.report_rate_hz = 0.0

    def run(self) -> None:
        if self._finder is find_controllers:
            _import_usb()  # fail fast with install instructions
        self.ready.set()
        while not self._stop.is_set():
            found = self._finder()
            if not found:
                self._stop.wait(self.rescan_s)
                continue
            self._session(found[0])

    def _session(self, found: FoundController) -> None:
        try:
            t = self._factory(found)
        except OSError as e:  # opened by someone exclusively, or unplugged meanwhile
            log.warning("switch2-usb: can't open %s: %s", NAMES[found.model], e)
            self._stop.wait(self.rescan_s)
            return
        dev = None
        try:
            cal = start_controller(t, found.model, self.player)
            info = DeviceInfo(-1, NAMES[found.model], backend="switch2-usb",
                              sdl_type="gamecube" if found.model == "gamecube" else "switchpro",
                              family=FAMILY_GAMECUBE if found.model == "gamecube" else FAMILY_SWITCH,
                              vendor_id=NINTENDO_VID, product_id=found.product_id,
                              connection="wired", serial=cal.serial,
                              extra={"calibration": cal.source})
            key = ("switch2-usb", cal.serial or found.hid_path)
            dev = self.hub.connect(key, info)
            log.info("switch2-usb: connected %s (%s)", info.name, cal.source)
            window_t, window_n = time.monotonic(), 0
            prev = PadState()
            while not self._stop.is_set():
                data = t.read_report(100)
                if data is None:
                    continue
                st = parse_report(found.model, data, cal, self.deadzone)
                if st is None:
                    continue
                self.reports += 1
                window_n += 1
                t_ns = now_ns()
                for i, v in enumerate(st.buttons):
                    if v != prev.buttons[i]:
                        self.hub.publish(InputEvent(t_ns, dev, BUTTON, i, v))
                for i, v in enumerate(st.axes):
                    if v != prev.axes[i]:
                        self.hub.publish(InputEvent(t_ns, dev, AXIS, i, v))
                prev = st
                now = time.monotonic()
                if now - window_t >= 1.0:
                    self.report_rate_hz = window_n / (now - window_t)
                    window_t, window_n = now, 0
        except OSError as e:  # unplugged
            log.info("switch2-usb: %s disconnected (%s)", NAMES[found.model], e)
        finally:
            if dev is not None:
                self.hub.disconnect(dev)
            t.close()
