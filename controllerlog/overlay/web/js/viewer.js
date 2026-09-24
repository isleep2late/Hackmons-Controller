// Recording viewer: piano roll, playback on the controller drawing, stats, markers, ghost compare.
//
// URL parameters: file=<name in recordings_dir>, ghost=<name>, fps=60|gb|gba|nes|<n>,
//                 layout=auto|<name>, map=src:dst,..., device=<id>, t=<seconds from start>

import {
  AXIS_INDEX, DIGITAL, DIGITAL_INDEX, FPS, clamp, formatTime, frameOf, frameStartMs, inputLabel, mapTable,
  parseMap, resolveFps,
} from "./model.js";
import { createController, fetchLayout, layoutForFamily } from "./layout-svg.js";
import { intervalStats, mapPresses, readRecordingFile, recordingFromJson } from "./recording.js";
import { PianoRoll } from "./piano-roll.js";

const $ = id => document.getElementById(id);
const params = new URLSearchParams(location.search);

const S = {
  main: null,            // Recording
  ghost: null,           // Recording
  device: null,
  ghostDevice: null,
  fps: 60,
  layoutSel: params.get("layout") || "auto",
  layout: null,
  layoutName: null,
  map: {},
  table: mapTable({}),
  playhead: 0,
  playing: false,
  speed: 1,
  follow: true,
  sticks: true,
  alignMain: -1,
  alignGhost: -1,
  listing: [],
};
let controller = null, ghostController = null;
let lastTick = 0;
let needsRender = true;

const roll = new PianoRoll($("roll"), {
  onSeek: t => seek(t),
});

// ------------------------------------------------------------------ helpers
function setStatus(msg, warn = false) {
  const el = $("status");
  el.textContent = msg || "";
  el.classList.toggle("warn", !!warn);
}

function originMs() {
  return S.main && S.alignMain >= 0 && S.main.markers[S.alignMain] ? S.main.markers[S.alignMain].t : 0;
}

/** main-time = ghost-time + ghostOffset() */
function ghostOffset() {
  if (!S.ghost) return 0;
  const g = S.alignGhost >= 0 && S.ghost.markers[S.alignGhost] ? S.ghost.markers[S.alignGhost].t : 0;
  return originMs() - g;
}

function frameMs() { return 1000 / S.fps; }

function range() {
  if (!S.main) return [0, 1000];
  let a = 0, b = Math.max(S.main.durationMs, 1);
  if (S.ghost) {
    const off = ghostOffset();
    a = Math.min(a, off);
    b = Math.max(b, S.ghost.durationMs + off);
  }
  return [a, b];
}

