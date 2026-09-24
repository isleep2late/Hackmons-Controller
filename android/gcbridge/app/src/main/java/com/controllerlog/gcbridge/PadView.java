package com.controllerlog.gcbridge;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.RectF;
import android.graphics.Typeface;
import android.util.AttributeSet;
import android.view.View;

import java.util.List;
import java.util.Map;

/**
 * Draws a {@link Layout} for a {@link Pad.State}: the same shapes and rules as the PC overlay
 * (web/js/layout-svg.js) and video renderer (render/draw.py). Scaled to fit the view, keeping
 * the layout's aspect ratio; the background outside the body is transparent.
 */
public final class PadView extends View {

    private Layout layout;
    private final Pad.State state = new Pad.State();
    private final Pad.State snapshot = new Pad.State();
    private final Paint fillPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint strokePaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint textPaint = new Paint(Paint.ANTI_ALIAS_FLAG | Paint.SUBPIXEL_TEXT_FLAG);
    private final Path path = new Path();
    private final RectF rect = new RectF();

    public PadView(Context context) {
        this(context, null);
    }

    public PadView(Context context, AttributeSet attrs) {
        super(context, attrs);
        fillPaint.setStyle(Paint.Style.FILL);
        strokePaint.setStyle(Paint.Style.STROKE);
        strokePaint.setStrokeJoin(Paint.Join.ROUND);
        textPaint.setTypeface(Typeface.DEFAULT_BOLD);
        textPaint.setTextAlign(Paint.Align.CENTER);
    }

    public void setLayout(Layout l) {
        layout = l;
        requestLayout();
        invalidate();
    }

    public Layout getLayout() {
        return layout;
    }

    /** Thread-safe: copies the state and schedules a redraw. */
    public void setState(Pad.State s) {
        synchronized (state) {
            state.set(s);
        }
        postInvalidateOnAnimation();
    }

    public void clearState() {
        synchronized (state) {
            state.clear();
        }
        postInvalidateOnAnimation();
    }

    @Override
    protected void onMeasure(int widthSpec, int heightSpec) {
        int wMode = MeasureSpec.getMode(widthSpec);
        int hMode = MeasureSpec.getMode(heightSpec);
        int w = MeasureSpec.getSize(widthSpec);
        int h = MeasureSpec.getSize(heightSpec);
        double aspect = layout == null || layout.width <= 0 ? 1.5 : layout.width / layout.height;
        if (wMode == MeasureSpec.EXACTLY && hMode == MeasureSpec.EXACTLY) {
            setMeasuredDimension(w, h);
        } else if (wMode != MeasureSpec.UNSPECIFIED && hMode != MeasureSpec.EXACTLY) {
            int hh = (int) Math.round(w / aspect);
            if (hMode == MeasureSpec.AT_MOST) {
                hh = Math.min(hh, h);
            }
            setMeasuredDimension(w, Math.max(1, hh));
        } else if (hMode != MeasureSpec.UNSPECIFIED) {
            setMeasuredDimension(Math.max(1, (int) Math.round(h * aspect)), h);
        } else {
            setMeasuredDimension(360, (int) Math.round(360 / aspect));
        }
    }

