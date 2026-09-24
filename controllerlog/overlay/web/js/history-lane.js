// Scrolling input-history lane: one row per input, newest input at the right edge.
// Times are in the page clock (performance.now() milliseconds).

const LABEL_FONT = '600 {px}px system-ui, "Segoe UI", Roboto, Arial, sans-serif';

export class HistoryLane {
  /**
   * @param {HTMLCanvasElement} canvas
   * @param {object} opts rows: [{input, label, color}], seconds (4), fps (60), scale (1),
   *                      theme (layout theme), width (css px)
   */
  constructor(canvas, opts) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.seconds = opts.seconds || 4;
    this.fps = opts.fps || 60;
    this.scale = opts.scale || 1;
    this.theme = opts.theme || {};
    this.rowH = Math.max(7, Math.round(12 * this.scale));
    this.labelW = Math.round(40 * this.scale);
    this.pad = Math.round(6 * this.scale);
    this.markers = [];
    this.setRows(opts.rows || [], opts.width || 400);
  }

  setRows(rows, width) {
    this.rows = rows.map(r => ({ ...r, spans: [], held: null }));
    this.index = new Map(this.rows.map((r, i) => [r.input, i]));
    this.cssW = Math.round(width);
    this.cssH = this.pad * 2 + this.rows.length * this.rowH + Math.round(10 * this.scale);
    const dpr = window.devicePixelRatio || 1;
    this.dpr = dpr;
    this.canvas.width = Math.round(this.cssW * dpr);
    this.canvas.height = Math.round(this.cssH * dpr);
    this.canvas.style.width = `${this.cssW}px`;
    this.canvas.style.height = `${this.cssH}px`;
  }

  /** Input went down at time t (ms, page clock). */
  press(input, t) {
    const i = this.index.get(input);
    if (i === undefined) return;
    const row = this.rows[i];
    if (row.held !== null) return;
    row.held = Math.min(t, performance.now());  // never ahead of the right edge
  }

  /** Input released at time t. */
  release(input, t) {
    const i = this.index.get(input);
    if (i === undefined) return;
    const row = this.rows[i];
    if (row.held === null) return;
    row.spans.push([row.held, Math.max(Math.min(t, performance.now()), row.held)]);
    row.held = null;
  }

  releaseAll(t) { for (const r of this.rows) if (r.held !== null) this.release(r.input, t); }

  mark(t, label) { this.markers.push([t, label]); }

  clear() {
    for (const r of this.rows) { r.spans.length = 0; r.held = null; }
    this.markers.length = 0;
  }

  /** Render the window ending at `now`. */
  draw(now) {
    const { ctx, dpr, scale } = this;
    const W = this.cssW, H = this.cssH, pad = this.pad;
    const x0 = pad + this.labelW, x1 = W - pad;
    const plotW = x1 - x0;
    const win = this.seconds * 1000;
    const tStart = now - win;
    const pxPerMs = plotW / win;
    const X = t => x1 - (now - t) * pxPerMs;
    const top = pad, rowsH = this.rows.length * this.rowH, bottom = top + rowsH;

    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    // panel
    ctx.fillStyle = "rgba(14,16,20,0.72)";
    roundRect(ctx, 0, 0, W, H, 10 * scale);
    ctx.fill();
    ctx.fillStyle = "rgba(255,255,255,0.035)";
    for (let i = 0; i < this.rows.length; i += 2) ctx.fillRect(x0, top + i * this.rowH, plotW, this.rowH);

    // frame grid (only when frames are at least 6 px apart)
    const frameMs = 1000 / this.fps;
    if (frameMs * pxPerMs >= 6) {
      ctx.strokeStyle = "rgba(255,255,255,0.05)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (let k = Math.ceil(tStart / frameMs); k * frameMs <= now; k++) {
        const x = Math.round(X(k * frameMs)) + 0.5;
        ctx.moveTo(x, top); ctx.lineTo(x, bottom);
      }
      ctx.stroke();
    }
    // 1 s ticks
    ctx.strokeStyle = "rgba(255,255,255,0.22)";
    ctx.beginPath();
    for (let s = Math.ceil(tStart / 1000); s * 1000 <= now; s++) {
      const x = Math.round(X(s * 1000)) + 0.5;
      ctx.moveTo(x, top); ctx.lineTo(x, bottom + 5 * scale);
    }
    ctx.stroke();

    // bars
    const barPad = Math.max(1, Math.round(this.rowH * 0.18));
    const fallback = this.theme.active || "#ffd23f";
    for (let i = 0; i < this.rows.length; i++) {
      const row = this.rows[i];
      const y = top + i * this.rowH + barPad, h = this.rowH - 2 * barPad;
      // prune spans that scrolled out
      let drop = 0;
      while (drop < row.spans.length && row.spans[drop][1] < tStart) drop++;
      if (drop) row.spans.splice(0, drop);
      ctx.fillStyle = row.color || fallback;
      for (const [a, b] of row.spans) bar(ctx, X(Math.max(a, tStart)), X(b), y, h, x0);
      if (row.held !== null) bar(ctx, X(Math.max(row.held, tStart)), x1, y, h, x0);
    }

    // markers
    while (this.markers.length && this.markers[0][0] < tStart) this.markers.shift();
    if (this.markers.length) {
      ctx.strokeStyle = "rgba(255,159,67,0.85)";
      ctx.fillStyle = "rgba(255,159,67,0.95)";
      ctx.font = LABEL_FONT.replace("{px}", Math.round(9 * scale));
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      for (const [t, label] of this.markers) {
        const x = Math.round(X(t)) + 0.5;
        ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke();
        ctx.fillText(String(label).slice(0, 24), x + 3, bottom + 1);
      }
    }

    // labels
    ctx.font = LABEL_FONT.replace("{px}", Math.round(Math.min(10 * scale, this.rowH - 1)));
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (let i = 0; i < this.rows.length; i++) {
      const row = this.rows[i];
      ctx.fillStyle = row.held !== null ? (row.color || fallback) : "rgba(232,235,240,0.78)";
      ctx.fillText(row.label, x0 - 5 * scale, top + (i + 0.5) * this.rowH + 0.5);
    }
    // "now" edge
    ctx.fillStyle = "rgba(255,255,255,0.35)";
    ctx.fillRect(x1 - 1, top, 1, rowsH);
  }
}

function bar(ctx, xa, xb, y, h, xmin) {
  xa = Math.max(xa, xmin);
  const w = Math.max(1.5, xb - xa);
  if (xb < xmin) return;
  ctx.fillRect(xa, y, w, h);
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  if (ctx.roundRect) { ctx.roundRect(x, y, w, h, r); return; }
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}
