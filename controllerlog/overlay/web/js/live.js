// WebSocket client for the overlay server (protocol v1, see controllerlog/overlay/server.py).
// Handles auto-reconnect with backoff and maps server perf_counter_ns onto performance.now().

export const PROTOCOL = 1;

/** Server clock -> page clock. offset is chosen from the lowest-RTT ping sample. */
export class ServerClock {
  constructor() { this.baseNs = 0; this.offset = 0; this.samples = []; this.rtt = NaN; }

  reset(serverTimeNs, receivedAt = performance.now()) {
    this.baseNs = serverTimeNs;
    this.offset = receivedAt;
    this.samples = [];
    this.rtt = NaN;
  }

  /** Server ns -> page ms. */
  toLocal(tNs) { return (tNs - this.baseNs) / 1e6 + this.offset; }

  addSample(sentAt, receivedAt, serverTimeNs) {
    const rtt = receivedAt - sentAt;
    const est = (sentAt + receivedAt) / 2 - (serverTimeNs - this.baseNs) / 1e6;
    this.samples.push({ rtt, est });
    if (this.samples.length > 16) this.samples.shift();
    let best = this.samples[0];
    for (const s of this.samples) if (s.rtt < best.rtt) best = s;
    this.offset = best.est;
    this.rtt = best.rtt;
  }
}

/**
 * new LiveConnection({url, onHello(msg), onBatch(rows), onStatus(connected)}).start()
 * url defaults to ws(s)://<page host>/ws.
 */
export class LiveConnection {
  constructor(opts) {
    this.url = opts.url || defaultWsUrl();
    this.onHello = opts.onHello || (() => {});
    this.onBatch = opts.onBatch || (() => {});
    this.onStatus = opts.onStatus || (() => {});
    this.clock = new ServerClock();
    this.ws = null;
    this.connected = false;
    this.backoff = 500;
    this.stopped = false;
    this.pings = new Map();
    this.pingId = 0;
    this.pingTimer = null;
    this.retryTimer = null;
  }

  start() { this.stopped = false; this._open(); return this; }

  stop() {
    this.stopped = true;
    clearTimeout(this.retryTimer);
    clearTimeout(this.pingTimer);
    if (this.ws) this.ws.close();
  }

  _open() {
    let ws;
    try {
      ws = new WebSocket(this.url);
    } catch (e) {
      this._retry();
      return;
    }
    this.ws = ws;
    ws.onmessage = ev => this._message(ev.data);
    ws.onclose = () => {
      if (this.ws !== ws) return;
      this.ws = null;
      clearTimeout(this.pingTimer);
      if (this.connected) { this.connected = false; this.onStatus(false); }
      if (!this.stopped) this._retry();
    };
    ws.onerror = () => {};
  }

  _retry() {
    clearTimeout(this.retryTimer);
    this.retryTimer = setTimeout(() => this._open(), this.backoff);
    this.backoff = Math.min(5000, this.backoff * 1.7);
  }

  _message(data) {
    let msg;
    try { msg = JSON.parse(data); } catch { return; }
    if (msg.type === "batch") {
      this.onBatch(msg.events);
    } else if (msg.type === "hello") {
      const fresh = !this.connected;
      if (fresh || !this.clock.samples.length) this.clock.reset(msg.server_time_ns);
      this.backoff = 500;
      if (fresh) {
        this.connected = true;
        this.onStatus(true);
        this.pingCount = 0;
        this._ping();
      }
      this.onHello(msg);
    } else if (msg.type === "pong") {
      const sent = this.pings.get(msg.id);
      if (sent !== undefined) {
        this.pings.delete(msg.id);
        this.clock.addSample(sent, performance.now(), msg.server_time_ns);
      }
    }
  }

  _ping() {
    clearTimeout(this.pingTimer);
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    const id = ++this.pingId;
    this.pings.set(id, performance.now());
    if (this.pings.size > 32) this.pings.delete(this.pings.keys().next().value);
    this.ws.send(JSON.stringify({ type: "ping", id }));
    this.pingCount = (this.pingCount || 0) + 1;
    // quick burst to converge, then keep tracking drift
    this.pingTimer = setTimeout(() => this._ping(), this.pingCount < 6 ? 300 : 5000);
  }
}

export function defaultWsUrl(params = new URLSearchParams(location.search)) {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const host = params.get("host") || location.hostname || "127.0.0.1";
  const port = params.get("port") || location.port;
  const h = host.includes(":") && !host.startsWith("[") ? `[${host}]` : host;
  return `${proto}//${h}${port ? `:${port}` : ""}/ws`;
}

/** Base http URL for REST calls matching the websocket host/port parameters ("" = same origin). */
export function apiBase(params = new URLSearchParams(location.search)) {
  if (!params.get("host") && !params.get("port")) return "";
  const host = params.get("host") || location.hostname || "127.0.0.1";
  const port = params.get("port") || location.port;
  const h = host.includes(":") && !host.startsWith("[") ? `[${host}]` : host;
  return `${location.protocol === "https:" ? "https:" : "http:"}//${h}${port ? `:${port}` : ""}`;
}
