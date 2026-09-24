// Renders a controller layout (docs/LAYOUTS.md) as inline SVG and lights it from a pad state.
//
//   const ctl = createController(parentEl, layoutJson, {scale: 1, labels: true, map: {...}});
//   ctl.update({buttons: [...26], axes: [...6]});
//
// Two stacked SVGs are used: a static one for the body (with a soft shadow) and a
// dynamic one for the inputs, so state changes never re-rasterise the filtered body.

import {
  AXIS_INDEX, DEFAULT_THEME, DIGITAL_INDEX, NUM_DIGITAL, TRIGGER_PRESS_THRESHOLD,
  AXIS_MAX, digitalLevels, mapTable, parseMap,
} from "./model.js";

const NS = "http://www.w3.org/2000/svg";
const FONT = 'system-ui, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Segoe UI Symbol", sans-serif';
let uid = 0;

function el(tag, attrs = {}, parent = null) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}

/** Create the SVG element for a layout shape (no styling). */
export function shapeElement(sp, parent = null) {
  switch (sp.shape) {
    case "rect": {
      const r = sp.r || 0;
      return el("rect", { x: sp.x, y: sp.y, width: sp.w, height: sp.h, rx: r || null, ry: r || null }, parent);
    }
    case "circle": return el("circle", { cx: sp.cx, cy: sp.cy, r: sp.r }, parent);
    case "ellipse": return el("ellipse", { cx: sp.cx, cy: sp.cy, rx: sp.rx, ry: sp.ry }, parent);
    case "polygon": return el("polygon", { points: sp.points.map(p => `${p[0]},${p[1]}`).join(" ") }, parent);
    case "text": {
      const t = el("text", {
        x: sp.x, y: sp.y, "text-anchor": sp.anchor || "middle", "dominant-baseline": "central",
      }, parent);
      t.textContent = sp.text;
      return t;
    }
    default: throw new Error(`unknown shape ${sp.shape}`);
  }
}

/** Bounding box {x, y, w, h} of a shape. */
export function shapeBBox(sp) {
  switch (sp.shape) {
    case "rect": return { x: sp.x, y: sp.y, w: sp.w, h: sp.h };
    case "circle": return { x: sp.cx - sp.r, y: sp.cy - sp.r, w: 2 * sp.r, h: 2 * sp.r };
    case "ellipse": return { x: sp.cx - sp.rx, y: sp.cy - sp.ry, w: 2 * sp.rx, h: 2 * sp.ry };
    case "polygon": {
      const xs = sp.points.map(p => p[0]), ys = sp.points.map(p => p[1]);
      const x = Math.min(...xs), y = Math.min(...ys);
      return { x, y, w: Math.max(...xs) - x, h: Math.max(...ys) - y };
    }
    case "text": {
      const s = sp.size || 14, w = String(sp.text).length * s * 0.6;
      const x = sp.anchor === "start" ? sp.x : sp.anchor === "end" ? sp.x - w : sp.x - w / 2;
      return { x, y: sp.y - s / 2, w, h: s };
    }
    default: return { x: 0, y: 0, w: 0, h: 0 };
  }
}

/** Point where a shape's label is centred (polygons: area centroid, like the video renderer). */
export function shapeCenter(sp) {
  switch (sp.shape) {
    case "rect": return [sp.x + sp.w / 2, sp.y + sp.h / 2];
    case "circle": case "ellipse": return [sp.cx, sp.cy];
    case "polygon": {
      const pts = sp.points;
      let a = 0, cx = 0, cy = 0;
      for (let i = 0; i < pts.length; i++) {
        const [xa, ya] = pts[i], [xb, yb] = pts[(i + 1) % pts.length];
        const c = xa * yb - xb * ya;
        a += c; cx += (xa + xb) * c; cy += (ya + yb) * c;
      }
      if (Math.abs(a) < 1e-9) {
        const b = shapeBBox(sp);
        return [b.x + b.w / 2, b.y + b.h / 2];
      }
      return [cx / (3 * a), cy / (3 * a)];
    }
    default: return [sp.x, sp.y];
  }
}

function labelText(parent, x, y, text, theme, size) {
  const t = el("text", {
    x, y, "text-anchor": "middle", "dominant-baseline": "central", fill: theme.label,
    "font-size": size || theme.font_size, "font-weight": 700, "pointer-events": "none",
  }, parent);
  t.textContent = text;
  return t;
}

