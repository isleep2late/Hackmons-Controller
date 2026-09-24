"""Automated TAS input optimisation, evaluated inside BizHawk.

The inputs are searched over a window of a ``.bk2`` movie and every candidate
is run on BizHawk's own core, so inputs that score well there also sync when
the exported ``.bk2`` is played back with the same core and sync settings.

How the pieces fit:

* ``tools/bizhawk/controllerlog_bot.lua`` runs inside EmuHawk. EmuHawk started
  with ``--socket_ip=127.0.0.1 --socket_port=PORT`` connects *as a TCP client*
  to a server we run (BizHawk calls it ``SocketServer``, but it is a client).
* :class:`BizHawkBridge` is that server. Both directions use BizHawk's framing
  (since 2.6.2): ``"{byte length} {utf-8 payload}"``. One command per message,
  one reply per command (``OK ...`` or ``ERR <message>``).
* The hot path is ``EVAL``: load a savestate, play N input lines, read RAM,
  reply - one round trip per candidate. ``EVAL ... UNTIL <read> <op> <value>``
  stops as soon as a condition holds and reports after how many frames, which
  is how "minimise frames until X" objectives are scored.
* Search algorithms (:func:`delay_search`, :func:`beam_search`,
  :func:`random_mutation`) only need an :class:`Evaluator`, so they can be
  tested against a pure-Python fake game.

This is *local search over a window* with a RAM objective the user picks, not
whole-game optimal solving.
"""

from __future__ import annotations

import argparse
import dataclasses
import random
import re
import selectors
import socket
import struct
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Protocol, Sequence, Union, runtime_checkable
from urllib.parse import quote

from .formats import bk2

DEFAULT_PORT = 43880
PROTO_VERSION = 1
BOT_SCRIPT_NAME = "controllerlog_bot.lua"
MAX_MESSAGE = 64 * 1024 * 1024
OPTIMIZED_TAG = "optimized by ControllerLog"

Progress = Callable[[str], None]


class BridgeError(RuntimeError):
    """Connection/transport problem between Python and BizHawk."""


class ProtocolError(BridgeError):
    """Malformed message or unexpected reply."""


class BizHawkError(BridgeError):
    """The bot answered ``ERR <message>``."""

    def __init__(self, message: str, command: str = "") -> None:
        super().__init__(message)
        self.command = command


# --- framing -----------------------------------------------------------------------------

def encode_message(msg: str) -> bytes:
    """BizHawk framing: ASCII decimal byte length, one space, UTF-8 payload."""
    payload = msg.encode("utf-8")
    return str(len(payload)).encode("ascii") + b" " + payload


class FrameDecoder:
    """Incremental decoder for length-prefixed messages; tolerates any fragmentation."""

    def __init__(self, max_len: int = MAX_MESSAGE) -> None:
        self._buf = bytearray()
        self.max_len = max_len

    def feed(self, data: bytes) -> list[str]:
        self._buf += data
        out: list[str] = []
        while self._buf:
            sp = self._buf.find(b" ", 0, 12)
            if sp < 0:
                if len(self._buf) >= 12 or not self._buf.isdigit():
                    raise ProtocolError(f"bad length prefix {bytes(self._buf[:12])!r}")
                break
            head = bytes(self._buf[:sp])
            if not head.isdigit():
                raise ProtocolError(f"bad length prefix {head!r}")
            n = int(head)
            if n > self.max_len:
                raise ProtocolError(f"message of {n} bytes is too large")
            if len(self._buf) < sp + 1 + n:
                break
            payload = bytes(self._buf[sp + 1:sp + 1 + n])
            del self._buf[:sp + 1 + n]
            try:
                out.append(payload.decode("utf-8"))
            except UnicodeDecodeError as e:
                raise ProtocolError(f"message is not UTF-8: {e}") from None
        return out

    @property
    def pending(self) -> int:
        return len(self._buf)


def encode_lines(lines: Sequence[str]) -> str:
    """Join bk2 input lines with ``;``, run-length encoding repeats as ``N*line``."""
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.startswith("|") or any(c in line for c in ";\r\n"):
            raise ValueError(f"not a bk2 input line: {line!r}")
        j = i + 1
        while j < n and lines[j] == line:
            j += 1
        out.append(line if j - i == 1 else f"{j - i}*{line}")
        i = j
    return ";".join(out)


_RLE = re.compile(r"^(\d+)\*(.*)$", re.S)


def expand_lines(text: str) -> list[str]:
    """Inverse of :func:`encode_lines`."""
    text = text.strip()
    if not text:
        return []
    out: list[str] = []
    for item in text.split(";"):
        m = _RLE.match(item)
        count = 1
        if m:
            count, item = int(m.group(1)), m.group(2)
            if count < 1:
                raise ValueError(f"bad repeat count in {item!r}")
        if not item:
            raise ValueError("empty input line")
        out.extend([item] * count)
    return out


def _pct(s: str) -> str:
    return quote(s, safe="")


# --- reads and conditions ---------------------------------------------------------------------

_SIZE_RE = re.compile(r"^([sS]?)([124])$")


def _parse_addr(s: str) -> int:
    s = s.strip()
    for p in ("0x", "0X", "$"):
        if s.startswith(p):
            s = s[len(p):]
            break
    try:
        v = int(s, 16)
    except ValueError:
        raise ValueError(f"bad address {s!r} (hex, e.g. 0xD361)") from None
    if v < 0:
        raise ValueError(f"negative address {s!r}")
    return v


@dataclass(frozen=True)
class Read:
    """One RAM value: ``size`` bytes (1/2/4) at ``addr`` in a BizHawk memory domain.

    Spec syntax: ``DOMAIN:ADDR[:SIZE[:ENDIAN]]`` with a hex address, size
    ``1``/``2``/``4`` (``s1``/``s2``/``s4`` = signed) and ``le``/``be``, e.g.
    ``WRAM:0x1361:1``, ``System Bus:0xFF44:1``, ``IWRAM:0x4A10:s2:le``.
    Domain offsets start at 0 (GB ``WRAM:0x0`` is bus address ``0xC000``).
    """

    domain: str
    addr: int
    size: int = 1
    endian: str = "le"
    signed: bool = False

    def __post_init__(self) -> None:
        if not self.domain:
            raise ValueError("empty memory domain")
        if self.size not in (1, 2, 4):
            raise ValueError(f"read size must be 1, 2 or 4 (got {self.size})")
        if self.endian not in ("le", "be"):
            raise ValueError(f"endian must be 'le' or 'be' (got {self.endian!r})")
        if self.addr < 0:
            raise ValueError("negative address")

    @property
    def length(self) -> int:
        return self.size

    def token(self) -> str:
        return f"{_pct(self.domain)} {self.addr:X} {'s' if self.signed else ''}{self.size} {self.endian}"

    def parse_value(self, tok: str) -> int:
        try:
            return int(tok)
        except ValueError:
            raise ProtocolError(f"bad value {tok!r} for {self}") from None

    @classmethod
    def parse(cls, spec: str) -> "Read":
        parts = spec.strip().split(":")
        if len(parts) < 2 or not parts[0].strip():
            raise ValueError(f"bad read {spec!r}: expected DOMAIN:ADDR[:SIZE[:ENDIAN]], "
                             "e.g. WRAM:0x1361:1")
        endian = "le"
        if len(parts) >= 3 and parts[-1].strip().lower() in ("le", "be"):
            endian = parts.pop().strip().lower()
        size, signed = 1, False
        if len(parts) >= 3:
            m = _SIZE_RE.match(parts[-1].strip())
            if not m:
                raise ValueError(f"bad read size {parts[-1]!r} in {spec!r} (1, 2, 4, s1, s2 or s4)")
            parts.pop()
            size, signed = int(m.group(2)), bool(m.group(1))
        addr = _parse_addr(parts.pop())
        return cls(":".join(parts).strip(), addr, size, endian, signed)

    def __str__(self) -> str:
        s = f"{self.domain}:0x{self.addr:X}:{'s' if self.signed else ''}{self.size}"
        return s + (":be" if self.endian == "be" else "")


