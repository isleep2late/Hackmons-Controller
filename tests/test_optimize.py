from __future__ import annotations

import argparse
import importlib.util
import random
import socket
import threading
import time

import pytest

from controllerlog import optimize as opt
from controllerlog.formats import bk2
from controllerlog.optimize import (OPTIMIZED_TAG, BizHawkBridge, BizHawkError, BridgeError, Condition,
                                    EvalResult, FrameDecoder, Hello, LineCodec, Objective, ProtocolError,
                                    RamHash, Read, beam_search, check_reads, delay_search, encode_lines,
                                    encode_message, expand_lines, find_presses, parse_read_spec,
                                    random_mutation, shift_press, splice_lines, window_lines)
from tests.fixtures.optimize.fake_bizhawk import (AIR, DOOR, FRAME16, GB_LOG_KEY, GOAL, PREV_A, SX, V, X,
                                                  FakeBizHawk, ToyEvaluator, ToyGame, door_frames)

GB = LineCodec.for_system("gb")
N = GB.neutral


def L(*buttons: str) -> str:
    return GB.encode(buttons)


def a_frames(lines: list[str]) -> list[int]:
    return [i for i, line in enumerate(lines) if "A" in GB.pressed(line)]


def door_setup(late: int = 3, length: int = 60) -> tuple[int, list[str]]:
    """A movie that presses A ``late`` frames after the only frame that opens the door."""
    df = [f for f in door_frames(200) if f >= 20][0]
    lines = [N] * length
    lines[df + late] = L("A")
    return df, lines


DOOR_OPEN = Objective.parse("until WRAM:0x5:1==1")
GOAL_FAST = Objective.parse("until WRAM:0x4:1==1", ["max WRAM:0x0:2"])
TOY_DEDUP = [Read("WRAM", X, 2), Read("WRAM", V), Read("WRAM", AIR), Read("WRAM", PREV_A)]
NAIVE = [L("Right", "A") if i % 2 == 0 else L("Right") for i in range(120)]   # mash jump while running


# --- framing ---------------------------------------------------------------------------------

def test_encode_message_uses_utf8_byte_length():
    assert encode_message("PING") == b"4 PING"
    assert encode_message("é") == b"2 \xc3\xa9"
    assert encode_message("") == b"0 "


MESSAGES = ["OK", "", "ERR unknown memory domain 'WRAMé漢字'", "x" * 9, "y" * 10, "z" * 123,
            "w" * 70_000, "OK 1 2 3"]


@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 4096, 10**6])
def test_decoder_handles_any_fragmentation(chunk):
    data = b"".join(encode_message(m) for m in MESSAGES)
    dec, out = FrameDecoder(), []
    for i in range(0, len(data), chunk):
        out += dec.feed(data[i:i + chunk])
    assert out == MESSAGES
    assert dec.pending == 0


def test_decoder_random_splits():
    rnd = random.Random(1)
    data = b"".join(encode_message(m) for m in MESSAGES * 3)
    dec, out, i = FrameDecoder(), [], 0
    while i < len(data):
        n = rnd.randint(1, 300)
        out += dec.feed(data[i:i + n])
        i += n
    assert out == MESSAGES * 3


@pytest.mark.parametrize("bad", [b"abc 12", b"-1 x", b"12345678901234567", b" 5 hello", b"4x PING"])
def test_decoder_rejects_bad_prefix(bad):
    with pytest.raises(ProtocolError):
        FrameDecoder().feed(bad)


def test_decoder_limits_size_and_needs_utf8():
    with pytest.raises(ProtocolError, match="too large"):
        FrameDecoder(max_len=10).feed(b"11 ")
    with pytest.raises(ProtocolError, match="UTF-8"):
        FrameDecoder().feed(b"1 \xff")


def test_lines_run_length_encoding_roundtrip():
    lines = [N] * 5 + [L("A")] + [N] * 2 + [L("Right")]
    text = encode_lines(lines)
    assert text == f"5*{N};{L('A')};2*{N};{L('Right')}"
    assert expand_lines(text) == lines
    assert expand_lines("") == [] and encode_lines([]) == ""
    for bad in (["no pipe"], ["|..;..|"], ["|..\n|"]):
        with pytest.raises(ValueError):
            encode_lines(bad)
    with pytest.raises(ValueError):
        expand_lines("0*|.........|")


# --- reads, objectives, codec ---------------------------------------------------------------------

def test_read_parse_and_tokens():
    assert Read.parse("WRAM:0x1361:1") == Read("WRAM", 0x1361, 1, "le", False)
    assert Read.parse("System Bus:FF44") == Read("System Bus", 0xFF44)
    assert Read.parse("IWRAM:0x10:s2:be") == Read("IWRAM", 0x10, 2, "be", True)
    assert Read.parse("EWRAM:$20:4") == Read("EWRAM", 0x20, 4)
    assert Read("System Bus", 0xFF44).token() == "System%20Bus FF44 1 le"
    assert Read("WRAM", 0xA, 1, signed=True).token() == "WRAM A s1 le"
    assert str(Read.parse("IWRAM:0x10:s2:be")) == "IWRAM:0x10:s2:be"
    for bad in ("WRAM", "WRAM:zz", "WRAM:1:3", ":1"):
        with pytest.raises(ValueError):
            Read.parse(bad)
    assert parse_read_spec("hash:WRAM") == RamHash("WRAM", 0, 0)
    assert parse_read_spec("hash:System Bus:0xC000:0x100") == RamHash("System Bus", 0xC000, 0x100)
    assert RamHash("WRAM", 0, 16).token() == "WRAM 0 h16 le"
    with pytest.raises(ValueError):
        RamHash("WRAM").token()


def test_condition_and_objective_parsing():
    c = Condition.parse("WRAM:0x135E:1==92")
    assert (c.read, c.op, c.value) == (Read("WRAM", 0x135E), "==", 92)
    assert Condition.parse("System Bus:0xFF44:1 >= 0x90").value == 0x90
    assert Condition.parse("WRAM:0:1&0x80").test(0x81) and not Condition.parse("WRAM:0:1&0x80").test(1)
    assert c.token() == "UNTIL WRAM 135E 1 le == 92"
    o = Objective.parse("max WRAM:0xD361:1")
    assert (o.kind, o.read, o.until) == ("max", Read("WRAM", 0xD361), None)
    o = Objective.parse("min System Bus:0xFF44:1")
    assert o.read.domain == "System Bus"
    for bad in ("maximize WRAM:0", "until WRAM:0:1", "max"):
        with pytest.raises(ValueError):
            Objective.parse(bad)
    assert Condition.parse("WRAM:0:1==09").value == 9               # leading zero: decimal, not an error
    assert Condition.parse("WRAM:0:s1>=-010").value == -10
    assert Condition.parse("WRAM:0:1==-0x10").value == -16
    for bad in ("hash:WRAM:0x100", "hash:"):                       # ADDR without LEN
        with pytest.raises(ValueError, match="hash:DOMAIN"):
            parse_read_spec(bad)
    with pytest.raises(ValueError):
        Objective.parse("max WRAM:0", ["until WRAM:1:1==2"])


