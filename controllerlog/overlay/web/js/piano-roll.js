// Piano-roll timeline for the recording viewer: rows = inputs, x = time.
// Wheel zooms around the cursor, drag pans, click / ruler-drag seeks, hover shows press details.

import { formatTime, frameOf } from "./model.js";
import { intervalAt, lowerBound, upperBound } from "./recording.js";

const FONT = 'system-ui, "Segoe UI", Roboto, Arial, sans-serif';
const TICK_STEPS = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 15000, 30000, 60000,
  120000, 300000, 600000, 900000, 1800000, 3600000];
const FRAME_STEPS = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000];
const MARKER = "#ff9f43";
const GHOST = "#7cc8ff";

export class PianoRoll {
  /**
   * @param {HTMLElement} container empty element; the roll sizes its height to the content
   * @param {object} opts onSeek(tMs), onViewChange()
   */
  constructor(container, opts = {}) {
    this.el = container;
    this.el.classList.add("roll");
    this.base = document.createElement("canvas");
    this.over = document.createElement("canvas");
    this.over.className = "roll-over";
    this.tip = document.createElement("div");
    this.tip.className = "roll-tip";
    this.el.append(this.base, this.over, this.tip);
    this.bctx = this.base.getContext("2d");
    this.octx = this.over.getContext("2d");
    this.onSeek = opts.onSeek || (() => {});
    this.onViewChange = opts.onViewChange || (() => {});
    this.labelW = 104;
    this.rulerH = 50;
    this.rowH = 20;
    this.analogH = 38;
    this.rows = [];
    this.analog = [];
    this.markers = [];
    this.ghostMarkers = [];
    this.range = [0, 1000];
    this.originMs = 0;
    this.frameMs = 1000 / 60;
    this.view = { t0: 0, t1: 1000 };
    this.playhead = 0;
    this.hover = null;
    this.ghost = false;
    this.baseDirty = true;
    this.overDirty = true;
    this.width = 0;
    this._bind();
    new ResizeObserver(() => this._resize()).observe(this.el);
    this._resize();
    const loop = () => {
      if (this.baseDirty) this._drawBase();
      if (this.overDirty) this._drawOver();
      requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
  }

  /**
   * rows: [{input, label, color, main: intervals, ghost: intervals|null}]
   * analog: [{label, color, t: Float64Array, v: Int32Array, gt, gv, ghostOffset}]
   * markers / ghostMarkers: [{t, label}] (ghost already shifted to main time)
   * range: [startMs, endMs] of everything drawable.
   */
  setData({ rows, analog = [], markers = [], ghostMarkers = [], range, ghost = false }) {
    this.rows = rows;
    this.analog = analog;
    this.markers = markers;
    this.ghostMarkers = ghostMarkers;
    this.ghost = ghost;
    const oldRange = this.range;
    this.range = range || [0, 1000];
    if (oldRange[0] !== this.range[0] || oldRange[1] !== this.range[1]) this.fit();
    this._resize();
    this.invalidate();
  }

  setFrames(frameMs, originMs) { this.frameMs = frameMs; this.originMs = originMs; this.invalidate(); }

  invalidate() { this.baseDirty = true; this.overDirty = true; }

  get plotW() { return Math.max(10, this.width - this.labelW - 8); }
  get pxPerMs() { return this.plotW / (this.view.t1 - this.view.t0); }
  X(t) { return this.labelW + (t - this.view.t0) * this.pxPerMs; }
  T(x) { return this.view.t0 + (x - this.labelW) / this.pxPerMs; }

  fit() {
    const [a, b] = this.range;
    const span = Math.max(500, b - a);
    this.setView(a - span * 0.01, a + span * 1.01);
  }

  setView(t0, t1) {
    const [a, b] = this.range;
    const span = Math.max(1000, b - a);
    let w = Math.max(30, Math.min(t1 - t0, span * 1.2));
    const lo = a - span * 0.1, hi = b + span * 0.1;
    if (t0 < lo) t0 = lo;
    if (t0 + w > hi) t0 = Math.max(lo, hi - w);
    this.view = { t0, t1: t0 + w };
    this.invalidate();
    this.onViewChange(this.view);
  }

  zoom(factor, x = this.labelW + this.plotW / 2) {
    const t = this.T(x);
    const w = (this.view.t1 - this.view.t0) * factor;
    const frac = (x - this.labelW) / this.plotW;
    this.setView(t - w * frac, t - w * frac + w);
  }

  /** Show `frames` frames around the playhead. */
  zoomFrames(frames) {
    const w = frames * this.frameMs;
    this.setView(this.playhead - w / 2, this.playhead + w / 2);
  }

  setPlayhead(t, follow = false) {
    this.playhead = t;
    if (follow) {
      const w = this.view.t1 - this.view.t0;
      if (t > this.view.t1 - w * 0.02 || t < this.view.t0) this.setView(t - w * 0.1, t + w * 0.9);
    }
    this.overDirty = true;
  }

  get bandH() { return this.ghostMarkers.length ? 22 : 6; }

  get contentH() {
    return this.rulerH + this.rows.length * this.rowH + this.analog.length * this.analogH + this.bandH;
  }

  _resize() {
    const w = Math.max(200, Math.floor(this.el.clientWidth));
    const h = this.contentH;
    const dpr = window.devicePixelRatio || 1;
    this.width = w;
    this.height = h;
    this.dpr = dpr;
    for (const c of [this.base, this.over]) {
      c.width = Math.round(w * dpr);
      c.height = Math.round(h * dpr);
      c.style.width = `${w}px`;
      c.style.height = `${h}px`;
    }
    this.el.style.height = `${h}px`;
    this.invalidate();
  }

  // ------------------------------------------------------------------ drawing
  _tickStep(minPx) {
    const ppm = this.pxPerMs;
    for (const s of TICK_STEPS) if (s * ppm >= minPx) return s;
    return TICK_STEPS[TICK_STEPS.length - 1];
  }

  _drawBase() {
    this.baseDirty = false;
    const ctx = this.bctx, W = this.width, H = this.height, L = this.labelW;
    const { t0, t1 } = this.view;
    const ppm = this.pxPerMs;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const top = this.rulerH;
    const rowsBottom = top + this.rows.length * this.rowH;
    const bottom = rowsBottom + this.analog.length * this.analogH;

    // ruler + row backgrounds
    ctx.fillStyle = "rgba(255,255,255,0.04)";
    ctx.fillRect(L, 0, W - L, top);
    for (let i = 0; i < this.rows.length; i++) {
      if (i % 2 === 0) { ctx.fillStyle = "rgba(255,255,255,0.025)"; ctx.fillRect(L, top + i * this.rowH, W - L, this.rowH); }
    }
    for (let i = 0; i < this.analog.length; i++) {
      ctx.fillStyle = i % 2 ? "rgba(255,255,255,0.02)" : "rgba(255,255,255,0.035)";
      ctx.fillRect(L, rowsBottom + i * this.analogH, W - L, this.analogH);
    }

    ctx.save();
    ctx.beginPath();
    ctx.rect(L, 0, W - L, H);
    ctx.clip();

    // outside-recording shading
    const [ra, rb] = this.range;
    ctx.fillStyle = "rgba(0,0,0,0.25)";
    if (this.X(ra) > L) ctx.fillRect(L, top, this.X(ra) - L, bottom - top);
    if (this.X(rb) < W) ctx.fillRect(this.X(rb), top, W - this.X(rb), bottom - top);

    // frame grid
    const fm = this.frameMs, org = this.originMs;
    const fpx = fm * ppm;
    if (fpx >= 5) {
      ctx.strokeStyle = fpx >= 14 ? "rgba(255,255,255,0.08)" : "rgba(255,255,255,0.05)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      const k0 = Math.floor((t0 - org) / fm), k1 = Math.ceil((t1 - org) / fm);
      for (let k = k0; k <= k1; k++) {
        const x = Math.round(this.X(org + k * fm)) + 0.5;
        ctx.moveTo(x, 16); ctx.lineTo(x, bottom);
      }
      ctx.stroke();
    }

    // time ticks (relative to origin)
    const step = this._tickStep(90);
    ctx.strokeStyle = "rgba(255,255,255,0.14)";
    ctx.fillStyle = "rgba(230,234,240,0.85)";
    ctx.font = `600 11px ${FONT}`;
    ctx.textBaseline = "top";
    ctx.textAlign = "left";
    ctx.beginPath();
    const decimals = step >= 1000 ? 1 : step >= 100 ? 2 : 3;
    for (let k = Math.floor((t0 - org) / step); k * step + org <= t1; k++) {
      const t = org + k * step;
      const x = Math.round(this.X(t)) + 0.5;
      ctx.moveTo(x, 0); ctx.lineTo(x, bottom);
      ctx.fillText(formatTime(t - org, decimals), x + 4, 4);
    }
    ctx.stroke();

    // frame numbers
    if (fpx >= 1.2) {
      let fstep = FRAME_STEPS[FRAME_STEPS.length - 1];
      for (const s of FRAME_STEPS) if (s * fpx >= 34) { fstep = s; break; }
      ctx.fillStyle = "rgba(160,170,185,0.85)";
      ctx.font = `500 10px ${FONT}`;
      ctx.textAlign = "center";
      const k0 = Math.floor((t0 - org) / fm / fstep) * fstep;
      for (let k = k0; (org + k * fm) <= t1 + fm; k += fstep) {
        const xc = this.X(org + (k + 0.5) * fm);
        ctx.fillText(String(k), xc, 18);
      }
    }

    // digital rows
    const hasGhost = this.ghost;
    const pad = 3;
    for (let i = 0; i < this.rows.length; i++) {
      const row = this.rows[i];
      const y = top + i * this.rowH;
      const mainH = hasGhost ? Math.round((this.rowH - 2 * pad) * 0.6) : this.rowH - 2 * pad;
      ctx.fillStyle = row.color;
      drawIntervals(ctx, row.main, t0, t1, this, y + pad, mainH, false);
      if (hasGhost && row.ghost) {
        ctx.fillStyle = "rgba(124,200,255,0.55)";
        drawIntervals(ctx, row.ghost, t0, t1, this, y + pad + mainH + 1, this.rowH - 2 * pad - mainH - 1, true);
      }
    }

    // analog rows
    for (let i = 0; i < this.analog.length; i++) {
      const a = this.analog[i];
      const y = rowsBottom + i * this.analogH;
      const mid = y + this.analogH / 2, amp = this.analogH / 2 - 4;
      ctx.strokeStyle = "rgba(255,255,255,0.12)";
      ctx.beginPath(); ctx.moveTo(L, mid + 0.5); ctx.lineTo(W, mid + 0.5); ctx.stroke();
      const s = a.invert ? -1 : 1;
      if (a.gt) drawAnalog(ctx, a.gt, a.gv, t0, t1, this, mid, amp * s, "rgba(124,200,255,0.7)", a.ghostOffset || 0);
      drawAnalog(ctx, a.t, a.v, t0, t1, this, mid, amp * s, a.color, 0);
    }

    // markers
    const drawMarkers = (list, color, yLabel, dash, lineTop, lineBottom) => {
      ctx.font = `600 10px ${FONT}`;
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      for (const m of list) {
        if (m.t < t0 - 1 || m.t > t1 + 1) continue;
        const x = Math.round(this.X(m.t)) + 0.5;
        ctx.strokeStyle = color;
        ctx.setLineDash(dash);
        ctx.beginPath(); ctx.moveTo(x, lineTop); ctx.lineTo(x, lineBottom); ctx.stroke();
        ctx.setLineDash([]);
        const text = m.label.length > 28 ? m.label.slice(0, 27) + "…" : m.label;
        const tw = ctx.measureText(text).width;
        ctx.fillStyle = color;
        roundRect(ctx, x, yLabel - 7, tw + 8, 14, 3);
        ctx.fill();
        ctx.fillStyle = "#15171b";
        ctx.fillText(text, x + 4, yLabel);
      }
    };
    drawMarkers(this.ghostMarkers, GHOST, bottom + 11, [2, 3], top, bottom + 4);
    drawMarkers(this.markers, MARKER, top - 10, [4, 3], top - 16, bottom);
    ctx.restore();

    // label column
    ctx.fillStyle = "rgba(18,20,25,0.96)";
    ctx.fillRect(0, 0, L, H);
    ctx.fillStyle = "rgba(255,255,255,0.08)";
    ctx.fillRect(L - 1, 0, 1, H);
    ctx.font = `600 12px ${FONT}`;
    ctx.textBaseline = "middle";
    ctx.textAlign = "left";
    for (let i = 0; i < this.rows.length; i++) {
      const row = this.rows[i];
      const y = top + i * this.rowH + this.rowH / 2;
      ctx.fillStyle = row.color;
      roundRect(ctx, 8, y - 5, 4, 10, 2);
      ctx.fill();
      ctx.fillStyle = "rgba(232,235,240,0.92)";
      ctx.fillText(row.label, 18, y + 0.5);
      if (row.sub) {
        ctx.fillStyle = "rgba(150,158,172,0.8)";
        ctx.font = `500 10px ${FONT}`;
        ctx.textAlign = "right";
        ctx.fillText(row.sub, L - 8, y + 0.5);
        ctx.textAlign = "left";
        ctx.font = `600 12px ${FONT}`;
      }
    }
    for (let i = 0; i < this.analog.length; i++) {
      const a = this.analog[i];
      const y = rowsBottom + i * this.analogH + this.analogH / 2;
      ctx.fillStyle = a.color;
      roundRect(ctx, 8, y - 5, 4, 10, 2);
      ctx.fill();
      ctx.fillStyle = "rgba(232,235,240,0.92)";
      ctx.fillText(a.label, 18, y + 0.5);
    }
    ctx.fillStyle = "rgba(150,158,172,0.9)";
    ctx.font = `600 10px ${FONT}`;
    ctx.fillText("time", 12, 10);
    if (fpx >= 1.2) ctx.fillText("frame", 12, 24);
    if (this.markers.length) ctx.fillText("markers", 12, top - 10);
    if (this.ghostMarkers.length) { ctx.fillStyle = GHOST; ctx.fillText("ghost marks", 12, bottom + 11); }
    this.overDirty = true;
  }

  _drawOver() {
    this.overDirty = false;
    const ctx = this.octx, W = this.width, H = this.height, L = this.labelW;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    ctx.save();
    ctx.beginPath(); ctx.rect(L, 0, W - L, H); ctx.clip();
    if (this.hover && this.hover.interval) {
      const { y, h, interval } = this.hover;
      const xa = this.X(interval[0]), xb = this.X(interval[1] === null ? this.range[1] : interval[1]);
      ctx.strokeStyle = "#ffffff";
      ctx.lineWidth = 1.5;
      ctx.strokeRect(xa - 1, y - 1, Math.max(2, xb - xa) + 2, h + 2);
    }
    if (this.hover && this.hover.x !== undefined) {
      ctx.fillStyle = "rgba(255,255,255,0.25)";
      ctx.fillRect(Math.round(this.hover.x), this.rulerH, 1, H - this.rulerH);
    }
    const x = Math.round(this.X(this.playhead)) + 0.5;
    if (x >= L - 1 && x <= W + 1) {
      ctx.strokeStyle = "#ff4d6d";
      ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
      ctx.fillStyle = "#ff4d6d";
      ctx.beginPath(); ctx.moveTo(x - 6, 0); ctx.lineTo(x + 6, 0); ctx.lineTo(x, 8); ctx.closePath(); ctx.fill();
    }
    ctx.restore();
  }

  // ------------------------------------------------------------------ interaction
  _hit(px, py) {
    if (px < this.labelW) return null;
    const t = this.T(px);
    const top = this.rulerH;
    const tol = 3 / this.pxPerMs;
    const i = Math.floor((py - top) / this.rowH);
    if (py >= top && i >= 0 && i < this.rows.length) {
      const row = this.rows[i];
      const y = top + i * this.rowH;
      const pad = 3, mainH = this.ghost ? Math.round((this.rowH - 2 * pad) * 0.6) : this.rowH - 2 * pad;
      const inGhost = this.ghost && py > y + pad + mainH;
      const list = inGhost ? row.ghost : row.main;
      const interval = list ? intervalAt(list, t, tol) : null;
      return {
        t, x: px, row, ghost: inGhost, interval,
        y: inGhost ? y + pad + mainH + 1 : y + pad, h: inGhost ? this.rowH - 2 * pad - mainH - 1 : mainH,
      };
    }
    const rowsBottom = top + this.rows.length * this.rowH;
    const j = Math.floor((py - rowsBottom) / this.analogH);
    if (py >= rowsBottom && j >= 0 && j < this.analog.length) {
      const a = this.analog[j];
      const k = upperBound(a.t, t) - 1;
      const v = k >= 0 ? a.v[k] : 0;
      let gvv = null;
      if (a.gt) { const g = upperBound(a.gt, t - (a.ghostOffset || 0)) - 1; gvv = g >= 0 ? a.gv[g] : 0; }
      return { t, x: px, analog: a, value: v, ghostValue: gvv };
    }
    return { t, x: px };
  }

  _tooltip(h, clientX, clientY) {
    const tip = this.tip;
    if (!h || (!h.interval && !h.analog)) { tip.style.display = "none"; return; }
    const org = this.originMs, fm = this.frameMs;
    const tf = t => `${formatTime(t - org)} <span class="dim">f ${frameOf(t - org, fm)}</span>`;
    let html;
    if (h.interval) {
      const [a, b] = h.interval;
      const end = b === null ? null : b;
      const holdMs = (end === null ? this.range[1] : end) - a;
      html = `<b>${escapeHtml(h.row.label)}</b>${h.ghost ? ' <span class="ghost-tag">ghost</span>' : ""}` +
        `<div>press ${tf(a)}</div>` +
        `<div>release ${end === null ? "<i>held to end</i>" : tf(end)}</div>` +
        `<div>held <b>${(holdMs / fm).toFixed(2)}</b> frames <span class="dim">(${holdMs.toFixed(1)} ms)</span></div>`;
    } else {
      const pct = v => `${v} <span class="dim">(${(v / 327.67).toFixed(0)}%)</span>`;
      html = `<b>${escapeHtml(h.analog.label)}</b><div>${tf(h.t)}</div><div>value ${pct(h.value)}</div>` +
        (h.ghostValue !== null ? `<div>ghost ${pct(h.ghostValue)}</div>` : "");
    }
    tip.innerHTML = html;
    tip.style.display = "block";
    const r = this.el.getBoundingClientRect();
    let x = clientX - r.left + 14, y = clientY - r.top + 14;
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    if (x + tw > r.width) x = clientX - r.left - tw - 14;
    if (y + th > r.height) y = Math.max(0, clientY - r.top - th - 14);
    tip.style.transform = `translate(${x}px, ${y}px)`;
  }

  _bind() {
    const c = this.over;
    const pos = e => { const r = c.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
    c.addEventListener("wheel", e => {
      e.preventDefault();
      this.tip.style.display = "none";
      const [x] = pos(e);
      if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
        const d = (e.shiftKey ? e.deltaY : e.deltaX) / this.pxPerMs;
        this.setView(this.view.t0 + d, this.view.t1 + d);
      } else {
        this.zoom(Math.exp(e.deltaY * (e.deltaMode === 1 ? 0.05 : 0.0018)), Math.max(this.labelW, x));
      }
    }, { passive: false });
    let drag = null;
    c.addEventListener("pointerdown", e => {
      if (e.button !== 0) return;
      const [x, y] = pos(e);
      if (x < this.labelW) return;
      c.setPointerCapture(e.pointerId);
      drag = { x, y, t0: this.view.t0, t1: this.view.t1, moved: false, scrub: y < this.rulerH };
      if (drag.scrub) this.onSeek(this.T(x));
    });
    c.addEventListener("pointermove", e => {
      const [x, y] = pos(e);
      if (drag) {
        if (Math.abs(x - drag.x) > 3) drag.moved = true;
        if (drag.scrub) { this.onSeek(this.T(Math.max(this.labelW, x))); return; }
        if (drag.moved) {
          const d = (x - drag.x) / this.pxPerMs;
          this.setView(drag.t0 - d, drag.t1 - d);
          c.style.cursor = "grabbing";
        }
        return;
      }
      this.hover = this._hit(x, y);
      this._tooltip(this.hover, e.clientX, e.clientY);
      c.style.cursor = y < this.rulerH ? "col-resize" : (this.hover && this.hover.interval ? "pointer" : "default");
      this.overDirty = true;
    });
    const end = e => {
      if (!drag) return;
      const [x] = pos(e);
      if (!drag.moved && !drag.scrub) this.onSeek(this.T(x));
      drag = null;
      c.style.cursor = "default";
    };
    c.addEventListener("pointerup", end);
    c.addEventListener("pointercancel", () => { drag = null; });
    c.addEventListener("pointerleave", () => { this.hover = null; this.tip.style.display = "none"; this.overDirty = true; });
  }
}

function drawIntervals(ctx, list, t0, t1, roll, y, h, outline) {
  if (!list || !list.length || h <= 0) return;
  // intervals are sorted and non-overlapping, so ends are sorted too
  let lo = 0, hi = list.length;
  while (lo < hi) {
    const m = (lo + hi) >> 1;
    const b = list[m][1] === null ? Infinity : list[m][1];
    if (b < t0) lo = m + 1; else hi = m;
  }
  const end = roll.range[1];
  let lastX = -1;
  for (let i = lo; i < list.length; i++) {
    const [a, b] = list[i];
    if (a > t1) break;
    const xa = roll.X(a), xb = roll.X(b === null ? Math.max(end, a) : b);
    const w = Math.max(2, xb - xa);
    if (xa + w < lastX + 0.5) continue; // sub-pixel crowd: already covered
    ctx.fillRect(xa, y, w, h);
    if (outline) { ctx.strokeStyle = "rgba(124,200,255,0.95)"; ctx.lineWidth = 1; ctx.strokeRect(xa + 0.5, y + 0.5, w - 1, h - 1); }
    lastX = xa + w;
  }
}

function drawAnalog(ctx, ts, vs, t0, t1, roll, mid, amp, color, offset) {
  if (!ts || !ts.length) return;
  const Y = v => mid - (v / 32768) * amp;   // up on screen = positive value
  let i = Math.max(0, upperBound(ts, t0 - offset) - 1);
  const iEnd = Math.min(ts.length, lowerBound(ts, t1 - offset) + 1);
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  let prevV = i < ts.length && ts[i] <= t0 - offset ? vs[i] : 0;
  let x = roll.X(Math.max(t0, (i < ts.length ? ts[i] : t0) + offset));
  ctx.moveTo(roll.labelW, Y(prevV));
  let colX = -1, colMin = 0, colMax = 0;
  for (; i < iEnd; i++) {
    x = roll.X(ts[i] + offset);
    const v = vs[i];
    const cx = Math.floor(x);
    if (cx === colX) { colMin = Math.min(colMin, v); colMax = Math.max(colMax, v); prevV = v; continue; }
    if (colX >= 0 && colMin !== colMax) { ctx.lineTo(colX, Y(colMin)); ctx.lineTo(colX, Y(colMax)); ctx.lineTo(colX, Y(prevV)); }
    ctx.lineTo(x, Y(prevV));
    ctx.lineTo(x, Y(v));
    colX = cx; colMin = v; colMax = v; prevV = v;
  }
  ctx.lineTo(Math.min(roll.X(roll.range[1]), roll.width), Y(prevV));
  ctx.stroke();
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  if (ctx.roundRect) { ctx.roundRect(x, y, w, h, r); return; }
  ctx.rect(x, y, w, h);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