@dataclass(frozen=True)
class RamHash:
    """SHA-256 of a memory region (``memory.hash_region``), used as a dedup key.

    Spec: ``hash:DOMAIN`` (whole domain, size from BizHawk) or
    ``hash:DOMAIN:ADDR:LENGTH`` (hex). The value is the hash's first 64 bits.
    """

    domain: str
    addr: int = 0
    length: int = 0

    def token(self) -> str:
        if self.length <= 0:
            raise ValueError(f"{self}: unknown length (resolve it with the domain size first)")
        return f"{_pct(self.domain)} {self.addr:X} h{self.length} le"

    def parse_value(self, tok: str) -> int:
        try:
            return int(tok[:16], 16)
        except ValueError:
            raise ProtocolError(f"bad hash {tok!r} for {self}") from None

    @classmethod
    def parse(cls, spec: str) -> "RamHash":
        body = spec.strip()
        if body.lower().startswith("hash:"):
            body = body[5:]
        parts = [p.strip() for p in body.split(":")]
        if len(parts) >= 3:
            return cls(":".join(parts[:-2]), _parse_addr(parts[-2]), _parse_addr(parts[-1]))
        if len(parts) == 2 or not parts[0]:
            raise ValueError(f"bad hash spec {spec!r}: expected hash:DOMAIN or hash:DOMAIN:ADDR:LEN, "
                             "e.g. hash:WRAM or hash:WRAM:0x0:0x100")
        return cls(parts[0])

    def __str__(self) -> str:
        return f"hash:{self.domain}" + (f":0x{self.addr:X}:0x{self.length:X}" if self.length else "")


ReadSpec = Union[Read, RamHash]


def parse_read_spec(spec: str) -> ReadSpec:
    """``hash:...`` -> :class:`RamHash`, anything else -> :class:`Read`."""
    return RamHash.parse(spec) if spec.strip().lower().startswith("hash:") else Read.parse(spec)


_OPS: dict[str, Callable[[int, int], bool]] = {
    "==": lambda a, b: a == b, "!=": lambda a, b: a != b,
    "<=": lambda a, b: a <= b, ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b, ">": lambda a, b: a > b,
    "&": lambda a, b: (a & b) != 0,
}
_COND_RE = re.compile(r"^(?P<read>.+?)\s*(?P<op>==|!=|<=|>=|<|>|&)\s*(?P<val>[-+]?(?:0[xX][0-9a-fA-F]+|\d+))\s*$")


def _parse_int(s: str) -> int:
    """Decimal (leading zeros allowed) or ``0x`` hex, optionally signed."""
    body = s.lstrip("+-")
    v = int(body, 16) if body[:2].lower() == "0x" else int(body, 10)
    return -v if s.startswith("-") else v


@dataclass(frozen=True)
class Condition:
    """``read op value``; ops ``== != < <= > >= &`` (``&`` = any of the mask bits set)."""

    read: Read
    op: str
    value: int

    def __post_init__(self) -> None:
        if self.op not in _OPS:
            raise ValueError(f"bad operator {self.op!r}")

    def test(self, v: int) -> bool:
        return _OPS[self.op](v, self.value)

    def token(self) -> str:
        return f"UNTIL {self.read.token()} {self.op} {self.value}"

    def heuristic(self, v: int) -> float:
        """How close ``v`` is to satisfying the condition (higher = closer)."""
        if self.op == "==":
            return -abs(v - self.value)
        if self.op in (">", ">="):
            return float(v)
        if self.op in ("<", "<="):
            return float(-v)
        return 0.0

    @classmethod
    def parse(cls, spec: str) -> "Condition":
        m = _COND_RE.match(spec.strip())
        if not m:
            raise ValueError(f"bad condition {spec!r}: expected READ OP VALUE, e.g. WRAM:0x135E:1==92")
        return cls(Read.parse(m.group("read")), m.group("op"), _parse_int(m.group("val")))

    def __str__(self) -> str:
        return f"{self.read}{self.op}{self.value}"


# --- results, evaluator protocol ------------------------------------------------------------------

@dataclass
class EvalResult:
    framecount: int
    lag: int
    values: list[int]
    hit: int | None = None   # frames until the UNTIL condition held (0 = at the start); None = never / not asked


@dataclass(frozen=True)
class Hello:
    system: str
    framecount: int
    movie_mode: str = "INACTIVE"
    proto: int = 0

    @classmethod
    def parse(cls, msg: str) -> "Hello":
        parts = msg.split()
        if len(parts) < 3 or parts[0] != "HELLO":
            raise ProtocolError(f"expected HELLO from BizHawk, got {msg[:80]!r}")
        try:
            return cls(parts[1], int(parts[2]), parts[3] if len(parts) > 3 else "INACTIVE",
                       int(parts[4]) if len(parts) > 4 else 0)
        except ValueError:
            raise ProtocolError(f"bad HELLO {msg[:80]!r}") from None


@runtime_checkable
class Evaluator(Protocol):
    """What the search algorithms need; :class:`BizHawkBridge` is the real one."""

    def evaluate(self, state_id: int, lines: Sequence[str], reads: Sequence[ReadSpec] = (),
                 until: Condition | None = None) -> EvalResult: ...

    def branch(self, state_id: int, lines: Sequence[str]) -> int: ...

    def free_state(self, state_id: int) -> None: ...


# --- the bridge -----------------------------------------------------------------------------------------

def bot_script_path() -> Path:
    """Where ``controllerlog_bot.lua`` lives (repo checkout, or shipped inside the package)."""
    here = Path(__file__).resolve().parent
    for cand in (here.parent / "tools" / "bizhawk" / BOT_SCRIPT_NAME, here / "bizhawk" / BOT_SCRIPT_NAME):
        if cand.exists():
            return cand
    return here.parent / "tools" / "bizhawk" / BOT_SCRIPT_NAME


def bizhawk_command(host: str, port: int, *, movie: str | Path | None = None,
                    rom: str | Path | None = None, emuhawk: str | Path | None = None,
                    script: str | Path | None = None) -> list[str]:
    """The EmuHawk command line (as a list) that connects to a bridge on host:port."""
    cmd = [str(emuhawk) if emuhawk else "EmuHawk.exe", f"--socket_ip={host}", f"--socket_port={port}"]
    if movie:
        cmd.append(f"--movie={Path(movie).resolve()}")
    cmd.append(f"--lua={Path(script).resolve() if script else bot_script_path()}")
    cmd.append(str(Path(rom).resolve()) if rom else r"path\to\rom.gbc")
    return cmd


def _quote_arg(a: str) -> str:
    if "=" in a and a.startswith("--"):
        k, v = a.split("=", 1)
        return f'{k}="{v}"' if " " in v else a
    return f'"{a}"' if " " in a else a


def bizhawk_instructions(host: str, port: int, *, movie: str | Path | None = None,
                         rom: str | Path | None = None, emuhawk: str | Path | None = None,
                         script: str | Path | None = None) -> str:
    cmd = " ".join(_quote_arg(a) for a in bizhawk_command(host, port, movie=movie, rom=rom,
                                                          emuhawk=emuhawk, script=script))
    lines = [
        f"Waiting for BizHawk to connect to {host}:{port} ...",
        "Start BizHawk (EmuHawk 2.6.2 or newer) now, after this message, with the ROM the movie was made for:",
        f"    {cmd}",
        "  * --socket_ip/--socket_port make EmuHawk connect to this program. They only work on the command",
        "    line, and EmuHawk refuses to start when nothing is listening, so start this program first.",
    ]
    if movie:
        lines.append("  * --movie replays the movie with its own core and sync settings up to the start frame;")
        lines.append("    the bot then stops the movie and takes over the input.")
    lines += [
        "  * --lua starts the bot. If EmuHawk is already running with these --socket flags, open",
        "    Tools > Lua Console and (re)start controllerlog_bot.lua instead.",
        "  * Leave the emulator alone while the search runs (the bot pauses it between commands).",
    ]
    return "\n".join(lines)