def test_objective_scores_order_candidates():
    o = GOAL_FAST
    early, late = EvalResult(0, 0, [1, 40], hit=20), EvalResult(0, 0, [1, 40], hit=30)
    near, far = EvalResult(0, 0, [0, 39], None), EvalResult(0, 0, [0, 10], None)
    assert o.score(early) > o.score(late) > o.score(near) > o.score(far)
    assert "after 20 frame(s)" in o.describe(early) and "not reached" in o.describe(far)
    m = Objective.parse("min WRAM:0:1", ["max WRAM:1:1"])
    assert m.score(EvalResult(0, 0, [3, 0])) > m.score(EvalResult(0, 0, [4, 9]))
    assert m.score(EvalResult(0, 0, [3, 5])) > m.score(EvalResult(0, 0, [3, 4]))


def test_line_codec_matches_bk2_writer():
    for system in ("gb", "gba"):
        c = LineCodec.for_system(system)
        mv = bk2.new_movie(system)
        mv.frames = [bk2.state_to_frame(bk2.PadState(), system)]
        mv.frames[0].update({"A": True, "Up": True})
        assert c.encode({"A", "Up"}) == mv.frame_lines()[0]
        assert c.log_key == mv.log_key
    gba = LineCodec.for_system("gba")
    line = gba.encode({"A", "L"})
    assert line == "|    0,    0,    0,    0,.......Al..|"
    assert gba.pressed(line) == {"A", "L"}
    assert gba.axis_values("|   12,    0,    0,    0,...........|")["Tilt X"] == 12
    tilted = "|   12,    0,    0,    0,...........|"
    assert gba.with_buttons(tilted, add=["B"]) == "|   12,    0,    0,    0,......B....|"


def test_line_codec_actions_and_opposites():
    assert GB.action("") == frozenset() and GB.action("none") == frozenset()
    assert GB.action("right+a") == {"Right", "A"}
    with pytest.raises(ValueError, match="opposite"):
        GB.action("Left+Right")
    with pytest.raises(ValueError, match="unknown button"):
        GB.action("Q")
    assert GB.with_buttons(L("Left", "B"), add=["Right"]) == L("Right", "B")
    assert GB.with_buttons(L("A"), remove=["A"]) == N


def test_line_codec_axis_neutrals_for_new_frames():
    # Sub-frame movies log an "Input Length" axis whose neutral is a whole frame, not 0 (mGBA
    # clamps 0 to 1 cycle); Gambatte's IR "Remote Command" idles at 127.
    gb = bk2.new_movie("gb")
    gb.groups = [["Input Length", "Remote Command"] + list(bk2.GB_BUTTONS)]
    gb.frames = [bk2.parse_frame("|35112,  127,.........|", gb.groups)[0]]
    c = LineCodec.from_movie(gb)
    assert c.neutral == "|35112,  127,.........|"
    assert c.encode({"A"}) == "|35112,  127,.......A.|"
    assert c.with_buttons("|  100,  127,.........|", add=["B"]) == "|  100,  127,......B..|"   # kept
    gba = bk2.new_movie("gba")
    gba.groups = [["Input Length"] + bk2.GBA_AXES + list(bk2.GBA_BUTTONS)]      # no frames yet
    assert LineCodec.from_movie(gba).neutral == "|280896,    0,    0,    0,    0,...........|"
    assert LineCodec.for_system("gba").neutral == "|    0,    0,    0,    0,...........|"


def test_find_and_shift_presses():
    lines = [N, L("A"), L("A"), N, L("Right"), N]
    assert find_presses(GB, lines) == [(1, 2, "A"), (4, 4, "Right")]
    assert find_presses(GB, lines, ["Right"]) == [(4, 4, "Right")]
    assert shift_press(GB, lines, 1, 2, "A", 2) == [N, N, N, L("A"), L("A", "Right"), N]
    assert shift_press(GB, lines, 1, 2, "A", -2) is None
    assert shift_press(GB, lines, 1, 2, "A", 4) is None


def test_toy_game_rules():
    ev = ToyEvaluator()
    s = ev.save_state()
    r = ev.evaluate(s, [L("Right")] * 10, [Read("WRAM", X, 2), Read("WRAM", FRAME16, 2, "be")])
    assert r == EvalResult(10, 1, [17, 10], None)            # frame 7 is a lag frame
    assert ev.evaluate(s, [L("Right")] * 40, [Read("WRAM", X, 2)]).values == [19]   # the wall
    assert ev.evaluate(s, NAIVE, GOAL_FAST.reads, GOAL_FAST.until).hit == 46


# --- bridge <-> fake BizHawk over TCP -----------------------------------------------------------------

@pytest.fixture(params=[0, 1, 5], ids=["whole", "bytewise", "chunked"])
def session(request):
    fake = FakeBizHawk(chunk=request.param)
    bridge = BizHawkBridge(port=0, timeout_s=15)
    t = fake.start("127.0.0.1", bridge.port)
    hello = bridge.wait_for_bizhawk(15, auto_keys=False)
    yield bridge, fake, hello
    bridge.close()
    t.join(5)
    assert not t.is_alive()


def test_bridge_every_command(session):
    bridge, fake, hello = session
    assert hello == Hello("GB", 0, "INACTIVE", 1)
    bridge.ping()
    assert bridge.set_keys(GB_LOG_KEY) == 9
    assert bridge.domains() == {"WRAM": 0x2000, "HRAM": 0x7F, "System Bus": 0x10000}
    bridge.speed("max")
    assert fake.throttle is False
    bridge.speed(250)
    assert (fake.throttle, fake.speed) == (True, 250)
    s0 = bridge.save_state()
    assert bridge.run([L("Right")] * 3, n_frames=10) == (10, 1)       # holds the last line
    assert bridge.read([Read("WRAM", X, 2), Read("System Bus", 0xC000 + X, 2),
                        Read("WRAM", FRAME16, 2, "be"), Read("WRAM", SX, 1, signed=True),
                        Read("WRAM", X, 4)]) == [17, 17, 10, -3, 17 | 2 << 16]   # x lo, x hi, speed 2, air 0
    h = bridge.read([RamHash("WRAM", 0, 0x2000)])[0]
    assert h == int(fake.game.hash_region("WRAM", 0, 0x2000)[:16], 16)
    assert bridge.framecount() == 10
    assert bridge.load_state(s0) == 0 and bridge.framecount() == 0
    assert bridge.run([], n_frames=3) == (3, 0)                          # neutral (needs KEYS)
    assert bridge.read([Read("WRAM", X, 2)]) == [0]
    res = bridge.evaluate(s0, [L("Right")] * 4, [Read("WRAM", X, 2)])
    assert res == EvalResult(4, 0, [7], None)
    res = bridge.evaluate(s0, [L("Right")] * 30, [Read("WRAM", X, 2)],
                          until=Condition(Read("WRAM", X, 2), ">=", 10))
    assert res == EvalResult(6, 0, [11], 6)                               # stopped early
    assert bridge.evaluate(s0, [N] * 5, [], until=Condition(Read("WRAM", GOAL), "==", 1)).hit is None
    assert bridge.evaluate(s0, [N], [], until=Condition(Read("WRAM", GOAL), "==", 0)).hit == 0
    s1 = bridge.branch(s0, [L("Right")] * 2)
    assert bridge.load_state(s1) == 2 and bridge.read([Read("WRAM", X, 2)]) == [3]
    bridge.free_state(s1)
    with pytest.raises(BizHawkError, match="unknown state id"):
        bridge.load_state(s1)


