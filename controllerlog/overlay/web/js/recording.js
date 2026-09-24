// Recording model for the viewer: parse .ctlog, random-access state, press intervals, stats.
// Times are milliseconds since the recording started (doubles).

import {
  NUM_BUTTONS, NUM_AXES, NUM_DIGITAL, TRIGGER_PRESS_THRESHOLD, AXIS_INDEX, newState, copyState,
} from "./model.js";

const KEY_EVERY = 256;
const LT = AXIS_INDEX.left_trigger, RT = AXIS_INDEX.right_trigger;

/** Parse .ctlog JSONL text: header line, then one row per line. Tolerates bad/truncated lines. */
export function parseCtlogText(text, name = "") {
  if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
  const nl = text.indexOf("\n");
  const first = (nl < 0 ? text : text.slice(0, nl)).trim();
  let header;
  try { header = JSON.parse(first); } catch { throw new Error(`${name || "file"}: bad header line`); }
  if (!header || typeof header !== "object" || Array.isArray(header) || header.format !== "controllerlog") {
    throw new Error(`${name || "file"}: not a controllerlog recording`);
  }
  const rows = [];
  let bad = 0;
  let pos = nl < 0 ? text.length : nl + 1;
  while (pos < text.length) {
    let end = text.indexOf("\n", pos);
    if (end < 0) end = text.length;
    const line = text.slice(pos, end).trim();
    pos = end + 1;
    if (!line) continue;
    try {
      const row = JSON.parse(line);
      if (Array.isArray(row) && row.length >= 3 && typeof row[0] === "number") rows.push(row);
      else bad++;
    } catch { bad++; }
  }
  return new Recording(header, rows, { name, badLines: bad });
}

/** Build from the server's /api/recording JSON. */
export function recordingFromJson(obj) {
  return new Recording(obj.header || {}, obj.events || [], { name: obj.name || "", devices: obj.devices || [] });
}

/** Read a dropped/picked File (.ctlog or .ctlog.gz). */
export async function readRecordingFile(file) {
  let text;
  if (/\.gz$/i.test(file.name)) {
    if (typeof DecompressionStream === "undefined") throw new Error("this browser cannot read .gz files");
    text = await new Response(file.stream().pipeThrough(new DecompressionStream("gzip"))).text();
  } else {
    text = await file.text();
  }
  return parseCtlogText(text, file.name);
}

export class Recording {
  constructor(header, rows, { name = "", devices = [], badLines = 0 } = {}) {
    this.header = header;
    this.name = name;
    this.badLines = badLines;
    rows = rows.slice().sort((a, b) => a[0] - b[0]);  // stable
    this.rows = rows;
    this.devices = new Map();
    for (const d of devices) if (d && d.id !== undefined) this.devices.set(d.id, d);
    this.markers = [];
    const counts = new Map();
    for (const r of rows) {
      const kind = r[2];
      if (kind === "b" || kind === "a") counts.set(r[1], (counts.get(r[1]) || 0) + 1);
      else if (kind === "+" && r[4] && typeof r[4] === "object") this.devices.set(r[1], { ...r[4], id: r[1] });
      else if (kind === "m") this.markers.push({ t: r[0] / 1e6, label: String(r[4] ?? "") });
    }
    for (const id of counts.keys()) {
      if (id !== null && !this.devices.has(id)) this.devices.set(id, { id, name: `Device ${id}`, family: "generic" });
    }
    this.inputCounts = counts;
    this.durationMs = rows.length ? rows[rows.length - 1][0] / 1e6 : 0;
    this.tracks = new Map();
  }

  get meta() { return this.header.meta || {}; }

  /** Device ids that produced input, most active first. */
  inputDevices() {
    return [...this.inputCounts.entries()].filter(([id]) => id !== null).sort((a, b) => b[1] - a[1]).map(e => e[0]);
  }