class BizHawkBridge:
    """TCP server that EmuHawk (``--socket_ip/--socket_port``) connects to, plus the bot's commands.

    The port is bound in the constructor so BizHawk can be launched right
    after; :meth:`wait_for_bizhawk` then accepts the connection that says
    ``HELLO``. EmuHawk opens one connection at startup and the bot script
    reconnects when it starts (so it can be restarted for a new session), so
    silent connections are accepted and dropped.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT, timeout_s: float = 120.0) -> None:
        self.host = host
        self.timeout_s = timeout_s
        self._listener: socket.socket | None = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if sys.platform == "win32":
                self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._listener.bind((host, port))
            self._listener.listen(8)
        except (OSError, OverflowError) as e:  # OverflowError: port outside 0-65535
            self._listener.close()
            self._listener = None
            raise BridgeError(f"can't listen on {host}:{port} ({e}); is another optimize session "
                              "running? Pick another --port") from None
        self.port: int = self._listener.getsockname()[1]
        self._sock: socket.socket | None = None
        self._decoder = FrameDecoder()
        self._pending: deque[str] = deque()
        self.hello: Hello | None = None
        self.requests = 0
        self.log_key: str | None = None

    # -- connection ---------------------------------------------------------------------------
    def __enter__(self) -> "BizHawkBridge":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def instructions(self, **kw) -> str:
        return bizhawk_instructions(self.host, self.port, **kw)

    def wait_for_bizhawk(self, timeout_s: float = 300.0, instructions: str | None = None,
                         auto_keys: bool = True) -> Hello:
        """Accept EmuHawk's connection and read its ``HELLO``.

        With ``auto_keys``, the standard bk2 LogKey of a GB/GBC/GBA core is sent
        right away (see :meth:`set_keys`); call :meth:`set_keys` yourself for
        other layouts.
        """
        if self._sock is not None:
            assert self.hello is not None
            return self.hello
        if self._listener is None:
            raise BridgeError("bridge is closed")
        deadline = time.monotonic() + timeout_s
        sel = selectors.DefaultSelector()
        sel.register(self._listener, selectors.EVENT_READ, None)
        conns: dict[socket.socket, FrameDecoder] = {}
        chosen: tuple[socket.socket, FrameDecoder, deque[str]] | None = None
        try:
            while chosen is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BridgeError("BizHawk did not connect within "
                                      f"{timeout_s:.0f}s.\n" + (instructions or self.instructions()))
                for key, _ in sel.select(timeout=min(remaining, 0.25)):
                    if key.fileobj is self._listener:
                        try:
                            conn, _ = self._listener.accept()
                        except OSError:
                            continue
                        conn.setblocking(False)
                        conns[conn] = FrameDecoder()
                        sel.register(conn, selectors.EVENT_READ, None)
                        continue
                    conn = key.fileobj  # type: ignore[assignment]
                    try:
                        data = conn.recv(65536)
                    except BlockingIOError:
                        continue
                    except OSError:
                        data = b""
                    if not data:
                        sel.unregister(conn)
                        conns.pop(conn, None)
                        conn.close()
                        continue
                    try:
                        msgs = conns[conn].feed(data)
                    except ProtocolError:
                        if data.startswith(b"HELLO "):
                            raise BridgeError("BizHawk sent an unframed message; this EmuHawk predates the "
                                              "length-prefixed socket protocol. Use EmuHawk 2.6.2 or newer "
                                              "(tested against 2.11)") from None
                        sel.unregister(conn)
                        conns.pop(conn)
                        conn.close()
                        continue
                    if msgs:
                        hello = Hello.parse(msgs[0])
                        sel.unregister(conn)
                        dec = conns.pop(conn)
                        chosen = (conn, dec, deque(msgs[1:]))
                        self.hello = hello
        finally:
            for c in list(conns):
                try:
                    sel.unregister(c)
                except (KeyError, ValueError):
                    pass
                _abort(c)
            sel.close()
        conn, dec, rest = chosen
        conn.setblocking(True)
        conn.settimeout(self.timeout_s)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        _set_abortive_close(conn)
        self._sock, self._decoder, self._pending = conn, dec, rest
        self._listener.close()
        self._listener = None
        assert self.hello is not None
        if self.hello.system == "NULL":
            self.close()
            raise BridgeError("BizHawk has no ROM loaded; pass the ROM path on the EmuHawk command line")
        system = {"GB": "gb", "GBC": "gb", "GBA": "gba"}.get(self.hello.system)
        if auto_keys and system:
            try:
                self.set_keys(LineCodec.for_system(system).log_key)
            except BizHawkError:
                pass
        return self.hello

    connect = wait_for_bizhawk

    def close(self) -> None:
        """Send QUIT (so the Lua loop ends cleanly), then reset the connection.

        Replies still in flight (e.g. after Ctrl+C interrupted a request) are
        skipped until ``OK bye`` arrives, for up to 5 seconds.
        """
        if self._sock is not None:
            try:
                self._sock.settimeout(5.0)
                self._sock.sendall(encode_message("QUIT"))
                deadline = time.monotonic() + 5.0
                while (left := deadline - time.monotonic()) > 0:
                    if self._recv(left) == "OK bye":
                        break
            except (OSError, BridgeError):
                pass
            self._drop()
        if self._listener is not None:
            self._listener.close()
            self._listener = None

    def _drop(self) -> None:
        if self._sock is not None:
            _abort(self._sock)
            self._sock = None

    # -- messaging ---------------------------------------------------------------------------
    def _recv(self, timeout_s: float | None = None) -> str:
        assert self._sock is not None
        self._sock.settimeout(self.timeout_s if timeout_s is None else timeout_s)
        while not self._pending:
            data = self._sock.recv(65536)
            if not data:
                raise BridgeError("BizHawk closed the connection (Lua script stopped or EmuHawk closed)")
            self._pending.extend(self._decoder.feed(data))
        return self._pending.popleft()

    def request_raw(self, cmd: str, timeout_s: float | None = None) -> str:
        if self._sock is None:
            raise BridgeError("not connected to BizHawk (call wait_for_bizhawk first)")
        verb = cmd.split(" ", 1)[0]
        try:
            self._sock.sendall(encode_message(cmd))
            reply = self._recv(timeout_s)
        except socket.timeout:
            self._drop()
            raise BridgeError(f"BizHawk did not answer {verb} within "
                              f"{self.timeout_s if timeout_s is None else timeout_s:.0f}s") from None
        except BridgeError:
            self._drop()
            raise
        except OSError as e:
            self._drop()
            raise BridgeError(f"connection to BizHawk lost during {verb}: {e}") from None
        self.requests += 1
        return reply

    def request(self, cmd: str, timeout_s: float | None = None) -> list[str]:
        """Send a command; returns the tokens after ``OK`` or raises :class:`BizHawkError`."""
        reply = self.request_raw(cmd, timeout_s)
        if reply == "ERR" or reply.startswith("ERR "):
            raise BizHawkError(reply[4:] or "unspecified error", cmd.split(" ", 1)[0])
        if reply == "OK" or reply.startswith("OK "):
            return reply[3:].split()
        raise ProtocolError(f"unexpected reply {reply[:80]!r} to {cmd.split(' ', 1)[0]}")

    @staticmethod
    def _ints(toks: list[str], n: int, cmd: str) -> list[int]:
        if len(toks) < n:
            raise ProtocolError(f"{cmd} reply has {len(toks)} value(s), expected {n}")
        try:
            return [int(t) for t in toks[:n]]
        except ValueError:
            raise ProtocolError(f"bad {cmd} reply {' '.join(toks)!r}") from None

    # -- commands ------------------------------------------------------------------------------
    def ping(self) -> None:
        self.request("PING")

    def set_keys(self, log_key: str) -> int:
        """Tell the bot the column layout of input lines (a bk2 LogKey, e.g. ``#Up|Down|...|``).

        With it the bot applies each line with ``joypad.set`` (effective on the
        very next frame); without it, with ``joypad.setfrommnemonicstr``, which
        EmuHawk drops for the first frame run after the bot was idle.
        """
        n = self._ints(self.request(f"KEYS {log_key}"), 1, "KEYS")[0]
        self.log_key = log_key
        return n

    def domains(self) -> dict[str, int]:
        """Memory domain name -> size in bytes."""
        from urllib.parse import unquote
        out: dict[str, int] = {}
        for tok in self.request("DOMAINS"):
            name, _, size = tok.rpartition(":")
            try:
                out[unquote(name)] = int(size, 16)
            except ValueError:
                raise ProtocolError(f"bad DOMAINS entry {tok!r}") from None
        return out

    def speed(self, value: int | str = "max") -> None:
        """``'max'`` = unthrottled (restored when the bot quits), or a speed percent 1..6400."""
        self.request(f"SPEED {value}")

    def save_state(self) -> int:
        return self._ints(self.request("SAVE"), 1, "SAVE")[0]

    def load_state(self, state_id: int) -> int:
        """Load an in-memory savestate; returns the frame count."""
        return self._ints(self.request(f"LOAD {int(state_id)}"), 1, "LOAD")[0]

    def free_state(self, state_id: int) -> None:
        self.request(f"FREE {int(state_id)}")

    def framecount(self) -> int:
        return self._ints(self.request("FRAME"), 1, "FRAME")[0]

    def run(self, lines: Sequence[str], n_frames: int | None = None,
            timeout_s: float | None = None) -> tuple[int, int]:
        """Play ``lines`` from the current state (``n_frames`` > len holds the last line).

        Returns ``(framecount, lag frames during the run)``. ``timeout_s``
        overrides the reply timeout (long runs).
        """
        n = len(lines) if n_frames is None else n_frames
        if n < len(lines):
            raise ValueError(f"n_frames={n} is shorter than the {len(lines)} lines given")
        text = encode_lines(lines)
        fc, lag = self._ints(self.request(f"RUN {n} {text}".rstrip(), timeout_s), 2, "RUN")
        return fc, lag

    def read(self, reads: Sequence[ReadSpec]) -> list[int]:
        if not reads:
            return []
        toks = self.request("READ " + " ".join(r.token() for r in reads))
        if len(toks) != len(reads):
            raise ProtocolError(f"READ returned {len(toks)} value(s) for {len(reads)} read(s)")
        return [r.parse_value(t) for r, t in zip(reads, toks)]

    def evaluate(self, state_id: int, lines: Sequence[str], reads: Sequence[ReadSpec] = (),
                 until: Condition | None = None) -> EvalResult:
        """Load ``state_id``, play ``lines``, read ``reads`` (one round trip).

        With ``until``, playback stops as soon as the condition holds and
        ``EvalResult.hit`` is the number of frames it took (None if never).
        """
        head = [f"EVAL {int(state_id)}"] + [r.token() for r in reads]
        if until is not None:
            head.append(until.token())
        toks = self.request(" ".join(head) + " | " + encode_lines(lines))
        extra = 1 if until is not None else 0
        if len(toks) != 2 + extra + len(reads):
            raise ProtocolError(f"EVAL reply has {len(toks)} value(s), expected {2 + extra + len(reads)}")
        fc, lag = self._ints(toks, 2, "EVAL")
        hit = None
        if until is not None:
            h = self._ints(toks[2:], 1, "EVAL")[0]
            hit = None if h < 0 else h
        values = [r.parse_value(t) for r, t in zip(reads, toks[2 + extra:])]
        return EvalResult(fc, lag, values, hit)

    def branch(self, state_id: int, lines: Sequence[str]) -> int:
        """Load ``state_id``, play ``lines``, save the result as a new state; returns its id."""
        return self._ints(self.request(f"BRANCH {int(state_id)} | {encode_lines(lines)}"), 1, "BRANCH")[0]

    def seek(self, frame: int, timeout_s: float | None = None) -> tuple[int, int]:
        """Let BizHawk's loaded movie play up to ``frame``, then stop the movie.

        Returns ``(framecount, movie length)``. ``timeout_s`` overrides the
        reply timeout (seeking far into a long movie).
        """
        fc, length = self._ints(self.request(f"SEEK {int(frame)}", timeout_s), 2, "SEEK")
        return fc, length


def _set_abortive_close(sock: socket.socket) -> None:
    """Close with RST: EmuHawk's ReceiveString spins forever on a gracefully closed socket."""
    fmt = "HH" if sys.platform == "win32" else "ii"
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack(fmt, 1, 0))
    except OSError:
        pass


