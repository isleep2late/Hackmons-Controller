// Canonical controller model, mirrored from controllerlog/model.py.
// Indices match SDL_GamepadButton / SDL_GamepadAxis (and the .ctlog rows).

export const BUTTONS = [
  "south", "east", "west", "north", "back", "guide", "start",
  "left_stick", "right_stick", "left_shoulder", "right_shoulder",
  "dpad_up", "dpad_down", "dpad_left", "dpad_right",
  "misc1", "right_paddle1", "left_paddle1", "right_paddle2", "left_paddle2",
  "touchpad", "misc2", "misc3", "misc4", "misc5", "misc6",
];
export const AXES = ["left_x", "left_y", "right_x", "right_y", "left_trigger", "right_trigger"];
export const NUM_BUTTONS = BUTTONS.length;
export const NUM_AXES = AXES.length;
export const BUTTON_INDEX = Object.fromEntries(BUTTONS.map((n, i) => [n, i]));
export const AXIS_INDEX = Object.fromEntries(AXES.map((n, i) => [n, i]));
export const TRIGGER_PRESS_THRESHOLD = 16384;
export const AXIS_MAX = 32767;

// Digital inputs: 26 buttons then the two triggers (same order as timeline.press_name).
export const DIGITAL = [...BUTTONS, "left_trigger", "right_trigger"];
export const NUM_DIGITAL = DIGITAL.length;
export const DIGITAL_INDEX = Object.fromEntries(DIGITAL.map((n, i) => [n, i]));
const LT = AXIS_INDEX.left_trigger;
const RT = AXIS_INDEX.right_trigger;

export const DEFAULT_THEME = {
  body: "#2b2f36", body_stroke: "#15171b",
  idle: "#3d434d", idle_stroke: "#15171b",
  active: "#ffd23f", label: "#e8e8e8", label_active: "#111111",
  font_size: 14,
};

// Native frame rates (controllerlog/timeline.py FPS).
export const FPS = {
  gb: 4194304 / 70224,
  gbc: 4194304 / 70224,
  gba: 16777216 / 280896,
  nes: (39375000 / 11 * 6 / 4) / 89341.5,
  snes: 21477272.727 / 357366,
  n64: 60, "60": 60, "30": 30, "50": 50,
};

export function resolveFps(value, fallback = 60) {
  if (value === null || value === undefined || value === "") return fallback;
  const key = String(value).toLowerCase();
  if (key in FPS) return FPS[key];
  const v = Number(key);
  return Number.isFinite(v) && v > 0 ? v : fallback;
}

export const FACE_LABELS = {
  xbox: { south: "A", east: "B", west: "X", north: "Y", back: "View", start: "Menu", guide: "Xbox",
    left_shoulder: "LB", right_shoulder: "RB", left_trigger: "LT", right_trigger: "RT", misc1: "Share" },
  playstation: { south: "✕", east: "○", west: "□", north: "△", back: "Share", start: "Options",
    guide: "PS", left_shoulder: "L1", right_shoulder: "R1", left_trigger: "L2", right_trigger: "R2",
    misc1: "Mic", touchpad: "Pad" },
  switch: { south: "B", east: "A", west: "Y", north: "X", back: "−", start: "+", guide: "Home",
    left_shoulder: "L", right_shoulder: "R", left_trigger: "ZL", right_trigger: "ZR", misc1: "Cap" },
  gamecube: { south: "A", east: "X", west: "B", north: "Y", start: "Start", right_shoulder: "Z",
    left_trigger: "L", right_trigger: "R", misc3: "L·", misc4: "R·" },
  gameboy: { south: "B", east: "A", back: "Select", start: "Start" },
  gba: { south: "B", east: "A", back: "Select", start: "Start", left_shoulder: "L", right_shoulder: "R" },
  generic: { south: "1", east: "2", west: "3", north: "4", back: "Sel", start: "Start", guide: "Home",
    left_shoulder: "L1", right_shoulder: "R1", left_trigger: "L2", right_trigger: "R2" },
};

const GENERIC_SHORT = {
  dpad_up: "↑", dpad_down: "↓", dpad_left: "←", dpad_right: "→",
  left_stick: "L3", right_stick: "R3", back: "Back", start: "Start", guide: "Home",
  left_shoulder: "LB", right_shoulder: "RB", left_trigger: "LT", right_trigger: "RT",
  right_paddle1: "P1", right_paddle2: "P2", left_paddle1: "P3", left_paddle2: "P4",
  touchpad: "Pad", misc1: "Misc1", misc2: "Misc2", misc3: "Misc3", misc4: "Misc4",
  misc5: "Misc5", misc6: "Misc6", south: "S", east: "E", west: "W", north: "N",
};

/**
 * Short display label for an input: layout element label, then the labels of the layout's
 * family (so rows match the drawing), then `family`'s labels, then a generic name.
 */
export function inputLabel(input, layout = null, family = null) {
  if (layout) {
    for (const el of layout.elements || []) {
      const inp = el.type === "stick" ? el.button : el.input;
      if (inp === input && el.label && el.type !== "stick") return el.label;
    }
  }
  const layoutFam = layout && (layout.families || [])[0];
  if (layoutFam && FACE_LABELS[layoutFam] && FACE_LABELS[layoutFam][input]) return FACE_LABELS[layoutFam][input];
  const fam = family || layoutFam;
  if (fam && FACE_LABELS[fam] && FACE_LABELS[fam][input]) return FACE_LABELS[fam][input];
  return GENERIC_SHORT[input] || input;
}

