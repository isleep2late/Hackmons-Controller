"""Switch 2 USB reader. Byte vectors are real captures from an NSO GameCube controller (057E:2073)."""
from __future__ import annotations

import threading
import time

import pytest

from controllerlog.hub import Hub
from controllerlog.input import switch2_usb as s2
from controllerlog.model import AXIS, BUTTON, BUTTON_INDEX, CONNECT, DISCONNECT

# Resting input report captured over USB after init (report id 0x05, 64 bytes).
REST = bytes.fromhex(
    "05 3d 24 05 00 00 00 00 00 00 00 2e c8 82 08 28 83 00 00 00 00 00 00 00 00 00 00 00 00 00"
    " 00 00 00 94 0d 34 00 00 00 00 00 00 01 92 af 02 00 03 00 47 00 1a f7 81 0d f0 ff f3 ff"
    " ed ff 20 1f 00")
assert len(REST) == 64 and REST[61:63] == b"\x20\x1f"  # resting L/R trigger bytes
# Factory stick calibration (flash 0x13080 / 0x130C0, bytes +0x28) and trigger rest (0x13140).
LEFT_CAL = bytes.fromhex("2c b8 82 d7 f4 47 d2 d4 4f")
RIGHT_CAL = bytes.fromhex("12 98 83 23 04 43 96 a4 49")


def cal() -> s2.Calibration:
    return s2.Calibration(s2.parse_stick_calibration(LEFT_CAL), s2.parse_stick_calibration(RIGHT_CAL),
                          trigger_zero=(0x24, 0x1D), serial="HHW00000000001", source="test")


def with_bytes(base: bytes, **changes: int) -> bytes:
    b = bytearray(base)
    for k, v in changes.items():
        b[int(k[1:])] = v
    return bytes(b)


def test_calibration_parsing_real_bytes():
    left = s2.parse_stick_calibration(LEFT_CAL)
    assert (left.x.neutral, left.y.neutral) == (0x82C, 0x82B)
    assert (left.x.above, left.x.below) == (0x4D7, 0x4D2)
    assert s2.parse_stick_calibration(b"\xff" * 9) is None


def test_read_calibration_uses_factory_user_and_trigger_blocks():
    blocks = {0x13000: b"\x01\x00HHW00000000001\x00\x00" + b"\xff" * 46,
              0x13080: b"\xff" * 0x28 + LEFT_CAL + b"\xff" * 15,
              0x130C0: b"\xff" * 0x28 + RIGHT_CAL + b"\xff" * 15,
              0x13140: b"\x24\x1d" + b"\xff" * 62,
              0x1FC080: s2.USER_CAL_MAGIC + LEFT_CAL + b"\xff" * 53}  # user right cal (SDL address)
    c = s2.read_calibration(lambda a: blocks.get(a, b"\xff" * 64), "gamecube")
    assert c.serial == "HHW00000000001"
    assert c.trigger_zero == (0x24, 0x1D)
    assert c.right == s2.parse_stick_calibration(LEFT_CAL)       # user calibration wins
    assert "factory left" in c.source and "user right" in c.source
    assert s2.read_calibration(lambda a: None, "pro") == s2.Calibration.defaults("pro")


def test_rest_report_is_centred_and_quiet():
    st = s2.parse_report("gamecube", REST, cal())
    assert not any(st.buttons)
    assert st.axes == [0, 0, 0, 0, 0, 0]          # inside the deadzone -> exactly 0
    raw = s2.parse_report("gamecube", REST, cal(), deadzone=0)
    assert all(abs(v) < 600 for v in raw.axes[:4])  # within ~2% of centre without a deadzone


@pytest.mark.parametrize("byte,mask,name", [
    (5, 0x01, "north"), (5, 0x02, "east"), (5, 0x04, "west"), (5, 0x08, "south"),   # Y X B A
    (5, 0x40, "misc4"), (5, 0x80, "right_shoulder"), (6, 0x02, "start"), (6, 0x10, "guide"),
    (6, 0x20, "misc1"), (6, 0x40, "misc2"), (7, 0x01, "dpad_down"), (7, 0x02, "dpad_up"),
    (7, 0x04, "dpad_right"), (7, 0x08, "dpad_left"), (7, 0x40, "misc3"), (7, 0x80, "left_shoulder"),
])
def test_gamecube_button_bits(byte, mask, name):
    st = s2.parse_report("gamecube", with_bytes(REST, **{f"b{byte}": mask}), cal())
    assert [i for i, v in enumerate(st.buttons) if v] == [BUTTON_INDEX[name]]