def _abort(sock: socket.socket) -> None:
    _set_abortive_close(sock)
    try:
        sock.close()
    except OSError:
        pass


# --- input lines ------------------------------------------------------------------------------------------

_OPPOSITE = {"Left": "Right", "Right": "Left", "Up": "Down", "Down": "Up"}


def _opposite(name: str) -> str | None:
    head, _, base = name.rpartition(" ")
    opp = _OPPOSITE.get(base)
    if opp is None:
        return None
    return f"{head} {opp}" if head else opp


# Neutral values of axes whose neutral isn't 0 (BizHawk ControllerDefinition.AddAxis):
# sub-frame "Input Length" is a whole frame (Gambatte.cs 35112, MGBAHawk CYCLES_PER_FRAME),
# Gambatte's "Remote Command" (HuC1/HuC3 IR) idles at 127.
AXIS_NEUTRAL: dict[str, dict[str, int]] = {
    "GB": {"Input Length": 35112, "Remote Command": 127},
    "GBA": {"Input Length": 280896},
}
_PLATFORM_AXES = {"GBC": "GB", "SGB": "GB"}


def axis_neutrals(platform: str) -> dict[str, int]:
    p = (platform or "").upper()
    return dict(AXIS_NEUTRAL.get(_PLATFORM_AXES.get(p, p), {}))


class LineCodec:
    """Build and inspect bk2 input-log lines (``|..R....A.|``) for one controller layout.

    ``axis_defaults`` gives the value of axes not specified when encoding
    (default 0); new frames get the axis's neutral value.
    """

    def __init__(self, groups: Sequence[Sequence[str]], axes: Iterable[str] = (),
                 axis_defaults: dict[str, int] | None = None) -> None:
        self.groups = [list(g) for g in groups]
        self.axes = set(axes)
        self.axis_defaults = dict(axis_defaults or {})
        self.buttons = [n for g in self.groups for n in g if n not in self.axes]
        self._lookup = {n.lower(): n for n in self.buttons}
        self._cache: dict[str, frozenset[str]] = {}
        self.neutral = self.encode(())

    @classmethod
    def for_system(cls, system: str) -> "LineCodec":
        if system not in bk2.SYSTEMS:
            raise ValueError(f"unknown system {system!r} (one of {', '.join(bk2.SYSTEMS)})")
        spec = bk2.SYSTEMS[system]
        return cls([spec["axes"] + spec["buttons"]], spec["axes"], axis_neutrals(spec["platform"]))

    @classmethod
    def from_movie(cls, movie: bk2.Bk2Movie) -> "LineCodec":
        known = set(bk2.GBA_AXES) | set(AXIS_NEUTRAL["GB"]) | set(AXIS_NEUTRAL["GBA"])
        axes = set(movie.axes) | {n for g in movie.groups for n in g if n in known}
        return cls(movie.groups, axes, axis_neutrals(movie.platform))

    @property
    def log_key(self) -> str:
        return "".join("#" + "".join(n + "|" for n in g) for g in self.groups)

    def encode(self, pressed: Iterable[str], axis_values: dict[str, int] | None = None) -> str:
        pressed = set(pressed)
        axis_values = axis_values or {}
        s = "|"
        for g in self.groups:
            for n in g:
                if n in self.axes:
                    s += str(int(axis_values.get(n, self.axis_defaults.get(n, 0)))).rjust(5) + ","
            for n in g:
                if n not in self.axes:
                    s += bk2.mnemonic(n) if n in pressed else "."
            s += "|"
        return s

    def pressed(self, line: str) -> frozenset[str]:
        got = self._cache.get(line)
        if got is None:
            fr, _ = bk2.parse_frame(line, self.groups)
            got = frozenset(k for k, v in fr.items() if v is True and k not in self.axes)
            if len(self._cache) > 100_000:
                self._cache.clear()
            self._cache[line] = got
        return got

    def axis_values(self, line: str) -> dict[str, int]:
        if not self.axes:
            return {}
        fr, _ = bk2.parse_frame(line, self.groups)
        return {k: int(v) for k, v in fr.items() if k in self.axes}

    def with_buttons(self, line: str, add: Iterable[str] = (), remove: Iterable[str] = ()) -> str:
        """``line`` with buttons added/removed (adding a direction releases its opposite)."""
        cur = set(self.pressed(line))
        cur.difference_update(remove)
        for b in add:
            opp = _opposite(b)
            if opp:
                cur.discard(opp)
            cur.add(b)
        return self.encode(cur, self.axis_values(line))

    def action(self, spec: str) -> frozenset[str]:
        """``''``/``'none'`` -> nothing; ``'Right+A'`` -> {'Right', 'A'} (case-insensitive)."""
        spec = spec.strip()
        if spec.lower() in ("", "none", ".", "neutral", "-"):
            return frozenset()
        out: set[str] = set()
        for part in spec.split("+"):
            name = self._lookup.get(part.strip().lower())
            if name is None:
                raise ValueError(f"unknown button {part.strip()!r} in action {spec!r} "
                                 f"(buttons: {', '.join(self.buttons)})")
            out.add(name)
        for b in out:
            if _opposite(b) in out:
                raise ValueError(f"action {spec!r} presses opposite directions")
        return frozenset(out)