export function newState() {
  return { buttons: new Array(NUM_BUTTONS).fill(0), axes: new Array(NUM_AXES).fill(0) };
}

export function copyState(src, dst = newState()) {
  for (let i = 0; i < NUM_BUTTONS; i++) dst.buttons[i] = src.buttons[i] | 0;
  for (let i = 0; i < NUM_AXES; i++) dst.axes[i] = src.axes[i] | 0;
  return dst;
}

export function stateFromJson(obj) {
  const s = newState();
  if (obj && Array.isArray(obj.buttons)) obj.buttons.forEach((v, i) => { if (i < NUM_BUTTONS) s.buttons[i] = v ? 1 : 0; });
  if (obj && Array.isArray(obj.axes)) obj.axes.forEach((v, i) => { if (i < NUM_AXES) s.axes[i] = v | 0; });
  return s;
}

/** Apply a b/a row ([t, dev, kind, code, value]) to a state. Returns true if it changed. */
export function applyRow(state, row) {
  const kind = row[2], code = row[3];
  if (!Number.isInteger(code)) return false;
  if (kind === "b") {
    if (code < 0 || code >= NUM_BUTTONS) return false;
    const v = row[4] ? 1 : 0;
    if (state.buttons[code] === v) return false;
    state.buttons[code] = v;
    return true;
  }
  if (kind === "a") {
    if (code < 0 || code >= NUM_AXES) return false;
    const v = row[4] | 0;
    if (state.axes[code] === v) return false;
    state.axes[code] = v;
    return true;
  }
  return false;
}

/** Parse "src:dst,src:dst" (layouts.parse_map). Invalid entries throw. */
export function parseMap(spec) {
  const out = {};
  if (!spec) return out;
  for (let part of String(spec).split(",")) {
    part = part.trim();
    if (!part) continue;
    const i = part.indexOf(":");
    if (i < 0) throw new Error(`bad map entry ${part} (expected src:dst)`);
    const src = part.slice(0, i).trim(), dst = part.slice(i + 1).trim();
    for (const v of [src, dst]) {
      if (!(v in DIGITAL_INDEX)) throw new Error(`bad map entry ${part}: unknown input ${v}`);
    }
    out[src] = dst;
  }
  return out;
}

/** mapping {src: dst} -> Uint8Array(NUM_DIGITAL) of destination indices. */
export function mapTable(mapping) {
  const t = new Uint8Array(NUM_DIGITAL);
  for (let i = 0; i < NUM_DIGITAL; i++) t[i] = i;
  for (const [src, dst] of Object.entries(mapping || {})) {
    if (src in DIGITAL_INDEX && dst in DIGITAL_INDEX) t[DIGITAL_INDEX[src]] = DIGITAL_INDEX[dst];
  }
  return t;
}

/**
 * Per destination input "level" 0..32767 after remapping: buttons count as 0 or 32767,
 * triggers keep their analog value. A level >= TRIGGER_PRESS_THRESHOLD means pressed.
 */
export function digitalLevels(state, table, out = new Float64Array(NUM_DIGITAL)) {
  out.fill(0);
  for (let i = 0; i < NUM_BUTTONS; i++) {
    if (state.buttons[i]) out[table[i]] = AXIS_MAX;
  }
  const lt = Math.max(0, Math.min(AXIS_MAX, state.axes[LT])), rt = Math.max(0, Math.min(AXIS_MAX, state.axes[RT]));
  const a = table[NUM_BUTTONS], b = table[NUM_BUTTONS + 1];
  if (lt > out[a]) out[a] = lt;
  if (rt > out[b]) out[b] = rt;
  return out;
}

/** Digital pressed state of source input index i (no mapping). */
export function pressedIndex(state, i) {
  if (i < NUM_BUTTONS) return !!state.buttons[i];
  return state.axes[i === NUM_BUTTONS ? LT : RT] >= TRIGGER_PRESS_THRESHOLD;
}

export function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }

/** "1:02.345" / "-0:00.016" from milliseconds. */
export function formatTime(ms, decimals = 3) {
  if (!Number.isFinite(ms)) return "–";
  const scale = 10 ** decimals;
  const units = Math.round(Math.abs(ms) / 1000 * scale);   // round first so 59.9996 s carries to 1:00.000
  const frac = units % scale;
  let secs = Math.floor(units / scale);
  const h = Math.floor(secs / 3600); secs -= h * 3600;
  const m = Math.floor(secs / 60); secs -= m * 60;
  const s = String(secs).padStart(2, "0") + (decimals ? "." + String(frac).padStart(decimals, "0") : "");
  const body = h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
  return (ms < 0 && units ? "-" : "") + body;
}

/** Frame index containing msRel (1 ns tolerance, so an event on a rounded frame start is in that frame). */
export function frameOf(msRel, frameMs) { return Math.floor((msRel + 1e-6) / frameMs); }

/**
 * Start of frame k in ms, rounded to the nanosecond exactly like timeline.Timeline.frames
 * (`offset_ns + round(k * 1e9 / fps)`), so the state sampled there includes events written
 * on that frame by timeline.frames_to_events (bk2/GSE imports, TAS output).
 */
export function frameStartMs(k, fps, originMs = 0) {
  return (Math.round(originMs * 1e6) + Math.round(k * (1e9 / fps))) / 1e6;
}

export function hexColor(value, fallback) {
  const v = String(value || "").replace(/^#/, "");
  return /^[0-9a-fA-F]{3,8}$/.test(v) ? `#${v}` : fallback;
}
