"""Live overlay + recording viewer web server (aiohttp, own thread and event loop).

Routes::

    GET /                      overlay page (OBS browser source), see web/overlay.html
    GET /viewer                recording viewer page
    GET /static/<path>         files from ``web/``
    GET /layouts               ["gameboy", "xbox", ...]
    GET /layouts/<name>.json   a layout (validated, theme merged with the defaults)
    GET /api/layouts           [{"name", "title", "families", "size"}, ...]
    GET /api/state             {"protocol", "server_time_ns", "devices": [...]} (hub snapshot)
    GET /api/recordings        [{"name", "size", "mtime"}, ...] newest first ([] if no recordings_dir)
    GET /api/recording?name=F  {"name", "header", "devices": [...], "events": [row, ...]}
    GET /ws                    live event stream (protocol below)

WebSocket protocol v1 (``/ws``, JSON text frames)
-------------------------------------------------

Server -> client:

* ``{"type": "hello", "protocol": 1, "server": "controllerlog/<ver>",
  "server_time_ns": int, "devices": [{...DeviceInfo.to_json(), "state":
  {"buttons": [26 ints], "axes": [6 ints]}}, ...]}``
  Always the first frame. Sent again whenever the client fell behind and
  queued events had to be dropped: a client must then *replace* its device
  table and states with the ones in the new hello.
* ``{"type": "batch", "events": [[t_ns, device, kind, code, value], ...]}``
  Rows exactly as :meth:`controllerlog.model.InputEvent.to_row`: kinds ``b``
  (button index, 1/0), ``a`` (axis index, int16), ``+`` (device connected,
  value = device json, state resets to neutral), ``-`` (device disconnected,
  row has 3 items) and ``m`` (marker, value = label, device null). Batches are
  flushed immediately when idle and at most every ``batch_interval`` (5 ms)
  under load. Events are state changes, so replaying one twice is harmless.
* ``{"type": "pong", "server_time_ns": int, "id": <echoed>}``

Client -> server (optional): ``{"type": "ping", "id": any}``.

All ``t_ns`` / ``server_time_ns`` values are ``time.perf_counter_ns()`` of the
server (the hub clock). Clients map them onto their own clock with the hello's
``server_time_ns`` and refine the offset with ping/pong round trips.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import ipaddress
import json
import logging
import os
import socket
import threading
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import urlencode

from aiohttp import WSMsgType, web
from yarl import URL

from .. import __version__
from ..hub import Hub, now_ns
from ..layouts import LAYOUT_DIR, LayoutError, list_layouts, load_layout
from ..logfile import LogFormatError, read_log
from ..model import InputEvent
from ..playback import high_resolution_timer

PROTOCOL_VERSION = 1
WEB_DIR = Path(__file__).resolve().parent / "web"
RECORDING_SUFFIXES = (".ctlog", ".ctlog.gz")
MAX_BATCH_EVENTS = 4096
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png",
    ".ico": "image/x-icon", ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8",
}
_LOCAL_NAMES = {"localhost", "127.0.0.1", "::1"}
_CORS = {"Access-Control-Allow-Origin": "*"}  # layouts only: public, lets host=/port= overlays load them

log = logging.getLogger(__name__)


class OverlayServerError(RuntimeError):
    """The overlay server could not start (port busy, bad host...)."""


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))


def _query(params: dict[str, Any]) -> str:
    return urlencode({k: int(v) if isinstance(v, bool) else v for k, v in params.items() if v is not None})


def _is_local_hostname(host: str | None) -> bool:
    if not host:
        return True
    host = host.strip("[]").lower()
    if host in _LOCAL_NAMES or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def recording_path(directory: Path, name: str) -> Path:
    """Resolve ``name`` to a recording file directly inside ``directory``.

    Raises ``ValueError`` for anything that is not a plain ``*.ctlog`` /
    ``*.ctlog.gz`` file name (separators, ``..``, drive letters, absolute paths,
    other extensions) and ``FileNotFoundError`` if no such file exists.
    """
    if not name or len(name) > 255 or any(c in name for c in '/\\:\0') or name != name.strip():
        raise ValueError(f"invalid recording name {name!r}")
    if name.startswith(".") or PurePosixPath(name).name != name or os.path.isabs(name):
        raise ValueError(f"invalid recording name {name!r}")
    if not name.lower().endswith(RECORDING_SUFFIXES):
        raise ValueError(f"not a recording file: {name!r}")
    base = directory.resolve()
    path = (base / name).resolve()
    if path.parent != base or not path.is_file():
        raise FileNotFoundError(name)
    return path


def static_files(web_dir: Path = WEB_DIR) -> dict[str, Path]:
    """``{"js/overlay.js": Path, ...}``: every servable file under ``web_dir`` (no dot files)."""
    base = web_dir.resolve()
    out = {}
    for p in base.rglob("*"):
        rel = p.relative_to(base)
        if p.is_file() and not any(x.startswith(".") for x in rel.parts):
            out[rel.as_posix()] = p
    return out


def list_recordings(directory: Path) -> list[dict[str, Any]]:
    """``[{"name", "size", "mtime"}]`` of recordings directly inside ``directory``, newest first."""
    out = []
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    for p in entries:
        if not p.name.lower().endswith(RECORDING_SUFFIXES) or p.name.startswith("."):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if p.is_file():
            out.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
    out.sort(key=lambda d: d["mtime"], reverse=True)
    return out


class _Client:
    """One websocket client: bounded queue of pre-serialized batches."""

    __slots__ = ("ws", "limit", "queue", "queued", "control", "resync", "wake", "closed",
                 "hellos", "overflows", "peer")

    def __init__(self, ws: web.WebSocketResponse, limit: int, peer: str) -> None:
        self.ws = ws
        self.limit = limit
        self.queue: collections.deque[tuple[str, int]] = collections.deque()
        self.queued = 0                      # events currently queued
        self.control: collections.deque[dict[str, Any]] = collections.deque(maxlen=64)  # pongs
        self.resync = True                   # next frame is a (fresh) hello
        self.wake = asyncio.Event()
        self.closed = False
        self.hellos = 0
        self.overflows = 0
        self.peer = peer

    def offer(self, msg: str, n: int) -> bool:
        """Queue a batch; on overflow drop everything and schedule a resync. Returns True on overflow."""
        if self.closed or self.resync:
            return False  # the coming hello snapshot already covers these events
        if self.queue and self.queued + n > self.limit:
            self.queue.clear()
            self.queued = 0
            self.resync = True
            self.overflows += 1
            self.wake.set()
            return True
        self.queue.append((msg, n))
        self.queued += n
        self.wake.set()
        return False


class OverlayServer:
    """Serves the live overlay, the recording viewer and the ``/ws`` event stream.

    Runs aiohttp on a private asyncio loop in a daemon thread. Registers itself
    as a :class:`~controllerlog.hub.Hub` sink on :meth:`start`; the sink only
    appends to a deque and wakes the loop, so it never blocks input capture.

    Args:
        hub: event source.
        host / port: listen address (``port=0`` picks a free port, see :attr:`port`).
        recordings_dir: directory whose ``*.ctlog`` files the viewer may list and open.
        layouts_dir: layout directory (default: the bundled ``controllerlog/layouts``).
        batch_interval: minimum seconds between two batches under load.
        client_queue_limit: max events queued per websocket client before the
            client's backlog is dropped and it gets a fresh hello.
        allowed_origins: extra websocket ``Origin`` values to accept (``"*"`` = any).
            Same-origin pages, localhost origins and local ``file://`` pages are
            always accepted; the opaque origin ``"null"`` only when listed.
        allowed_hosts: extra ``Host`` names to answer to. Local names, IP literals and
            this PC's host name are always accepted; anything else gets 403 (DNS-rebinding
            guard, for every bind address). There is no authentication: with a non-loopback
            ``host`` anyone on the network can read the recordings and the live stream.
        high_res_timer: on Windows, request 1 ms timer resolution while running
            so the 5 ms batching timer is honoured (otherwise ~15.6 ms).
    """

    def __init__(self, hub: Hub, host: str = "127.0.0.1", port: int = 8765,
                 recordings_dir: Path | str | None = None, *,
                 layouts_dir: Path | str | None = None,
                 batch_interval: float = 0.005, client_queue_limit: int = 20000,
                 allowed_origins: Iterable[str] = (), allowed_hosts: Iterable[str] = (),
                 high_res_timer: bool = True) -> None:
        self.hub = hub
        self.host = host
        self.requested_port = port
        self.recordings_dir = Path(recordings_dir) if recordings_dir is not None else None
        self.layouts_dir = Path(layouts_dir) if layouts_dir is not None else LAYOUT_DIR
        self.batch_interval = batch_interval
        self.client_queue_limit = max(1, int(client_queue_limit))
        self.allowed_origins = {o.rstrip("/") for o in allowed_origins}
        # Host names (besides localhost / IP literals / this PC's name) the server answers to.
        self.allowed_hosts = {h.strip().strip("[]").lower() for h in allowed_hosts if h.strip()}
        self.high_res_timer = high_res_timer
        self._static_files: dict[str, Path] = {}
        self._port: int | None = None
        self._loopback = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._runner: web.AppRunner | None = None
        self._clients: set[_Client] = set()
        self._pending: collections.deque[list[Any]] = collections.deque(maxlen=1_000_000)
        self._wake_scheduled = False
        self._flush_handle: asyncio.TimerHandle | None = None
        self._last_flush = 0.0
        self._stopping = False
        self._counters = {"events": 0, "batches": 0, "overflows": 0, "connections": 0}

    # -- lifecycle -----------------------------------------------------------
    def start(self, timeout: float = 10.0) -> None:
        """Bind, start the server thread and return once it accepts connections.

        Raises :class:`OverlayServerError` if the address is in use or invalid.
        """
        if self._thread is not None:
            raise OverlayServerError("overlay server already started")
        sock = self._bind()
        # reset state so a stopped instance can be started again
        self._ready.clear()
        self._startup_error = None
        self._stopping = False
        self._wake_scheduled = False
        self._flush_handle = None
        self._pending.clear()
        self._clients.clear()
        self._thread = threading.Thread(target=self._thread_main, args=(sock,),
                                        name="overlay-server", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            self.stop()
            raise OverlayServerError("overlay server did not start in time")
        if self._startup_error is not None:
            err = self._startup_error
            self._thread.join(2.0)
            self._thread = None
            raise OverlayServerError(f"overlay server failed to start: {err}") from err
        self.hub.add_sink(self._sink)
        log.info("overlay listening on %s", self.url)
        if not self._loopback:
            log.warning("overlay listens on %s: anyone on the network can read the recordings "
                        "and the live input stream", self.host)

    @property
    def loopback_only(self) -> bool:
        """True when bound to a loopback address (only this PC can connect)."""
        return self._loopback

    def stop(self, timeout: float = 5.0) -> None:
        """Unregister from the hub, close all clients and stop the thread (idempotent)."""
        self.hub.remove_sink(self._sink)
        self._stopping = True
        loop, thread = self._loop, self._thread
        if loop is not None and thread is not None and thread.is_alive():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(loop.stop)
            if thread is not threading.current_thread():
                thread.join(timeout)
        self._thread = None

    def __enter__(self) -> "OverlayServer":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._ready.is_set()

    @property
    def port(self) -> int:
        """The bound port (useful with ``port=0``)."""
        return self._port if self._port is not None else self.requested_port

    @property
    def url(self) -> str:
        """Base URL, e.g. ``http://127.0.0.1:8765/`` (wildcard hosts shown as 127.0.0.1)."""
        host = self.host
        if host in ("", "0.0.0.0"):
            host = "127.0.0.1"
        elif host == "::":
            host = "::1"
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{self.port}/"

    def overlay_url(self, **params: Any) -> str:
        """Overlay URL with query parameters, e.g. ``overlay_url(layout="gba", history=False)``.

        ``None`` values are skipped and booleans become ``1`` / ``0`` (what the page expects).
        """
        q = _query(params)
        return self.url + (f"?{q}" if q else "")

    def viewer_url(self, file: str | None = None, **params: Any) -> str:
        """Viewer URL; ``file`` names a recording inside ``recordings_dir`` to open."""
        q = _query({"file": file, **params})
        return self.url + "viewer" + (f"?{q}" if q else "")

    @property
    def stats(self) -> dict[str, int]:
        """Counters: events, batches, overflows, connections (total) and clients (current)."""
        return {**self._counters, "clients": len(self._clients)}

    # -- socket / thread -------------------------------------------------------
    def _bind(self) -> socket.socket:
        try:
            infos = socket.getaddrinfo(self.host or None, self.requested_port,
                                       type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
        except socket.gaierror as e:
            raise OverlayServerError(f"cannot resolve host {self.host!r}: {e}") from e
        infos.sort(key=lambda i: i[0] != socket.AF_INET)
        family, _, _, _, addr = infos[0]
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            elif os.name == "posix":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(addr)
            sock.listen(128)
            sock.setblocking(False)
        except OSError as e:
            sock.close()
            if getattr(e, "winerror", None) in (10048, 10013) or e.errno in (98, 48, 10048, 13):
                raise OverlayServerError(
                    f"cannot listen on {self.host}:{self.requested_port}: the port is already in use "
                    f"(another overlay running?) or not permitted; choose another port") from e
            raise OverlayServerError(f"cannot listen on {self.host}:{self.requested_port}: {e}") from e
        self._port = sock.getsockname()[1]
        try:
            self._loopback = ipaddress.ip_address(addr[0]).is_loopback
        except ValueError:
            self._loopback = False
        return sock

    def _thread_main(self, sock: socket.socket) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        timer = high_resolution_timer() if self.high_res_timer else contextlib.nullcontext()
        with timer:
            try:
                loop.run_until_complete(self._startup(sock))
            except BaseException as e:  # reported to start()
                self._startup_error = e
                sock.close()
                self._ready.set()
                loop.close()
                return
            self._ready.set()
            try:
                loop.run_forever()
            finally:
                try:
                    loop.run_until_complete(self._shutdown())
                    loop.run_until_complete(loop.shutdown_asyncgens())
                except Exception:
                    log.exception("overlay shutdown failed")
                finally:
                    loop.close()

    async def _startup(self, sock: socket.socket) -> None:
        self._static_files = static_files(WEB_DIR)
        app = web.Application(middlewares=[self._guard])
        r = app.router
        r.add_get("/", self._page_overlay)
        r.add_get("/overlay", self._page_overlay)
        r.add_get("/viewer", self._page_viewer)
        r.add_get("/static/{path:.+}", self._static)
        r.add_get("/layouts", self._layouts)
        r.add_get("/layouts/{name}.json", self._layout)
        r.add_get("/api/layouts", self._api_layouts)
        r.add_get("/api/state", self._api_state)
        r.add_get("/api/recordings", self._api_recordings)
        r.add_get("/api/recording", self._api_recording)
        r.add_get("/ws", self._ws)
        app.on_shutdown.append(self._on_app_shutdown)
        self._runner = web.AppRunner(app, access_log=None, shutdown_timeout=1.0)
        await self._runner.setup()
        site = web.SockSite(self._runner, sock)
        await site.start()
        self._last_flush = asyncio.get_running_loop().time() - self.batch_interval

    async def _shutdown(self) -> None:
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _on_app_shutdown(self, app: web.Application) -> None:
        clients = list(self._clients)
        for c in clients:
            c.closed = True
            c.wake.set()

        async def close(c: _Client) -> None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(c.ws.close(code=1001, message=b"server shutdown"), 1.0)
        if clients:
            await asyncio.gather(*(close(c) for c in clients))

    # -- hub sink (backend threads) ---------------------------------------------
    def _sink(self, ev: InputEvent) -> None:
        self._pending.append(ev.to_row())
        if not self._wake_scheduled and self._loop is not None and not self._stopping:
            self._wake_scheduled = True
            try:
                self._loop.call_soon_threadsafe(self._on_wake)
            except RuntimeError:  # loop closed during shutdown
                pass

    # -- batching (loop thread) ------------------------------------------------
    def _on_wake(self) -> None:
        self._wake_scheduled = False  # before draining: see _sink
        if self._flush_handle is not None:
            return
        loop = asyncio.get_running_loop()
        delay = self._last_flush + self.batch_interval - loop.time()
        if delay <= 0:
            self._flush()
        else:
            self._flush_handle = loop.call_later(delay, self._flush)

    def _flush(self) -> None:
        self._flush_handle = None
        self._last_flush = asyncio.get_running_loop().time()
        pending = self._pending
        n = len(pending)
        if not n:
            return
        overflow = n >= (pending.maxlen or n + 1)
        rows = [pending.popleft() for _ in range(n)]
        self._counters["events"] += n
        if overflow:  # the loop stalled; events were lost, resync everyone
            for c in self._clients:
                c.queue.clear()
                c.queued = 0
                c.resync = True
                c.wake.set()
            return
        if not self._clients:
            return
        for i in range(0, n, MAX_BATCH_EVENTS):
            chunk = rows[i:i + MAX_BATCH_EVENTS]
            msg = _dumps({"type": "batch", "events": chunk})
            self._counters["batches"] += 1
            for c in self._clients:
                if c.offer(msg, len(chunk)):
                    self._counters["overflows"] += 1
                    log.info("overlay client %s fell behind; resyncing", c.peer)

    def _devices_json(self) -> list[dict[str, Any]]:
        out = []
        for dev_id, (info, state) in sorted(self.hub.snapshot().items()):
            d = info.to_json()
            d["state"] = state.to_json()
            out.append(d)
        return out

    def hello_message(self) -> dict[str, Any]:
        """The protocol ``hello`` frame for the current hub state."""
        devices = self._devices_json()
        return {"type": "hello", "protocol": PROTOCOL_VERSION, "server": f"controllerlog/{__version__}",
                "server_time_ns": now_ns(), "devices": devices}

    # -- websocket -------------------------------------------------------------
    def _origin_allowed(self, request: web.Request, origin: str) -> bool:
        origin = origin.rstrip("/")
        if "*" in self.allowed_origins or origin in self.allowed_origins:
            return True
        try:
            u = URL(origin)
        except ValueError:
            return False
        if u.scheme == "file":  # a local file page (web pages cannot open file:// URLs)
            return True
        if not u.host:  # "null": sandboxed iframes / data: URLs of *any* website
            return False
        if _is_local_hostname(u.host):
            return True
        return u.host is not None and f"{u.host}:{u.port}".lower() == \
            f"{request.url.host}:{request.url.port}".lower()

    async def _ws(self, request: web.Request) -> web.StreamResponse:
        origin = request.headers.get("Origin")
        if origin and not self._origin_allowed(request, origin):
            return web.json_response({"error": f"origin {origin} not allowed"}, status=403)
        ws = web.WebSocketResponse(heartbeat=20.0, timeout=2.0, compress=False, max_msg_size=65536)
        await ws.prepare(request)
        client = _Client(ws, self.client_queue_limit, request.remote or "?")
        self._clients.add(client)
        self._counters["connections"] += 1
        writer = asyncio.create_task(self._writer(client))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    self._on_client_message(client, msg.data)
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            client.closed = True
            client.wake.set()
            self._clients.discard(client)
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await writer
        return ws

    def _on_client_message(self, client: _Client, data: str) -> None:
        try:
            msg = json.loads(data)
        except ValueError:
            return
        if isinstance(msg, dict) and msg.get("type") == "ping":
            pong: dict[str, Any] = {"type": "pong"}
            if "id" in msg:
                pong["id"] = msg["id"]
            client.control.append(pong)
            client.wake.set()

    async def _writer(self, c: _Client) -> None:
        ws = c.ws
        try:
            while not c.closed and not ws.closed:
                if c.control:
                    item = c.control.popleft()
                    item["server_time_ns"] = now_ns()  # stamped at send time for clock sync
                    msg = _dumps(item)
                elif c.resync:
                    c.resync = False
                    c.queue.clear()
                    c.queued = 0
                    msg = _dumps(self.hello_message())
                    c.hellos += 1
                elif c.queue:
                    msg, n = c.queue.popleft()
                    c.queued -= n
                else:
                    c.wake.clear()
                    await c.wake.wait()
                    continue
                await ws.send_str(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # connection reset etc.
            log.debug("overlay client %s writer ended: %s", c.peer, e)
            c.closed = True
            with contextlib.suppress(Exception):
                await ws.close()

    # -- http --------------------------------------------------------------------
    def host_allowed(self, host: str | None) -> bool:
        """DNS-rebinding guard: answer only to local names, IP literals, this PC's own
        name (``NAME`` / ``NAME.local``) and ``allowed_hosts``, whatever the bind address.

        A wildcard bind (``--host 0.0.0.0``) also listens on 127.0.0.1, so without this
        any web page could rebind its own domain to it and read recordings and live input.
        """
        if not host:
            return True
        h = host.strip("[]").lower().rstrip(".")
        if _is_local_hostname(h) or _is_ip_literal(h) or h in self.allowed_hosts:
            return True
        name = socket.gethostname().lower()
        return h in (name, f"{name}.local")

    @web.middleware
    async def _guard(self, request: web.Request, handler: Any) -> web.StreamResponse:
        if not self.host_allowed(request.url.host):
            return web.Response(status=403, text="forbidden host")
        resp = await handler(request)
        if not resp.prepared:
            resp.headers.setdefault("Cache-Control", "no-cache")
            resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        return resp

    def _file(self, path: Path) -> web.FileResponse:
        ctype = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        return web.FileResponse(path, headers={"Content-Type": ctype})

    async def _page_overlay(self, request: web.Request) -> web.StreamResponse:
        return self._file(WEB_DIR / "overlay.html")

    async def _page_viewer(self, request: web.Request) -> web.StreamResponse:
        return self._file(WEB_DIR / "viewer.html")

    async def _static(self, request: web.Request) -> web.StreamResponse:
        # Serve exact hits from the allowlist built at startup: request data never reaches
        # the filesystem (on Windows "//host/share" would otherwise become a UNC path, i.e.
        # an SMB connection with NTLM auth that also blocks the event loop).
        path = self._static_files.get(request.match_info["path"])
        if path is None:
            raise web.HTTPNotFound()
        return self._file(path)

    async def _layouts(self, request: web.Request) -> web.StreamResponse:
        return web.json_response(list_layouts(self.layouts_dir), headers=_CORS)

    async def _layout(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        if name not in list_layouts(self.layouts_dir):  # never let a name become a path
            return web.json_response({"error": f"unknown layout {name!r}"}, status=404)
        try:
            layout = load_layout(name, self.layouts_dir)
        except (LayoutError, ValueError) as e:
            return web.json_response({"error": str(e)}, status=500)
        return web.json_response(layout, headers=_CORS)

    async def _api_layouts(self, request: web.Request) -> web.StreamResponse:
        out = []
        for name in list_layouts(self.layouts_dir):
            try:
                layout = load_layout(name, self.layouts_dir)
            except (LayoutError, ValueError) as e:
                log.warning("skipping layout %s: %s", name, e)
                continue
            out.append({"name": name, "title": layout.get("title", name),
                        "families": layout.get("families", []), "size": layout["size"]})
        return web.json_response(out, headers=_CORS)

    async def _api_state(self, request: web.Request) -> web.StreamResponse:
        return web.json_response({"protocol": PROTOCOL_VERSION, "server_time_ns": now_ns(),
                                  "devices": self._devices_json()})

    async def _api_recordings(self, request: web.Request) -> web.StreamResponse:
        if self.recordings_dir is None:
            return web.json_response([])
        return web.json_response(list_recordings(self.recordings_dir))

    async def _api_recording(self, request: web.Request) -> web.StreamResponse:
        if self.recordings_dir is None:
            return web.json_response({"error": "no recordings directory configured"}, status=404)
        name = request.query.get("name", "")
        try:
            path = recording_path(self.recordings_dir, name)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except FileNotFoundError:
            return web.json_response({"error": f"recording not found: {name}"}, status=404)
        try:
            body = await asyncio.get_running_loop().run_in_executor(None, _recording_json, path)
        except (LogFormatError, OSError, ValueError, EOFError) as e:
            return web.json_response({"error": str(e)}, status=422)
        return web.Response(text=body, content_type="application/json")


def _recording_json(path: Path) -> str:
    rec = read_log(path)
    return _dumps({
        "name": path.name,
        "header": rec.header,
        "devices": [info.to_json() for _, info in sorted(rec.devices.items())],
        "events": [ev.to_row() for ev in rec.events],
    })


__all__ = ["OverlayServer", "OverlayServerError", "PROTOCOL_VERSION", "WEB_DIR",
           "recording_path", "list_recordings"]