# --- objectives ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Objective:
    """What to optimise, read from RAM after each candidate.

    * ``max READ`` / ``min READ`` - value at the end of the window;
    * ``until READ OP VALUE`` - minimise frames until the condition holds
      (candidates that never reach it rank below any that do, ordered by the
      tie-breakers and then by how close the value is).

    ``tiebreaks`` are ``(max|min, Read)`` pairs compared in order.
    """

    kind: str
    read: Read
    op: str = "=="
    value: int = 0
    tiebreaks: tuple[tuple[str, Read], ...] = ()

    @classmethod
    def parse(cls, spec: str, tiebreaks: Sequence[str] = ()) -> "Objective":
        kind, _, rest = spec.strip().partition(" ")
        kind = kind.lower()
        tbs = []
        for t in tiebreaks:
            k, _, r = t.strip().partition(" ")
            if k.lower() not in ("max", "min"):
                raise ValueError(f"bad tie-breaker {t!r}: expected 'max READ' or 'min READ'")
            tbs.append((k.lower(), Read.parse(r)))
        if kind in ("max", "min"):
            return cls(kind, Read.parse(rest), tiebreaks=tuple(tbs))
        if kind == "until":
            c = Condition.parse(rest)
            return cls("until", c.read, c.op, c.value, tuple(tbs))
        raise ValueError(f"bad objective {spec!r}: expected 'max READ', 'min READ' or "
                         "'until READ OP VALUE', e.g. 'max WRAM:0x1361:1' or 'until WRAM:0x135E:1==92'")

    @property
    def until(self) -> Condition | None:
        return Condition(self.read, self.op, self.value) if self.kind == "until" else None

    @property
    def reads(self) -> list[Read]:
        return [self.read] + [r for _, r in self.tiebreaks]

    def score(self, res: EvalResult) -> tuple:
        """Comparable score, higher is better."""
        n = len(self.tiebreaks)
        if len(res.values) < 1 + n:
            raise ValueError("EvalResult is missing objective values")
        tb = tuple(v if k == "max" else -v for (k, _), v in zip(self.tiebreaks, res.values[1:1 + n]))
        v = res.values[0]
        if self.kind == "max":
            return (v, *tb)
        if self.kind == "min":
            return (-v, *tb)
        if res.hit is not None:
            return (1, -res.hit, *tb)
        cond = self.until
        assert cond is not None
        return (0, *tb, cond.heuristic(v))

    def describe(self, res: EvalResult) -> str:
        if self.kind == "until":
            if res.hit is not None:
                return f"{self.until} after {res.hit} frame(s)"
            return f"{self.until} not reached ({self.read}={res.values[0]})"
        return f"{self.read}={res.values[0]}"

    def __str__(self) -> str:
        base = f"until {self.until}" if self.kind == "until" else f"{self.kind} {self.read}"
        return base + "".join(f", then {k} {r}" for k, r in self.tiebreaks)


# --- search -----------------------------------------------------------------------------------------------------

@dataclass
class SearchResult:
    method: str
    lines: list[str]
    score: tuple
    result: EvalResult
    baseline_score: tuple | None = None
    baseline: EvalResult | None = None
    evaluations: int = 0
    details: dict = field(default_factory=dict)

    @property
    def improved(self) -> bool:
        return self.baseline_score is None or self.score > self.baseline_score


class _Scorer:
    """Evaluates window candidates (+ fixed tail) with a result cache."""

    def __init__(self, ev: Evaluator, state_id: int, objective: Objective,
                 tail: Sequence[str] = (), extra_reads: Sequence[ReadSpec] = ()) -> None:
        self.ev, self.state_id, self.objective = ev, state_id, objective
        self.tail = list(tail)
        self.reads: list[ReadSpec] = list(objective.reads) + list(extra_reads)
        self.until = objective.until
        self.cache: dict[tuple[str, ...], tuple[tuple, EvalResult]] = {}
        self.evaluations = 0

    def __call__(self, lines: Sequence[str]) -> tuple[tuple, EvalResult]:
        key = tuple(lines)
        got = self.cache.get(key)
        if got is not None:
            return got
        res = self.ev.evaluate(self.state_id, list(lines) + self.tail, self.reads, self.until)
        self.evaluations += 1
        got = (self.objective.score(res), res)
        if len(self.cache) > 50_000:
            self.cache.clear()
        self.cache[key] = got
        return got


def find_presses(codec: LineCodec, lines: Sequence[str],
                 buttons: Iterable[str] | None = None) -> list[tuple[int, int, str]]:
    """Maximal runs ``(first, last, button)`` of held buttons, ordered by first frame."""
    allowed = set(buttons) if buttons is not None else {b for b in codec.buttons if b != "Power"}
    out: list[tuple[int, int, str]] = []
    open_: dict[str, int] = {}
    for i, line in enumerate(lines):
        p = codec.pressed(line)
        for b in list(open_):
            if b not in p:
                out.append((open_.pop(b), i - 1, b))
        for b in p:
            if b in allowed and b not in open_:
                open_[b] = i
    for b, s in open_.items():
        out.append((s, len(lines) - 1, b))
    out.sort(key=lambda t: (t[0], t[2]))
    return out


def shift_press(codec: LineCodec, lines: Sequence[str], first: int, last: int, button: str,
                delta: int) -> list[str] | None:
    """Move a press ``delta`` frames (keeping its length); None if it would leave the window."""
    n = len(lines)
    if first + delta < 0 or last + delta >= n:
        return None
    out = list(lines)
    for i in range(first, last + 1):
        out[i] = codec.with_buttons(out[i], remove=[button])
    for i in range(first + delta, last + delta + 1):
        out[i] = codec.with_buttons(out[i], add=[button])
    return out


def delay_search(ev: Evaluator, state_id: int, lines: Sequence[str], objective: Objective, *,
                 codec: LineCodec | None = None, max_shift: int = 5,
                 buttons: Iterable[str] | None = None, presses: Sequence[tuple[int, str]] | None = None,
                 tail: Sequence[str] = (), passes: int = 1,
                 progress: Progress | None = None) -> SearchResult:
    """Try every press in the window ``-max_shift..+max_shift`` frames earlier/later.

    Presses are handled one at a time in frame order (coordinate descent),
    each keeping the best shift found; ``passes`` repeats the sweep. Ties keep
    the smaller shift. ``presses`` restricts it to given ``(frame, button)``
    presses; ``buttons`` restricts which buttons are moved.
    """
    codec = codec or LineCodec.for_system("gb")
    scorer = _Scorer(ev, state_id, objective, tail)
    best = list(lines)
    best_sc, best_res = scorer(best)
    base_sc, base_res = best_sc, best_res
    shifts = sorted((d for d in range(-max_shift, max_shift + 1) if d), key=lambda d: (abs(d), d))
    moved: list[tuple[int, str, int]] = []
    fixed = list(presses) if presses is not None else None
    for pas in range(max(1, passes)):
        changed = False
        # The presses to visit this pass, as (a frame inside the press, button), fixed at the start
        # of the pass: moving one press must not make the sweep skip or revisit another.
        todo = fixed if fixed is not None else [(p[0], p[2]) for p in find_presses(codec, best, buttons)]
        for idx, (frame, button) in enumerate(todo):
            match = [p for p in find_presses(codec, best, [button]) if p[0] <= frame <= p[1]]
            if not match:
                continue
            first, last, button = match[0]
            pick = None
            for d in shifts:
                cand = shift_press(codec, best, first, last, button, d)
                if cand is None:
                    continue
                sc, res = scorer(cand)
                if sc > best_sc:
                    best_sc, best_res, pick = sc, res, (d, cand)
            if pick is not None:
                d, best = pick
                changed = True
                moved.append((first, button, d))
                if fixed is not None:
                    fixed[idx] = (frame + d, button)
                if progress:
                    progress(f"delay: {button}@{first} moved {d:+d} -> {objective.describe(best_res)}")
        if progress:
            progress(f"delay: pass {pas + 1} done, best {objective.describe(best_res)} "
                     f"({scorer.evaluations} evaluations)")
        if not changed:
            break
    return SearchResult("delay", best, best_sc, best_res, base_sc, base_res, scorer.evaluations,
                        {"moved": moved})


@dataclass
class _Node:
    lines: list[str]
    state: int | None
    score: tuple | None = None
    result: EvalResult | None = None
    lag: int = 0                 # lag frames along the path from the start state