    @Override
    protected void onDraw(Canvas canvas) {
        Layout l = layout;
        if (l == null || l.width <= 0 || l.height <= 0) {
            return;
        }
        synchronized (state) {
            snapshot.set(state);
        }
        float w = getWidth() - getPaddingLeft() - getPaddingRight();
        float h = getHeight() - getPaddingTop() - getPaddingBottom();
        float scale = (float) Math.min(w / l.width, h / l.height);
        canvas.save();
        canvas.translate(getPaddingLeft() + (float) ((w - l.width * scale) / 2),
                getPaddingTop() + (float) ((h - l.height * scale) / 2));
        canvas.scale(scale, scale);
        float sw = (float) l.themeNumber("stroke_width");
        int labelColor = Layout.color(l.themeColor("label"));
        int labelActive = Layout.color(l.themeColor("label_active"));
        float fontSize = (float) l.themeNumber("font_size");

        for (Map<String, Object> obj : l.body) {
            boolean text = "text".equals(Json.str(obj, "shape", ""));
            int fill = Layout.color(Json.str(obj, "fill", text ? l.themeColor("label") : l.themeColor("body")));
            int stroke = text && !Json.has(obj, "stroke") ? 0
                    : Layout.color(Json.str(obj, "stroke", l.themeColor("body_stroke")));
            drawShape(canvas, obj, fill, stroke, (float) Json.num(obj, "stroke_width", sw), fontSize);
        }
        for (Map<String, Object> el : l.elements) {
            String type = Json.str(el, "type", "");
            int idleFill = Layout.color(Json.str(el, "fill", l.themeColor("idle")));
            int idleStroke = Layout.color(Json.str(el, "stroke",
                    Json.str(el, "idle_stroke", l.themeColor("idle_stroke"))));
            int active = Layout.color(Json.str(el, "active", l.themeColor("active")));
            float esw = (float) Json.num(el, "stroke_width", sw);
            String label = Json.str(el, "label", null);
            float labelSize = (float) Json.num(el, "label_size", fontSize);
            if ("button".equals(type)) {
                boolean pressed = snapshot.pressed(Json.str(el, "input", ""));
                drawShape(canvas, el, pressed ? active : idleFill, idleStroke, esw, fontSize);
                if (label != null) {
                    center(el);
                    drawLabel(canvas, label, rect.centerX(), rect.centerY(), labelSize,
                            pressed ? labelActive : labelColor);
                }
            } else if ("trigger".equals(type)) {
                String input = Json.str(el, "input", "");
                int axis = "left_trigger".equals(input) ? Pad.LEFT_TRIGGER : Pad.RIGHT_TRIGGER;
                float frac = Math.max(0f, Math.min(1f, snapshot.axes[axis] / (float) Pad.AXIS_MAX));
                boolean pressed = snapshot.pressed(input);
                drawShape(canvas, el, idleFill, 0, esw, fontSize);
                if (frac > 0f) {
                    bounds(el);
                    RectF bar = new RectF(rect);
                    switch (Json.str(el, "fill_dir", "up")) {
                        case "down":
                            bar.bottom = bar.top + bar.height() * frac;
                            break;
                        case "left":
                            bar.left = bar.right - bar.width() * frac;
                            break;
                        case "right":
                            bar.right = bar.left + bar.width() * frac;
                            break;
                        default:
                            bar.top = bar.bottom - bar.height() * frac;
                    }
                    canvas.save();
                    shapePath(el);
                    canvas.clipPath(path);
                    fillPaint.setColor(active);
                    canvas.drawRect(bar, fillPaint);
                    canvas.restore();
                }
                drawShape(canvas, el, 0, pressed ? active : idleStroke, esw, fontSize);
                if (label != null) {
                    center(el);
                    drawLabel(canvas, label, rect.centerX(), rect.centerY(), labelSize,
                            pressed ? labelActive : labelColor);
                }
            } else if ("stick".equals(type)) {
                float cx = (float) Json.num(el, "cx", 0), cy = (float) Json.num(el, "cy", 0);
                float r = (float) Json.num(el, "r", 0);
                float knobR = (float) Json.num(el, "knob_r", Math.round(r * 0.55 * 10) / 10.0);
                float travel = (float) Json.num(el, "travel", Math.max(0, r - knobR));
                int ring = Layout.color(Json.str(el, "ring", l.themeColor("body_stroke")));
                fillPaint.setColor(ring);
                if (ring != 0) {
                    canvas.drawCircle(cx, cy, r, fillPaint);
                }
                if (idleStroke != 0) {
                    strokePaint.setColor(idleStroke);
                    strokePaint.setStrokeWidth(esw);
                    canvas.drawCircle(cx, cy, r, strokePaint);
                }
                int xa = Pad.axisIndex(Json.str(el, "x_axis", "left_x"));
                int ya = Pad.axisIndex(Json.str(el, "y_axis", "left_y"));
                float kx = cx + travel * snapshot.axes[xa] / 32768f;
                float ky = cy + travel * snapshot.axes[ya] / 32768f;
                String button = Json.str(el, "button", null);
                boolean pressed = button != null && snapshot.pressed(button);
                fillPaint.setColor(pressed ? active : idleFill);
                canvas.drawCircle(kx, ky, knobR, fillPaint);
                if (idleStroke != 0) {
                    strokePaint.setColor(idleStroke);
                    strokePaint.setStrokeWidth(esw);
                    canvas.drawCircle(kx, ky, knobR, strokePaint);
                }
                if (label != null) {
                    float size = Json.has(el, "label_size") ? labelSize : Math.min(labelSize, knobR);
                    drawLabel(canvas, label, kx, ky, size, pressed ? labelActive : labelColor);
                }
            }
        }
        canvas.restore();
    }

    private void drawLabel(Canvas canvas, String text, float x, float y, float size, int color) {
        if (color == 0 || size <= 0) {
            return;
        }
        textPaint.setColor(color);
        textPaint.setTextSize(size);
        Paint.FontMetrics fm = textPaint.getFontMetrics();
        canvas.drawText(text, x, y - (fm.ascent + fm.descent) / 2, textPaint);
    }