def test_bridge_error_replies(session):
    bridge, fake, _ = session
    s0 = bridge.save_state()
    with pytest.raises(BizHawkError, match="needs KEYS"):
        bridge.run([], n_frames=2)
    bridge.set_keys(GB_LOG_KEY)
    with pytest.raises(BizHawkError, match="unknown memory domain 'VRAMé'"):   # unicode both ways
        bridge.read([Read("VRAMé", 0)])
    with pytest.raises(BizHawkError, match=r"outside WRAM \(size 0x2000\)"):
        bridge.read([Read("WRAM", 0x1FFF, 2)])
    with pytest.raises(BizHawkError, match="unknown command 'FOO'"):
        bridge.request("FOO bar")
    with pytest.raises(BizHawkError, match="input line must look like"):
        bridge.request(f"EVAL {s0} | 1*xyz")
    with pytest.raises(BizHawkError, match="too short"):
        bridge.request(f"EVAL {s0} | |...|")
    with pytest.raises(BizHawkError, match="not on this core"):
        bridge.set_keys("#Up|Down|Turbo|")
    with pytest.raises(BizHawkError, match="bad UNTIL operator"):
        bridge.request(f"EVAL {s0} UNTIL WRAM 0 1 le => 3 | {N}")
    with pytest.raises(BizHawkError, match="SEEK needs a movie"):
        bridge.seek(3)
    with pytest.raises(BizHawkError, match="bad SPEED"):
        bridge.speed("fast")
    bridge.ping()                          # still in sync after errors


def test_bridge_seek_plays_movie_then_takes_over():
    movie = [L("Right")] * 5 + [N] * 5
    fake = FakeBizHawk(movie_lines=movie)
    with BizHawkBridge(port=0, timeout_s=15) as bridge:
        t = fake.start("127.0.0.1", bridge.port)
        assert bridge.wait_for_bizhawk(15).movie_mode == "PLAY"
        bridge.set_keys(GB_LOG_KEY)
        s = bridge.save_state()
        with pytest.raises(BizHawkError, match="movie is loaded"):
            bridge.evaluate(s, [N])
        assert bridge.seek(6, timeout_s=30) == (6, 10)
        assert bridge.read([Read("WRAM", X, 2)]) == [9]
        s6 = bridge.save_state()
        assert bridge.evaluate(s6, [L("Right")], [Read("WRAM", X, 2)]) == EvalResult(7, 0, [10], None)
        with pytest.raises(BizHawkError, match="SEEK needs a movie"):
            bridge.seek(8)
    t.join(5)
    assert fake.quit


def test_bridge_ignores_silent_startup_connection():
    fake = FakeBizHawk(silent_first=True)
    with BizHawkBridge(port=0, timeout_s=15) as bridge:
        t = fake.start("127.0.0.1", bridge.port)
        assert bridge.wait_for_bizhawk(15).system == "GB"
        bridge.ping()
        assert bridge.log_key == GB_LOG_KEY and fake.key_groups is not None     # auto KEYS for GB
    t.join(5)
    assert fake.quit


def test_bridge_timeout_explains_how_to_start_bizhawk():
    with BizHawkBridge(port=0) as b:
        with pytest.raises(BridgeError) as ei:
            b.wait_for_bizhawk(0.3)
        port = b.port
    msg = str(ei.value)
    for needle in (f"--socket_port={port}", "--socket_ip=127.0.0.1", "controllerlog_bot.lua", "Lua Console",
                   "did not connect"):
        assert needle in msg
    assert "--movie" in opt.bizhawk_instructions("127.0.0.1", 1, movie="x.bk2")


def test_bridge_port_in_use_and_not_connected():
    with BizHawkBridge(port=0) as a:
        with pytest.raises(BridgeError, match="another optimize session"):
            BizHawkBridge(port=a.port)
        with pytest.raises(BridgeError, match="not connected"):
            a.ping()


def test_bridge_detects_dead_bizhawk():
    with BizHawkBridge(port=0, timeout_s=5) as bridge:
        c = socket.create_connection(("127.0.0.1", bridge.port))
        c.sendall(encode_message("HELLO GB 0 INACTIVE 1"))
        bridge.wait_for_bizhawk(5, auto_keys=False)
        c.close()
        with pytest.raises(BridgeError):
            bridge.ping()
        assert not bridge.connected


def test_bridge_close_skips_stale_reply_and_waits_for_bye():
    # Ctrl+C during a request leaves its reply in flight: close() must not take it for QUIT's.
    seen: list[str] = []
    box: dict = {}

    def bot(port: int) -> None:
        with socket.create_connection(("127.0.0.1", port)) as c:
            c.sendall(encode_message("HELLO GB 0 INACTIVE 1"))
            dec = FrameDecoder()
            while "QUIT" not in seen:
                seen.extend(dec.feed(c.recv(100)))
            c.sendall(encode_message("OK pong"))           # the interrupted PING's reply, late
            time.sleep(0.3)
            try:
                c.sendall(encode_message("OK bye"))
                box["bye"] = True
                c.recv(10)                                 # then the bridge resets the connection
            except OSError:
                box.setdefault("bye", False)

    with BizHawkBridge(port=0, timeout_s=5) as bridge:
        t = threading.Thread(target=bot, args=(bridge.port,), daemon=True)
        t.start()
        bridge.wait_for_bizhawk(5, auto_keys=False)
        bridge._sock.sendall(encode_message("PING"))      # request sent, reply never read
        t0 = time.monotonic()
        bridge.close()
        assert time.monotonic() - t0 >= 0.25 and not bridge.connected
    t.join(5)
    assert seen == ["PING", "QUIT"] and box["bye"] is True and not t.is_alive()


def test_bridge_explains_unframed_hello_from_old_bizhawk():
    with BizHawkBridge(port=0, timeout_s=5) as bridge:
        c = socket.create_connection(("127.0.0.1", bridge.port))
        c.sendall(b"HELLO GB 0")                           # pre-2.6.2: no length prefix
        with pytest.raises(BridgeError, match="2.6.2"):
            bridge.wait_for_bizhawk(5)
        c.close()


def test_bridge_reply_timeout_drops_the_session():
    with BizHawkBridge(port=0, timeout_s=5) as bridge:
        c = socket.create_connection(("127.0.0.1", bridge.port))
        c.sendall(encode_message("HELLO GB 0 INACTIVE 1"))     # then never answers
        bridge.wait_for_bizhawk(5, auto_keys=False)
        t0 = time.monotonic()
        with pytest.raises(BridgeError, match="did not answer SEEK within 0s"):
            bridge.seek(5, timeout_s=0.3)
        assert 0.25 <= time.monotonic() - t0 < 3 and not bridge.connected
        with pytest.raises(BridgeError, match="not connected"):
            bridge.ping()
        c.close()