def beam_search(ev: Evaluator, state_id: int, horizon: int, actions: Sequence[str], objective: Objective, *,
                codec: LineCodec | None = None, macro_len: int = 1, beam_width: int = 16,
                dedup: Sequence[ReadSpec] | None = None, baseline: Sequence[str] | None = None,
                progress: Progress | None = None) -> SearchResult:
    """Beam search over sequences of button combinations held for ``macro_len`` frames.

    Each step expands every beam node with every action (one ``EVAL`` from the
    node's savestate), keeps the ``beam_width`` best, dropping candidates whose
    ``dedup`` read values match a better one (default: the objective's reads;
    pass e.g. ``[RamHash("WRAM", 0, 0x2000)]`` for true state dedup, ``()`` to
    disable), and saves the survivors' states with ``branch``. For ``until``
    objectives it stops at the first step where a candidate reaches the goal
    and returns the lines up to that frame; for ``max``/``min`` it returns
    the best of the final beam (``horizon`` lines).
    """
    codec = codec or LineCodec.for_system("gb")
    if horizon <= 0 or macro_len <= 0 or beam_width <= 0:
        raise ValueError("horizon, macro_len and beam_width must be positive")
    acts: list[tuple[str, str]] = []
    seen_lines: set[str] = set()
    for a in actions:
        line = codec.encode(codec.action(a))
        if line not in seen_lines:
            seen_lines.add(line)
            acts.append((a, line))
    if not acts:
        raise ValueError("beam search needs at least one action")
    obj_reads = objective.reads
    dedup_reads: list[ReadSpec] = list(obj_reads) if dedup is None else list(dedup)
    reads: list[ReadSpec] = list(obj_reads) + list(dedup_reads)
    n_obj = len(obj_reads)
    until = objective.until
    base_sc = base_res = None
    evaluations = 0
    if baseline is not None:
        base_res = ev.evaluate(state_id, list(baseline), obj_reads, until)
        base_sc = objective.score(base_res)
        evaluations += 1
    beam = [_Node([], state_id)]
    pending: list[_Node] = []
    done = 0
    finished: tuple[tuple, list[str], EvalResult] | None = None
    try:
        while done < horizon:
            m = min(macro_len, horizon - done)
            cands: list[tuple[tuple, int, _Node, list[str], EvalResult, tuple | None]] = []
            order = 0
            for node in beam:
                assert node.state is not None
                for _, line in acts:
                    macro = [line] * m
                    res = ev.evaluate(node.state, macro, reads, until)
                    evaluations += 1
                    key = tuple(res.values[n_obj:]) if dedup_reads else None
                    res = dataclasses.replace(res, values=res.values[:n_obj], lag=node.lag + res.lag,
                                              hit=None if res.hit is None else done + res.hit)
                    cands.append((objective.score(res), order, node, macro, res, key))
                    order += 1
            done += m
            if until is not None:
                hits = [c for c in cands if c[4].hit is not None]
                if hits:
                    sc, _, node, macro, res, _ = max(hits, key=lambda c: (c[0], -c[1]))
                    assert res.hit is not None
                    finished = (sc, (node.lines + macro)[:res.hit], res)
                    if progress:
                        progress(f"beam: goal reached at frame {res.hit}")
                    break
            cands.sort(key=lambda c: (c[0], -c[1]), reverse=True)
            chosen = []
            keys: set[tuple] = set()
            for c in cands:
                if c[5] is not None:
                    if c[5] in keys:
                        continue
                    keys.add(c[5])
                chosen.append(c)
                if len(chosen) >= beam_width:
                    break
            pending = []                 # freed by the finally block if a branch() fails midway
            for sc, _, node, macro, res, _ in chosen:
                st = ev.branch(node.state, macro) if done < horizon else None
                pending.append(_Node(node.lines + macro, st, sc, res, res.lag))
            old, beam, pending = beam, pending, []
            for node in old:
                if node.state is not None and node.state != state_id:
                    ev.free_state(node.state)
                node.state = None
            if progress:
                assert beam[0].result is not None
                progress(f"beam: frame {done}/{horizon}: best {objective.describe(beam[0].result)} "
                         f"({len(cands)} candidates, kept {len(beam)})")
    finally:
        for node in beam + pending:
            if node.state is not None and node.state != state_id:
                try:
                    ev.free_state(node.state)
                except BridgeError:
                    pass
                node.state = None
    if finished is not None:
        sc, lines, res = finished
    else:
        top = beam[0]
        assert top.score is not None and top.result is not None
        sc, lines, res = top.score, top.lines, top.result
    return SearchResult("beam", list(lines), sc, res, base_sc, base_res, evaluations)


def _mutate(rng: random.Random, codec: LineCodec, lines: list[str], buttons: Sequence[str],
            ops: Sequence[str]) -> list[str]:
    n = len(lines)
    out = list(lines)
    op = rng.choice(ops)
    if op == "shift":                       # move one boundary of a hold by one frame
        runs = find_presses(codec, out, buttons)
        if runs:
            first, last, b = rng.choice(runs)
            grow = rng.random() < 0.5
            if rng.random() < 0.5:
                i = first - 1 if grow else first
            else:
                i = last + 1 if grow else last
            if 0 <= i < n:
                out[i] = (codec.with_buttons(out[i], add=[b]) if grow
                          else codec.with_buttons(out[i], remove=[b]))
            return out
        op = "flip"
    if op == "insert":
        i = rng.randrange(n)
        new = out[i - 1] if i > 0 and rng.random() < 0.5 else codec.neutral
        out.insert(i, new)
        out.pop()
        return out
    if op == "delete":
        i = rng.randrange(n)
        del out[i]
        out.append(out[-1] if out else codec.neutral)
        return out
    i = rng.randrange(n)
    b = rng.choice(list(buttons))
    if b in codec.pressed(out[i]):
        out[i] = codec.with_buttons(out[i], remove=[b])
    else:
        out[i] = codec.with_buttons(out[i], add=[b])
    return out