def test_sticks_and_triggers_full_deflection():
    c = cal()
    # left stick fully right/up: neutral + span; 12-bit packing x=d11|(d12&0xF)<<8, y=(d12>>4)|d13<<4
    x = c.left.x.neutral + c.left.x.above
    y = c.left.y.neutral + c.left.y.above
    d = with_bytes(REST, b11=x & 0xFF, b12=((x >> 8) & 0x0F) | ((y & 0x0F) << 4), b13=y >> 4,
                   b61=232, b62=0x1D + 1)
    st = s2.parse_report("gamecube", d, c)
    assert st.axes[0] == 32767 and st.axes[1] == -32768     # right, and up (canonical y<0)
    assert st.axes[4] == 32767 and st.axes[5] == 0          # L fully in; R at rest (deadzone)


def test_pro_layout_and_digital_triggers():
    d = with_bytes(REST, b5=0x80 | 0x08, b6=0x01 | 0x08, b7=0x80, b8=0x02)
    st = s2.parse_report("pro", d, s2.Calibration.defaults("pro"))
    held = {n for n, i in BUTTON_INDEX.items() if st.buttons[i]}
    assert held == {"east", "back", "left_stick", "left_paddle1"}
    assert st.axes[4] == 32767 and st.axes[5] == 32767


def test_non_input_reports_are_ignored():
    assert s2.parse_report("gamecube", b"\x06" + REST[1:], cal()) is None
    assert s2.parse_report("gamecube", REST[:40], cal()) is None


def test_radial_deadzone_rescales():
    assert s2.radial_deadzone(500, -300, 0.03) == (0, 0)
    x, _ = s2.radial_deadzone(32767, 0, 0.03)
    assert x == 32767
    assert s2.radial_deadzone(10, 10, 0) == (10, 10)


def test_init_commands_match_sdl_lengths():
    for cmd in s2.INIT_SEQUENCE:
        assert len(cmd) == cmd[5] + 8   # SDL sends init_sequence[i][5] + 8 bytes
    assert s2.flash_read_command(0x13080)[12:16] == bytes([0x80, 0x30, 0x01, 0x00])


class FakeTransport:
    """Scripted controller: flash blocks, a list of reports, then 'unplug' (OSError)."""

    def __init__(self, reports, claim_ok=True):
        self.reports = list(reports)
        self.claim_ok = claim_ok
        self.commands = []
        self.released = self.closed = False

    def claim(self):
        return self.claim_ok

    def command(self, data, reply_len=64):
        self.commands.append(bytes(data))
        return b"\x00" * 16

    def read_flash(self, addr):
        return {0x13080: b"\xff" * 0x28 + LEFT_CAL + b"\xff" * 15,
                0x130C0: b"\xff" * 0x28 + RIGHT_CAL + b"\xff" * 15,
                0x13140: b"\x24\x1d" + b"\xff" * 62}.get(addr)

    def release(self):
        self.released = True

    def read_report(self, timeout_ms=100):
        if not self.reports:
            raise OSError("device disconnected")
        time.sleep(0.001)
        return self.reports.pop(0)

    def close(self):
        self.closed = True


def run_backend(transports, seconds=0.5):
    hub = Hub()
    events = []
    hub.add_sink(events.append)
    found = [s2.FoundController(0x2073, "gamecube", b"path")]
    queue = list(transports)
    lock = threading.Lock()

    def factory(f):
        with lock:
            return queue.pop(0) if queue else FakeTransport([])

    be = s2.Switch2UsbBackend(hub, rescan_s=0.01, transport_factory=factory, finder=lambda: found)
    be.start()
    time.sleep(seconds)
    be.stop()
    return be, events


def test_backend_publishes_changes_and_handles_unplug():
    press_a = with_bytes(REST, b5=0x08)          # the GameCube's A: bit 3, bottom button
    t1 = FakeTransport([REST, REST, press_a, press_a, REST])
    be, events = run_backend([t1])
    kinds = [(e.kind, e.code, e.value) for e in events if e.device == 0]
    assert kinds[0][0] == CONNECT and events[0].value["family"] == "gamecube"
    presses = [(k, c, v) for k, c, v in kinds if k == BUTTON]
    assert presses[:2] == [(BUTTON, BUTTON_INDEX["south"], 1), (BUTTON, BUTTON_INDEX["south"], 0)]
    assert not [k for k in kinds if k[0] == AXIS]              # rest noise never published
    assert (DISCONNECT, None, None) in kinds                  # unplug -> disconnect
    assert t1.released and t1.closed
    assert t1.commands[: len(s2.INIT_SEQUENCE)] == list(s2.INIT_SEQUENCE)
    assert be.error is None


def test_busy_command_interface_falls_back_to_defaults():
    t = FakeTransport([REST], claim_ok=False)
    be, events = run_backend([t], 0.2)
    connect = next(e for e in events if e.kind == CONNECT)
    assert connect.value["extra"]["calibration"] == "defaults"
    assert t.commands == []   # never talks to an interface it couldn't claim
