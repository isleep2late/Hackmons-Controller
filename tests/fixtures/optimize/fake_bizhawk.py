"""A fake EmuHawk for testing :mod:`controllerlog.optimize` without BizHawk.

* :class:`ToyGame` - a tiny deterministic "Game Boy game" with GB-like memory
  domains (``WRAM``, ``HRAM``, ``System Bus``), lag frames and an RNG.
* :class:`ToyEvaluator` - an in-process :class:`controllerlog.optimize.Evaluator`
  backed by the toy game (no sockets), for unit-testing the search algorithms.
* :class:`FakeBizHawk` - a pure-Python implementation of the
  ``controllerlog_bot.lua`` wire protocol, backed by the toy game. It can
  answer messages directly (:meth:`FakeBizHawk.handle`) or connect as a TCP
  client to a :class:`controllerlog.optimize.BizHawkBridge`, exactly like
  EmuHawk started with ``--socket_ip/--socket_port``.

Toy game rules (one call to :meth:`ToyGame.step` = one emulated frame):

* frames with ``frame % 8 == 7`` are lag frames: input is not polled and
  nothing moves (only the RNG and the frame counter advance);
* holding Right accelerates (speed 1, then 2 px/frame); Left walks back 1 px;
* a new A press (A not held on the previous polled frame) starts a 6-frame
  jump; airborne you move 1 px/frame while Right is held;
* a wall at x=20 blocks you unless you cross it airborne;
* the goal flag is set once x >= 40;
* a door opens only if A is newly pressed on a frame whose RNG value has
  ``rng & 0x1F == DOOR_MAGIC`` (one frame in 32).
"""

from __future__ import annotations

import hashlib
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable, Sequence
from urllib.parse import unquote

from controllerlog.formats.bk2 import GB_BUTTONS, parse_frame, parse_log_key
from controllerlog.optimize import (EvalResult, FrameDecoder, RamHash, Read, Condition,
                                    encode_message, expand_lines)

# WRAM layout
X = 0x00          # u16 LE
V = 0x02          # speed 0..2
AIR = 0x03        # jump frames left
GOAL = 0x04       # 1 once x >= GOAL_X
DOOR = 0x05       # 1 once opened
RNG = 0x06        # advances every frame
PREV_A = 0x07     # A held on the previous polled frame
FRAME16 = 0x08    # u16 BE frame counter
SX = 0x0A         # s8: signed copy of (x - 20), for signed-read tests

WALL_X = 20
GOAL_X = 40
JUMP_LEN = 6
DOOR_MAGIC = 0x0B
RNG_SEED = 0x2A

WRAM_SIZE = 0x2000
HRAM_SIZE = 0x7F
DOMAINS = {"WRAM": WRAM_SIZE, "HRAM": HRAM_SIZE, "System Bus": 0x10000}
GB_LOG_KEY = "#" + "".join(b + "|" for b in GB_BUTTONS)