def random_mutation(ev: Evaluator, state_id: int, lines: Sequence[str], objective: Objective, *,
                    codec: LineCodec | None = None, iterations: int = 500, seed: int | None = 0,
                    buttons: Iterable[str] | None = None, tail: Sequence[str] = (),
                    ops: Sequence[str] = ("flip", "shift", "insert", "delete"),
                    progress: Progress | None = None) -> SearchResult:
    """Keep-best hill climbing (like BizHawk's Basic Bot, but mutating the best attempt).

    Each iteration applies one mutation - flip a button on one frame, move a
    hold boundary by one frame, insert a frame or delete a frame (the window
    keeps its length) - and accepts it if the score is not worse. The same
    ``seed`` and a deterministic core give the same result.
    """
    codec = codec or LineCodec.for_system("gb")
    rng = random.Random(seed)
    for op in ops:
        if op not in ("flip", "shift", "insert", "delete"):
            raise ValueError(f"unknown mutation {op!r}")
    cur = list(lines)
    if buttons is None:
        used = sorted({b for l in cur for b in codec.pressed(l)} - {"Power"})
        btns = used or [b for b in codec.buttons if b != "Power"]
    else:
        btns = list(buttons)
        for b in btns:
            if b not in codec.buttons:
                raise ValueError(f"unknown button {b!r}")
    scorer = _Scorer(ev, state_id, objective, tail)
    cur_sc, cur_res = scorer(cur)
    base_sc, base_res = cur_sc, cur_res
    accepted = improvements = 0
    if not cur or not btns:
        return SearchResult("mutate", cur, cur_sc, cur_res, base_sc, base_res, scorer.evaluations)
    report_every = max(1, iterations // 10)
    for it in range(1, iterations + 1):
        cand = _mutate(rng, codec, cur, btns, ops)
        if cand != cur:
            sc, res = scorer(cand)
            if sc >= cur_sc:
                if sc > cur_sc:
                    improvements += 1
                    if progress:
                        progress(f"mutate: iteration {it}: {objective.describe(res)}")
                cur, cur_sc, cur_res = cand, sc, res
                accepted += 1
        if progress and it % report_every == 0:
            progress(f"mutate: {it}/{iterations}, best {objective.describe(cur_res)}, "
                     f"accepted {accepted}, {scorer.evaluations} evaluations")
    return SearchResult("mutate", cur, cur_sc, cur_res, base_sc, base_res, scorer.evaluations,
                        {"accepted": accepted, "improvements": improvements, "seed": seed})


# --- movies ---------------------------------------------------------------------------------------------

PLATFORM_SYSTEM_IDS = {"GB": {"GB", "GBC"}, "GBC": {"GB", "GBC"}, "SGB": {"SGB"}, "GBA": {"GBA"}}


def movie_lines(movie: bk2.Bk2Movie) -> list[str]:
    return movie.frame_lines()


def window_lines(movie: bk2.Bk2Movie, start: int, count: int, codec: LineCodec | None = None) -> list[str]:
    """Input lines ``start .. start+count-1`` of ``movie`` (neutral past its end)."""
    if start < 0 or count < 0:
        raise ValueError("start and count must be >= 0")
    codec = codec or LineCodec.from_movie(movie)
    lines = movie.frame_lines()[start:start + count]
    return lines + [codec.neutral] * (count - len(lines))


def splice_lines(movie: bk2.Bk2Movie, start: int, old_count: int, new_lines: Sequence[str],
                 note: str | None = None, rerecords: int = 0) -> bk2.Bk2Movie:
    """Copy of ``movie`` with frames ``start .. start+old_count-1`` replaced by ``new_lines``.

    The ``CycleCount`` header (running time, only valid for the input it was
    measured on) is dropped, as BizHawk does; it is rewritten when the movie
    is played to its end and saved.
    """
    if start < 0 or old_count < 0:
        raise ValueError("start and old_count must be >= 0")
    codec = LineCodec.from_movie(movie)
    frames = list(movie.frames)
    if start > len(frames):
        neutral, _ = bk2.parse_frame(codec.neutral, movie.groups)
        frames.extend(dict(neutral) for _ in range(start - len(frames)))
    axes = set(movie.axes)
    new_frames = []
    for line in new_lines:
        fr, ax = bk2.parse_frame(line, movie.groups)
        new_frames.append(fr)
        axes |= ax
    frames[start:start + old_count] = new_frames
    header = dict(movie.header)
    header.pop("CycleCount", None)
    if rerecords:
        try:
            header["rerecordCount"] = str(int(header.get("rerecordCount", "0") or 0) + rerecords)
        except ValueError:
            header["rerecordCount"] = str(rerecords)
    comments = list(movie.comments)
    if note:
        comments.append(note)
    return dataclasses.replace(movie, header=header, frames=frames, comments=comments, axes=axes,
                               subtitles=list(movie.subtitles), groups=[list(g) for g in movie.groups])


# --- CLI ------------------------------------------------------------------------------------------------

DEFAULT_ACTIONS = ["", "A", "B", "Up", "Down", "Left", "Right", "Right+A", "Left+A"]
# 'until' splicing: how many movie frames past the window to search for the old input's goal frame
UNTIL_LOOKAHEAD = 3600


def default_dedup(domains: dict[str, int]) -> list[RamHash]:
    """RAM hashes that identify the game state well enough for beam dedup."""
    for names in (("WRAM", "HRAM"), ("IWRAM", "EWRAM"), ("RAM",), ("Main RAM",)):
        present = [n for n in names if n in domains]
        if present:
            return [RamHash(n, 0, domains[n]) for n in present]
    return []


def check_reads(reads: Iterable[ReadSpec], domains: dict[str, int]) -> list[ReadSpec]:
    """Validate reads against BizHawk's domains; resolves whole-domain hashes. Raises ValueError."""
    out: list[ReadSpec] = []
    for r in reads:
        if r.domain not in domains:
            raise ValueError(f"memory domain {r.domain!r} doesn't exist in this core; "
                             f"available: {', '.join(domains)}")
        size = domains[r.domain]
        if isinstance(r, RamHash) and r.length <= 0:
            r = RamHash(r.domain, 0, size)
        n = r.length
        if r.addr + n > size:
            hint = ""
            bus = {"WRAM": (0xC000, 0xE000), "HRAM": (0xFF80, 0xFFFF),
                   "IWRAM": (0x03000000, 0x03008000), "EWRAM": (0x02000000, 0x02040000)}.get(r.domain)
            if bus and bus[0] <= r.addr < bus[1]:
                hint = (f"; 0x{r.addr:X} looks like a bus address: domain offsets start at 0, so use "
                        f"{r.domain}:0x{r.addr - bus[0]:X} or 'System Bus:0x{r.addr:X}'")
            raise ValueError(f"{r} is outside {r.domain} (size 0x{size:X}){hint}")
        out.append(r)
    return out


def _port_arg(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a port number: {text!r}") from None
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"port must be 0-65535, got {text}")
    return port


def add_cli(subparsers) -> None:
    """Register ``controllerlog optimize``."""
    sp = subparsers.add_parser(
        "optimize", help="search a .bk2 window for better inputs, evaluated in BizHawk (Lua bot + socket)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Optimise the inputs of a window of a BizHawk .bk2 movie by local search. Every candidate is\n"
            "played in BizHawk itself (same core as the movie), scored by a RAM value you choose, and the\n"
            "best input is spliced back into a new .bk2.\n\n"
            "Run this first, then start EmuHawk with the command line it prints:\n"
            "  EmuHawk.exe --socket_ip=127.0.0.1 --socket_port=43880 --movie=in.bk2 \\\n"
            "              --lua=tools/bizhawk/controllerlog_bot.lua \"path\\to\\rom.gbc\"\n\n"
            "Objectives: 'max READ', 'min READ', 'until READ OP VALUE' (fewest frames until true).\n"
            "READ is DOMAIN:ADDR[:SIZE[:ENDIAN]], e.g. WRAM:0x1361:1, 'System Bus:0xFF44:1', IWRAM:0x10:s2.\n"
            "Domain offsets start at 0 (GB WRAM:0x0 = bus 0xC000). Find addresses in RAM maps\n"
            "(e.g. TASVideos game resources, pret disassemblies) or with BizHawk's RAM Search."),
        epilog=("This is local search over a window, not whole-game optimal solving. Results are only\n"
                "guaranteed to sync on the same core and sync settings they were found on."))
    sp.add_argument("--movie", required=True, help="input .bk2")
    sp.add_argument("--out", required=True, help="output .bk2 (must differ from --movie)")
    sp.add_argument("--start", type=int, required=True,
                    help="first frame of the window (the movie is played up to here, then saved)")
    sp.add_argument("--window", type=int, required=True, help="number of frames to optimise")
    sp.add_argument("--method", choices=("beam", "delay", "mutate"), default="beam")
    sp.add_argument("--objective", required=True,
                    help="e.g. 'max WRAM:0x1361:1', 'min System Bus:0xFF44:1', 'until WRAM:0x135E:1==92'")
    sp.add_argument("--tiebreak", action="append", default=[], metavar="'max|min READ'",
                    help="secondary objective, repeatable (strongly recommended for 'until' + beam)")
    sp.add_argument("--actions", nargs="+", default=None, metavar="ACTION",
                    help="beam: button combinations, '' = nothing, e.g. '' A Right Right+A "
                         f"(default: {' '.join(repr(a) for a in DEFAULT_ACTIONS)})")
    sp.add_argument("--beam-width", type=int, default=32)
    sp.add_argument("--macro-len", type=int, default=2, help="beam: frames each action is held (default 2)")
    sp.add_argument("--dedup", action="append", default=[], metavar="READ",
                    help="beam: state key for dropping duplicates, repeatable (READ or hash:DOMAIN[:ADDR:LEN]); "
                         "default: hash of WRAM+HRAM (GB) / IWRAM+EWRAM (GBA)")
    sp.add_argument("--iterations", type=int, default=500, help="mutate: iterations (default 500)")
    sp.add_argument("--seed", type=int, default=0, help="mutate: RNG seed (default 0)")
    sp.add_argument("--max-shift", type=int, default=6, help="delay: frames to try earlier/later (default 6)")
    sp.add_argument("--passes", type=int, default=2, help="delay: sweeps over all presses (default 2)")
    sp.add_argument("--buttons", default=None,
                    help="delay/mutate: comma list of buttons to move/flip (default: those used in the window)")
    sp.add_argument("--tail", type=int, default=0,
                    help="delay/mutate: also play this many original frames after the window when scoring")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=_port_arg, default=DEFAULT_PORT, help=f"socket port (default {DEFAULT_PORT})")
    sp.add_argument("--connect-timeout", type=float, default=600.0, help="seconds to wait for BizHawk")
    sp.add_argument("--reply-timeout", type=float, default=300.0, help="seconds to wait for one command")
    sp.add_argument("--speed", default="max", help="emulation speed while searching: max or a percent")
    sp.add_argument("--emuhawk", default=None, help="path to EmuHawk.exe (for the printed/launched command)")
    sp.add_argument("--rom", default=None, help="ROM path (for the printed/launched command)")
    sp.add_argument("--launch", action="store_true", help="start EmuHawk yourself (needs --emuhawk and --rom)")
    sp.set_defaults(func=cmd_optimize)


def _say(msg: str = "") -> None:
    print(msg, flush=True)