def test_bridge_rejects_no_rom():
    got: list[str] = []

    def bot(port: int) -> None:                # says HELLO from a core with no ROM, answers QUIT
        with socket.create_connection(("127.0.0.1", port)) as c:
            c.sendall(encode_message("HELLO NULL 0 INACTIVE 1"))
            dec = FrameDecoder()
            while not got:
                got.extend(dec.feed(c.recv(100)))
            c.sendall(encode_message("OK bye"))

    with BizHawkBridge(port=0, timeout_s=5) as bridge:
        t = threading.Thread(target=bot, args=(bridge.port,), daemon=True)
        t.start()
        with pytest.raises(BridgeError, match="no ROM"):
            bridge.wait_for_bizhawk(5)
    t.join(5)
    assert got == ["QUIT"] and not t.is_alive()


# --- search algorithms (pure-Python evaluator) ------------------------------------------------------------

def test_delay_search_finds_exact_door_frame():
    df, lines = door_setup(late=3)
    ev = ToyEvaluator()
    s = ev.save_state()
    res = delay_search(ev, s, lines, Objective.parse("max WRAM:0x5:1"), codec=GB, max_shift=5)
    assert res.improved and res.baseline.values == [0] and res.result.values == [1]
    assert a_frames(res.lines) == [df]
    assert res.details["moved"] == [(df + 3, "A", -3)]


def test_delay_search_until_objective_and_explicit_press():
    df, lines = door_setup(late=-4)
    ev = ToyEvaluator()
    s = ev.save_state()
    msgs: list[str] = []
    res = delay_search(ev, s, lines, DOOR_OPEN, codec=GB, max_shift=5, presses=[(df - 4, "A")],
                       progress=msgs.append)
    assert res.result.hit == df + 1 and res.baseline.hit is None
    assert a_frames(res.lines) == [df]
    assert any("moved +4" in m for m in msgs)
    # already optimal: nothing moves, baseline kept
    again = delay_search(ev, s, res.lines, DOOR_OPEN, codec=GB, max_shift=5)
    assert again.lines == res.lines and not again.improved


class PressEvaluator:
    """Scores inputs by where A and B are first pressed (wants A on frame 15, B on frame 8)."""

    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, state_id, lines, reads=(), until=None):
        self.calls += 1
        first = {b: next((i for i, line in enumerate(lines) if b in GB.pressed(line)), None) for b in "AB"}
        v = 100
        for b, want in (("A", 15), ("B", 8)):
            v -= abs(first[b] - want) if first[b] is not None else 50
        return EvalResult(len(lines), 0, [v] * len(reads))

    def branch(self, state_id, lines):
        return state_id

    def free_state(self, state_id):
        pass


def test_delay_search_visits_every_press_once_per_pass():
    # Moving A (frame 10) to 15 reorders the presses; the sweep must still visit B (frame 12)
    # in the same pass instead of revisiting A.
    lines = [N] * 30
    lines[10], lines[12] = L("A"), L("B")
    res = delay_search(PressEvaluator(), 0, lines, Objective.parse("max WRAM:0:1"), codec=GB,
                       max_shift=5, passes=1)
    assert res.result.values == [100]
    assert find_presses(GB, res.lines) == [(8, 8, "B"), (15, 15, "A")]
    assert res.details["moved"] == [(10, "A", 5), (12, "B", -4)]
    # the explicit-press path tracks moved presses across passes too
    res2 = delay_search(PressEvaluator(), 0, lines, Objective.parse("max WRAM:0:1"), codec=GB,
                        max_shift=3, passes=3, presses=[(10, "A"), (12, "B")])
    assert find_presses(GB, res2.lines) == [(8, 8, "B"), (15, 15, "A")]


def test_beam_search_beats_naive_and_matches_exhaustive_optimum():
    ev = ToyEvaluator()
    s = ev.save_state()
    msgs: list[str] = []
    res = beam_search(ev, s, 80, ["", "Right", "Right+A", "A"], GOAL_FAST, codec=GB, macro_len=1,
                      beam_width=24, dedup=TOY_DEDUP, baseline=NAIVE, progress=msgs.append)
    assert res.baseline.hit == 46
    assert res.result.hit is not None and res.result.hit < res.baseline.hit
    assert res.result.hit == _bfs_optimum(["", "Right", "Right+A", "A"])
    assert len(res.lines) == res.result.hit
    chk = ev.evaluate(s, res.lines, GOAL_FAST.reads, GOAL_FAST.until)
    assert chk.hit == res.result.hit
    assert set(ev.states) == {s}                 # every branch state was freed
    assert any("goal reached" in m for m in msgs)


def test_beam_search_max_objective_with_macros_and_hash_dedup():
    ev = ToyEvaluator()
    s = ev.save_state()
    obj = Objective.parse("max WRAM:0x0:2")
    res = beam_search(ev, s, 24, ["", "Right", "Right+A"], obj, codec=GB, macro_len=3, beam_width=8,
                      dedup=[RamHash("WRAM", 0, 0x20)], baseline=[L("Right")] * 24)
    assert len(res.lines) == 24 and res.improved
    assert res.baseline.values == [19] and res.result.values[0] > 19      # jumped the wall
    assert ev.evaluate(s, res.lines, obj.reads).values == res.result.values
    assert set(ev.states) == {s}


def test_beam_search_validates_arguments():
    ev = ToyEvaluator()
    s = ev.save_state()
    with pytest.raises(ValueError):
        beam_search(ev, s, 0, [""], DOOR_OPEN)
    with pytest.raises(ValueError, match="unknown button"):
        beam_search(ev, s, 5, ["Jump"], DOOR_OPEN)


def test_beam_search_lag_is_cumulative_and_states_freed_on_failure():
    ev = ToyEvaluator()
    s = ev.save_state()
    obj = Objective.parse("max WRAM:0x0:2")
    res = beam_search(ev, s, 24, ["", "Right", "Right+A"], obj, codec=GB, macro_len=3, beam_width=4,
                      dedup=())
    replay = ev.evaluate(s, res.lines, obj.reads)
    assert (res.result.framecount, res.result.lag) == (replay.framecount, replay.lag) == (24, 3)

    class Flaky(ToyEvaluator):                   # the bridge dies during the 3rd step's branches
        def branch(self, state_id, lines):
            if len(self.states) >= 7:
                raise BridgeError("connection lost")
            return super().branch(state_id, lines)

    ev = Flaky()
    s = ev.save_state()
    with pytest.raises(BridgeError):
        beam_search(ev, s, 30, ["", "Right", "Right+A"], obj, codec=GB, beam_width=4, dedup=())
    assert set(ev.states) == {s}                 # half-built next beam freed as well


def test_random_mutation_improves_and_is_reproducible():
    obj = Objective.parse("max WRAM:0x0:2")
    runs = []
    for _ in range(2):
        ev = ToyEvaluator()
        s = ev.save_state()
        runs.append(random_mutation(ev, s, [N] * 24, obj, codec=GB, iterations=400, seed=7,
                                    buttons=["Right", "A", "Left"]))
    a, b = runs
    assert a.lines == b.lines and a.score == b.score and a.evaluations == b.evaluations
    assert a.baseline.values == [0] and a.result.values[0] > 10 and a.improved
    assert len(a.lines) == 24
    ev = ToyEvaluator()
    s = ev.save_state()
    assert ev.evaluate(s, a.lines, obj.reads).values == a.result.values