  /** Device with the most input events (logfile.Recording.primary_device). */
  primaryDevice() {
    const ids = this.inputDevices();
    if (ids.length) return ids[0];
    const all = [...this.devices.keys()].sort((a, b) => a - b);
    return all.length ? all[0] : null;
  }

  family(device) { return (this.devices.get(device) || {}).family || "generic"; }

  track(device) {
    if (!this.tracks.has(device)) {
      this.tracks.set(device, new DeviceTrack(this.rows.filter(r => r[1] === device && validInputRow(r)), this.durationMs));
    }
    return this.tracks.get(device);
  }
}

/** b/a row with an in-range integer code (model.PadState.apply ignores anything else). */
function validInputRow(r) {
  const code = r[3];
  if (!Number.isInteger(code) || code < 0) return false;
  return r[2] === "b" ? code < NUM_BUTTONS : r[2] === "a" && code < NUM_AXES;
}

/** Random-access state + derived data for one device's b/a events. */
export class DeviceTrack {
  constructor(rows, endMs) {
    const n = rows.length;
    this.n = n;
    this.endMs = endMs;
    this.t = new Float64Array(n);
    this.kind = new Uint8Array(n);   // 0 = button, 1 = axis
    this.code = new Uint8Array(n);
    this.value = new Int32Array(n);
    this.keys = [];
    this.presses = Array.from({ length: NUM_DIGITAL }, () => []);  // [[down, up|null], ...]
    this.axisT = Array.from({ length: NUM_AXES }, () => []);
    this.axisV = Array.from({ length: NUM_AXES }, () => []);
    const st = newState();
    const down = new Float64Array(NUM_DIGITAL).fill(NaN);
    const prev = new Uint8Array(NUM_DIGITAL);
    const cur = new Uint8Array(NUM_DIGITAL);
    for (let i = 0; i < n; i++) {
      const r = rows[i];
      if (i % KEY_EVERY === 0) this.keys.push(copyState(st));
      const t = r[0] / 1e6;
      this.t[i] = t;
      const isAxis = r[2] === "a";
      this.kind[i] = isAxis ? 1 : 0;
      this.code[i] = r[3];
      this.value[i] = isAxis ? (r[4] | 0) : (r[4] ? 1 : 0);
      applyIndex(st, this, i);
      if (isAxis && r[3] < NUM_AXES) {
        const at = this.axisT[r[3]], av = this.axisV[r[3]];
        if (!av.length || av[av.length - 1] !== this.value[i]) { at.push(t); av.push(this.value[i]); }
      }
      // presses from the state after the last event sharing this timestamp (timeline.Timeline.presses)
      if (i + 1 < n && rows[i + 1][0] === r[0]) continue;
      for (let b = 0; b < NUM_BUTTONS; b++) cur[b] = st.buttons[b];
      cur[NUM_BUTTONS] = st.axes[LT] >= TRIGGER_PRESS_THRESHOLD ? 1 : 0;
      cur[NUM_BUTTONS + 1] = st.axes[RT] >= TRIGGER_PRESS_THRESHOLD ? 1 : 0;
      for (let k = 0; k < NUM_DIGITAL; k++) {
        if (cur[k] === prev[k]) continue;
        if (cur[k]) down[k] = t;
        else if (!Number.isNaN(down[k])) { this.presses[k].push([down[k], t]); down[k] = NaN; }
        prev[k] = cur[k];
      }
    }
    for (let k = 0; k < NUM_DIGITAL; k++) if (!Number.isNaN(down[k])) this.presses[k].push([down[k], null]);
    this.axisT = this.axisT.map(a => Float64Array.from(a));
    this.axisV = this.axisV.map(a => Int32Array.from(a));
    this._cache = { idx: 0, state: newState() };
  }

  /** Number of events with t <= tMs. */
  indexAt(tMs) { return upperBound(this.t, tMs); }

