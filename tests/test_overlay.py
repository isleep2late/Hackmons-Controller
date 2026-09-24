"""Tests for the live overlay server, web assets and controller layouts."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import aiohttp
import pytest

from controllerlog.hub import Hub, now_ns
from controllerlog.layouts import (DEFAULT_THEME, element_inputs, layout_for_family, list_layouts,
                                   load_layout)
from controllerlog.logfile import LogWriter
from controllerlog.model import (AXES, BUTTONS, TRIGGER_PRESS_THRESHOLD, DeviceInfo, InputEvent,
                                 PadState)
from controllerlog.overlay import WEB_DIR, OverlayServer, OverlayServerError
from controllerlog.overlay.server import _Client, recording_path
from controllerlog.timeline import FPS, Timeline, frames_to_events
from controllerlog.logfile import read_log

FIXTURES = Path(__file__).parent / "fixtures" / "overlay"
NODE = shutil.which("node")
MS = 1_000_000

# --- layouts -------------------------------------------------------------------

COMMON = {"dpad_up", "dpad_down", "dpad_left", "dpad_right", "south", "east", "west", "north",
          "back", "start", "guide", "left_shoulder", "right_shoulder", "left_stick", "right_stick",
          "left_trigger", "right_trigger"}
PADDLES = {"left_paddle1", "left_paddle2", "right_paddle1", "right_paddle2"}
GB = {"dpad_up", "dpad_down", "dpad_left", "dpad_right", "south", "east", "back", "start"}
EXPECTED = {
    "xbox": (COMMON | {"misc1"} | PADDLES, 2),
    "playstation": (COMMON | {"misc1", "touchpad"}, 2),
    "switch": (COMMON | {"misc1"}, 2),
    "gamecube": ({"dpad_up", "dpad_down", "dpad_left", "dpad_right", "south", "east", "west", "north",
                  "start", "right_shoulder", "left_trigger", "right_trigger", "misc3", "misc4"}, 2),
    "gameboy": (GB, 0),
    "gba": (GB | {"left_shoulder", "right_shoulder"}, 0),
    "generic": (COMMON | {"misc1", "touchpad"} | PADDLES, 2),
}


def _bbox(el: dict) -> tuple[float, float, float, float]:
    s = el.get("shape", "circle")
    if s == "rect":
        return el["x"], el["y"], el["x"] + el["w"], el["y"] + el["h"]
    if s == "circle":
        return el["cx"] - el["r"], el["cy"] - el["r"], el["cx"] + el["r"], el["cy"] + el["r"]
    if s == "ellipse":
        return el["cx"] - el["rx"], el["cy"] - el["ry"], el["cx"] + el["rx"], el["cy"] + el["ry"]
    if s == "polygon":
        xs, ys = [p[0] for p in el["points"]], [p[1] for p in el["points"]]
        return min(xs), min(ys), max(xs), max(ys)
    return el["x"], el["y"], el["x"], el["y"]


def test_bundled_layouts_are_complete_and_valid():
    names = list_layouts()
    assert set(EXPECTED) <= set(names)
    for name, (required, sticks) in EXPECTED.items():
        layout = load_layout(name)  # validates
        assert layout["families"] == [name]
        assert layout_for_family(name) == name
        w, h = layout["size"]
        assert 460 <= w <= 580 and 250 <= h <= 400, (name, layout["size"])
        assert set(DEFAULT_THEME) <= set(layout["theme"])
        drawn = [inp for el in layout["elements"] for inp in element_inputs(el)]
        assert len(drawn) == len(set(drawn)), f"{name}: input drawn twice"
        assert required <= set(drawn), f"{name}: missing {required - set(drawn)}"
        assert sum(el["type"] == "stick" for el in layout["elements"]) == sticks
        assert layout["history"] and set(layout["history"]) <= set(drawn), name
        for el in layout["body"] + layout["elements"]:
            x0, y0, x1, y1 = _bbox(el)
            assert -2 <= x0 and x1 <= w + 2 and -2 <= y0 and y1 <= h + 2, (name, el)
    assert layout_for_family("no-such-family") == "generic"


def test_switch_and_handheld_positional_labels():
    labels = {el.get("input"): el.get("label") for el in load_layout("switch")["elements"]}
    assert (labels["south"], labels["east"], labels["west"], labels["north"]) == ("B", "A", "Y", "X")
    gb = {el.get("input"): el.get("label") for el in load_layout("gameboy")["elements"]}
    assert gb["east"] == "A" and gb["south"] == "B"
    trig = [el for el in load_layout("switch")["elements"] if el["type"] == "trigger"]
    assert sorted(el["label"] for el in trig) == ["ZL", "ZR"]


def test_js_model_matches_python():
    src = (WEB_DIR / "js" / "model.js").read_text(encoding="utf-8")

    def js_list(name: str) -> list[str]:
        m = re.search(rf"export const {name} = \[(.*?)\];", src, re.S)
        assert m, name
        return re.findall(r'"([a-z0-9_]+)"', m.group(1))

    assert tuple(js_list("BUTTONS")) == BUTTONS
    assert tuple(js_list("AXES")) == AXES
    assert f"TRIGGER_PRESS_THRESHOLD = {TRIGGER_PRESS_THRESHOLD};" in src


# --- recordings fixture -------------------------------------------------------------


def write_fixture(path: Path) -> None:
    pad = DeviceInfo(0, name="Test Pad", backend="virtual", family="xbox")
    other = DeviceInfo(1, name="Other", backend="virtual", family="playstation")
    ev = []
    ev += [InputEvent(100 * MS, 0, "b", 0, 1), InputEvent(200 * MS, 0, "b", 0, 0)]
    ev += [InputEvent(500 * MS, 0, "b", 1, 1), InputEvent(516_666_667, 0, "b", 1, 0)]
    for i in range(10):  # mash west
        ev += [InputEvent((1000 + 50 * i) * MS, 0, "b", 2, 1), InputEvent((1020 + 50 * i) * MS, 0, "b", 2, 0)]
    ev += [InputEvent(1500 * MS, 0, "a", 4, 20000), InputEvent(1600 * MS, 0, "a", 4, 0),
           InputEvent(1700 * MS, 0, "a", 4, 10000), InputEvent(1800 * MS, 0, "a", 4, 0)]
    ev += [InputEvent(2000 * MS, 0, "a", 0, 32767), InputEvent(2100 * MS, 0, "a", 0, 0)]
    ev += [InputEvent(2500 * MS, None, "m", None, "split:Level 1")]
    ev += [InputEvent(2600 * MS, 0, "b", 10, 1)]  # held to the end
    ev += [InputEvent(2700 * MS, 1, "b", 0, 1), InputEvent(2800 * MS, 1, "b", 0, 0)]
    ev += [InputEvent(3000 * MS, 0, "a", 3, -5)]
    with LogWriter(path, devices=[pad, other], meta={"game": "Test Game"}) as w:
        for e in ev:
            w.write(e)


@pytest.fixture
def hub_dev():
    hub = Hub()
    dev = hub.connect(("test", 1), DeviceInfo(0, name="Test Pad", backend="virtual", family="xbox"))
    return hub, dev


@pytest.fixture
def server(hub_dev, tmp_path):
    hub, _ = hub_dev
    rec_dir = tmp_path / "recs"
    rec_dir.mkdir()
    write_fixture(rec_dir / "run1.ctlog")
    (rec_dir / "notes.txt").write_text("secret")
    write_fixture(tmp_path / "outside.ctlog")
    srv = OverlayServer(hub, port=0, recordings_dir=rec_dir)
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


def get(srv: OverlayServer, path: str, headers: dict | None = None) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(srv.url.rstrip("/") + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, dict(e.headers), e.read()


# --- http ----------------------------------------------------------------------------

def test_pages_and_static_files(server):
    for path in ("/", "/viewer"):
        status, headers, body = get(server, path)
        assert status == 200 and headers["Content-Type"].startswith("text/html")
        assert b'<script type="module"' in body
    status, headers, body = get(server, "/static/js/layout-svg.js")
    assert status == 200 and headers["Content-Type"].startswith("text/javascript")
    assert b"export function createController" in body
    for path in ("/static/css/overlay.css", "/static/css/viewer.css", "/static/favicon.svg",
                 "/static/js/overlay.js", "/static/js/viewer.js"):
        assert get(server, path)[0] == 200, path
    for path in ("/static/../server.py", "/static/..%2Fserver.py", "/static/js/..%5C..%5Cserver.py",
                 "/static/nope.js"):
        assert get(server, path)[0] == 404, path


def test_layout_routes(server):
    status, headers, body = get(server, "/layouts")
    assert status == 200
    names = json.loads(body)
    assert "xbox" in names and "gameboy" in names
    status, _, body = get(server, "/layouts/xbox.json")
    layout = json.loads(body)
    assert status == 200 and layout["name"] == "xbox"
    assert set(DEFAULT_THEME) <= set(layout["theme"]) and layout["history"]
    assert get(server, "/layouts/nope.json")[0] == 404
    assert get(server, "/layouts/..%5C..%5Cpyproject.json")[0] == 404
    status, _, body = get(server, "/api/layouts")
    listing = {d["name"]: d for d in json.loads(body)}
    assert listing["playstation"]["families"] == ["playstation"]


def test_api_state(server, hub_dev):
    hub, dev = hub_dev
    hub.publish(InputEvent(now_ns(), dev, "b", 3, 1))
    status, _, body = get(server, "/api/state")
    state = json.loads(body)
    assert status == 200 and state["protocol"] == 1
    d = state["devices"][0]
    assert d["id"] == dev and d["family"] == "xbox"
    assert len(d["state"]["buttons"]) == 26 and d["state"]["buttons"][3] == 1 and len(d["state"]["axes"]) == 6


def test_recordings_list_and_load(server):
    status, _, body = get(server, "/api/recordings")
    files = json.loads(body)
    assert status == 200 and [f["name"] for f in files] == ["run1.ctlog"]
    assert files[0]["size"] > 0 and files[0]["mtime"] > 0
    status, _, body = get(server, "/api/recording?name=run1.ctlog")
    data = json.loads(body)
    assert status == 200 and data["name"] == "run1.ctlog"
    assert data["header"]["format"] == "controllerlog" and data["header"]["meta"]["game"] == "Test Game"
    assert {d["id"] for d in data["devices"]} == {0, 1}
    rec = read_log(Path(server.recordings_dir) / "run1.ctlog")
    assert data["events"] == [e.to_row() for e in rec.events]


@pytest.mark.parametrize("name", [
    "../outside.ctlog", "..\\outside.ctlog", "..%2Foutside.ctlog", "notes.txt", "run1.ctlog/../run1.ctlog",
    "C:\\Windows\\win.ini", "C:outside.ctlog", "/etc/passwd.ctlog", ".hidden.ctlog", "", "run1.ctlog ",
    "run1.ctlog:stream", "sub/run1.ctlog",
])
def test_recording_rejects_bad_names(server, name, tmp_path):
    status, _, body = get(server, "/api/recording?name=" + urllib.request.quote(name, safe="%"))
    assert status == 400, (name, status, body)


def test_recording_rejects_absolute_and_missing(server, tmp_path):
    absolute = str(tmp_path / "outside.ctlog")
    assert get(server, "/api/recording?name=" + urllib.request.quote(absolute))[0] == 400
    assert get(server, "/api/recording?name=missing.ctlog")[0] == 404
    with pytest.raises(ValueError):
        recording_path(tmp_path / "recs", absolute)
    with pytest.raises(FileNotFoundError):
        recording_path(tmp_path / "recs", "missing.ctlog")
    assert recording_path(tmp_path / "recs", "run1.ctlog").name == "run1.ctlog"


def test_no_recordings_dir(hub_dev):
    hub, _ = hub_dev
    with OverlayServer(hub, port=0) as srv:
        assert json.loads(get(srv, "/api/recordings")[2]) == []
        assert get(srv, "/api/recording?name=run1.ctlog")[0] == 404


def test_host_header_guard(server):
    assert get(server, "/", headers={"Host": "evil.example:1234"})[0] == 403
    assert get(server, "/", headers={"Host": f"localhost:{server.port}"})[0] == 200


def test_port_busy_and_restart(hub_dev):
    hub, _ = hub_dev
    srv = OverlayServer(hub, port=0)
    srv.start()
    port = srv.port
    try:
        assert srv.running and srv.url == f"http://127.0.0.1:{port}/"
        with pytest.raises(OverlayServerError, match="in use"):
            OverlayServer(hub, port=port).start()
    finally:
        srv.stop()
    assert not srv.running
    again = OverlayServer(hub, port=port)
    again.start()  # port released by stop()
    again.stop()
    srv.start()  # the same instance can be restarted and still streams events
    try:
        async def one_event():
            async with aiohttp.ClientSession() as s, s.ws_connect(srv.url + "ws") as ws:
                assert (await ws.receive_json(timeout=5))["type"] == "hello"
                hub.mark("again")
                rows, _ = await _rows(ws, 1)
                assert rows[0][2:] == ["m", None, "again"]
        asyncio.run(one_event())
    finally:
        srv.stop()
    assert srv.overlay_url(layout="gba", history=0).endswith("/?layout=gba&history=0")
    assert srv.overlay_url(history=False, counts=True, map=None).endswith("/?history=0&counts=1")
    assert srv.viewer_url("run 1.ctlog").endswith("/viewer?file=run+1.ctlog")


# --- websocket -----------------------------------------------------------------------

async def _rows(ws: aiohttp.ClientWebSocketResponse, n: int, timeout: float = 5.0) -> tuple[list, int]:
    rows: list = []
    frames = 0
    deadline = time.monotonic() + timeout
    while len(rows) < n:
        msg = await ws.receive_json(timeout=max(0.05, deadline - time.monotonic()))
        if msg["type"] == "batch":
            rows += msg["events"]
            frames += 1
    return rows, frames


def test_websocket_hello_batch_and_ping(server, hub_dev):
    hub, dev = hub_dev

    async def run():
        async with aiohttp.ClientSession() as s, s.ws_connect(server.url + "ws") as ws:
            hello = await ws.receive_json(timeout=5)
            assert hello["type"] == "hello" and hello["protocol"] == 1
            assert isinstance(hello["server_time_ns"], int)
            d = hello["devices"][0]
            assert d["id"] == dev and d["name"] == "Test Pad"
            assert len(d["state"]["buttons"]) == 26 and len(d["state"]["axes"]) == 6
            t = now_ns()
            sent = time.perf_counter()
            hub.publish(InputEvent(t, dev, "b", 0, 1))
            rows, _ = await _rows(ws, 1)
            latency = time.perf_counter() - sent
            assert rows == [[t, dev, "b", 0, 1]]
            assert latency < 0.1, latency  # idle: flushed immediately
            hub.publish(InputEvent(t + 1, dev, "a", 1, -1234))
            hub.publish(InputEvent(t + 2, dev, "a", 1, -1234))  # duplicate, dropped by the hub
            hub.mark("split:1", t_ns=t + 3)
            rows, _ = await _rows(ws, 2)
            assert rows == [[t + 1, dev, "a", 1, -1234], [t + 3, None, "m", None, "split:1"]]
            await ws.send_json({"type": "ping", "id": 42})
            while True:
                msg = await ws.receive_json(timeout=5)
                if msg["type"] == "pong":
                    break
            assert msg["id"] == 42 and msg["server_time_ns"] >= t
            # device lifecycle: '+' carries device json, '-' has three items and follows releases
            dev2 = hub.connect(("test", 2), DeviceInfo(0, name="Second", family="gba"))
            hub.publish(InputEvent(now_ns(), dev2, "b", 1, 1))
            hub.disconnect(dev2)
            rows, _ = await _rows(ws, 4)
            assert rows[0][2] == "+" and rows[0][4]["name"] == "Second" and rows[0][4]["family"] == "gba"
            assert rows[1][2:] == ["b", 1, 1] and rows[2][2:] == ["b", 1, 0]
            assert len(rows[3]) == 3 and rows[3][1:] == [dev2, "-"]

    asyncio.run(run())


def test_websocket_batches_bursts_and_serves_many_clients(server, hub_dev):
    hub, dev = hub_dev

    async def run():
        async with aiohttp.ClientSession() as s:
            clients = [await s.ws_connect(server.url + "ws") for _ in range(3)]
            for ws in clients:
                assert (await ws.receive_json(timeout=5))["type"] == "hello"
            n = 3000
            t0 = now_ns()
            for i in range(n):
                hub.publish(InputEvent(t0 + i, dev, "b", 5, (i + 1) % 2))
            results = await asyncio.gather(*(_rows(ws, n) for ws in clients))
            for rows, frames in results:
                assert [r[0] for r in rows] == [t0 + i for i in range(n)]
                assert frames < n / 10, frames  # batched, not one frame per event
            assert server.stats["clients"] == 3
            for ws in clients:
                await ws.close()

    asyncio.run(run())


def test_slow_client_gets_resync_not_hang(hub_dev):
    hub, dev = hub_dev
    srv = OverlayServer(hub, port=0, client_queue_limit=200)
    srv.start()

    async def run():
        async with aiohttp.ClientSession() as s:
            slow = await s.ws_connect(srv.url + "ws", max_msg_size=0)
            fast = await s.ws_connect(srv.url + "ws", max_msg_size=0)
            assert (await slow.receive_json(timeout=5))["type"] == "hello"
            assert (await fast.receive_json(timeout=5))["type"] == "hello"
            fast_state = {"frames": 0, "east": 0}

            async def read_fast():
                async for msg in fast:
                    data = json.loads(msg.data)
                    fast_state["frames"] += 1
                    if data["type"] == "hello":
                        fast_state["east"] = data["devices"][0]["state"]["buttons"][1]
                    elif data["type"] == "batch":
                        for r in data["events"]:
                            if r[2] == "b" and r[3] == 1:
                                fast_state["east"] = r[4]

            reader = asyncio.create_task(read_fast())
            big = "x" * 20000
            deadline = time.monotonic() + 30
            while srv.stats["overflows"] == 0 and time.monotonic() < deadline:
                for _ in range(40):
                    hub.mark(big)
                await asyncio.sleep(0.002)
            assert srv.stats["overflows"] >= 1, "slow client never overflowed"
            # the server loop is still responsive while the slow client is stuck
            t = time.perf_counter()
            async with s.get(srv.url + "api/state") as r:
                assert r.status == 200
            assert time.perf_counter() - t < 2.0
            hub.publish(InputEvent(now_ns(), dev, "b", 1, 1))
            # the fast client keeps receiving and sees the press
            deadline = time.monotonic() + 10
            while fast_state["east"] != 1 and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert fast_state["east"] == 1 and fast_state["frames"] > 2
            # the slow client drains, then gets a fresh hello with the current state
            hellos, east = 0, 0
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not (hellos and east == 1):
                data = await slow.receive_json(timeout=max(0.05, deadline - time.monotonic()))
                if data["type"] == "hello":
                    hellos += 1
                    east = data["devices"][0]["state"]["buttons"][1]
                elif data["type"] == "batch":
                    for r in data["events"]:
                        if r[2] == "b" and r[3] == 1:
                            east = r[4]
            assert hellos >= 1 and east == 1
            reader.cancel()
            await slow.close()
            await fast.close()

    try:
        asyncio.run(run())
    finally:
        srv.stop()


def test_websocket_origin_check(server):
    async def run():
        async with aiohttp.ClientSession() as s:
            with pytest.raises(aiohttp.WSServerHandshakeError) as err:
                await s.ws_connect(server.url + "ws", headers={"Origin": "http://evil.example"})
            assert err.value.status == 403
            for origin in (f"http://127.0.0.1:{server.port}", "http://localhost:3000", "file://"):
                async with s.ws_connect(server.url + "ws", headers={"Origin": origin}) as ws:
                    assert (await ws.receive_json(timeout=5))["type"] == "hello"
            # opaque origin: sandboxed iframes / data: URLs on *any* website send "null"
            for origin in ("null", "http://localhost.evil.example", "http://127.0.0.1.evil.example"):
                with pytest.raises(aiohttp.WSServerHandshakeError) as err:
                    await s.ws_connect(server.url + "ws", headers={"Origin": origin})
                assert err.value.status == 403, origin

    asyncio.run(run())


def test_websocket_allowed_origins(hub_dev):
    hub, _ = hub_dev

    async def run(srv, origin):
        async with aiohttp.ClientSession() as s, s.ws_connect(srv.url + "ws", headers={"Origin": origin}) as ws:
            return (await ws.receive_json(timeout=5))["type"]

    with OverlayServer(hub, port=0, allowed_origins=["null", "https://my.site/"]) as srv:
        assert asyncio.run(run(srv, "null")) == "hello"
        assert asyncio.run(run(srv, "https://my.site")) == "hello"
    with OverlayServer(hub, port=0, allowed_origins=["*"]) as srv:
        assert asyncio.run(run(srv, "http://anything.example")) == "hello"


def test_client_pong_queue_is_bounded():
    """A client that floods pings without reading can't grow server memory without bound."""
    async def run():
        srv = OverlayServer(Hub(), port=0)
        c = _Client(None, 10, "peer")  # type: ignore[arg-type]
        for i in range(10_000):
            srv._on_client_message(c, json.dumps({"type": "ping", "id": i}))
        srv._on_client_message(c, "not json")
        return list(c.control)
    pongs = asyncio.run(run())
    assert 1 <= len(pongs) <= 64 and pongs[-1] == {"type": "pong", "id": 9999}