def test_random_mutation_ops_keep_length_and_accept_only_not_worse():
    df, lines = door_setup(late=2)
    ev = ToyEvaluator()
    s = ev.save_state()
    msgs: list[str] = []
    res = random_mutation(ev, s, lines, DOOR_OPEN, codec=GB, iterations=300, seed=3, progress=msgs.append)
    assert len(res.lines) == len(lines)
    assert res.score >= res.baseline_score
    # the late press misses the door; mutation finds a press on a door frame (there are two in range)
    assert res.baseline.hit is None and res.result.hit is not None
    assert res.result.hit - 1 in door_frames() and res.result.hit <= df + 1
    assert any("300/300" in m for m in msgs)
    with pytest.raises(ValueError):
        random_mutation(ev, s, lines, DOOR_OPEN, codec=GB, ops=("explode",))


def _bfs_optimum(actions: list[str], limit: int = 80) -> int:
    g = ToyGame()
    frontier = {bytes(g.wram[:0x10]): g.snapshot()}
    acts = [GB.action(a) for a in actions]
    for depth in range(1, limit + 1):
        nxt: dict[bytes, tuple] = {}
        for snap in frontier.values():
            for a in acts:
                g.restore(snap)
                g.step(a)
                if g.wram[GOAL]:
                    return depth
                nxt.setdefault(bytes(g.wram[:0x10]), g.snapshot())
        frontier = nxt
    raise AssertionError("goal unreachable")


# --- searches through the bridge ----------------------------------------------------------------------------

def test_searches_through_bridge_match_in_process_results():
    df, lines = door_setup(late=3)
    fake = FakeBizHawk()
    with BizHawkBridge(port=0, timeout_s=15) as bridge:
        t = fake.start("127.0.0.1", bridge.port)
        bridge.wait_for_bizhawk(15)
        bridge.set_keys(GB_LOG_KEY)
        s = bridge.save_state()
        got_delay = delay_search(bridge, s, lines, DOOR_OPEN, codec=GB, max_shift=5)
        got_beam = beam_search(bridge, s, 40, ["", "Right", "Right+A"], GOAL_FAST, codec=GB,
                               beam_width=12, dedup=TOY_DEDUP)
        got_mut = random_mutation(bridge, s, [N] * 16, Objective.parse("max WRAM:0:2"), codec=GB,
                                  iterations=60, seed=5, buttons=["Right", "A"])
        assert len(fake.states) == 1                     # beam freed its branch states
    t.join(5)
    ev = ToyEvaluator()
    es = ev.save_state()
    assert got_delay.lines == delay_search(ev, es, lines, DOOR_OPEN, codec=GB, max_shift=5).lines
    ref_beam = beam_search(ev, es, 40, ["", "Right", "Right+A"], GOAL_FAST, codec=GB, beam_width=12,
                           dedup=TOY_DEDUP)
    assert (got_beam.lines, got_beam.result.hit) == (ref_beam.lines, ref_beam.result.hit)
    ref_mut = random_mutation(ev, es, [N] * 16, Objective.parse("max WRAM:0:2"), codec=GB,
                              iterations=60, seed=5, buttons=["Right", "A"])
    assert (got_mut.lines, got_mut.score) == (ref_mut.lines, ref_mut.score)


# --- movies --------------------------------------------------------------------------------------------------

def make_movie(lines: list[str], system: str = "gb") -> bk2.Bk2Movie:
    mv = bk2.new_movie(system, game_name="Toy")
    mv.frames = [bk2.parse_frame(line, mv.groups)[0] for line in lines]
    return mv


def test_window_and_splice_write_valid_bk2(tmp_path):
    lines = [L("Right")] * 10 + [L("A")] * 3 + [N] * 7
    mv = make_movie(lines)
    mv.header.update(CycleCount="1403040", ClockRate="2097152")
    assert window_lines(mv, 10, 3) == [L("A")] * 3
    assert window_lines(mv, 18, 5) == [N] * 5
    new = splice_lines(mv, 10, 3, [L("B")] * 2, note=f"{OPTIMIZED_TAG}: test", rerecords=42)
    assert new.frame_lines() == lines[:10] + [L("B")] * 2 + lines[13:]
    assert mv.frame_lines() == lines and mv.comments == []          # original untouched
    p = tmp_path / "o.bk2"
    bk2.write_bk2(p, new)
    back = bk2.read_bk2(p)
    assert back.frame_lines() == new.frame_lines()
    assert back.log_key == mv.log_key and back.sync_settings == mv.sync_settings
    assert any(OPTIMIZED_TAG in c for c in back.comments)
    assert back.header["rerecordCount"] == "42"
    assert "CycleCount" not in back.header and back.header["ClockRate"] == "2097152"   # stale running time
    assert mv.header["CycleCount"] == "1403040"
    ext = splice_lines(mv, 25, 0, [L("Start")])                     # past the end: padded
    assert len(ext.frames) == 26 and ext.frame_lines()[-1] == L("Start")


def test_splice_gba_keeps_axes(tmp_path):
    gba = LineCodec.for_system("gba")
    lines = [gba.encode(set())] * 4
    mv = make_movie(lines, "gba")
    new = splice_lines(mv, 1, 2, ["|    5,    0,    0,    0,.......A...|"])
    p = tmp_path / "g.bk2"
    bk2.write_bk2(p, new)
    back = bk2.read_bk2(p)
    assert back.frame_lines() == [lines[0], "|    5,    0,    0,    0,.......A...|", lines[3]]
    assert LineCodec.from_movie(back).pressed(back.frame_lines()[1]) == {"A"}


def test_check_reads_hints_at_bus_addresses():
    doms = {"WRAM": 0x2000, "HRAM": 0x7F, "System Bus": 0x10000}
    with pytest.raises(ValueError) as ei:
        check_reads([Read("WRAM", 0xD361)], doms)
    assert "WRAM:0x1361" in str(ei.value) and "System Bus:0xD361" in str(ei.value)
    with pytest.raises(ValueError, match="available: WRAM, HRAM, System Bus"):
        check_reads([Read("VRAM", 0)], doms)
    assert check_reads([RamHash("WRAM")], doms) == [RamHash("WRAM", 0, 0x2000)]
    assert opt.default_dedup(doms) == [RamHash("WRAM", 0, 0x2000), RamHash("HRAM", 0, 0x7F)]
    assert opt.default_dedup({"IWRAM": 0x8000, "EWRAM": 0x40000})[1] == RamHash("EWRAM", 0, 0x40000)


# --- CLI ------------------------------------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_cli(argv: list[str], fake: FakeBizHawk | None) -> int:
    parser = argparse.ArgumentParser(prog="controllerlog")
    sub = parser.add_subparsers(dest="cmd", required=True)
    opt.add_cli(sub)
    port = _free_port()
    args = parser.parse_args(argv + ["--port", str(port), "--connect-timeout", "20", "--reply-timeout", "20"])
    t = fake.start("127.0.0.1", port) if fake is not None else None
    try:
        return args.func(args)
    finally:
        if t is not None:
            t.join(5)
            assert not t.is_alive()