class ToyGame:
    def __init__(self) -> None:
        self.wram = bytearray(WRAM_SIZE)
        self.hram = bytearray(HRAM_SIZE)
        self.frame = 0
        self.lagcount = 0
        self.is_lag = False
        self.reset()

    def reset(self) -> None:
        self.wram[:] = bytes(WRAM_SIZE)
        self.hram[:] = bytes(HRAM_SIZE)
        self.wram[RNG] = RNG_SEED
        self._store_frame()

    # -- state ------------------------------------------------------------------
    def snapshot(self) -> tuple:
        return (bytes(self.wram), bytes(self.hram), self.frame, self.lagcount, self.is_lag)

    def restore(self, snap: tuple) -> None:
        w, h, self.frame, self.lagcount, self.is_lag = snap
        self.wram[:] = w
        self.hram[:] = h

    @property
    def x(self) -> int:
        return self.wram[X] | self.wram[X + 1] << 8

    def _store_frame(self) -> None:
        f = self.frame & 0xFFFF
        self.wram[FRAME16] = f >> 8
        self.wram[FRAME16 + 1] = f & 0xFF

    # -- emulation ----------------------------------------------------------------
    def step(self, pressed: Iterable[str]) -> bool:
        """Emulate one frame with ``pressed`` bk2 button names held. Returns is-lag."""
        pressed = set(pressed)
        if "Power" in pressed:
            self.reset()
        w = self.wram
        rng = w[RNG]
        lag = self.frame % 8 == 7
        if not lag:
            a = "A" in pressed
            a_new = a and not w[PREV_A]
            w[PREV_A] = 1 if a else 0
            if a_new and (rng & 0x1F) == DOOR_MAGIC:
                w[DOOR] = 1
            x, v, air = self.x, w[V], w[AIR]
            if a_new and air == 0:
                air = JUMP_LEN
            if air > 0:
                dx = 1 if "Right" in pressed else (-1 if "Left" in pressed and x > 0 else 0)
                air -= 1
                v = 0
                nx = x + dx
            else:
                if "Right" in pressed:
                    v = min(v + 1, 2)
                    dx = v
                elif "Left" in pressed:
                    v, dx = 0, (-1 if x > 0 else 0)
                else:
                    v, dx = 0, 0
                nx = x + dx
                if x < WALL_X <= nx:        # grounded: the wall stops you
                    nx, v = WALL_X - 1, 0
            w[X], w[X + 1] = nx & 0xFF, nx >> 8
            w[V], w[AIR] = v, air
            if nx >= GOAL_X:
                w[GOAL] = 1
            w[SX] = (nx - 20) & 0xFF
        w[RNG] = (rng * 5 + 1) & 0xFF
        self.frame += 1
        self._store_frame()
        self.is_lag = lag
        if lag:
            self.lagcount += 1
        return lag

    # -- memory ---------------------------------------------------------------------
    def peek(self, domain: str, addr: int) -> int:
        if domain == "WRAM":
            return self.wram[addr]
        if domain == "HRAM":
            return self.hram[addr]
        if domain == "System Bus":
            if 0xC000 <= addr < 0xE000:
                return self.wram[addr - 0xC000]
            if 0xFF80 <= addr < 0xFFFF:
                return self.hram[addr - 0xFF80]
            return 0
        raise KeyError(domain)

    def read(self, domain: str, addr: int, size: int = 1, endian: str = "le",
             signed: bool = False) -> int:
        bs = [self.peek(domain, addr + i) for i in range(size)]
        if endian == "be":
            bs.reverse()
        v = sum(b << (8 * i) for i, b in enumerate(bs))
        if signed and v >= 1 << (8 * size - 1):
            v -= 1 << (8 * size)
        return v

    def hash_region(self, domain: str, addr: int, length: int) -> str:
        data = bytes(self.peek(domain, addr + i) for i in range(length))
        return hashlib.sha256(data).hexdigest().upper()

    def read_spec(self, r: Read | RamHash) -> int:
        if isinstance(r, RamHash):
            return int(self.hash_region(r.domain, r.addr, r.length)[:16], 16)
        return self.read(r.domain, r.addr, r.size, r.endian, r.signed)


def door_frames(limit: int = 256) -> list[int]:
    """Frames (0-based, from power-on) on which a new A press opens the door."""
    out, rng = [], RNG_SEED
    for f in range(limit):
        if (rng & 0x1F) == DOOR_MAGIC and f % 8 != 7:
            out.append(f)
        rng = (rng * 5 + 1) & 0xFF
    return out


def pressed_from_line(line: str, groups: list[list[str]] | None = None) -> set[str]:
    fr, _ = parse_frame(line, groups or [list(GB_BUTTONS)])
    return {k for k, v in fr.items() if v is True}