  /** State after every event with t <= tMs. The returned object is reused; copy it to keep it. */
  stateAt(tMs) {
    const idx = this.indexAt(tMs);
    const c = this._cache;
    if (!(idx >= c.idx && idx - c.idx <= KEY_EVERY * 2)) {
      const k = Math.min(Math.floor(idx / KEY_EVERY), this.keys.length - 1);
      if (k >= 0) { copyState(this.keys[k], c.state); c.idx = k * KEY_EVERY; }
      else { copyState(newState(), c.state); c.idx = 0; }
    }
    for (let i = c.idx; i < idx; i++) applyIndex(c.state, this, i);
    c.idx = idx;
    return c.state;
  }

  hasAxis(a) { return this.axisV[a].some(v => v !== 0); }
}

function applyIndex(st, tr, i) {
  const code = tr.code[i];
  if (tr.kind[i]) { if (code < NUM_AXES) st.axes[code] = tr.value[i]; }
  else if (code < NUM_BUTTONS) st.buttons[code] = tr.value[i];
}

/** First index with arr[i] > v (arr sorted ascending). */
export function upperBound(arr, v) {
  let lo = 0, hi = arr.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (arr[m] <= v) lo = m + 1; else hi = m; }
  return lo;
}

/** First index with arr[i] >= v. */
export function lowerBound(arr, v) {
  let lo = 0, hi = arr.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (arr[m] < v) lo = m + 1; else hi = m; }
  return lo;
}

/** Union of interval lists ([down, up|null]); result sorted and non-overlapping. */
export function mergeIntervals(lists) {
  const all = [].concat(...lists).sort((a, b) => a[0] - b[0]);
  const out = [];
  for (const [a, b] of all) {
    const last = out[out.length - 1];
    const bb = b === null ? Infinity : b;
    if (last && a <= (last[1] === null ? Infinity : last[1])) {
      if (bb > (last[1] === null ? Infinity : last[1])) last[1] = b;
    } else out.push([a, b]);
  }
  return out;
}

/** Apply a display map table (model.mapTable) to per-source presses -> per-destination presses. */
export function mapPresses(presses, table) {
  const out = [];
  for (let d = 0; d < NUM_DIGITAL; d++) {
    const src = [];
    for (let s = 0; s < NUM_DIGITAL; s++) if (table[s] === d) src.push(presses[s]);
    out.push(src.length === 1 ? src[0] : mergeIntervals(src));
  }
  return out;
}

/** Max presses inside any window of `windowMs`, per second (timeline.peak_rate). */
export function peakRate(times, windowMs = 1000) {
  let best = 0, j = 0;
  for (let i = 0; i < times.length; i++) {
    while (times[j] <= times[i] - windowMs) j++;
    best = Math.max(best, i - j + 1);
  }
  return best * 1000 / windowMs;
}

/** Stats of one input's intervals (holds in frames), like timeline.Timeline.stats. */
export function intervalStats(intervals, frameMs, endMs) {
  if (!intervals.length) return null;
  let min = Infinity, max = 0, sum = 0;
  for (const [a, b] of intervals) {
    const f = ((b === null ? endMs : b) - a) / frameMs;
    min = Math.min(min, f); max = Math.max(max, f); sum += f;
  }
  return {
    presses: intervals.length, holdMin: min, holdMax: max, holdMean: sum / intervals.length, heldTotal: sum,
    peakPerSec: peakRate(intervals.map(i => i[0])),
  };
}

/** Interval containing time t (with tolerance), or null. Intervals sorted, non-overlapping. */
export function intervalAt(intervals, t, tol = 0) {
  let lo = 0, hi = intervals.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (intervals[m][0] <= t + tol) lo = m + 1; else hi = m; }
  for (let i = lo - 1; i >= 0 && i >= lo - 3; i--) {
    const [a, b] = intervals[i];
    if (a - tol <= t && t <= (b === null ? Infinity : b) + tol) return intervals[i];
  }
  return null;
}