def test_cli_delay_until_saves_frames(tmp_path, capsys):
    # Run right, get stuck at the wall and jump 3 frames too late: moving the jump saves 3 frames.
    lines = [L("Right")] * 80
    lines[14] = L("Right", "A")
    lines[40] = L("Start")                                 # marks "the rest of the movie"
    src, out = tmp_path / "in.bk2", tmp_path / "out.bk2"
    bk2.write_bk2(src, make_movie(lines))
    fake = FakeBizHawk()                                   # no movie loaded: RUN from power-on
    rc = run_cli(["optimize", "--movie", str(src), "--out", str(out), "--start", "4", "--window", "30",
                  "--tail", "20", "--method", "delay", "--objective", "until WRAM:0x4:1==1",
                  "--max-shift", "6"], fake)
    assert rc == 0 and fake.quit
    text = capsys.readouterr().out
    assert "--socket_ip=127.0.0.1" in text and "controllerlog_bot.lua" in text
    assert "before: WRAM:0x4:1==1 after 26 frame(s)" in text
    assert "after:  WRAM:0x4:1==1 after 23 frame(s)" in text and "saved 3 frame(s)" in text
    assert "RUN 4 4*|...R.....|" in fake.received          # replayed frames 0-3, then SAVE
    back = bk2.read_bk2(out)
    assert len(back.frames) == len(lines) - 3
    assert any(OPTIMIZED_TAG in c for c in back.comments)
    assert int(back.header["rerecordCount"]) > 0
    new_lines = back.frame_lines()
    assert a_frames(new_lines) == [11]
    g = ToyGame()                                          # replay the whole output movie
    for i, line in enumerate(new_lines):
        g.step(GB.pressed(line))
        if g.wram[GOAL]:
            break
    assert i + 1 == 27                                     # goal after 27 frames instead of 30
    assert new_lines[27:] == lines[30:]                    # the original input after the goal follows


def test_cli_no_movie_after_emuhawk_already_ran_a_frame(tmp_path, capsys):
    # EmuHawk emulates a frame before it starts a --lua script, so HELLO says frame 1. With no
    # movie loaded that's fine as long as the movie's first frame has no input.
    lines = [N] + [L("Right")] * 79
    lines[18] = L("Right", "A")                          # goal at 34; 28 is possible
    src, out = tmp_path / "in.bk2", tmp_path / "out.bk2"
    bk2.write_bk2(src, make_movie(lines))
    fake = FakeBizHawk(pre_frames=1)
    rc = run_cli(["optimize", "--movie", str(src), "--out", str(out), "--start", "5", "--window", "30",
                  "--method", "delay", "--objective", "until WRAM:0x4:1==1", "--max-shift", "6"], fake)
    assert rc == 0
    assert "BizHawk already ran 1 frame(s)" in capsys.readouterr().out
    assert "RUN 4 4*|...R.....|" in fake.received          # frames 1-4 replayed, frame 0 already ran
    back = bk2.read_bk2(out).frame_lines()
    old, new = _goal_frames(lines), _goal_frames(back)
    assert new < old and back[new:] == lines[old:] and len(back) == len(lines) - (old - new)
    # ...but not when the frame BizHawk already ran should have had input
    lines[0] = L("Right")
    bk2.write_bk2(src, make_movie(lines))
    with pytest.raises(ValueError, match=r"frame 0 has input .*--movie"):
        run_cli(["optimize", "--movie", str(src), "--out", str(out), "--start", "5", "--window", "30",
                 "--method", "delay", "--objective", "until WRAM:0x4:1==1"], FakeBizHawk(pre_frames=1))


def test_cli_movie_already_past_start(tmp_path):
    lines = [L("Right")] * 40
    src = tmp_path / "in.bk2"
    bk2.write_bk2(src, make_movie(lines))
    with pytest.raises(ValueError, match="already at frame 2, past --start 1.*--start 2 or later"):
        run_cli(["optimize", "--movie", str(src), "--out", str(tmp_path / "o.bk2"), "--start", "1",
                 "--window", "10", "--objective", "max WRAM:0:1"],
                FakeBizHawk(movie_lines=lines, pre_frames=2))


def test_cli_beam_until_cuts_frames_when_old_goal_is_past_window(tmp_path, capsys):
    # The old input only jumps the wall at frame 40, after the window (4-33), and reaches the goal
    # later. Beam reaches it inside the window; the splice must cut the saved frames so the old
    # post-goal input follows, rather than padding the window with neutral input.
    lines = [L("Right")] * 90
    lines[40] = L("Right", "A")
    lines[70] = L("Start")                                 # marks "after the goal"
    old_goal = _goal_frames(lines)
    assert 34 < old_goal < 70
    src, out = tmp_path / "in.bk2", tmp_path / "out.bk2"
    bk2.write_bk2(src, make_movie(lines))
    rc = run_cli(["optimize", "--movie", str(src), "--out", str(out), "--start", "4", "--window", "30",
                  "--method", "beam", "--objective", "until WRAM:0x4:1==1", "--tiebreak", "max WRAM:0x0:2",
                  "--actions", "", "Right", "Right+A", "--beam-width", "12", "--macro-len", "1"], FakeBizHawk())
    assert rc == 0
    text = capsys.readouterr().out
    assert "not reached" in text
    assert f"reaches the goal after {old_goal - 4} frame(s), past the window" in text
    back = bk2.read_bk2(out).frame_lines()
    hit = _goal_frames(back)
    assert hit <= 34                                       # inside the window now
    assert back[hit:] == lines[old_goal:]                  # old input after its goal follows at once
    assert "saved" in text and len(back) == len(lines) - (old_goal - hit)


def _goal_frames(lines: list[str]) -> int:
    """Frames played until the toy game's goal flag is set (from power-on)."""
    g = ToyGame()
    for i, line in enumerate(lines, 1):
        g.step(GB.pressed(line))
        if g.wram[GOAL]:
            return i
    raise AssertionError("goal not reached")


def _replay_until_goal(lines: list[str]) -> bool:
    g = ToyGame()
    for line in lines:
        g.step(GB.pressed(line))
    return bool(g.wram[GOAL])


def test_cli_refuses_savestate_anchored_movie(tmp_path):
    mv = make_movie([N] * 10)
    mv.header["StartsFromSavestate"] = "True"
    src = tmp_path / "in.bk2"
    bk2.write_bk2(src, mv)
    with pytest.raises(ValueError, match="savestate"):
        run_cli(["optimize", "--movie", str(src), "--out", str(tmp_path / "o.bk2"), "--start", "0",
                 "--window", "5", "--objective", "max WRAM:0:1"], None)


def test_cli_delay_until_door_keeps_length(tmp_path, capsys):
    # The late A press misses the door completely: nothing to align to, so the window is replaced
    # and the movie keeps its length.
    df, lines = door_setup(late=3, length=80)
    src, out = tmp_path / "in.bk2", tmp_path / "out.bk2"
    bk2.write_bk2(src, make_movie(lines))
    rc = run_cli(["optimize", "--movie", str(src), "--out", str(out), "--start", "10", "--window", "40",
                  "--method", "delay", "--objective", "until WRAM:0x5:1==1", "--max-shift", "5"], FakeBizHawk())
    assert rc == 0
    text = capsys.readouterr().out
    assert "not reached" in text and "frames after the window are unchanged" in text
    back = bk2.read_bk2(out)
    assert len(back.frames) == 80 and a_frames(back.frame_lines()) == [df]
    g = ToyGame()
    for line in back.frame_lines()[:df + 1]:
        g.step(GB.pressed(line))
    assert g.wram[DOOR] == 1