class ToyEvaluator:
    """In-process Evaluator over a :class:`ToyGame` (what BizHawk + the bot do, minus sockets)."""

    def __init__(self, game: ToyGame | None = None) -> None:
        self.game = game or ToyGame()
        self.states: dict[int, tuple] = {}
        self._next = 1
        self.evaluations = 0
        self.frames_run = 0

    def save_state(self) -> int:
        sid = self._next
        self._next += 1
        self.states[sid] = self.game.snapshot()
        return sid

    def run(self, lines: Sequence[str]) -> tuple[int, int]:
        lag = 0
        for line in lines:
            lag += self.game.step(pressed_from_line(line))
        return self.game.frame, lag

    def evaluate(self, state_id: int, lines: Sequence[str], reads: Sequence[Read | RamHash] = (),
                 until: Condition | None = None) -> EvalResult:
        self.evaluations += 1
        self.game.restore(self.states[state_id])
        lag, hit = 0, None
        if until is not None and until.test(self.game.read_spec(until.read)):
            hit = 0
        for i, line in enumerate(lines, 1):
            if hit is not None:
                break
            lag += self.game.step(pressed_from_line(line))
            self.frames_run += 1
            if until is not None and until.test(self.game.read_spec(until.read)):
                hit = i
        return EvalResult(self.game.frame, lag, [self.game.read_spec(r) for r in reads],
                          hit if until is not None else None)

    def branch(self, state_id: int, lines: Sequence[str]) -> int:
        self.game.restore(self.states[state_id])
        self.run(lines)
        self.frames_run += len(lines)
        return self.save_state()

    def free_state(self, state_id: int) -> None:
        self.states.pop(state_id, None)


# --- the wire protocol, in Python -------------------------------------------------------

class _Err(Exception):
    pass


_SIGNED_LIMIT = {1: 128, 2: 32768, 4: 2147483648}


@dataclass
class _WireRead:
    domain: str
    addr: int
    size: int = 1
    signed: bool = False
    be: bool = False
    hash_len: int = 0


