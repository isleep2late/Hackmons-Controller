// Live overlay page: controller drawing + input-history lane, fed by /ws.
//
// Query parameters (all optional):
//   layout=auto|<name>   device=auto|<id>   history=0|1   seconds=4   fps=60|gb|gba|nes|<n>
//   bg=transparent|chroma|<hex>   scale=1   map=src:dst,...   labels=0|1   counts=0|1
//   host=<ws host>   port=<ws port>

import {
  DIGITAL, NUM_DIGITAL, TRIGGER_PRESS_THRESHOLD, AXIS_INDEX, newState, stateFromJson, applyRow,
  digitalLevels, mapTable, parseMap, resolveFps, inputLabel, hexColor,
} from "./model.js";
import { createController, fetchLayout, layoutForFamily } from "./layout-svg.js";
import { HistoryLane } from "./history-lane.js";
import { LiveConnection, apiBase, defaultWsUrl } from "./live.js";

const params = new URLSearchParams(location.search);
const num = (k, d) => { const v = parseFloat(params.get(k)); return Number.isFinite(v) && v > 0 ? v : d; };
const cfg = {
  layout: params.get("layout") || "auto",
  device: params.get("device") || "auto",
  history: params.get("history") !== "0",
  seconds: num("seconds", 4),
  fps: resolveFps(params.get("fps"), 60),
  scale: num("scale", 1),
  labels: params.get("labels") !== "0",
  counts: params.get("counts") === "1",
  mapSpec: params.get("map") || "",
};
const base = apiBase(params);
const stage = document.getElementById("stage");
const badge = document.getElementById("badge");

// background
const bg = (params.get("bg") || "transparent").toLowerCase();
const bgColor = bg === "transparent" ? "transparent" : bg === "chroma" ? "#00ff00" : hexColor(bg, "transparent");
document.documentElement.style.background = bgColor;
document.body.style.background = bgColor;

let mapping = {};
try { mapping = parseMap(cfg.mapSpec); } catch (e) { console.warn(e.message); }
const table = mapTable(mapping);

const devices = new Map();       // id -> {info, state}
let current = null;              // selected device id
let layoutName = null;
let controller = null;
let lane = null;
let listing = null;
let dirty = true;
let building = null;             // pending layout build promise
const levels = new Float64Array(NUM_DIGITAL);
let pressed = new Uint8Array(NUM_DIGITAL);
const counts = new Map();

const conn = new LiveConnection({
  url: defaultWsUrl(params),
  onHello, onBatch,
  onStatus: connected => setBadge(!connected),
});

let badgeTimer = null;
function setBadge(show) {
  clearTimeout(badgeTimer);
  if (show) badgeTimer = setTimeout(() => badge.classList.add("show"), 800);
  else badge.classList.remove("show");
}