def test_cli_beam_with_movie_loaded_in_bizhawk(tmp_path, capsys):
    lines = [L("Right")] * 60
    src, out = tmp_path / "in.bk2", tmp_path / "out.bk2"
    bk2.write_bk2(src, make_movie(lines))
    fake = FakeBizHawk(movie_lines=lines)                  # EmuHawk --movie: SEEK path
    rc = run_cli(["optimize", "--movie", str(src), "--out", str(out), "--start", "4", "--window", "24",
                  "--method", "beam", "--objective", "max WRAM:0x0:2", "--actions", "", "Right", "Right+A",
                  "--beam-width", "8", "--macro-len", "2"], fake)
    assert rc == 0
    text = capsys.readouterr().out
    assert "movie PLAY" in text and "wrote" in text
    assert "SEEK 4" in fake.received
    assert any(m.startswith("EVAL") and " h8192 " in m for m in fake.received)   # default WRAM hash dedup
    back = bk2.read_bk2(out)
    assert len(back.frames) == 60
    g = ToyGame()
    for line in back.frame_lines()[:28]:
        g.step(GB.pressed(line))
    assert g.x > 19                                        # original run was stuck at the wall


def test_cli_mutate_no_improvement_writes_nothing(tmp_path, capsys):
    df, lines = door_setup(late=0, length=50)
    src, out = tmp_path / "in.bk2", tmp_path / "out.bk2"
    bk2.write_bk2(src, make_movie(lines))
    rc = run_cli(["optimize", "--movie", str(src), "--out", str(out), "--start", "0", "--window", "40",
                  "--method", "mutate", "--iterations", "30", "--objective", "max WRAM:0x6:1",
                  "--buttons", "A"], FakeBizHawk())          # the RNG byte doesn't depend on input
    assert rc == 0 and not out.exists()
    assert "no improvement" in capsys.readouterr().out


def test_cli_refuses_wrong_core_and_bus_address(tmp_path):
    lines = [N] * 20
    src = tmp_path / "in.bk2"
    bk2.write_bk2(src, make_movie(lines))
    base = ["optimize", "--movie", str(src), "--out", str(tmp_path / "o.bk2"), "--start", "0",
            "--window", "5", "--method", "delay"]
    with pytest.raises(ValueError, match="GBA core"):
        run_cli(base + ["--objective", "max WRAM:0:1"], FakeBizHawk(system_id="GBA"))
    with pytest.raises(ValueError, match="WRAM:0x1361"):
        run_cli(base + ["--objective", "max WRAM:0xD361:1"], FakeBizHawk())
    with pytest.raises(ValueError, match="different file"):
        run_cli(["optimize", "--movie", str(src), "--out", str(src), "--start", "0", "--window", "5",
                 "--objective", "max WRAM:0:1"], None)
    # bad options fail before the user is asked to start EmuHawk (no fake: nothing connects)
    for extra, msg in ((["--speed", "fast"], "--speed"), (["--speed", "150.5"], "--speed"),
                       (["--beam-width", "0"], "--beam-width"), (["--passes", "0"], "--passes")):
        with pytest.raises(ValueError, match=msg):
            run_cli(base + ["--objective", "max WRAM:0:1"] + extra, None)


# --- the real Lua bot, under lupa ------------------------------------------------------------------------------

def _lua_modules() -> list[str]:
    """Lua 5.4 (what EmuHawk embeds) and 5.1 (no yield across pcall: catches that mistake)."""
    if importlib.util.find_spec("lupa") is None:
        return []
    out = []
    for m in ("lupa.lua54", "lupa.lua51"):
        try:
            importlib.import_module(m)
            out.append(m)
        except ImportError:
            pass
    return out


LUAS = _lua_modules()
needs_lupa = pytest.mark.skipif(not LUAS, reason="lupa (Lua for Python) is not installed; "
                                                 "controllerlog_bot.lua was not executed")


@pytest.fixture(params=LUAS or ["none"])
def lua(request):
    if request.param == "none":
        pytest.skip("lupa is not installed")
    return request.param


def _host(lua_mod, **kw):
    from tests.fixtures.optimize.lua_host import BizHawkLuaHost
    h = BizHawkLuaHost(lua=lua_mod, **kw)
    return h, h.run_until_reply()


def _session_commands() -> list[str]:
    R = L("Right")
    return [
        "PING", "ping", "FOO 1", "FRAME", "SAVE",       # (an empty message reads as a timeout: no reply)
        f"RUN 2 {N}",                                     # without KEYS: setfrommnemonicstr path
        "RUN 2",                                          # neutral needs KEYS
        "KEYS #Up|Down|Turbo|", f"KEYS {GB_LOG_KEY}", "DOMAINS",
        "LOAD 1", f"RUN 10 3*{R}", "FRAME",
        "READ WRAM 0 2 le System%20Bus C000 2 le WRAM 8 2 be WRAM A s1 le WRAM 0 4 le",
        "READ WRAM 0 h32 le", "READ", "READ VRAM 0 1 le", "READ WRAM 1FFF 2 le", "READ WRAM 0 3 le",
        "READ WRAM 0 1 xe", "READ WRAM zz 1 le", "READ WRAM 0",
        "LOAD 1", "SAVE", f"EVAL 1 WRAM 0 2 le | 4*{R}",
        f"EVAL 1 WRAM 0 2 le UNTIL WRAM 0 2 le >= 10 | 30*{R}",
        f"EVAL 1 UNTIL WRAM 4 1 le == 1 | 5*{N}",
        f"EVAL 1 UNTIL WRAM 4 1 le == 0 | {N}",
        f"EVAL 1 WRAM 0 1 le UNTIL WRAM 0 1 le & 0x8 | 20*{R}",
        f"EVAL 1 UNTIL WRAM 0 1 le => 3 | {N}", f"EVAL 1 UNTIL WRAM 0 h4 le == 3 | {N}",
        f"EVAL 1 UNTIL WRAM 0 1 le | {N}",
        f"EVAL 1 WRAM 0 1 le UNTIL WRAM 0 1 le == 1 UNTIL WRAM 0 1 le == 2 | {N}",
        f"EVAL 99 | {N}", "EVAL 1", "EVAL | ", "EVAL 1 | xyz", "EVAL 1 | |...|", "EVAL 1 | 0*|.........|",
        f"EVAL 1 WRAM 0 2 le | {R};{L('Right', 'A')};5*{R}",
        f"BRANCH 1 | 2*{R}", "LOAD 3", "READ WRAM 0 2 le", f"BRANCH 1 WRAM 0 1 le | {R}", "FREE 3", "LOAD 3",
        "FREE", "LOAD x", f"RUN 1 {R};{R}", "RUN x", "SPEED max", "SPEED 150", "SPEED 0", "SPEED", "SPEED 150.5",
        "SEEK 3", "SEEK x",
    ]


