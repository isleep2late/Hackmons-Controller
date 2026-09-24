"""Run ``tools/bizhawk/controllerlog_bot.lua`` under lupa with a model of EmuHawk.

The model follows what EmuHawk's source does (MainForm.ProgramRunLoop,
InputManager.RunControllerChain, OverrideAdapter, LuaLibraries.ResumeScripts,
JoypadApi, MemoryApi, MemorySaveStateApi, SocketServer):

one GUI loop iteration =
  1. RunControllerChain: controller = physical input (neutral here) + override adapter
  2. ResumeScripts(false): resume scripts that called emu.yield() (even while paused)
  3. if not paused: FrameTick (clear the override adapter), emulate one frame
     (movie input in PLAY mode, else the controller), then ResumeScripts(true)
     (resume everything, including emu.frameadvance() waiters)

joypad.set() writes the adapter AND the controller immediately;
joypad.setfrommnemonicstr() only writes the adapter. Unknown memory domains fall
back to the "current" domain and out-of-range reads return 0, like BizHawk, so
the script's own validation is what the tests exercise.
"""

from __future__ import annotations

import importlib
import socket
import uuid
from collections import deque
from pathlib import Path

from controllerlog.formats.bk2 import GB_BUTTONS, parse_frame
from controllerlog.optimize import FrameDecoder, encode_message

from .fake_bizhawk import DOMAINS, ToyGame, pressed_from_line

BOT = Path(__file__).resolve().parents[3] / "tools" / "bizhawk" / "controllerlog_bot.lua"


class QueueTransport:
    """comm.socketServer* backed by Python lists (no sockets)."""

    def __init__(self) -> None:
        self.inbox: deque[str] = deque()
        self.outbox: list[str] = []
        self.reconnects = 0
        self.timeout_ms = 0

    def send(self, msg: str) -> int:
        self.outbox.append(msg)
        return len(encode_message(msg))

    def recv(self) -> str:
        return self.inbox.popleft() if self.inbox else ""

    def reconnect(self) -> None:
        self.reconnects += 1

    def close(self) -> None:
        pass


class SocketTransport:
    """comm.socketServer* over TCP, like BizHawk's SocketServer (a client despite the name)."""

    def __init__(self, host: str, port: int) -> None:
        self.addr = (host, port)
        self.old: list[socket.socket] = []
        self.sock = socket.create_connection(self.addr, timeout=5)   # MainForm's startup connection
        self.dec = FrameDecoder()
        self.pending: deque[str] = deque()
        self.timeout_ms = 0
        self.reconnects = 0

    def reconnect(self) -> None:
        self.old.append(self.sock)          # BizHawk just replaces the Socket object
        self.sock = socket.create_connection(self.addr, timeout=5)
        self.dec = FrameDecoder()
        self.reconnects += 1

    def send(self, msg: str) -> int:
        data = encode_message(msg)
        try:
            self.sock.sendall(data)
        except OSError:
            return -1
        return len(data)

    def recv(self) -> str:
        self.sock.settimeout(self.timeout_ms / 1000 if self.timeout_ms > 0 else None)
        try:
            while not self.pending:
                data = self.sock.recv(65536)
                if not data:
                    return ""
                self.pending.extend(self.dec.feed(data))
        except (socket.timeout, OSError):
            return ""
        return self.pending.popleft()

    def close(self) -> None:
        for s in self.old + [self.sock]:
            try:
                s.close()
            except OSError:
                pass


class ScriptError(AssertionError):
    pass