def cmd_optimize(args: argparse.Namespace) -> int:
    movie_path, out_path = Path(args.movie), Path(args.out)
    if out_path.resolve() == movie_path.resolve():
        raise ValueError("--out must be a different file from --movie")
    if args.start < 0 or args.window <= 0:
        raise ValueError("--start must be >= 0 and --window > 0")
    # check everything that doesn't need BizHawk before the user is asked to start it
    for name, v, lo in (("--beam-width", args.beam_width, 1), ("--macro-len", args.macro_len, 1),
                        ("--iterations", args.iterations, 0), ("--max-shift", args.max_shift, 0),
                        ("--passes", args.passes, 1)):
        if v < lo:
            raise ValueError(f"{name} must be >= {lo}")
    speed = str(args.speed).strip().lower()
    if speed != "max" and not (speed.isdigit() and 1 <= int(speed) <= 6400):
        raise ValueError("--speed must be 'max' or a percent from 1 to 6400")
    movie = bk2.read_bk2(movie_path)
    if movie.header.get("StartsFromSavestate", "").strip().lower() == "true":
        raise ValueError(f"{movie_path.name} starts from a savestate; only power-on (or save-RAM) movies are "
                         "supported, because the .bk2 writer doesn't carry the anchor savestate (Core.bin)")
    codec = LineCodec.from_movie(movie)
    lines = movie.frame_lines()
    if args.start > len(lines):
        raise ValueError(f"--start {args.start} is past the end of the movie ({len(lines)} frames)")
    objective = Objective.parse(args.objective, args.tiebreak)
    buttons = None
    if args.buttons:
        buttons = [codec.action(b) for b in args.buttons.split(",")]
        buttons = sorted({b for s in buttons for b in s})
    actions = args.actions if args.actions is not None else DEFAULT_ACTIONS
    if args.method == "beam":
        for a in actions:
            codec.action(a)
        if objective.kind == "until" and not objective.tiebreaks:
            _say("note: beam search with an 'until' objective ranks unfinished candidates only by how close "
                 "the watched value is; add --tiebreak 'max READ' (e.g. a position) to guide it")
    dedup: list[ReadSpec] = [parse_read_spec(d) for d in args.dedup]
    orig = window_lines(movie, args.start, args.window, codec)
    # --tail: original frames after the window, played when scoring delay/mutate candidates
    end = args.start + args.window
    tail = [] if args.method == "beam" else lines[end:end + max(0, args.tail)]
    if args.launch and not (args.emuhawk and args.rom):
        raise ValueError("--launch needs --emuhawk and --rom")

    bridge = BizHawkBridge(args.host, args.port, timeout_s=args.reply_timeout)
    try:
        text = bridge.instructions(movie=movie_path, rom=args.rom, emuhawk=args.emuhawk)
        _say(text)
        if args.launch:
            import subprocess
            subprocess.Popen(bizhawk_command(bridge.host, bridge.port, movie=movie_path, rom=args.rom,
                                             emuhawk=args.emuhawk), cwd=Path(args.emuhawk).resolve().parent)
        hello = bridge.wait_for_bizhawk(args.connect_timeout, instructions=text)
        _say(f"BizHawk connected: system {hello.system}, frame {hello.framecount}, movie {hello.movie_mode}")
        if hello.proto != PROTO_VERSION:
            _say(f"warning: bot protocol {hello.proto}, expected {PROTO_VERSION}; update controllerlog_bot.lua")
        plat = movie.platform.upper()
        want = PLATFORM_SYSTEM_IDS.get(plat)
        if want is not None and hello.system not in want:
            raise ValueError(f"BizHawk runs a {hello.system} core but the movie is for {plat}; "
                             "load the ROM the movie was made for")
        if want is None:
            _say(f"warning: platform {plat or '?'} is untested with the optimizer")
        bridge.set_keys(codec.log_key)
        domains = bridge.domains()
        check_reads(objective.reads, domains)
        if args.method == "beam":
            dedup = check_reads(dedup, domains) if dedup else default_dedup(domains)
        bridge.speed(speed)

        # EmuHawk starts a --lua script only after its first main-loop iteration, which already
        # emulates a frame, so HELLO usually reports frame 1 (or more), not 0.
        k = hello.framecount
        # replaying the prefix may take a while: allow at least 100 frames/s on top of --reply-timeout
        prefix_timeout = args.reply_timeout + max(0, args.start - k) / 100
        if hello.movie_mode == "PLAY":
            if k > args.start:
                raise ValueError(
                    f"BizHawk's movie is already at frame {k}, past --start {args.start} (EmuHawk emulates a "
                    f"frame or so before the Lua script starts); use --start {k} or later")
            fc, length = bridge.seek(args.start, timeout_s=prefix_timeout)
            if length != len(lines):
                _say(f"warning: the movie loaded in BizHawk has {length} frames, {movie_path.name} has "
                     f"{len(lines)}; is it the same movie?")
        elif hello.movie_mode == "INACTIVE":
            pressed = [i for i, line in enumerate(lines[:k]) if codec.pressed(line)]
            if k > args.start or pressed:
                raise ValueError(
                    f"BizHawk is at frame {k} and no movie is loaded, so the movie can't be replayed from "
                    "power-on" + (f" (its frame {pressed[0]} has input BizHawk didn't get)" if pressed else "")
                    + ". Start EmuHawk with --movie (recommended)")
            _say("note: no movie loaded in BizHawk; replaying the movie's input with your current core "
                 "settings. If they differ from the movie's sync settings the result won't sync; "
                 "prefer starting EmuHawk with --movie")
            if k:
                _say(f"note: BizHawk already ran {k} frame(s) with no input; the movie's first {k} frame(s) "
                     "have no input either, so replaying from there (don't touch the controls)")
            fc, _ = (bridge.run(lines[k:args.start], timeout_s=prefix_timeout) if args.start > k
                     else (bridge.framecount(), 0))
        else:
            raise ValueError(f"the movie in BizHawk is in {hello.movie_mode} mode; start EmuHawk with "
                             "--movie (read-only playback) or stop the movie")
        if fc != args.start:
            raise BridgeError(f"expected to be at frame {args.start}, BizHawk is at {fc}")
        state = bridge.save_state()
        _say(f"saved state at frame {args.start}; optimising frames {args.start}-"
             f"{args.start + args.window - 1} with {args.method}, objective: {objective}")

        before = bridge.evaluate(state, orig + tail, objective.reads, objective.until)
        before_sc = objective.score(before)
        _say(f"before: {objective.describe(before)}")
        t0 = time.monotonic()
        prog = lambda s: _say("  " + s)  # noqa: E731
        if args.method == "delay":
            res = delay_search(bridge, state, orig, objective, codec=codec, max_shift=args.max_shift,
                               buttons=buttons, tail=tail, passes=args.passes, progress=prog)
        elif args.method == "mutate":
            res = random_mutation(bridge, state, orig, objective, codec=codec, iterations=args.iterations,
                                  seed=args.seed, buttons=buttons, tail=tail, progress=prog)
        else:
            res = beam_search(bridge, state, args.window, actions, objective, codec=codec,
                              macro_len=args.macro_len, beam_width=args.beam_width, dedup=dedup,
                              baseline=orig, progress=prog)
        dt = time.monotonic() - t0
        _say(f"search done: {res.evaluations} evaluations in {dt:.1f}s")
        if not res.score > before_sc:
            _say(f"after:  {objective.describe(res.result)}")
            _say("no improvement found; nothing written")
            return 0

        until = objective.until
        full = res.lines + tail
        old_hit = before.hit
        if until is not None and old_hit is None and res.result.hit is not None:
            # The old input doesn't reach the goal inside the window (+tail): find where it does,
            # further into the movie, so the splice can cut the saved frames instead of
            # leaving pre-goal input behind post-goal state.
            ahead = lines[args.start:end + len(tail) + UNTIL_LOOKAHEAD]
            if len(ahead) > len(orig) + len(tail):
                far = bridge.evaluate(state, ahead, objective.reads, until)
                old_hit = far.hit
                if old_hit is not None:
                    _say(f"the old input reaches the goal after {old_hit} frame(s), past the window")
        if until is not None and old_hit is not None and res.result.hit is not None:
            seg, old = full[:res.result.hit], old_hit
            check_lines = seg
        else:
            seg = res.lines[:args.window] + [codec.neutral] * (args.window - len(res.lines))
            old = args.window
            check_lines = seg + tail
            if len(res.lines) < args.window:
                _say(f"note: the goal is reached after {len(res.lines)} frame(s); the rest of the window is "
                     "filled with neutral input")
        after = bridge.evaluate(state, check_lines, objective.reads, until)
        _say(f"after:  {objective.describe(after)} (re-verified from the savestate)")
        if objective.score(after) != res.score and not (until is not None and after.hit == res.result.hit):
            _say("warning: re-verification scored differently from the search; the core may not be "
                 "deterministic from in-memory states (e.g. RTC, uninitialised RAM settings)")
        note = (f"{OPTIMIZED_TAG}: frames {args.start}-{args.start + old - 1} -> {len(seg)} frame(s), "
                f"{res.method}, {objective}: {objective.describe(before)} -> {objective.describe(after)}")
        out = splice_lines(movie, args.start, old, seg, note=note, rerecords=res.evaluations)
        bk2.write_bk2(out_path, out)
        if old != len(seg):
            _say(f"saved {old - len(seg)} frame(s): the movie is now {len(out.frames)} frames; the original "
                 "input after the goal follows immediately")
        else:
            _say("frames after the window are unchanged; if the window's end state changed, check the rest "
                 "of the movie for desyncs (e.g. in TAStudio)")
        _say(f"wrote {out_path}")
        return 0
    finally:
        bridge.close()