@needs_lupa
def test_lua_bot_scripted_session_matches_reference(lua):
    host, hello = _host(lua)
    fake = FakeBizHawk()
    assert hello == fake.hello() == "HELLO GB 0 INACTIVE 1"
    for cmd in _session_commands():
        got, want = host.call(cmd), fake.handle(cmd)
        assert got == want, f"{cmd!r}: lua {got!r} != reference {want!r}"
    # spot checks on the transcript
    replies = dict(zip(_session_commands(), fake.replies))
    assert replies["PING"] == "OK pong"
    assert replies[f"EVAL 1 WRAM 0 2 le UNTIL WRAM 0 2 le >= 10 | 30*{L('Right')}"] == "OK 6 0 6 11"
    assert replies["READ VRAM 0 1 le"] == "ERR unknown memory domain 'VRAM'"
    assert host.throttle is True and host.speed_pct == 150 and host.unthrottled is False
    assert replies["SPEED 150.5"] == "ERR bad SPEED '150.5' (max or 1..6400)"
    assert not any("falling back" in m or "outside the memory" in m for m in host.bizhawk_log)
    assert host.call("QUIT") == "OK bye"
    for _ in range(3):
        host.step()
    assert host.state == "dead"
    assert host.savestates == {}                     # the script freed its states
    assert host.throttle is True                     # restored after SPEED max
    assert host.t.reconnects == 1                    # reconnected on start
    assert host.paused


@needs_lupa
def test_lua_bot_idles_paused_without_emulating(lua):
    host, _ = _host(lua)
    host.call("KEYS " + GB_LOG_KEY)
    host.call("SAVE")
    frames = host.frames_emulated
    for _ in range(200):
        host.step()
    assert host.frames_emulated == frames == 0 and host.paused and host.game.frame == 0
    assert host.state == "yield"
    assert host.call(f"EVAL 1 WRAM 0 2 le | 3*{L('Right')}") == "OK 3 0 5"
    assert host.frames_emulated == 3 and host.paused
    for _ in range(50):
        host.step()
    assert host.frames_emulated == 3


@needs_lupa
def test_lua_bot_first_frame_after_idle_needs_joypad_set(lua):
    # EmuHawk copies joypad.setfrommnemonicstr() overrides into the controller before resuming
    # emu.yield() scripts, then clears them before the frame: without KEYS the first frame of an
    # EVAL started from idle loses its input (x=3 instead of 5). With KEYS (joypad.set) it doesn't.
    host, _ = _host(lua)
    host.call("SAVE")
    assert host.call(f"EVAL 1 WRAM 0 2 le | 3*{L('Right')}") == "OK 3 0 3"
    assert host.frame_inputs[0] == frozenset()
    host.call("KEYS " + GB_LOG_KEY)
    assert host.call(f"EVAL 1 WRAM 0 2 le | 3*{L('Right')}") == "OK 3 0 5"
    assert host.frame_inputs[3] == {"Right"}


@needs_lupa
def test_lua_bot_seek_plays_movie_then_stops_it(lua):
    movie = [L("Right")] * 5 + [N] * 5
    host, hello = _host(lua, movie_lines=movie)
    assert hello == "HELLO GB 0 PLAY 1"
    host.call("KEYS " + GB_LOG_KEY)
    assert host.call("SAVE") == "OK 1"
    assert host.call(f"EVAL 1 | {N}").startswith("ERR a movie is loaded (mode PLAY)")
    assert host.call("SEEK 20") == "ERR movie has only 10 frames"
    assert host.call("SEEK 6") == "OK 6 10"
    assert host.movie_mode == "INACTIVE"
    assert host.call("READ WRAM 0 2 le") == "OK 9"
    assert host.call("SEEK 7") == "ERR SEEK needs a movie loaded in BizHawk (start EmuHawk with --movie)"
    assert host.call("SAVE") == "OK 2"
    assert host.call(f"EVAL 2 WRAM 0 2 le | {L('Right')}") == "OK 7 0 10"
    host.call("QUIT")


@needs_lupa
def test_lua_bot_end_to_end_with_bridge_over_tcp(lua):
    from tests.fixtures.optimize.lua_host import BizHawkLuaHost, SocketTransport
    bridge = BizHawkBridge(port=0, timeout_s=30)
    box: dict = {}

    def emuhawk():
        box["host"] = h = BizHawkLuaHost(transport=SocketTransport("127.0.0.1", bridge.port), lua=lua)
        try:
            h.run_forever()
        except Exception as e:          # surfaced by the assertions below
            box["error"] = e

    th = threading.Thread(target=emuhawk, daemon=True)
    th.start()
    try:
        hello = bridge.wait_for_bizhawk(30)
        assert hello == Hello("GB", 0, "INACTIVE", 1)
        bridge.set_keys(GB_LOG_KEY)
        bridge.speed("max")
        doms = bridge.domains()
        s = bridge.save_state()
        df, lines = door_setup(late=3)
        got = delay_search(bridge, s, lines, DOOR_OPEN, codec=GB, max_shift=5)
        beam = beam_search(bridge, s, 40, ["", "Right", "Right+A"], GOAL_FAST, codec=GB, beam_width=8,
                           dedup=check_reads([RamHash("WRAM")], doms))
    finally:
        bridge.close()
    th.join(20)
    assert not th.is_alive() and "error" not in box, box.get("error")
    host = box["host"]
    assert host.state == "dead" and host.t.reconnects == 1 and host.savestates == {}
    assert a_frames(got.lines) == [df] and got.result.hit == df + 1
    ev = ToyEvaluator()
    es = ev.save_state()
    ref = beam_search(ev, es, 40, ["", "Right", "Right+A"], GOAL_FAST, codec=GB, beam_width=8,
                      dedup=[RamHash("WRAM", 0, 0x2000)])
    assert (beam.lines, beam.result.hit) == (ref.lines, ref.result.hit)


@needs_lupa
def test_lua_bot_speed_max_unthrottles_and_restores_settings(lua):
    from tests.fixtures.optimize.lua_host import BizHawkLuaHost
    host = BizHawkLuaHost(lua=lua)
    host.speed_pct = 75                          # the user's setting, saved when the script starts
    assert host.run_until_reply().startswith("HELLO")
    assert host.call("SPEED max") == "OK"
    assert (host.throttle, host.unthrottled) == (False, True)     # also beats sound/vsync throttle
    assert host.call("SPEED 200") == "OK"
    assert (host.throttle, host.unthrottled, host.speed_pct) == (True, False, 200)
    assert host.call("SPEED max") == "OK"
    assert host.call("QUIT") == "OK bye"
    host.step()
    assert host.state == "dead"
    assert (host.throttle, host.unthrottled, host.speed_pct) == (True, False, 75)


@needs_lupa
def test_lua_bot_gives_up_cleanly_without_optimizer(lua):
    from tests.fixtures.optimize.lua_host import BizHawkLuaHost, QueueTransport

    class Refused(QueueTransport):             # nothing listening: every send fails
        def send(self, msg: str) -> int:
            self.outbox.append(msg)
            return -1

    host = BizHawkLuaHost(transport=Refused(), lua=lua)
    host.step()
    assert host.state == "dead" and not host.paused          # left running, as it was
    assert any("could not reach the ControllerLog optimizer" in m for m in host.printed)
    paused = BizHawkLuaHost(transport=Refused(), lua=lua, start_paused=True)
    paused.step()
    assert paused.state == "dead" and paused.paused