async function layoutListing() {
  if (!listing) {
    try {
      const r = await fetch(`${base}/api/layouts`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      listing = await r.json();
    } catch {
      return [];  // not cached: retried on the next (re)connect
    }
  }
  return listing;
}

async function wantedLayout() {
  if (cfg.layout !== "auto") return cfg.layout;
  const dev = devices.get(current);
  return layoutForFamily(dev ? dev.info.family : "generic", await layoutListing());
}

async function ensureLayout() {
  const name = await wantedLayout();
  if (name === layoutName && controller) return;
  let layout;
  try {
    layout = await fetchLayout(name, base);
  } catch (e) {
    console.warn(e.message);
    if (name !== "generic") layout = await fetchLayout("generic", base);
    else return;
  }
  if (name !== await wantedLayout()) return; // superseded while loading
  layoutName = name;
  build(layout);
}

function queueLayout() {
  building = (building || Promise.resolve()).then(ensureLayout).catch(e => console.error(e));
}

function build(layout) {
  stage.textContent = "";
  controller = createController(stage, layout, { scale: cfg.scale, labels: cfg.labels, map: mapping, counts: cfg.counts });
  lane = null;
  if (cfg.history) {
    const canvas = document.createElement("canvas");
    canvas.className = "lane";
    stage.appendChild(canvas);
    const family = (devices.get(current) || {}).info?.family;
    const colors = activeColors(layout);
    const rows = (layout.history || []).map(input => ({
      input, label: inputLabel(input, layout, family), color: colors[input],
    }));
    lane = new HistoryLane(canvas, { rows, seconds: cfg.seconds, fps: cfg.fps, scale: cfg.scale,
      theme: controller.theme, width: controller.width });
    const now = performance.now();
    for (let i = 0; i < NUM_DIGITAL; i++) if (pressed[i]) lane.press(DIGITAL[i], now);
  }
  if (cfg.counts) controller.setCounts(counts);
  dirty = true;
}

function activeColors(layout) {
  const out = {};
  for (const el of layout.elements || []) {
    const inp = el.type === "stick" ? el.button : el.input;
    if (inp && el.active && !(inp in out)) out[inp] = el.active;
  }
  return out;
}

function currentState() {
  const dev = devices.get(current);
  return dev ? dev.state : newState();
}

/** Recompute digital edges of the selected device; t = page-clock ms. */
function edges(t) {
  digitalLevels(currentState(), table, levels);
  for (let i = 0; i < NUM_DIGITAL; i++) {
    const on = levels[i] >= TRIGGER_PRESS_THRESHOLD ? 1 : 0;
    if (on === pressed[i]) continue;
    pressed[i] = on;
    const input = DIGITAL[i];
    if (on) {
      if (lane) lane.press(input, t);
      counts.set(input, (counts.get(input) || 0) + 1);
      if (cfg.counts && controller) controller.setCounts(counts);
    } else if (lane) {
      lane.release(input, t);
    }
  }
}

function select(id, t) {
  if (id === current) return;
  current = id;
  if (lane) lane.releaseAll(t);
  pressed = new Uint8Array(NUM_DIGITAL);
  edges(t);
  dirty = true;
  queueLayout();
}

function pickDefault() {
  if (cfg.device !== "auto") {
    const want = parseInt(cfg.device, 10);
    return devices.has(want) ? want : null;
  }
  if (current !== null && devices.has(current)) return current;
  const ids = [...devices.keys()].sort((a, b) => a - b);
  return ids.length ? ids[0] : null;
}

// A row counts as "real input" for device=auto (ignore stick noise on idle pads).
function significant(row) {
  if (row[2] === "b") return !!row[4];
  if (row[2] === "a") {
    const code = row[3], v = row[4];
    if (code === AXIS_INDEX.left_trigger || code === AXIS_INDEX.right_trigger) return v >= TRIGGER_PRESS_THRESHOLD;
    return Math.abs(v) >= 16000;
  }
  return false;
}

function onHello(msg) {
  devices.clear();
  for (const d of msg.devices || []) devices.set(d.id, { info: d, state: stateFromJson(d.state) });
  const now = performance.now();
  const id = pickDefault();
  if (id !== current) select(id, now);
  else { edges(now); dirty = true; }
  queueLayout();
}

function onBatch(rows) {
  const clock = conn.clock;
  for (const row of rows) {
    const kind = row[2], dev = row[1];
    if (kind === "b" || kind === "a") {
      const d = devices.get(dev);
      if (!d || !applyRow(d.state, row)) continue;
      if (dev !== current && cfg.device === "auto" && significant(row)) {
        select(dev, clock.toLocal(row[0]));
        continue;
      }
      if (dev === current) {
        dirty = true;
        if (kind === "b" || row[3] >= AXIS_INDEX.left_trigger) edges(clock.toLocal(row[0]));
      }
    } else if (kind === "+") {
      const info = { ...(row[4] || {}), id: dev };
      devices.set(dev, { info, state: newState() });
      if (dev === current) {
        edges(clock.toLocal(row[0]));
        dirty = true;
        queueLayout();
      } else if (cfg.device === "auto" ? current === null : parseInt(cfg.device, 10) === dev) {
        select(dev, clock.toLocal(row[0]));  // a fixed device= never falls back to another pad
      }
    } else if (kind === "-") {
      if (!devices.has(dev)) continue;
      devices.delete(dev);
      if (dev === current) {
        const t = clock.toLocal(row[0]);
        if (lane) lane.releaseAll(t);
        pressed = new Uint8Array(NUM_DIGITAL);
        current = null;
        const next = pickDefault();
        if (next !== null) select(next, t);
        dirty = true;
      }
    } else if (kind === "m") {
      if (lane) lane.mark(clock.toLocal(row[0]), row[4]);
    }
  }
}

function frame() {
  requestAnimationFrame(frame);  // re-arm first: one failing frame must not freeze the overlay
  if (controller && dirty) {
    dirty = false;
    controller.update(currentState());
  }
  if (lane) lane.draw(performance.now());
}

queueLayout();
setBadge(true);  // until the first hello (hidden again if it arrives within 0.8 s)
conn.start();
requestAnimationFrame(frame);
window.controllerlogOverlay = { cfg, devices, conn, get current() { return current; }, get layout() { return layoutName; } };