function paint(node, fill, stroke, width) {
  node.setAttribute("fill", fill);
  node.setAttribute("stroke", stroke);
  node.setAttribute("stroke-width", width);
  node.setAttribute("stroke-linejoin", "round");
}

/**
 * Build a controller drawing inside `parent`.
 *
 * options: scale (1), labels (true), map ({src: dst} or "src:dst,..."), counts (false),
 *          shadow (true), halo (true), className ("").
 * Returns {root, layout, theme, width, height, update(state), setMap(map), setCounts(counts), destroy()}.
 */
export function createController(parent, layout, options = {}) {
  const o = { scale: 1, labels: true, map: null, counts: false, shadow: true, halo: true, className: "", ...options };
  const theme = { ...DEFAULT_THEME, ...(layout.theme || {}) };
  const id = `ctl${++uid}`;
  const [W, H] = layout.size;
  const width = Math.round(W * o.scale), height = Math.round(H * o.scale);

  const root = document.createElement("div");
  root.className = `ctl ${o.className}`.trim();
  root.style.cssText = `position:relative;width:${width}px;height:${height}px;flex:none`;
  const svgAttrs = {
    xmlns: NS, viewBox: `0 0 ${W} ${H}`, width, height, "font-family": FONT,
    style: "position:absolute;left:0;top:0;overflow:visible",
  };

  // -- static body layer
  const bodySvg = el("svg", { ...svgAttrs, "aria-hidden": "true" });
  if (o.shadow) {
    const defs = el("defs", {}, bodySvg);
    const f = el("filter", { id: `${id}-sh`, x: "-10%", y: "-10%", width: "120%", height: "130%" }, defs);
    el("feDropShadow", { dx: 0, dy: 4, stdDeviation: 5, "flood-color": "#000", "flood-opacity": 0.45 }, f);
  }
  const gBody = el("g", { filter: o.shadow ? `url(#${id}-sh)` : null }, bodySvg);
  for (const sp of layout.body || []) {
    const node = shapeElement(sp, gBody);
    if (sp.shape === "text") {
      node.setAttribute("fill", sp.fill || theme.label);
      node.setAttribute("font-size", sp.size || theme.font_size);
      node.setAttribute("font-weight", 700);
      node.setAttribute("letter-spacing", 0.5);
    } else {
      paint(node, sp.fill || theme.body, sp.stroke || theme.body_stroke, sp.stroke_width ?? theme.stroke_width ?? 2);
    }
  }

  // -- dynamic input layer
  const svg = el("svg", { ...svgAttrs, role: "img", "aria-label": layout.title || layout.name || "controller" });
  const defs = el("defs", {}, svg);
  root.append(bodySvg, svg);
  parent.appendChild(root);

  const handlers = [];
  const gBadges = o.counts ? el("g", { class: "badges" }) : null;  // appended last: always on top
  let clipN = 0;
  for (const spec of layout.elements || []) {
    const g = el("g", { class: `el el-${spec.type}` }, svg);
    if (spec.type === "stick") handlers.push(makeStick(g, spec));
    else if (spec.type === "trigger") handlers.push(makeTrigger(g, spec));
    else handlers.push(makeButton(g, spec));
  }
  if (gBadges) svg.appendChild(gBadges);

  function haloFor(g, spec, color) {
    if (!o.halo || spec.shape === "text") return null;
    const h = shapeElement(spec, g);
    h.setAttribute("fill", "none");
    h.setAttribute("stroke", color);
    h.setAttribute("stroke-width", 7);
    h.setAttribute("stroke-linejoin", "round");
    h.setAttribute("opacity", 0);
    return h;
  }

  function countBadge(g, bbox) {
    if (!o.counts) return null;
    const t = el("text", {
      x: bbox.x + bbox.w + 1, y: bbox.y - 1, "text-anchor": "start", "dominant-baseline": "auto",
      "font-size": 11, "font-weight": 700, fill: theme.label, stroke: "#000", "stroke-width": 3,
      "paint-order": "stroke", "stroke-linejoin": "round", "pointer-events": "none",
    }, gBadges);
    t.textContent = "";
    return t;
  }

  function makeButton(g, spec) {
    const active = spec.active || theme.active;
    // Text-shaped buttons idle in the label colour (theme.idle would vanish on the body);
    // same rule as render/draw.py.
    const idleFill = spec.fill || (spec.shape === "text" ? theme.label : theme.idle);
    const idleStroke = spec.stroke || spec.idle_stroke || theme.idle_stroke;
    const sw = spec.stroke_width ?? theme.stroke_width ?? 2;
    const halo = haloFor(g, spec, active);
    const face = shapeElement(spec, g);
    if (spec.shape === "text") {
      face.setAttribute("fill", idleFill);
      face.setAttribute("font-size", spec.size || theme.font_size);
      face.setAttribute("font-weight", 700);
    } else {
      paint(face, idleFill, idleStroke, sw);
    }
    let label = null;
    if (o.labels && spec.label && spec.shape !== "text") {
      const [cx, cy] = shapeCenter(spec);
      label = labelText(g, cx, cy, spec.label, theme, spec.label_size);
    }
    const badge = countBadge(g, shapeBBox(spec));
    const idx = DIGITAL_INDEX[spec.input];
    let lit = false;
    return {
      input: spec.input, badge,
      update(levels) {
        const on = levels[idx] >= TRIGGER_PRESS_THRESHOLD;
        if (on === lit) return;
        lit = on;
        face.setAttribute("fill", on ? active : idleFill);
        if (halo) halo.setAttribute("opacity", on ? 0.35 : 0);
        if (label) label.setAttribute("fill", on ? theme.label_active : theme.label);
      },
    };
  }

  function makeTrigger(g, spec) {
    const active = spec.active || theme.active;
    const idleFill = spec.fill || theme.idle;
    const idleStroke = spec.stroke || spec.idle_stroke || theme.idle_stroke;
    const sw = spec.stroke_width ?? theme.stroke_width ?? 2;
    const bbox = shapeBBox(spec);
    const dir = spec.fill_dir || "up";
    const halo = haloFor(g, spec, active);
    const face = shapeElement(spec, g);
    paint(face, idleFill, idleStroke, sw);
    const clip = el("clipPath", { id: `${id}-clip${clipN++}` }, defs);
    shapeElement(spec, clip);
    const bar = el("rect", { x: bbox.x, y: bbox.y, width: 0, height: 0, fill: active, "clip-path": `url(#${clip.id})` }, g);
    const outline = shapeElement(spec, g);
    paint(outline, "none", idleStroke, sw);
    let label = null;
    if (o.labels && spec.label) {
      const [cx, cy] = shapeCenter(spec);
      label = labelText(g, cx, cy, spec.label, theme, spec.label_size);
    }
    const badge = countBadge(g, bbox);
    const idx = DIGITAL_INDEX[spec.input];
    let last = -1, lit = null;
    return {
      input: spec.input, badge,
      update(levels) {
        const v = levels[idx];
        if (v !== last) {
          last = v;
          const f = Math.max(0, Math.min(1, v / AXIS_MAX));
          let { x, y, w, h } = bbox;
          if (dir === "up") { y = y + h * (1 - f); h = h * f; }
          else if (dir === "down") { h = h * f; }
          else if (dir === "left") { x = x + w * (1 - f); w = w * f; }
          else { w = w * f; }
          bar.setAttribute("x", x); bar.setAttribute("y", y);
          bar.setAttribute("width", w); bar.setAttribute("height", h);
        }
        const on = v >= TRIGGER_PRESS_THRESHOLD;
        if (on === lit) return;
        lit = on;
        bar.setAttribute("opacity", on ? 1 : 0.6);
        outline.setAttribute("stroke", on ? active : idleStroke);
        if (halo) halo.setAttribute("opacity", on ? 0.35 : 0);
        if (label) label.setAttribute("fill", on ? theme.label_active : theme.label);
      },
    };
  }

  function makeStick(g, spec) {
    const r = spec.r, kr = spec.knob_r ?? r * 0.55, travel = spec.travel ?? (r - kr);
    const active = spec.active || theme.active;
    const knobFill = spec.fill || theme.idle;
    const ring = el("circle", { cx: spec.cx, cy: spec.cy, r }, g);
    paint(ring, spec.ring || theme.body_stroke, spec.stroke || spec.idle_stroke || theme.idle_stroke,
      spec.stroke_width ?? theme.stroke_width ?? 2);
    el("circle", { cx: spec.cx, cy: spec.cy, r: Math.max(2, travel), fill: "none", stroke: theme.idle,
      "stroke-width": 1, "stroke-dasharray": "2 3", opacity: 0.6 }, g);
    const line = el("line", { x1: spec.cx, y1: spec.cy, x2: spec.cx, y2: spec.cy, stroke: active,
      "stroke-width": 3, "stroke-linecap": "round", opacity: 0 }, g);
    const knob = el("g", {}, g);
    const kspec = { shape: "circle", cx: spec.cx, cy: spec.cy, r: kr };
    const halo = haloFor(knob, kspec, active);
    const face = el("circle", { cx: spec.cx, cy: spec.cy, r: kr }, knob);
    paint(face, knobFill, spec.stroke || spec.idle_stroke || theme.idle_stroke, spec.stroke_width ?? theme.stroke_width ?? 2);
    el("circle", { cx: spec.cx, cy: spec.cy, r: kr * 0.62, fill: "none", stroke: "#ffffff",
      "stroke-opacity": 0.09, "stroke-width": 2 }, knob);
    let label = null;
    if (o.labels && spec.label) label = labelText(knob, spec.cx, spec.cy, spec.label, theme, spec.label_size || Math.min(theme.font_size, kr));
    const bIdx = spec.button ? DIGITAL_INDEX[spec.button] : -1;
    const badge = spec.button ? countBadge(g, shapeBBox({ shape: "circle", cx: spec.cx, cy: spec.cy, r })) : null;
    const xi = AXIS_INDEX[spec.x_axis], yi = AXIS_INDEX[spec.y_axis];
    let lx = NaN, ly = NaN, lit = false;
    return {
      input: spec.button || null, badge,
      update(levels, state) {
        const x = state.axes[xi] / 32768, y = state.axes[yi] / 32768;
        if (x !== lx || y !== ly) {
          lx = x; ly = y;
          const dx = travel * x, dy = travel * y;
          knob.setAttribute("transform", `translate(${dx.toFixed(2)} ${dy.toFixed(2)})`);
          line.setAttribute("x2", spec.cx + dx); line.setAttribute("y2", spec.cy + dy);
          line.setAttribute("opacity", Math.hypot(x, y) > 0.12 ? 0.55 : 0);
        }
        const on = bIdx >= 0 && levels[bIdx] >= TRIGGER_PRESS_THRESHOLD;
        if (on === lit) return;
        lit = on;
        face.setAttribute("fill", on ? active : knobFill);
        if (halo) halo.setAttribute("opacity", on ? 0.35 : 0);
        if (label) label.setAttribute("fill", on ? theme.label_active : theme.label);
      },
    };
  }

  let table = mapTable(typeof o.map === "string" ? parseMap(o.map) : o.map);
  const levels = new Float64Array(NUM_DIGITAL);

  const api = {
    root, svg, bodySvg, layout, theme, width, height,
    /** Draw a pad state ({buttons: [...26], axes: [...6]}). Only changed attributes are touched. */
    update(state) {
      digitalLevels(state, table, levels);
      for (const h of handlers) h.update(levels, state);
    },
    /** Replace the display remapping ({src: dst} or "src:dst,..."). */
    setMap(map) { table = mapTable(typeof map === "string" ? parseMap(map) : map); },
    /** Show per-input press counters ({input: n}); requires options.counts. */
    setCounts(counts) {
      for (const h of handlers) {
        if (!h.badge || !h.input) continue;
        const n = counts ? (counts instanceof Map ? counts.get(h.input) : counts[h.input]) : 0;
        const txt = n ? String(n) : "";
        if (h.badge.textContent !== txt) h.badge.textContent = txt;
      }
    },
    /** Inputs drawn by this layout (digital names). */
    inputs: handlers.map(h => h.input).filter(Boolean),
    destroy() { root.remove(); },
  };
  return api;
}

/** Fetch a layout JSON from the overlay server (cached per name). */
const layoutCache = new Map();
export function fetchLayout(name, base = "") {
  if (!layoutCache.has(name)) {
    const p = fetch(`${base}/layouts/${encodeURIComponent(name)}.json`).then(r => {
      if (!r.ok) throw new Error(`layout ${name}: HTTP ${r.status}`);
      return r.json();
    });
    p.catch(() => layoutCache.delete(name));
    layoutCache.set(name, p);
  }
  return layoutCache.get(name);
}

/** Layout name for a controller family given the /api/layouts listing. */
export function layoutForFamily(family, listing) {
  for (const l of listing || []) if ((l.families || []).includes(family)) return l.name;
  if ((listing || []).some(l => l.name === "generic")) return "generic";
  return listing && listing.length ? listing[0].name : "generic";
}