    /** Fills {@link #rect} with the shape's bounding box. */
    private void bounds(Map<String, Object> obj) {
        switch (Json.str(obj, "shape", "")) {
            case "rect": {
                float x = (float) Json.num(obj, "x", 0), y = (float) Json.num(obj, "y", 0);
                rect.set(x, y, x + (float) Json.num(obj, "w", 0), y + (float) Json.num(obj, "h", 0));
                break;
            }
            case "circle": {
                float cx = (float) Json.num(obj, "cx", 0), cy = (float) Json.num(obj, "cy", 0);
                float r = (float) Json.num(obj, "r", 0);
                rect.set(cx - r, cy - r, cx + r, cy + r);
                break;
            }
            case "ellipse": {
                float cx = (float) Json.num(obj, "cx", 0), cy = (float) Json.num(obj, "cy", 0);
                float rx = (float) Json.num(obj, "rx", 0), ry = (float) Json.num(obj, "ry", 0);
                rect.set(cx - rx, cy - ry, cx + rx, cy + ry);
                break;
            }
            case "polygon": {
                shapePath(obj);
                path.computeBounds(rect, true);
                break;
            }
            default: {
                float x = (float) Json.num(obj, "x", 0), y = (float) Json.num(obj, "y", 0);
                rect.set(x, y, x, y);
            }
        }
    }

    /** Fills {@link #rect} with a 0-size rect at the shape's centre (polygons: centroid). */
    private void center(Map<String, Object> obj) {
        if ("polygon".equals(Json.str(obj, "shape", ""))) {
            List<Object> pts = Json.asArray(obj.get("points"));
            double a = 0, cx = 0, cy = 0;
            int n = pts == null ? 0 : pts.size();
            for (int i = 0; i < n; i++) {
                List<Object> p = Json.asArray(pts.get(i));
                List<Object> q = Json.asArray(pts.get((i + 1) % n));
                double xa = ((Number) p.get(0)).doubleValue(), ya = ((Number) p.get(1)).doubleValue();
                double xb = ((Number) q.get(0)).doubleValue(), yb = ((Number) q.get(1)).doubleValue();
                double c = xa * yb - xb * ya;
                a += c;
                cx += (xa + xb) * c;
                cy += (ya + yb) * c;
            }
            if (Math.abs(a) > 1e-9) {
                float x = (float) (cx / (3 * a)), y = (float) (cy / (3 * a));
                rect.set(x, y, x, y);
                return;
            }
        }
        bounds(obj);
        float x = rect.centerX(), y = rect.centerY();
        rect.set(x, y, x, y);
    }

    private void shapePath(Map<String, Object> obj) {
        path.reset();
        switch (Json.str(obj, "shape", "")) {
            case "rect": {
                bounds(obj);
                float r = (float) Json.num(obj, "r", 0);
                path.addRoundRect(rect, r, r, Path.Direction.CW);
                break;
            }
            case "circle":
            case "ellipse":
                bounds(obj);
                path.addOval(rect, Path.Direction.CW);
                break;
            case "polygon": {
                List<Object> pts = Json.asArray(obj.get("points"));
                if (pts != null) {
                    for (int i = 0; i < pts.size(); i++) {
                        List<Object> p = Json.asArray(pts.get(i));
                        if (p == null || p.size() < 2) {
                            continue;
                        }
                        float x = ((Number) p.get(0)).floatValue(), y = ((Number) p.get(1)).floatValue();
                        if (i == 0) {
                            path.moveTo(x, y);
                        } else {
                            path.lineTo(x, y);
                        }
                    }
                    path.close();
                }
                break;
            }
            default:
                break;
        }
    }

    private void drawShape(Canvas canvas, Map<String, Object> obj, int fill, int stroke,
                           float strokeWidth, float defaultFont) {
        String shape = Json.str(obj, "shape", "");
        if ("text".equals(shape)) {
            float x = (float) Json.num(obj, "x", 0), y = (float) Json.num(obj, "y", 0);
            String anchor = Json.str(obj, "anchor", "middle");
            textPaint.setTextAlign("start".equals(anchor) ? Paint.Align.LEFT
                    : "end".equals(anchor) ? Paint.Align.RIGHT : Paint.Align.CENTER);
            drawLabel(canvas, Json.str(obj, "text", ""), x, y, (float) Json.num(obj, "size", defaultFont), fill);
            textPaint.setTextAlign(Paint.Align.CENTER);
            return;
        }
        shapePath(obj);
        if (fill != 0) {
            fillPaint.setColor(fill);
            canvas.drawPath(path, fillPaint);
        }
        if (stroke != 0 && strokeWidth > 0) {
            strokePaint.setColor(stroke);
            strokePaint.setStrokeWidth(strokeWidth);
            canvas.drawPath(path, strokePaint);
        }
    }
}