class BizHawkLuaHost:
    def __init__(self, transport=None, game: ToyGame | None = None, lua: str = "lupa.lua54",
                 system_id: str = "GB", movie_lines: list[str] | None = None,
                 script: Path = BOT, start_paused: bool = False) -> None:
        self.lupa = importlib.import_module(lua)
        self.L = self.lupa.LuaRuntime(unpack_returned_tuples=True)
        self.t = transport or QueueTransport()
        self.game = game or ToyGame()
        self.system_id = system_id
        self.buttons = list(GB_BUTTONS)
        self.paused = start_paused
        self.overrides: dict[str, bool] = {}
        self.axis_overrides: dict[str, int] = {}
        self.controller: dict[str, bool] = {b: False for b in self.buttons}
        self.savestates: dict[str, tuple] = {}
        self.movie_lines = movie_lines
        self.movie_mode = "PLAY" if movie_lines is not None else "INACTIVE"
        self.throttle, self.speed_pct, self.unthrottled = True, 100, False
        self.printed: list[str] = []
        self.bizhawk_log: list[str] = []
        self.onexit: list = []
        self.iterations = 0
        self.frames_emulated = 0
        self.frame_inputs: list[frozenset[str]] = []
        self.state = "start"       # start | yield | frame | dead
        self._stuck = 0
        self.error: str | None = None
        self._install()
        src = script.read_text(encoding="utf-8")
        # Keep the coroutine on the Lua side (lupa wraps Lua threads when they cross into Python).
        self.L.execute("""
            local co
            function cl_host_start(src)
              local ld = loadstring or load
              co = coroutine.create(assert(ld(src, '=controllerlog_bot.lua')))
            end
            function cl_host_resume()
              local ok, what = coroutine.resume(co)
              return ok, tostring(what), coroutine.status(co)
            end
        """)
        self.L.globals().cl_host_start(src)
        self._resume_fn = self.L.globals().cl_host_resume

    # -- Lua globals ------------------------------------------------------------------------
    def _install(self) -> None:
        L, g = self.L, self.L.globals()
        tf = L.table_from
        g.print = lambda *a: self.printed.append(" ".join(str(x) for x in a))
        L.execute("emu = {}; function emu.frameadvance() coroutine.yield('frame') end; "
                  "function emu.yield() coroutine.yield('yield') end")
        emu = g.emu
        emu.framecount = lambda: self.game.frame
        emu.islagged = lambda: self.game.is_lag
        emu.lagcount = lambda: self.game.lagcount
        emu.getsystemid = lambda: self.system_id
        emu.limitframerate = self._limitframerate
        # client.getconfig() is the live Config object: reads and writes go to the host's fields
        # (NLua raises for properties Config doesn't have).
        L.execute("""
            function cl_host_config(get, set)
              return setmetatable({}, {__index = function(_, k) return get(k) end,
                                       __newindex = function(_, k, v) set(k, v) end})
            end
        """)
        cfg = g.cl_host_config(self._cfg_get, self._cfg_set)
        g.client = tf({
            "pause": self._pause, "unpause": self._unpause, "ispaused": lambda: self.paused,
            "speedmode": self._speedmode, "getconfig": lambda: cfg,
        })
        g.comm = tf({
            "socketServerSend": lambda s: self.t.send(s),
            "socketServerResponse": lambda: self.t.recv(),
            "socketServerSetTimeout": self._set_timeout,
            "socketServerGetInfo": lambda: "127.0.0.1:43880",
            "socketServerGetPort": lambda: 43880,
            "socketServerGetIp": lambda: "127.0.0.1",
            "socketServerSetPort": lambda p: self.t.reconnect(),
            "socketServerIsConnected": lambda: True,
        })
        g.joypad = tf({"set": self._joypad_set, "setfrommnemonicstr": self._joypad_mnemonic,
                       "get": lambda *a: tf({b: False for b in self.buttons})})
        g.memorysavestate = tf({"savecorestate": self._save, "loadcorestate": self._load,
                                "removestate": lambda gid: self.savestates.pop(gid, None)})
        g.memory = tf({
            "getmemorydomainlist": lambda: tf({i: n for i, n in enumerate(DOMAINS)}),
            "getmemorydomainsize": lambda name="": DOMAINS.get(name, DOMAINS["WRAM"]),
            "read_u8": lambda a, d=None: self._read(a, d, 1, "le"),
            "read_u16_le": lambda a, d=None: self._read(a, d, 2, "le"),
            "read_u16_be": lambda a, d=None: self._read(a, d, 2, "be"),
            "read_u32_le": lambda a, d=None: self._read(a, d, 4, "le"),
            "read_u32_be": lambda a, d=None: self._read(a, d, 4, "be"),
            "hash_region": self._hash,
        })
        g.movie = tf({"mode": lambda: self.movie_mode, "isloaded": lambda: self.movie_mode != "INACTIVE",
                      "length": lambda: len(self.movie_lines or []), "stop": self._movie_stop})
        g.event = tf({"onexit": lambda fn, name=None: self.onexit.append(fn) or "guid"})

    _CFG = {"ClockThrottle": "throttle", "SpeedPercent": "speed_pct", "Unthrottled": "unthrottled"}

    def _cfg_get(self, key):
        if str(key) not in self._CFG:
            raise AttributeError(f"Config has no property {key}")
        return getattr(self, self._CFG[str(key)])

    def _cfg_set(self, key, value) -> None:
        if str(key) not in self._CFG:
            raise AttributeError(f"Config has no property {key}")
        setattr(self, self._CFG[str(key)], value)

    def _limitframerate(self, v) -> None:
        self.throttle = bool(v)

    def _speedmode(self, pct) -> None:
        self.speed_pct = int(pct)

    def _set_timeout(self, ms) -> None:
        self.t.timeout_ms = int(ms)

    def _pause(self) -> None:
        self.paused = True

    def _unpause(self) -> None:
        self.paused = False

    def _joypad_set(self, tbl) -> None:
        # JoypadApi.Set: every bool button of the controller; missing ones are un-overridden.
        given = {str(k): bool(v) for k, v in tbl.items()}
        for b in self.buttons:
            if b in given:
                self.overrides[b] = given[b]
            else:
                self.overrides.pop(b, None)
        # LatchFromPhysical + Overrides(adapter): applied right now
        self.controller = {b: False for b in self.buttons}
        self.controller.update(self.overrides)

    def _joypad_mnemonic(self, s) -> None:
        try:
            fr, _ = parse_frame(str(s), [self.buttons])
        except ValueError:
            self.bizhawk_log.append(f"invalid mnemonic string: {s}")
            return
        for b in self.buttons:
            self.overrides[b] = bool(fr[b])

    def _save(self) -> str:
        gid = str(uuid.uuid4())
        self.savestates[gid] = self.game.snapshot()
        return gid

    def _load(self, gid) -> None:
        snap = self.savestates.get(str(gid))
        if snap is None:
            self.bizhawk_log.append("Unable to find the given savestate in memory")
            return
        self.game.restore(snap)

    def _domain(self, d) -> str:
        if d is None or str(d) not in DOMAINS:
            self.bizhawk_log.append(f"Unable to find domain: {d}, falling back to current")
            return "WRAM"
        return str(d)

    def _read(self, addr, domain, size, endian) -> int:
        d = self._domain(domain)
        if addr < 0 or addr + size > DOMAINS[d]:
            self.bizhawk_log.append(f"Warning: attempted read of {addr} outside the memory size of {DOMAINS[d]}")
            return 0
        return self.game.read(d, int(addr), size, endian)

    def _hash(self, addr, count, domain=None) -> str:
        d = self._domain(domain)
        if addr < 0 or addr + count > DOMAINS[d]:
            raise ValueError(f"Address {addr} + count {count} is outside the bounds of domain {d}")
        return self.game.hash_region(d, int(addr), int(count))

    def _movie_stop(self, save=True) -> None:
        self.movie_mode = "INACTIVE"

    # -- main loop --------------------------------------------------------------------------
    def _resume(self) -> None:
        ok, what, status = self._resume_fn()
        if not ok:
            self.state = "dead"
            self.error = str(what)
            raise ScriptError(f"Lua error: {what}")
        if status == "dead":
            self.state = "dead"
            for fn in self.onexit:
                fn()
            return
        if what not in ("frame", "yield"):
            raise ScriptError(f"script yielded {what!r}")
        self.state = what

    def step(self) -> None:
        """One EmuHawk GUI loop iteration."""
        if self.state == "dead":
            return
        self.iterations += 1
        # RunControllerChain
        self.controller = {b: False for b in self.buttons}
        self.controller.update(self.overrides)
        # ResumeScripts(false)
        if self.state in ("start", "yield"):
            self._resume()
        if self.state == "dead":
            return
        # StepRunLoop_Core
        if not self.paused:
            self.overrides.clear()
            self.axis_overrides.clear()
            playing = self.movie_mode == "PLAY" and self.movie_lines is not None
            if playing and self.game.frame < len(self.movie_lines):      # LatchInputToLog
                pressed = frozenset(pressed_from_line(self.movie_lines[self.game.frame]))
            else:
                pressed = frozenset(b for b, v in self.controller.items() if v)
            self.frame_inputs.append(pressed)
            self.game.step(pressed)
            self.frames_emulated += 1
            self._stuck = 0
            if playing and self.game.frame >= len(self.movie_lines):
                self.movie_mode = "FINISHED"
            # UpdateAfter -> ResumeScripts(true)
            if self.state in ("frame", "yield"):
                self._resume()
        elif self.state == "frame":
            self._stuck += 1
            if self._stuck > 1000:
                raise ScriptError("script waits for a frame while the emulator is paused (deadlock)")

    def run_until_reply(self, max_iters: int = 200_000) -> str:
        """(Queue transport) step until the script has sent one more message."""
        n = len(self.t.outbox)
        for _ in range(max_iters):
            if len(self.t.outbox) > n:
                return self.t.outbox[n]
            if self.state == "dead":
                break
            self.step()
        if len(self.t.outbox) > n:
            return self.t.outbox[n]
        raise ScriptError(f"no reply (state {self.state}, error {self.error})")

    def call(self, cmd: str, max_iters: int = 200_000) -> str:
        self.t.inbox.append(cmd)
        return self.run_until_reply(max_iters)

    def run_forever(self, max_iters: int = 10_000_000) -> None:
        """(Socket transport) iterate until the script ends."""
        try:
            for _ in range(max_iters):
                if self.state == "dead":
                    return
                self.step()
        finally:
            self.t.close()