function fmtSize(n) {
  return n > 1 << 20 ? `${(n / (1 << 20)).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// ------------------------------------------------------------------ loading
async function refreshServerList() {
  try {
    const r = await fetch("/api/recordings");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    S.listing = await r.json();
  } catch {
    S.listing = [];
  }
  for (const [sel, first] of [[$("sel-main"), "— server list —"], [$("sel-ghost"), "— none —"]]) {
    const keep = sel.value;
    sel.textContent = "";
    sel.append(new Option(S.listing.length ? first : "(no recordings on server)", ""));
    for (const f of S.listing) {
      const d = new Date(f.mtime * 1000);
      sel.append(new Option(`${f.name}  ·  ${fmtSize(f.size)}  ·  ${d.toLocaleString()}`, f.name));
    }
    sel.value = keep;
  }
}

async function fetchRecording(name) {
  setStatus(`Loading ${name}…`);
  const r = await fetch(`/api/recording?name=${encodeURIComponent(name)}`);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  return recordingFromJson(body);
}

async function loadMain(rec) {
  S.main = rec;
  S.playhead = 0;
  S.alignMain = -1;
  S.alignGhost = -1;
  const want = parseInt(params.get("device"), 10);
  S.device = rec.inputCounts.has(want) || rec.devices.has(want) ? want : rec.primaryDevice();
  $("empty").hidden = true;
  fillDeviceSelect();
  fillAlignSelects();
  await applyLayout();
  roll.fit();
  summary();
  document.title = `${rec.name || "Recording"} – ControllerLog Viewer`;
}

function summary() {
  const rec = S.main;
  if (!rec) return;
  let msg = `${rec.name || "recording"} · ${rec.rows.length.toLocaleString()} events · ${formatTime(rec.durationMs)}`;
  if (rec.badLines) msg += ` · ${rec.badLines} unreadable line(s) skipped`;
  if (S.ghost) msg += `  |  ghost: ${S.ghost.name || "local file"} · ${formatTime(S.ghost.durationMs)}`;
  setStatus(msg, !!rec.badLines);
}

function loadGhost(rec) {
  S.ghost = rec;
  S.ghostDevice = rec ? rec.primaryDevice() : null;
  S.alignGhost = -1;
  document.body.classList.toggle("has-ghost", !!rec);
  $("btn-clear-ghost").hidden = !rec;
  $("ghost-wrap").hidden = !rec;
  if (!rec) $("sel-ghost").value = "";
  fillAlignSelects();
  // align at the same marker label as the main run, if any
  if (rec && S.alignMain >= 0) matchGhostMarker();
  rebuild();
  roll.fit();
  summary();
}

const LOCAL = "__local__";

/** Show a locally opened file in a recording dropdown. */
function setLocalOption(sel, name) {
  let opt = [...sel.options].find(o => o.value === LOCAL);
  if (!opt) { opt = new Option("", LOCAL); sel.add(opt, 1); }
  opt.textContent = `local file: ${name}`;
  sel.value = LOCAL;
}

async function openFile(file, target) {
  try {
    setStatus(`Reading ${file.name}…`);
    const rec = await readRecordingFile(file);
    if (target === "ghost" && S.main) { loadGhost(rec); setLocalOption($("sel-ghost"), file.name); }
    else { await loadMain(rec); setLocalOption($("sel-main"), file.name); }
  } catch (e) {
    setStatus(e.message, true);
  }
}

async function applyLayout() {
  const family = S.main ? S.main.family(S.device) : "generic";
  let name = S.layoutSel;
  if (name === "auto") {
    let listing = [];
    try { listing = await (await fetch("/api/layouts")).json(); } catch { /* offline */ }
    name = layoutForFamily(family, listing);
  }
  try {
    S.layout = await fetchLayout(name);
    S.layoutName = name;
  } catch (e) {
    setStatus(e.message, true);
    return;
  }
  $("lbl-layout").textContent = `${S.layout.title || name}${S.main ? ` · ${deviceName(S.main, S.device)}` : ""}`;
  buildControllers();
  rebuild();
}

function deviceName(rec, id) {
  const d = rec.devices.get(id);
  return d ? `#${id} ${d.name || "controller"}` : `#${id}`;
}

function buildControllers() {
  const stage = $("controller");
  stage.textContent = "";
  const W = S.layout.size[0];
  const avail = Math.max(240, stage.clientWidth - 24);
  controller = createController(stage, S.layout, { scale: Math.min(1, avail / W), map: S.map });
  const gs = $("ghost-controller");
  gs.textContent = "";
  ghostController = createController(gs, S.layout, { scale: Math.min(0.42, 220 / W), map: S.map, labels: false, shadow: false });
  needsRender = true;
}

// ------------------------------------------------------------------ derived data
function activeColors(layout) {
  const out = {};
  for (const el of layout.elements || []) {
    const inp = el.type === "stick" ? el.button : el.input;
    if (inp && !(inp in out)) out[inp] = el.active || layout.theme.active;
  }
  return out;
}

