// Node-side checks of the overlay/viewer ES modules (no browser needed).
// usage: node js_check.mjs <web_dir> <recording.ctlog> <fps> <layouts.json> [<frames.ctlog> <frames_fps> <count>]
// Prints one JSON object on stdout; tests/test_overlay.py compares it with Python results.

import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { join } from "node:path";

const [webDir, recPath, fpsArg, layoutsPath, framesPath, framesFps, framesCount] = process.argv.slice(2);

// --- minimal DOM so layout-svg.js can build its SVG tree
class Node {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.children = []; this.style = {}; this.textContent = ""; this.parent = null; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  append(...cs) { for (const c of cs) this.appendChild(c); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); }
  set className(v) { this.attrs.class = v; }
  get className() { return this.attrs.class || ""; }
  set id(v) { this.attrs.id = v; }
  get id() { return this.attrs.id; }
  walk(fn) { fn(this); for (const c of this.children) c.walk(fn); }
}
globalThis.document = {
  createElementNS: (_ns, tag) => new Node(tag),
  createElement: tag => new Node(tag),
};

const imp = name => import(pathToFileURL(join(webDir, "js", name)).href);
const model = await imp("model.js");
const rec = await imp("recording.js");
const svg = await imp("layout-svg.js");

const out = { buttons: model.BUTTONS, axes: model.AXES, threshold: model.TRIGGER_PRESS_THRESHOLD, fps: {} };
for (const k of ["gb", "gba", "nes", "snes"]) out.fps[k] = model.FPS[k];

// --- recording analysis
const r = rec.parseCtlogText(readFileSync(recPath, "utf8"), "fixture.ctlog");
const fps = Number(fpsArg);
const dev = r.primaryDevice();
const track = r.track(dev);
out.primary = dev;
out.duration_ms = r.durationMs;
out.markers = r.markers;
out.bad_lines = r.badLines;
out.stats = {};
model.DIGITAL.forEach((name, i) => {
  const st = rec.intervalStats(track.presses[i], 1000 / fps, r.durationMs);
  if (st) out.stats[name] = st;
});
// state probes at a few times
out.states = {};
for (const t of [0, 100, 250, 505, 1000, 1500, 2600, r.durationMs]) {
  const s = track.stateAt(t);
  out.states[t] = { buttons: [...s.buttons], axes: [...s.axes] };
}
// random-order probes must agree with sequential ones (keyframe cache)
const seq = [3000, 10, 2999, 1250, 0, 2000];
out.probe_consistent = seq.every(t => {
  const a = JSON.stringify(track.stateAt(t));
  track.stateAt(0);
  const b = JSON.stringify(track.stateAt(t));
  return a === b;
});
// remapping merges presses (south->east): east presses == union
const table = model.mapTable(model.parseMap("south:east"));
const mapped = rec.mapPresses(track.presses, table);
out.mapped_east = mapped[model.DIGITAL_INDEX.east].length;
out.mapped_south = mapped[model.DIGITAL_INDEX.south].length;
out.format_time = [model.formatTime(0), model.formatTime(62345.6), model.formatTime(-16.7), model.formatTime(3723000)];
let mapError = null;
try { model.parseMap("south:nope"); } catch (e) { mapError = e.message; }
out.map_error = mapError;

// --- every layout builds and updates without throwing
const layouts = JSON.parse(readFileSync(layoutsPath, "utf8"));
out.layouts = {};
for (const [name, layout] of Object.entries(layouts)) {
  const parent = new Node("div");
  const ctl = svg.createController(parent, layout, { counts: true, map: "south:east" });
  const s = model.newState();
  ctl.update(s);
  s.buttons.fill(1);
  s.axes = [32767, -32768, -20000, 20000, 32767, 20000];
  ctl.update(s);
  ctl.setCounts({ south: 3, east: 1 });
  let lit = 0, nodes = 0, badges = 0;
  const activeColors = new Set([ctl.theme.active]);
  for (const el of layout.elements) if (el.active) activeColors.add(el.active);
  ctl.root.walk(n => {
    nodes++;
    if (n.tag !== "text" && activeColors.has(n.attrs.fill)) lit++;
    if (n.tag === "text" && n.attrs["paint-order"] === "stroke" && n.textContent) badges++;
  });
  out.layouts[name] = { nodes, lit, badges, width: ctl.width, height: ctl.height, inputs: ctl.inputs };
}
// --- frame stepping like viewer.stepFrames: frame k starts at frameStartMs(k, fps, origin marker)
if (framesPath) {
  const fr = rec.parseCtlogText(readFileSync(framesPath, "utf8"), "frames.ctlog");
  const ffps = model.resolveFps(framesFps);
  const fm = 1000 / ffps;
  const org = fr.markers.length ? fr.markers[0].t : 0;
  const ftrack = fr.track(fr.primaryDevice());
  let playhead = org;
  out.frame_states = [];
  out.frame_numbers_ok = true;
  for (let k = 0; k < Number(framesCount); k++) {
    if (k > 0) playhead = model.frameStartMs(model.frameOf(playhead - org, fm) + 1, ffps, org);  // "next frame"
    if (model.frameOf(playhead - org, fm) !== k) out.frame_numbers_ok = false;
    const s = ftrack.stateAt(playhead);
    out.frame_states.push({ buttons: [...s.buttons], axes: [...s.axes] });
  }
  // frame numbers stay exact at high custom rates too
  out.frame_numbers_1khz_ok = [0, 1, 7, 12345].every(k => model.frameOf(model.frameStartMs(k, 1000, org) - org, 1) === k);
}

// --- malformed rows (null / out-of-range / fractional codes) are ignored like model.PadState.apply
const junk = new rec.Recording({ format: "controllerlog" }, [
  [0, 0, "+", null, { name: "x" }], [1e6, 0, "b", null, 1], [2e6, 0, "b", 256, 1], [3e6, 0, "b", 1.5, 1],
  [4e6, 0, "a", 260, 999], [5e6, 0, "b", -1, 1], [6e6, 0, "b", 3, 1]]);
const jt = junk.track(0);
out.junk = { presses: jt.presses.map(p => p.length), state: [...jt.stateAt(10).buttons], axes: [...jt.stateAt(10).axes] };
const live = model.newState();
out.junk_live_changed = [[0, 0, "b", null, 1], [0, 0, "b", "1", 1], [0, 0, "a", 1.5, 3]].map(r => model.applyRow(live, r));

out.layout_for_family = {
  xbox: svg.layoutForFamily("xbox", Object.values(layouts).map(l => ({ name: l.name, families: l.families }))),
  nope: svg.layoutForFamily("nope", Object.values(layouts).map(l => ({ name: l.name, families: l.families }))),
};
process.stdout.write(JSON.stringify(out));