def test_stop_closes_clients(hub_dev):
    hub, _ = hub_dev
    srv = OverlayServer(hub, port=0)
    srv.start()

    async def run():
        async with aiohttp.ClientSession() as s, s.ws_connect(srv.url + "ws") as ws:
            assert (await ws.receive_json(timeout=5))["type"] == "hello"
            t = time.perf_counter()
            await asyncio.get_running_loop().run_in_executor(None, srv.stop)
            assert time.perf_counter() - t < 5
            msg = await ws.receive(timeout=5)
            assert msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING)

    asyncio.run(run())
    assert not srv.running
    assert srv._sink not in hub._sinks


# --- javascript ----------------------------------------------------------------------

@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_js_syntax():
    files = sorted((WEB_DIR / "js").glob("*.js"))
    assert len(files) >= 7
    for f in files:
        r = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, f"{f.name}: {r.stderr}"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_js_analysis_matches_python(tmp_path):
    rec_path = tmp_path / "fixture.ctlog"
    write_fixture(rec_path)
    layouts = {name: load_layout(name) for name in list_layouts()}
    layouts_path = tmp_path / "layouts.json"
    layouts_path.write_text(json.dumps(layouts), encoding="utf-8")
    r = subprocess.run([NODE, str(FIXTURES / "js_check.mjs"), str(WEB_DIR), str(rec_path), "60",
                        str(layouts_path)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert tuple(out["buttons"]) == BUTTONS and tuple(out["axes"]) == AXES
    assert out["threshold"] == TRIGGER_PRESS_THRESHOLD
    for k in ("gb", "gba", "nes", "snes"):
        assert out["fps"][k] == pytest.approx(FPS[k], rel=1e-9)

    rec = read_log(rec_path)
    tl = Timeline(rec)
    assert out["primary"] == rec.primary_device() == 0
    assert out["duration_ms"] == pytest.approx(rec.duration_ns / 1e6)
    assert out["markers"] == [{"t": 2500.0, "label": "split:Level 1"}]
    assert out["bad_lines"] == 0
    py = tl.stats(0, fps=60)
    assert set(out["stats"]) == set(py)
    for name, st in py.items():
        js = out["stats"][name]
        assert js["presses"] == st["presses"], name
        assert round(js["holdMin"], 2) == pytest.approx(st["hold_frames_min"]), name
        assert round(js["holdMax"], 2) == pytest.approx(st["hold_frames_max"]), name
        assert round(js["holdMean"], 2) == pytest.approx(st["hold_frames_mean"]), name
        assert js["peakPerSec"] == pytest.approx(st["peak_mash_hz"]), name
    for t_ms, st in out["states"].items():
        ref = tl.state_at(0, round(float(t_ms) * MS))
        assert st == ref.to_json(), t_ms
    assert out["probe_consistent"]
    assert out["mapped_east"] == 2 and out["mapped_south"] == 0
    assert out["format_time"] == ["0:00.000", "1:02.346", "-0:00.017", "1:02:03.000"]
    assert "unknown input" in out["map_error"]

    for name, info in out["layouts"].items():
        layout = layouts[name]
        assert info["width"] == layout["size"][0] and info["height"] == layout["size"][1]
        # everything lights except sticks without a click and "south" (remapped onto east)
        unlit = sum(1 for e in layout["elements"]
                    if (e["type"] == "stick" and not e.get("button")) or e.get("input") == "south")
        assert info["lit"] >= len(layout["elements"]) - unlit, name
        assert info["badges"] >= 1, name  # east counter (south is remapped onto east)
    assert out["layout_for_family"] == {"xbox": "xbox", "nope": "generic"}
    # malformed rows never become phantom presses (Uint8Array would wrap null/256 to "south")
    assert sum(out["junk"]["presses"]) == 1 and out["junk"]["presses"][3] == 1
    assert out["junk"]["state"][:4] == [0, 0, 0, 1] and out["junk"]["axes"] == [0] * 6
    assert out["junk_live_changed"] == [False, False, False]


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("fps_key", ["60", "gba", "nes"])
def test_js_frame_stepping_matches_timeline_frames(tmp_path, fps_key):
    """Viewer frame stepping on a frame-aligned (TAS / bk2 / GSE) recording shows exactly
    the per-frame states of Timeline.frames, also with time 0 at a marker between frames."""
    fps = FPS[fps_key]
    count = 150
    frames = []
    for i in range(count):
        st = PadState()
        st.buttons[0] = i % 2                     # A every other frame
        st.buttons[1] = (i // 3) % 2
        st.buttons[11] = 1 if i % 7 in (2, 3) else 0
        st.axes[0] = [0, 32767, -32768, 12000][i % 4]
        st.axes[4] = 32767 if i % 5 == 1 else 0
        frames.append(st)
    marker_ns = 1_234_567_891                     # not on any frame boundary
    events = [InputEvent(5 * MS, 0, "b", 2, 1), InputEvent(6 * MS, 0, "b", 2, 0)]  # before time 0
    events += frames_to_events(frames, fps, device=0, start_ns=marker_ns)
    path = tmp_path / f"frames_{fps_key}.ctlog"
    with LogWriter(path, devices=[DeviceInfo(0, name="TAS")]) as w:
        w.mark(marker_ns, "start")
        for e in sorted(events, key=lambda e: e.t_ns):
            w.write(e)
    ref = Timeline(read_log(path)).frames(0, fps=fps, offset_ns=marker_ns, count=count, mode="sample")
    assert [s.to_json() for s in ref] == [s.to_json() for s in frames]  # sanity of the fixture
    layouts_path = tmp_path / "layouts.json"
    layouts_path.write_text(json.dumps({"xbox": load_layout("xbox")}), encoding="utf-8")
    rec_path = tmp_path / "fixture.ctlog"
    write_fixture(rec_path)
    r = subprocess.run([NODE, str(FIXTURES / "js_check.mjs"), str(WEB_DIR), str(rec_path), "60",
                        str(layouts_path), str(path), fps_key, str(count)],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["frame_numbers_ok"] and out["frame_numbers_1khz_ok"]
    bad = [k for k, (js, py) in enumerate(zip(out["frame_states"], ref)) if js != py.to_json()]
    assert not bad, f"frames showing the wrong state: {bad[:10]}"


# --- security hardening ---------------------------------------------------------------

def _raw_get(port: int, target: str, host: str | None = None) -> int:
    import socket
    host = host or f"127.0.0.1:{port}"
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(f"GET {target} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    return int(data.split(b" ", 2)[1])


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="UNC join semantics are Windows-only")
@pytest.mark.parametrize("target", ["/static///attacker.invalid/share/x.js",
                                    "/static///attacker.invalid/share/js/overlay.js",
                                    "/static/%2F%2Fattacker.invalid/share/x.js"])
def test_static_never_touches_unc_path(monkeypatch, target):
    import os
    touched = []
    real_realpath, real_stat = os.path.realpath, os.stat

    def is_unc(p):
        s = os.fspath(p) if not isinstance(p, int) else ""
        return isinstance(s, str) and s.replace("/", "\\").startswith("\\\\")

    def spy_realpath(p, *a, **kw):
        if is_unc(p):  # would go through the SMB redirector (NTLM auth, ~20 s stall); don't
            touched.append(os.fspath(p))
            return os.fspath(p)
        return real_realpath(p, *a, **kw)

    def spy_stat(p, *a, **kw):
        if is_unc(p):
            touched.append(os.fspath(p))
            raise FileNotFoundError(p)
        return real_stat(p, *a, **kw)

    monkeypatch.setattr(os.path, "realpath", spy_realpath)
    monkeypatch.setattr(os, "stat", spy_stat)
    with OverlayServer(Hub(), port=0, high_res_timer=False) as srv:
        status = _raw_get(srv.port, target)
        assert _raw_get(srv.port, "/static/js/overlay.js") == 200
    assert touched == [], f"request-derived UNC path reached the filesystem: {touched}"
    assert status == 404


def test_viewer_api_serves_unclosed_gz_recording(tmp_path):
    recs = tmp_path / "recs"
    recs.mkdir()
    w = LogWriter(recs / "live.ctlog.gz")
    try:
        for i in range(100):
            w.write(InputEvent(i * MS, 0, "a", 0, i))
        w.flush()
        shutil.copyfile(recs / "live.ctlog.gz", recs / "crashed.ctlog.gz")   # killed process
    finally:
        w.close()
    with OverlayServer(Hub(), port=0, recordings_dir=recs, high_res_timer=False) as srv:
        status, _, body = get(srv, "/api/recording?name=crashed.ctlog.gz")
    assert status == 200, (status, body[:200])
    assert len(json.loads(body)["events"]) == 100


def _status(port: int, path: str, host_header: str) -> int:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"Host": host_header})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        with e:
            return e.code


def test_wildcard_bind_still_rejects_foreign_host_header(tmp_path):
    import socket
    with LogWriter(tmp_path / "pb.ctlog", devices=[DeviceInfo(0, "Pad")]) as w:
        w.mark(1000, "split:1")
    with OverlayServer(Hub(), host="0.0.0.0", port=0, recordings_dir=tmp_path,
                       allowed_hosts=["overlay.lan"], high_res_timer=False) as srv:
        p = srv.port
        # legitimate LAN / local access keeps working
        assert _status(p, "/api/recordings", f"127.0.0.1:{p}") == 200
        assert _status(p, "/api/recordings", f"192.168.1.20:{p}") == 200
        assert _status(p, "/api/recordings", f"localhost:{p}") == 200
        assert _status(p, "/api/recordings", f"{socket.gethostname()}:{p}") == 200
        assert _status(p, "/api/recordings", f"overlay.lan:{p}") == 200     # --allow-host
        # a DNS-rebinding page reaches the loopback listener with its own Host name
        assert _status(p, "/api/recordings", f"rebind.evil.example:{p}") == 403
        assert _status(p, "/api/recording?name=pb.ctlog", f"rebind.evil.example:{p}") == 403


EVIL_COLOUR = '#fff"></span><img src=x onerror="alert(document.domain)"><span x="'


def _write_layout(tmp_path, mutate):
    from controllerlog.layouts import LAYOUT_DIR
    layout = json.loads((LAYOUT_DIR / "gameboy.json").read_text(encoding="utf-8"))
    mutate(layout)
    (tmp_path / "evil.json").write_text(json.dumps(layout), encoding="utf-8")


def test_layout_theme_colour_must_be_a_colour(tmp_path):
    from controllerlog.layouts import LayoutError
    _write_layout(tmp_path, lambda L: L.setdefault("theme", {}).__setitem__("active", EVIL_COLOUR))
    with pytest.raises(LayoutError):
        load_layout("evil", tmp_path)


def test_layout_element_colour_must_be_a_colour(tmp_path):
    from controllerlog.layouts import LayoutError

    def mutate(L):
        L["elements"][0]["active"] = EVIL_COLOUR
    _write_layout(tmp_path, mutate)
    with pytest.raises(LayoutError):
        load_layout("evil", tmp_path)


def test_bundled_layouts_pass_colour_validation():
    for name in list_layouts():
        load_layout(name)


def test_viewer_stats_does_not_interpolate_colour_into_html():
    src = (WEB_DIR / "js" / "viewer.js").read_text(encoding="utf-8")
    assert not re.search(r"style=\"background:\$\{row\.color\}\"", src)