function rebuild() {
  if (!S.main || !S.layout) return;
  const fm = frameMs();
  const org = originMs();
  const family = S.main.family(S.device);
  const track = S.device !== null ? S.main.track(S.device) : null;
  const mainP = track ? mapPresses(track.presses, S.table) : DIGITAL.map(() => []);
  const off = ghostOffset();
  let ghostP = null, gtrack = null;
  if (S.ghost && S.ghostDevice !== null) {
    gtrack = S.ghost.track(S.ghostDevice);
    ghostP = mapPresses(gtrack.presses, S.table).map(list => list.map(([a, b]) => [a + off, b === null ? null : b + off]));
  }
  const colors = activeColors(S.layout);
  const order = [...(S.layout.history || [])];
  for (let i = 0; i < DIGITAL.length; i++) {
    if (!order.includes(DIGITAL[i]) && (mainP[i].length || (ghostP && ghostP[i].length))) order.push(DIGITAL[i]);
  }
  const rows = order.map(input => {
    const i = DIGITAL_INDEX[input];
    return {
      input, label: inputLabel(input, S.layout, family), sub: shortName(input),
      color: colors[input] || S.layout.theme.active || "#ffd23f",
      main: mainP[i], ghost: ghostP ? ghostP[i] : null,
    };
  });
  const analog = [];
  if (S.sticks) {
    const axes = [["LS X", "left_x"], ["LS Y", "left_y"], ["RS X", "right_x"], ["RS Y", "right_y"]];
    const colorsA = ["#8ab4ff", "#b58cff", "#4fd1c5", "#7ee07e"];
    axes.forEach(([label, name], k) => {
      const a = AXIS_INDEX[name];
      const has = (track && track.hasAxis(a)) || (gtrack && gtrack.hasAxis(a));
      if (!has) return;
      analog.push({
        label, color: colorsA[k], invert: name.endsWith("_y"),  // y: negative = stick up, drawn upwards
        t: track ? track.axisT[a] : new Float64Array(0), v: track ? track.axisV[a] : new Int32Array(0),
        gt: gtrack ? gtrack.axisT[a] : null, gv: gtrack ? gtrack.axisV[a] : null, ghostOffset: off,
      });
    });
  }
  const ghostMarkers = S.ghost ? S.ghost.markers.map(m => ({ t: m.t + off, label: m.label })) : [];
  roll.setData({ rows, analog, markers: S.main.markers, ghostMarkers, range: range(), ghost: !!S.ghost });
  roll.setFrames(fm, org);
  renderStats(rows);
  renderMarkers();
  renderInfo();
  $("scrub").min = range()[0];
  $("scrub").max = range()[1];
  needsRender = true;
}

function shortName(input) {
  return input.replace("dpad_", "").replace("_shoulder", " sh.").replace("_trigger", " trig.")
    .replace("_stick", " stick").replace("_paddle", " pad.");
}

function renderStats(rows) {
  const tbody = $("tbl-stats").querySelector("tbody");
  const fm = frameMs();
  const end = S.main.durationMs;
  const html = [];
  const swatches = [];
  for (const row of rows) {
    const st = intervalStats(row.main, fm, end);
    const gcount = row.ghost ? row.ghost.length : 0;
    if (!st && !gcount) continue;
    const f = v => v.toFixed(v < 10 ? 2 : 1);
    swatches.push(row.color);  // layout colours are set through the DOM, never parsed as HTML
    html.push(`<tr><td><span class="swatch"></span>${esc(row.label)}` +
      `<span class="sub">${esc(row.input)}</span></td>` +
      (st ? `<td>${st.presses}</td><td>${f(st.holdMin)}</td><td>${f(st.holdMean)}</td><td>${f(st.holdMax)}</td>` +
        `<td>${st.peakPerSec.toFixed(0)}</td>` : `<td>0</td><td>–</td><td>–</td><td>–</td><td>–</td>`) +
      `<td class="ghost-col">${gcount}</td></tr>`);
  }
  tbody.innerHTML = html.join("");
  tbody.querySelectorAll(".swatch").forEach((el, k) => { el.style.background = swatches[k]; });
  $("stats-empty").hidden = html.length > 0;
  $("stats-empty").textContent = S.main ? "No button presses for this device." : "Load a recording to see per-input statistics.";
}

function markerDelta(i) {
  if (!S.ghost) return null;
  const m = S.main.markers[i];
  const nth = S.main.markers.slice(0, i).filter(x => x.label === m.label).length;
  const g = S.ghost.markers.filter(x => x.label === m.label)[nth];
  return g ? g.t + ghostOffset() - m.t : null;
}

function renderMarkers() {
  const tbody = $("tbl-markers").querySelector("tbody");
  const org = originMs(), fm = frameMs();
  tbody.innerHTML = S.main.markers.map((m, i) => {
    const d = markerDelta(i);
    const ds = d === null ? "–" : `<span class="${d > 0.0005 ? "delta-pos" : d < -0.0005 ? "delta-neg" : ""}">` +
      `${d > 0 ? "+" : ""}${(d / 1000).toFixed(3)}</span>`;
    return `<tr data-t="${m.t}"><td>${formatTime(m.t - org)}</td><td>${frameOf(m.t - org, fm)}</td>` +
      `<td>${esc(m.label)}</td><td class="ghost-col">${ds}</td></tr>`;
  }).join("");
  $("markers-empty").hidden = S.main.markers.length > 0;
  $("cnt-markers").textContent = S.main.markers.length;
}