@dataclass
class FakeBizHawk:
    """Reference implementation of controllerlog_bot.lua's protocol over a ToyGame.

    ``movie_lines``: if given, a movie is "loaded" in PLAY mode (like
    ``EmuHawk --movie``) so ``SEEK`` can be tested.
    ``chunk``: when serving over TCP, send every outgoing message in pieces of
    this many bytes (with a tiny pause) to exercise partial reads.
    ``silent_first``: open one extra connection that never speaks before the
    real one, like EmuHawk's startup connection followed by the script's
    reconnect.
    ``pre_frames``: frames emulated before the script says HELLO (EmuHawk runs
    a main-loop iteration, and so a frame, before it starts a ``--lua``
    script): movie input in PLAY mode, no input otherwise.
    """

    game: ToyGame = field(default_factory=ToyGame)
    system_id: str = "GB"
    movie_lines: list[str] | None = None
    chunk: int = 0
    silent_first: bool = False
    pre_frames: int = 0
    states: dict[int, tuple] = field(default_factory=dict)
    next_state: int = 1
    key_groups: list[list[str]] | None = None
    movie_mode: str = "INACTIVE"
    throttle: bool = True
    unthrottled: bool = False
    speed: int = 100
    quit: bool = False
    received: list[str] = field(default_factory=list)
    replies: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.movie_lines is not None:
            self.movie_mode = "PLAY"
        for _ in range(self.pre_frames):
            playing = self.movie_lines is not None and self.game.frame < len(self.movie_lines)
            self.game.step(pressed_from_line(self.movie_lines[self.game.frame]) if playing else ())

    # -- helpers ------------------------------------------------------------------------
    def hello(self) -> str:
        return f"HELLO {self.system_id} {self.game.frame} {self.movie_mode} 1"

    def _state(self, tok: str | None) -> int:
        shown = tok if tok else "nil"          # Lua: tostring(nil)
        try:
            sid = int(tok or "")
        except ValueError:
            raise _Err(f"unknown state id '{shown}'") from None
        if sid not in self.states:
            raise _Err(f"unknown state id '{shown}'")
        return sid

    def _parse_read(self, tok: list[str], i: int) -> tuple[_WireRead, int]:
        if i + 3 >= len(tok):
            raise _Err("incomplete read spec (need <domain> <addr_hex> <size> <le|be>)")
        dom, addr_s, size_s, endian = tok[i:i + 4]
        domain = unquote(dom)
        if domain not in DOMAINS:
            raise _Err(f"unknown memory domain '{domain}'")
        try:
            addr = int(addr_s[2:] if addr_s.lower().startswith("0x") else addr_s, 16)
        except ValueError:
            raise _Err(f"bad address '{addr_s}'") from None
        r = _WireRead(domain, addr)
        if size_s[:1] in ("h", "H") and size_s[1:].isdigit() and int(size_s[1:]) > 0:
            r.hash_len = int(size_s[1:])
            n = r.hash_len
        elif size_s in ("1", "2", "4", "s1", "s2", "s4", "S1", "S2", "S4"):
            r.size = int(size_s[-1])
            r.signed = size_s[0] in "sS"
            if endian not in ("le", "be"):
                raise _Err(f"bad endian '{endian}' (le or be)")
            r.be = endian == "be"
            n = r.size
        else:
            raise _Err(f"bad size '{size_s}' (1, 2, 4, s1, s2, s4 or h<len>)")
        if addr + n > DOMAINS[domain]:
            raise _Err(f"address 0x{addr:X}+{n} outside {domain} (size 0x{DOMAINS[domain]:X})")
        return r, i + 4

    def _do_read(self, r: _WireRead) -> int | str:
        if r.hash_len:
            return self.game.hash_region(r.domain, r.addr, r.hash_len)
        return self.game.read(r.domain, r.addr, r.size, "be" if r.be else "le", r.signed)

    def _parse_until(self, tok: list[str], i: int):
        r, i = self._parse_read(tok, i)
        if r.hash_len:
            raise _Err("UNTIL needs a numeric read, not a hash")
        if i + 1 >= len(tok):
            raise _Err("UNTIL needs <op> <value>")
        op, val = tok[i], tok[i + 1]
        if op not in ("==", "!=", "<", "<=", ">", ">=", "&"):
            raise _Err(f"bad UNTIL operator '{op}'")
        try:
            value = int(val, 0)
        except ValueError:
            raise _Err(f"bad UNTIL value '{val}'") from None
        return (r, op, value), i + 2

    def _check(self, cond) -> bool:
        r, op, value = cond
        v = self._do_read(r)
        return {"==": v == value, "!=": v != value, "<": v < value, "<=": v <= value,
                ">": v > value, ">=": v >= value, "&": (v & value) != 0}[op]

    def _parse_line(self, raw: str) -> set[str]:
        """Same checks and messages as parse_line() in the Lua script."""
        if not (raw.startswith("|") and raw.endswith("|") and len(raw) >= 2):
            raise _Err(f"input line must look like |...|, got '{raw}'")
        if self.key_groups is None:
            # The Lua falls back to joypad.setfrommnemonicstr, which parses with the core's
            # own layout and silently ignores lines it can't parse.
            try:
                return pressed_from_line(raw)
            except ValueError:
                return set()
        segs = raw[1:].split("|")[:-1]
        groups = self.key_groups
        if len(segs) < len(groups):
            raise _Err(f"input line has {len(segs)} group(s), the LogKey has {len(groups)}: '{raw}'")
        pressed: set[str] = set()
        for names, seg in zip(groups, segs):
            parts = (seg + ",").split(",")[:-1]
            n_axes = len(parts) - 1
            if n_axes > len(names):
                raise _Err(f"too many axis values in '{raw}'")
            chars, nb = parts[-1], len(names) - n_axes
            if len(chars) < nb:
                raise _Err(f"input line too short for the LogKey: '{raw}'")
            pressed.update(names[n_axes + i] for i in range(nb) if chars[i] != ".")
        return pressed

    def _lines(self, text: str) -> list[set[str]]:
        try:
            raw = expand_lines(text)
        except ValueError as e:
            raise _Err(str(e)) from None
        cache: dict[str, set[str]] = {}
        out = []
        for line in raw:
            if line not in cache:
                cache[line] = self._parse_line(line)
            out.append(cache[line])
        return out

    def _require_no_movie(self) -> None:
        if self.movie_mode != "INACTIVE":
            raise _Err(f"a movie is loaded (mode {self.movie_mode}): joypad input would be "
                       "ignored or recorded. Use SEEK, or stop the movie first "
                       "(File > Movie > Stop Movie)")

    def _run(self, frames: list, n: int, hold=None, cond=None) -> tuple[int, int | None]:
        lag, hit, ran = 0, None, 0
        if cond is not None and self._check(cond):
            hit = 0
        while hit is None and ran < n:
            p = frames[ran] if ran < len(frames) else hold
            lag += self.game.step(p or ())
            ran += 1
            if cond is not None and self._check(cond):
                hit = ran
        return lag, hit

    @staticmethod
    def _fmt(v: int | str) -> str:
        return v if isinstance(v, str) else str(v)

    # -- commands ----------------------------------------------------------------------
    def handle(self, msg: str) -> str:
        self.received.append(msg)
        try:
            reply = self._dispatch(msg)
        except _Err as e:
            reply = f"ERR {e}"
        self.replies.append(reply)
        return reply

    def _dispatch(self, msg: str) -> str:
        cmd, _, rest = msg.partition(" ")
        cmd = cmd.upper()
        g = self.game
        if cmd == "":
            raise _Err("empty command")
        if cmd == "PING":
            return "OK pong"
        if cmd == "FRAME":
            return f"OK {g.frame}"
        if cmd == "SAVE":
            sid = self.next_state
            self.next_state += 1
            self.states[sid] = g.snapshot()
            return f"OK {sid}"
        if cmd == "LOAD":
            g.restore(self.states[self._state(rest.split()[0] if rest.split() else None)])
            return f"OK {g.frame}"
        if cmd == "FREE":
            del self.states[self._state(rest.split()[0] if rest.split() else None)]
            return "OK"
        if cmd == "KEYS":
            try:
                groups = parse_log_key("LogKey:" + rest.strip())
            except ValueError as e:
                raise _Err(str(e)) from None
            names = [n for grp in groups for n in grp]
            if not names:
                raise _Err("KEYS needs a LogKey like #Up|Down|...|")
            for n in names:
                if n not in GB_BUTTONS:
                    raise _Err(f"button '{n}' is not on this core's controller (wrong system or LogKey?)")
            self.key_groups = groups
            return f"OK {len(names)}"
        if cmd == "DOMAINS":
            from urllib.parse import quote
            return "OK " + " ".join(f"{quote(d, safe='')}:{s:X}" for d, s in DOMAINS.items())
        if cmd == "SPEED":
            arg = rest.strip().lower()
            if arg == "max":
                self.throttle, self.unthrottled = False, True
            elif arg.isdigit() and 0 < int(arg) <= 6400:
                self.throttle, self.unthrottled, self.speed = True, False, int(arg)
            else:
                raise _Err(f"bad SPEED '{rest.strip()}' (max or 1..6400)")
            return "OK"
        if cmd == "READ":
            tok = rest.split()
            if not tok:
                raise _Err("READ needs at least one read spec")
            i, reads = 0, []
            while i < len(tok):
                r, i = self._parse_read(tok, i)
                reads.append(r)
            return "OK " + " ".join(self._fmt(self._do_read(r)) for r in reads)
        if cmd == "RUN":
            n_s, _, text = rest.strip().partition(" ")
            if not n_s.isdigit():
                raise _Err(f"RUN needs a frame count, got '{n_s}'")
            n = int(n_s)
            frames = self._lines(text)
            if len(frames) > n:
                raise _Err(f"RUN {n} got {len(frames)} lines")
            if not frames and self.key_groups is None and n:
                raise _Err("RUN without lines needs KEYS (to build a neutral input)")
            self._require_no_movie()
            hold = frames[-1] if frames else set()
            lag, _ = self._run(frames, n, hold)
            return f"OK {g.frame} {lag}"
        if cmd in ("EVAL", "BRANCH"):
            head, sep, text = rest.partition("|")
            if not sep:
                raise _Err(f"{cmd} needs ' | ' before the input lines")
            tok = head.split()
            if not tok:
                raise _Err(f"{cmd} needs a state id")
            sid = self._state(tok[0])
            reads, cond, i = [], None, 1
            if cmd == "BRANCH" and len(tok) > 1:
                raise _Err("BRANCH takes only a state id")
            while i < len(tok):
                if tok[i].upper() == "UNTIL":
                    if cond is not None:
                        raise _Err("only one UNTIL per EVAL")
                    cond, i = self._parse_until(tok, i + 1)
                else:
                    r, i = self._parse_read(tok, i)
                    reads.append(r)
            frames = self._lines(text)
            self._require_no_movie()
            g.restore(self.states[sid])
            lag, hit = self._run(frames, len(frames), None, cond)
            if cmd == "BRANCH":
                new = self.next_state
                self.next_state += 1
                self.states[new] = g.snapshot()
                return f"OK {new} {g.frame} {lag}"
            vals = [self._fmt(self._do_read(r)) for r in reads]
            parts = [str(g.frame), str(lag)]
            if cond is not None:
                parts.append(str(-1 if hit is None else hit))
            return "OK " + " ".join(parts + vals)
        if cmd == "SEEK":
            s = rest.strip()
            if not s.isdigit():
                raise _Err(f"SEEK needs a frame number, got '{s}'")
            target = int(s)
            if self.movie_mode not in ("PLAY", "FINISHED") or self.movie_lines is None:
                raise _Err("SEEK needs a movie loaded in BizHawk (start EmuHawk with --movie)")
            length = len(self.movie_lines)
            if target < g.frame:
                raise _Err(f"already at frame {g.frame}, past {target}; restart the movie")
            if target > length:
                raise _Err(f"movie has only {length} frames")
            while g.frame < target:
                g.step(pressed_from_line(self.movie_lines[g.frame]))
            self.movie_mode = "INACTIVE"
            return f"OK {g.frame} {length}"
        if cmd == "QUIT":
            self.quit = True
            return "OK bye"
        raise _Err(f"unknown command '{cmd}'")

    # -- TCP client ----------------------------------------------------------------------
    def _send(self, sock: socket.socket, msg: str) -> None:
        data = encode_message(msg)
        if self.chunk <= 0:
            sock.sendall(data)
            return
        for i in range(0, len(data), self.chunk):
            sock.sendall(data[i:i + self.chunk])
            time.sleep(0.0005)

    @staticmethod
    def _connect(host: str, port: int, timeout_s: float) -> socket.socket:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                sock = socket.create_connection((host, port), timeout=timeout_s)
                sock.settimeout(None)
                return sock
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)

    def serve(self, host: str, port: int, timeout_s: float = 10.0) -> None:
        """Connect to a BizHawkBridge, say HELLO and answer commands until QUIT/EOF."""
        silent = self._connect(host, port, timeout_s) if self.silent_first else None
        sock = self._connect(host, port, timeout_s)
        try:
            if silent is not None:
                time.sleep(0.05)
            self._send(sock, self.hello())
            dec = FrameDecoder()
            while not self.quit:
                try:
                    data = sock.recv(65536)
                except OSError:
                    break
                if not data:
                    break
                for msg in dec.feed(data):
                    self._send(sock, self.handle(msg))
                    if self.quit:
                        break
        finally:
            sock.close()
            if silent is not None:
                silent.close()

    def start(self, host: str, port: int) -> threading.Thread:
        t = threading.Thread(target=self.serve, args=(host, port), daemon=True)
        t.start()
        return t
