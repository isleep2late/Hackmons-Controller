"""Minimal BLE sniffer pcapng reader for the optional real-capture tests.

Reads Nordic nRF Sniffer (linktype 272) and ``BLUETOOTH_LE_LL_WITH_PHDR`` (256)
captures, reassembles fragmented LL data PDUs and yields ATT PDUs. Enough to
replay the nRF52840 captures published in ndeadly/switch2_controller_research
(not bundled here: that repository has no license file).
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Iterator

_ADV_ACCESS_ADDRESS = bytes.fromhex("d6be898e")


def _packets(path: str | Path) -> Iterator[tuple[int, int, bytes]]:
    """(linktype, timestamp, data) for every Enhanced Packet Block."""
    buf = Path(path).read_bytes()
    off, endian, linktypes = 0, "<", []
    while off + 12 <= len(buf):
        btype, blen = struct.unpack_from(endian + "II", buf, off)
        if btype == 0x0A0D0D0A:  # section header: byte-order magic decides endianness
            endian = "<" if buf[off + 8:off + 12] == b"\x4d\x3c\x2b\x1a" else ">"
            btype, blen = struct.unpack_from(endian + "II", buf, off)
        if blen < 12:
            break
        if btype == 1:
            linktypes.append(struct.unpack_from(endian + "H", buf, off + 8)[0])
        elif btype == 6:
            iface, th, tl, caplen = struct.unpack_from(endian + "IIII", buf, off + 8)
            yield linktypes[iface], (th << 32) | tl, buf[off + 28:off + 28 + caplen]
        off += blen


def att_pdus(path: str | Path) -> Iterator[tuple[int, int, bytes]]:
    """Yield ``(timestamp, direction, att_pdu)`` from a BLE sniffer capture."""
    pending: dict[int, bytearray] = {}
    for lt, ts, d in _packets(path):
        if lt == 272 and len(d) >= 26:          # Nordic BLE: LL starts at byte 17
            aa, flags, ll = d[17:21], d[8], 21
            if not flags & 0x01:                # CRC failed
                continue
            direction = (flags >> 1) & 1
        elif lt == 256 and len(d) >= 16:        # LE LL with 10-byte pseudo header
            aa, ll = d[10:14], 14
            flags = struct.unpack_from("<H", d, 8)[0]
            if flags & 0x0400 and not flags & 0x0800:
                continue
            direction = (flags >> 7) & 7
        else:
            continue
        if aa == _ADV_ACCESS_ADDRESS:
            continue
        llid, length = d[ll] & 3, d[ll + 1]
        payload = d[ll + 2:ll + 2 + length]
        if len(payload) != length:
            continue
        if llid == 2:                           # start of an L2CAP frame
            pending[direction] = bytearray(payload)
        elif llid == 1 and length and direction in pending:
            pending[direction] += payload       # continuation
        else:
            continue
        frame = pending[direction]
        if len(frame) >= 4:
            l2len, cid = struct.unpack_from("<HH", frame, 0)
            if len(frame) >= 4 + l2len:
                if cid == 4:
                    yield ts, direction, bytes(frame[4:4 + l2len])
                del pending[direction]


def notifications(path: str | Path, handle: int) -> list[tuple[int, bytes]]:
    """``(timestamp, value)`` of every ATT Handle Value Notification on ``handle``."""
    out = []
    for ts, _d, pdu in att_pdus(path):
        if len(pdu) >= 3 and pdu[0] == 0x1B and struct.unpack_from("<H", pdu, 1)[0] == handle:
            out.append((ts, pdu[3:]))
    return out