function renderInfo() {
  const rec = S.main;
  const items = [["File", rec.name || "(local)"]];
  if (rec.header.created_utc) items.push(["Recorded", new Date(rec.header.created_utc).toLocaleString()]);
  items.push(["Duration", `${formatTime(rec.durationMs)} · ${Math.round(rec.durationMs / frameMs()).toLocaleString()} frames @ ${S.fps.toFixed(4).replace(/\.?0+$/, "")} fps`]);
  items.push(["Events", rec.rows.length.toLocaleString()]);
  for (const [id, d] of rec.devices) {
    items.push([`Device #${id}`, `${d.name || "?"} · ${d.family || "generic"}${d.backend ? ` · ${d.backend}` : ""}` +
      ` · ${(rec.inputCounts.get(id) || 0).toLocaleString()} inputs`]);
  }
  for (const [k, v] of Object.entries(rec.meta)) items.push([k, typeof v === "object" ? JSON.stringify(v) : String(v)]);
  if (S.ghost) {
    items.push(["Ghost", `${S.ghost.name || "(local)"} · ${formatTime(S.ghost.durationMs)} · offset ${(ghostOffset() / 1000).toFixed(3)} s`]);
  }
  if (rec.badLines) items.push(["Warning", `${rec.badLines} unreadable line(s) skipped`]);
  $("info").innerHTML = items.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
}

function fillDeviceSelect() {
  const sel = $("sel-device");
  sel.textContent = "";
  const ids = S.main.inputDevices();
  for (const id of S.main.devices.keys()) if (!ids.includes(id)) ids.push(id);
  for (const id of ids) {
    const n = S.main.inputCounts.get(id) || 0;
    sel.append(new Option(`${deviceName(S.main, id)} (${n.toLocaleString()})`, String(id)));
  }
  sel.value = String(S.device);
  $("grp-device").hidden = ids.length < 2;
}

function fillAlignSelects() {
  const fill = (sel, rec, value) => {
    sel.textContent = "";
    sel.append(new Option("recording start", "-1"));
    (rec ? rec.markers : []).forEach((m, i) => sel.append(new Option(`${m.label} (${formatTime(m.t)})`, String(i))));
    sel.value = String(value);
  };
  fill($("sel-align"), S.main, S.alignMain);
  fill($("sel-align-ghost"), S.ghost, S.alignGhost);
}

function matchGhostMarker() {
  if (!S.ghost) return;
  const m = S.alignMain >= 0 ? S.main.markers[S.alignMain] : null;
  if (!m) { S.alignGhost = -1; }
  else {
    const nth = S.main.markers.slice(0, S.alignMain).filter(x => x.label === m.label).length;
    const idx = S.ghost.markers.map((x, i) => [x, i]).filter(([x]) => x.label === m.label).map(e => e[1]);
    S.alignGhost = idx.length ? idx[Math.min(nth, idx.length - 1)] : -1;
  }
  $("sel-align-ghost").value = String(S.alignGhost);
}

// ------------------------------------------------------------------ playback
function seek(t) {
  const [a, b] = range();
  S.playhead = clamp(t, a, b);
  needsRender = true;
}

function setPlaying(on) {
  if (!S.main) on = false;
  if (on && S.playhead >= range()[1]) S.playhead = range()[0];
  S.playing = on;
  lastTick = performance.now();
  $("btn-play").textContent = on ? "⏸︎" : "▶︎";
}

function stepFrames(n) {
  setPlaying(false);
  const org = originMs();
  const k = frameOf(S.playhead - org, frameMs()) + n;
  seek(frameStartMs(k, S.fps, org));  // ns-exact like Timeline.frames: frame-aligned events count
}

function render() {
  if (!S.main) return;
  roll.setPlayhead(S.playhead, S.follow && S.playing);
  if (controller && S.device !== null) controller.update(S.main.track(S.device).stateAt(S.playhead));
  if (ghostController && S.ghost && S.ghostDevice !== null) {
    // +0.1 ns absorbs float noise of the offset so the ghost's frame-aligned events line up too
    ghostController.update(S.ghost.track(S.ghostDevice).stateAt(S.playhead - ghostOffset() + 1e-7));
  }
  const org = originMs();
  $("lbl-time").textContent = formatTime(S.playhead - org);
  $("lbl-frame").textContent = `f ${frameOf(S.playhead - org, frameMs())}`;
  if (document.activeElement !== $("scrub")) $("scrub").value = S.playhead;
}

function tick(now) {
  requestAnimationFrame(tick);  // re-arm first: a failing frame must not stop playback for good
  if (S.playing) {
    // clamp: a hidden tab gets no frames; resume where it paused instead of jumping
    S.playhead += Math.min(100, Math.max(0, now - lastTick)) * S.speed;
    const end = range()[1];
    if (S.playhead >= end) { S.playhead = end; setPlaying(false); }
    needsRender = true;
  }
  lastTick = now;
  if (needsRender) {
    needsRender = false;
    try { render(); } catch (e) { console.error("viewer render failed", e); }
  }
}

// ------------------------------------------------------------------ UI wiring
$("btn-open").onclick = () => $("file-main").click();
$("lnk-open").onclick = e => { e.preventDefault(); $("file-main").click(); };
$("btn-open-ghost").onclick = () => S.main ? $("file-ghost").click() : setStatus("Load a recording first", true);
$("file-main").onchange = e => { if (e.target.files[0]) openFile(e.target.files[0], "main"); e.target.value = ""; };
$("file-ghost").onchange = e => { if (e.target.files[0]) openFile(e.target.files[0], "ghost"); e.target.value = ""; };
$("btn-clear-ghost").onclick = () => loadGhost(null);

$("sel-main").onchange = async e => {
  if (!e.target.value || e.target.value === LOCAL) return;
  try { await loadMain(await fetchRecording(e.target.value)); } catch (err) { setStatus(err.message, true); }
};
$("sel-ghost").onchange = async e => {
  if (e.target.value === LOCAL) return;
  if (!e.target.value) { loadGhost(null); return; }
  if (!S.main) { setStatus("Load a recording first", true); e.target.value = ""; return; }
  try { loadGhost(await fetchRecording(e.target.value)); }
  catch (err) { setStatus(err.message, true); }
};

function setFps(value) {
  S.fps = resolveFps(value, 60);
  if (S.main) rebuild();
}
$("sel-fps").onchange = e => {
  const custom = e.target.value === "custom";
  $("inp-fps").hidden = !custom;
  setFps(custom ? $("inp-fps").value : e.target.value);
};
$("inp-fps").onchange = e => setFps(e.target.value);

$("sel-layout").onchange = e => { S.layoutSel = e.target.value; applyLayout(); };
$("sel-device").onchange = e => { S.device = parseInt(e.target.value, 10); applyLayout(); };
$("sel-speed").onchange = e => { S.speed = parseFloat(e.target.value); };
$("chk-follow").onchange = e => { S.follow = e.target.checked; };
$("chk-sticks").onchange = e => { S.sticks = e.target.checked; rebuild(); };
$("inp-map").onchange = e => {
  try {
    S.map = parseMap(e.target.value);
    S.table = mapTable(S.map);
    e.target.setCustomValidity("");
    if (controller) controller.setMap(S.map);
    if (ghostController) ghostController.setMap(S.map);
    rebuild();
  } catch (err) {
    e.target.setCustomValidity(err.message);
    setStatus(err.message, true);
  }
};
$("sel-align").onchange = e => {
  S.alignMain = parseInt(e.target.value, 10);
  matchGhostMarker();
  rebuild();
  if (S.alignMain >= 0) seek(originMs());
};
$("sel-align-ghost").onchange = e => { S.alignGhost = parseInt(e.target.value, 10); rebuild(); };
$("tbl-markers").onclick = e => {
  const tr = e.target.closest("tr[data-t]");
  if (tr) { seek(parseFloat(tr.dataset.t)); roll.setPlayhead(S.playhead, true); }
};

$("btn-play").onclick = () => setPlaying(!S.playing);
$("btn-prev").onclick = () => stepFrames(-1);
$("btn-next").onclick = () => stepFrames(1);
$("btn-start").onclick = () => { seek(range()[0]); roll.setPlayhead(S.playhead, true); };
$("btn-end").onclick = () => { seek(range()[1]); roll.setPlayhead(S.playhead, true); };
$("scrub").oninput = e => seek(parseFloat(e.target.value));
$("btn-fit").onclick = () => roll.fit();
$("btn-zoom-in").onclick = () => roll.zoom(0.5);
$("btn-zoom-out").onclick = () => roll.zoom(2);
$("btn-zoom-frames").onclick = () => roll.zoomFrames(60);

for (const tab of document.querySelectorAll(".tab")) {
  tab.onclick = () => {
    for (const t of document.querySelectorAll(".tab")) t.classList.toggle("active", t === tab);
    for (const b of document.querySelectorAll(".tab-body")) b.hidden = b.id !== `tab-${tab.dataset.tab}`;
  };
}

document.addEventListener("keydown", e => {
  const tgt = e.target;
  if (tgt instanceof Element && tgt.closest("input, select, textarea") && tgt.type !== "range") return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const step = e.shiftKey ? Math.round(1000 / frameMs()) : 1;
  switch (e.key) {
    case " ": e.preventDefault(); setPlaying(!S.playing); break;
    case "ArrowLeft": e.preventDefault(); stepFrames(-step); roll.setPlayhead(S.playhead, true); break;
    case "ArrowRight": e.preventDefault(); stepFrames(step); roll.setPlayhead(S.playhead, true); break;
    case "Home": seek(range()[0]); roll.setPlayhead(S.playhead, true); break;
    case "End": seek(range()[1]); roll.setPlayhead(S.playhead, true); break;
    case "+": case "=": roll.zoom(0.5, roll.X(S.playhead)); break;
    case "-": case "_": roll.zoom(2, roll.X(S.playhead)); break;
    case "f": case "F": roll.fit(); break;
    default: return;
  }
});

// drag & drop (left half: recording, right half: ghost)
let dragDepth = 0;
const drop = $("drop");
window.addEventListener("dragenter", e => {
  if (![...(e.dataTransfer?.types || [])].includes("Files")) return;
  e.preventDefault();
  dragDepth++;
  drop.hidden = false;
  drop.classList.toggle("single", !S.main);
});
window.addEventListener("dragover", e => {
  e.preventDefault();
  for (const z of drop.querySelectorAll(".drop-zone")) {
    const r = z.getBoundingClientRect();
    z.classList.toggle("over", e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom);
  }
});
window.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; drop.hidden = true; } });
window.addEventListener("drop", e => {
  e.preventDefault();
  dragDepth = 0;
  drop.hidden = true;
  const file = e.dataTransfer?.files?.[0];
  if (!file) return;
  const zone = [...drop.querySelectorAll(".drop-zone")].find(z => z.classList.contains("over"));
  openFile(file, zone && zone.dataset.target === "ghost" ? "ghost" : "main");
});
let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (S.layout) buildControllers(); }, 150);
});

// ------------------------------------------------------------------ start
async function init() {
  const fpsParam = params.get("fps");
  if (fpsParam) {
    const key = fpsParam.toLowerCase();
    const opt = [...$("sel-fps").options].find(o => o.value === key);
    if (opt) $("sel-fps").value = key;
    S.fps = resolveFps(fpsParam, 60);
    // named rates without their own option (e.g. snes, n64) show as a number in the custom box
    if (!opt) { $("sel-fps").value = "custom"; $("inp-fps").hidden = false; $("inp-fps").value = String(S.fps); }
  }
  if (params.get("map")) {
    $("inp-map").value = params.get("map");
    try { S.map = parseMap(params.get("map")); S.table = mapTable(S.map); } catch (e) { setStatus(e.message, true); }
  }
  try {
    const names = await (await fetch("/layouts")).json();
    for (const n of names) $("sel-layout").append(new Option(n, n));
    $("sel-layout").value = S.layoutSel;
  } catch { /* layouts unavailable */ }
  await refreshServerList();
  const file = params.get("file");
  if (file) {
    try {
      await loadMain(await fetchRecording(file));
      $("sel-main").value = file;
      const ghost = params.get("ghost");
      if (ghost) { loadGhost(await fetchRecording(ghost)); $("sel-ghost").value = ghost; }
      const t = parseFloat(params.get("t"));
      if (Number.isFinite(t)) { seek(t * 1000); roll.setPlayhead(S.playhead, true); }
    } catch (e) { setStatus(e.message, true); }
  } else if (!S.main) {
    await applyLayout();
  }
  requestAnimationFrame(t => { lastTick = t; tick(t); });
}

init();
window.controllerlogViewer = { S, roll, seek, setPlaying, loadMain, loadGhost, render, FPS, get controller() { return controller; } };
