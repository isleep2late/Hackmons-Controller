"""Pillow painters for controller layouts (docs/LAYOUTS.md) and the input-history lane.

:class:`LayoutPainter` draws one layout for a :class:`~controllerlog.model.PadState`.
Everything is drawn at ``supersample`` x resolution and downscaled with LANCZOS
for smooth edges. The static body is cached, and consecutive calls only redraw
(and re-downscale) the screen regions of elements whose look changed, so
typical frames cost well under a millisecond.

:class:`HistoryPainter` draws the scrolling piano-roll lane: one row per input,
a bar while it is held, newest input at the right edge.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from PIL import Image, ImageColor, ImageDraw, ImageFont

from ..layouts import DEFAULT_THEME, DIGITAL_INPUTS, element_inputs, parse_map, remap_digital, validate_layout
from ..model import (AXIS_INDEX, AXIS_MAX, AXIS_MIN, BUTTON_INDEX, BUTTONS, FACE_LABELS,
                     FAMILY_GENERIC, TRIGGER_PRESS_THRESHOLD, PadState)
from ..timeline import NS_PER_S, Timeline, press_name

RGBA = tuple[int, int, int, int]
Font = ImageFont.FreeTypeFont | ImageFont.ImageFont

# Bold UI fonts first (labels on small buttons read better), then regular, then
# symbol fonts for glyphs like the PlayStation shapes.
DEFAULT_FONTS: tuple[str, ...] = (
    "segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf",
    "Arial Bold.ttf", "segoeui.ttf", "arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf",
    "Arial.ttf", "FreeSans.ttf")
SYMBOL_FONTS: tuple[str, ...] = (
    "seguisym.ttf", "DejaVuSans.ttf", "NotoSansSymbols2-Regular.ttf", "arialuni.ttf",
    "Symbola.ttf", "Apple Symbols.ttf")
MONO_FONTS: tuple[str, ...] = (
    "consolab.ttf", "consola.ttf", "DejaVuSansMono-Bold.ttf", "DejaVuSansMono.ttf",
    "LiberationMono-Bold.ttf", "courbd.ttf", "Menlo.ttc", "cour.ttf")

_ANCHORS = {"middle": "mm", "start": "lm", "end": "rm"}
_TRIGGER_AXIS = {"left_trigger": AXIS_INDEX["left_trigger"], "right_trigger": AXIS_INDEX["right_trigger"]}
_FILL_DIRS = ("up", "down", "left", "right")
_LANCZOS_SUPPORT = 3  # output pixels influenced on each side by one source pixel
_INF = 1 << 62


def _r(v: float) -> int:
    """Round half up (``round`` is banker's rounding, which breaks shift invariance)."""
    return math.floor(v + 0.5)


# Fallback row labels for the history lane.
SHORT_LABELS: dict[str, str] = {
    "dpad_up": "↑", "dpad_down": "↓", "dpad_left": "←", "dpad_right": "→",
    "left_stick": "LS", "right_stick": "RS", "left_shoulder": "L", "right_shoulder": "R",
    "left_trigger": "LT", "right_trigger": "RT", "back": "Sel", "start": "Start",
    "guide": "Home", "touchpad": "Pad", "misc1": "M1", "right_paddle1": "P1",
    "left_paddle1": "P3", "right_paddle2": "P2", "left_paddle2": "P4",
}


# --- helpers -------------------------------------------------------------------

def parse_color(value: Any) -> RGBA | None:
    """CSS colour (``#rgb``, ``#rrggbb``, ``#rrggbbaa``, names) -> RGBA; ``"none"`` -> None."""
    if value is None:
        return None
    if isinstance(value, (tuple, list)):
        c = tuple(int(v) for v in value)
        return c if len(c) == 4 else (c[0], c[1], c[2], 255)  # type: ignore[return-value]
    s = str(value).strip()
    if s.lower() in ("", "none", "transparent"):
        return None
    c = ImageColor.getrgb(s)
    return c if len(c) == 4 else (c[0], c[1], c[2], 255)  # type: ignore[return-value]


def normalize_layout(layout: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a layout dict and fill defaults exactly like :func:`layouts.load_layout`."""
    out = dict(layout)
    out.setdefault("name", "layout")
    validate_layout(out)
    theme = dict(DEFAULT_THEME)
    theme.update(layout.get("theme", {}))
    out["theme"] = theme
    if "history" not in out:
        out["history"] = [inp for el in out["elements"]
                          for inp in element_inputs(el) if inp in DIGITAL_INPUTS]
    return out


def digital_state(state: PadState, mapping: Mapping[str, str] | None = None) -> dict[str, bool]:
    """``{input: pressed}`` for every button and both triggers, after a display remap."""
    pressed = {name: bool(v) for name, v in zip(BUTTONS, state.buttons)}
    for name, ax in _TRIGGER_AXIS.items():
        pressed[name] = state.axes[ax] >= TRIGGER_PRESS_THRESHOLD
    return remap_digital(pressed, dict(mapping)) if mapping else pressed


def composite(dst: Image.Image, src: Image.Image, x: int, y: int) -> None:
    """Draw ``src`` over ``dst`` at (x, y), clipping at the edges.

    RGBA targets use true alpha compositing; opaque (RGB) targets use a masked
    paste, which is equivalent there and faster."""
    sx0, sy0 = max(0, -x), max(0, -y)
    sx1, sy1 = min(src.width, dst.width - x), min(src.height, dst.height - y)
    if sx1 <= sx0 or sy1 <= sy0:
        return
    if dst.mode == "RGBA" and src.mode == "RGBA":
        dst.alpha_composite(src, (x + sx0, y + sy0), (sx0, sy0, sx1, sy1))
        return
    box = (sx0, sy0, sx1, sy1)
    region = src if box == (0, 0, src.width, src.height) else src.crop(box)
    dst.paste(region, (x + sx0, y + sy0), region if region.mode == "RGBA" else None)


class FontBook:
    """Picks the first candidate TrueType font that has glyphs for a string, per size."""

    def __init__(self, font_path: str | None = None,
                 candidates: Sequence[str] = DEFAULT_FONTS) -> None:
        names = ([font_path] if font_path else []) + list(candidates)
        names += [f for f in SYMBOL_FONTS if f not in names]
        self._names = names
        self._fonts: dict[tuple[str, int], Font | None] = {}
        self._glyphs: dict[tuple[str, str], bool] = {}
        self._notdef: dict[str, tuple[Any, ...]] = {}
        self._picked: dict[tuple[str, int], Font] = {}

    def get(self, size: int, text: str = "") -> Font:
        size = max(1, int(size))
        key = (text, size)
        font = self._picked.get(key)
        if font is None:
            font = self._picked[key] = self._choose(size, text)
        return font

    def _load(self, name: str, size: int) -> Font | None:
        key = (name, size)
        if key not in self._fonts:
            try:
                self._fonts[key] = ImageFont.truetype(name, size)
            except OSError:
                self._fonts[key] = None
        return self._fonts[key]

    def _covers(self, name: str, text: str) -> bool:
        """Glyph coverage, checked at a fixed size (missing glyphs render as .notdef)."""
        ref = self._load(name, 32)
        if ref is None:
            return False
        for ch in set(text):
            if ch.isspace():
                continue
            key = (name, ch)
            if key not in self._glyphs:
                if name not in self._notdef:
                    self._notdef[name] = _glyph_signature(ref, "\uffff")
                self._glyphs[key] = _glyph_signature(ref, ch) != self._notdef[name]
            if not self._glyphs[key]:
                return False
        return True

    def _choose(self, size: int, text: str) -> Font:
        first: Font | None = None
        for name in self._names:
            font = self._load(name, size)
            if font is None:
                continue
            if first is None:
                first = font
                if not text:
                    return font
            if self._covers(name, text):
                return font
        return first if first is not None else ImageFont.load_default(size=size)


def _glyph_signature(font: Font, ch: str) -> tuple[Any, ...]:
    l, t, r, b = font.getbbox(ch)
    img = Image.new("L", (max(1, int(r - l)), max(1, int(b - t))), 0)
    ImageDraw.Draw(img).text((-l, -t), ch, font=font, fill=255)
    return (l, t, r, b, img.tobytes())


@dataclass
class _Shape:
    kind: str                     # rect | ellipse | polygon | text
    x0: float
    y0: float
    x1: float
    y1: float
    r: float = 0.0
    points: tuple[tuple[float, float], ...] = ()
    text: str = ""
    size: float = 14.0
    anchor: str = "mm"

    def center(self) -> tuple[float, float]:
        if self.kind != "polygon" or len(self.points) < 3:
            return (self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2
        a = cx = cy = 0.0
        pts = self.points
        for (xa, ya), (xb, yb) in zip(pts, pts[1:] + pts[:1]):
            c = xa * yb - xb * ya
            a += c
            cx += (xa + xb) * c
            cy += (ya + yb) * c
        if abs(a) < 1e-9:
            return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
        return cx / (3 * a), cy / (3 * a)


def parse_shape(obj: Mapping[str, Any], default_size: float = 14.0) -> _Shape:
    s = obj["shape"]
    if s == "rect":
        x, y, w, h = (float(obj[k]) for k in ("x", "y", "w", "h"))
        return _Shape("rect", x, y, x + w, y + h, r=float(obj.get("r") or 0))
    if s == "circle":
        cx, cy, r = float(obj["cx"]), float(obj["cy"]), float(obj["r"])
        return _Shape("ellipse", cx - r, cy - r, cx + r, cy + r)
    if s == "ellipse":
        cx, cy, rx, ry = (float(obj[k]) for k in ("cx", "cy", "rx", "ry"))
        return _Shape("ellipse", cx - rx, cy - ry, cx + rx, cy + ry)
    if s == "polygon":
        pts = tuple((float(p[0]), float(p[1])) for p in obj["points"])
        if not pts:  # valid per validate_layout; draws nothing (like an empty SVG polygon)
            return _Shape("polygon", 0.0, 0.0, 0.0, 0.0)
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return _Shape("polygon", min(xs), min(ys), max(xs), max(ys), points=pts)
    if s == "text":
        x, y = float(obj["x"]), float(obj["y"])
        return _Shape("text", x, y, x, y, text=str(obj["text"]),
                      size=float(obj.get("size", default_size)),
                      anchor=_ANCHORS.get(obj.get("anchor", "middle"), "mm"))
    raise ValueError(f"unknown shape {s!r}")


@dataclass(eq=False)
class _Element:
    kind: str
    shape: _Shape
    fill: RGBA | None
    stroke: RGBA | None
    sw: float
    active: RGBA | None
    label: str | None = None
    label_size: float = 14.0
    input: str = ""
    fill_dir: str = "up"
    extent: int = 0               # trigger: fill range in hi-res px
    mask: Image.Image | None = None
    mask_origin: tuple[int, int] = (0, 0)
    x_axis: int = 0               # stick
    y_axis: int = 1
    button: str | None = None
    travel: float = 0.0
    knob_r: float = 0.0
    knob_fill: RGBA | None = None
    bbox_hi: tuple[int, int, int, int] = (0, 0, 0, 0)
    out_rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    cache: dict[Any, Any] = field(default_factory=dict)


def _merge_rects(rects: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    out = list(rects)
    merged = True
    while merged:
        merged = False
        for i in range(len(out)):
            for j in range(i + 1, len(out)):
                a, b = out[i], out[j]
                if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                    out[i] = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
                    del out[j]
                    merged = True
                    break
            if merged:
                break
    return out


# --- layout painter -------------------------------------------------------------

class LayoutPainter:
    """Draws a controller layout (docs/LAYOUTS.md) for a :class:`PadState`.

    ``scale`` multiplies the layout's canvas size; ``mapping`` is a display remap
    (``{"south": "east"}`` or ``"south:east,east:south"``, source input -> the
    layout input it lights, see :func:`layouts.parse_map`). Output images are
    RGBA with a transparent background outside the drawn body.

    Extensions beyond the spec (all optional): ``theme.stroke_width`` (default 2),
    element ``label_size``, element ``idle_stroke`` (alias of ``stroke``), stick
    ``fill`` = knob colour and ``ring`` = ring colour (default ``theme.body_stroke``).
    A trigger's ``fill_dir`` defaults to ``"up"``; a stick's ``knob_r`` to 55 % of ``r``.
    """

    def __init__(self, layout: Mapping[str, Any], scale: float = 1.0,
                 mapping: Mapping[str, str] | str | None = None,
                 font_path: str | None = None, supersample: int = 2) -> None:
        self.layout = normalize_layout(layout)
        self.theme = self.layout["theme"]
        self.scale = float(scale)
        if self.scale <= 0:
            raise ValueError("scale must be positive")
        self.ss = max(1, int(supersample))
        self.k = self.scale * self.ss
        w, h = self.layout["size"]
        self.size = (max(1, round(w * self.scale)), max(1, round(h * self.scale)))
        self.hi_size = (self.size[0] * self.ss, self.size[1] * self.ss)
        self.mapping: dict[str, str] = parse_map(mapping) if isinstance(mapping, str) else dict(mapping or {})
        self._sources = {d: [s for s in DIGITAL_INPUTS if self.mapping.get(s, s) == d]
                         for d in DIGITAL_INPUTS}
        self.fonts = FontBook(font_path)
        self._sprites: dict[tuple[Any, ...], tuple[Image.Image, int, int]] = {}
        th = self.theme
        self._sw = float(th.get("stroke_width", 2))
        self._label = parse_color(th["label"])
        self._label_active = parse_color(th["label_active"])
        self._body = self._render_body()
        self.elements = [self._build_element(el) for el in self.layout["elements"]]
        self._cur_key: tuple[Any, ...] | None = None
        self._cur_img: Image.Image | None = None

    # -- public ---------------------------------------------------------------
    def pressed(self, state: PadState) -> dict[str, bool]:
        """Digital state of every layout input after the display remap."""
        return digital_state(state, self.mapping)

    def visual_key(self, state: PadState) -> tuple[Any, ...]:
        """Hashable summary of what :meth:`paint` would draw; equal keys give equal images."""
        pressed = self.pressed(state)
        out: list[Any] = []
        for e in self.elements:
            if e.kind == "button":
                out.append(pressed.get(e.input, False))
            elif e.kind == "trigger":
                frac = min(1.0, max(0.0, self._analog(state, e.input) / AXIS_MAX))
                out.append((round(frac * e.extent), pressed.get(e.input, False)))
            else:
                # Clamp: out-of-range values would push the knob outside its dirty rect.
                t = e.travel * self.k / 32768
                x = min(AXIS_MAX, max(AXIS_MIN, state.axes[e.x_axis]))
                y = min(AXIS_MAX, max(AXIS_MIN, state.axes[e.y_axis]))
                out.append((round(t * x), round(t * y), bool(e.button and pressed.get(e.button, False))))
        return tuple(out)

    def paint(self, state: PadState, image: Image.Image | None = None,
              dest: tuple[int, int] = (0, 0)) -> Image.Image:
        """Render ``state``. Returns a new RGBA image of :attr:`size`, or, when
        ``image`` (RGBA or opaque RGB) is given, draws over it at ``dest`` and returns it."""
        img = self._update(self.visual_key(state))
        if image is None:
            return img.copy()
        composite(image, img, int(dest[0]), int(dest[1]))
        return image

    def element_center(self, name: str) -> tuple[float, float] | None:
        """Output-pixel centre of the first element showing input/stick-button ``name``."""
        for e in self.elements:
            if e.input == name or (e.kind == "stick" and e.button == name):
                cx, cy = e.shape.center()
                return cx * self.scale, cy * self.scale
        return None

    # -- building ---------------------------------------------------------------
    def _analog(self, state: PadState, name: str) -> int:
        if not self.mapping:
            return state.axes[_TRIGGER_AXIS[name]]
        v = 0
        for src in self._sources.get(name, ()):
            if src in _TRIGGER_AXIS:
                v = max(v, state.axes[_TRIGGER_AXIS[src]])
            elif state.buttons[BUTTON_INDEX[src]]:
                v = AXIS_MAX
        return v

    def _render_body(self) -> Image.Image:
        img = Image.new("RGBA", self.hi_size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(img, "RGBA")
        th = self.theme
        for obj in self.layout.get("body", []):
            shp = parse_shape(obj, th["font_size"])
            if shp.kind == "text":
                fill = parse_color(obj.get("fill", th["label"]))
                stroke = parse_color(obj["stroke"]) if "stroke" in obj else None
            else:
                fill = parse_color(obj.get("fill", th["body"]))
                stroke = parse_color(obj.get("stroke", th["body_stroke"]))
            sw = float(obj.get("stroke_width", self._sw))
            self._draw_shape(img, draw, shp, 0, 0, fill, stroke, sw)
        return img

    def _build_element(self, el: Mapping[str, Any]) -> _Element:
        th = self.theme
        kind = el["type"]
        stroke = parse_color(el.get("stroke", el.get("idle_stroke", th["idle_stroke"])))
        sw = float(el.get("stroke_width", self._sw))
        active = parse_color(el.get("active", th["active"]))
        label = el.get("label")
        label = str(label) if label not in (None, "") else None
        label_size = float(el.get("label_size", th["font_size"]))
        k = self.k
        if kind == "stick":
            cx, cy, r = float(el["cx"]), float(el["cy"]), float(el["r"])
            knob_r = float(el.get("knob_r") or round(r * 0.55, 1))
            travel = float(el["travel"]) if el.get("travel") is not None else max(0.0, r - knob_r)
            if not el.get("label_size"):  # like the web overlay: knob labels fit the knob
                label_size = min(label_size, knob_r)
            e = _Element("stick", _Shape("ellipse", cx - r, cy - r, cx + r, cy + r),
                         parse_color(el.get("ring", th["body_stroke"])), stroke, sw, active,
                         label, label_size, x_axis=AXIS_INDEX[el["x_axis"]],
                         y_axis=AXIS_INDEX[el["y_axis"]], button=el.get("button") or None,
                         travel=travel, knob_r=knob_r,
                         knob_fill=parse_color(el.get("fill", th["idle"])))
            reach = max(r, travel + knob_r)
            box = [(cx - reach) * k, (cy - reach) * k, (cx + reach) * k, (cy + reach) * k]
            box = self._pad_box(box, sw)
            if label:
                box = self._union(box, self._label_box(label, label_size, cx, cy, travel))
        else:
            shp = parse_shape(el, th["font_size"])
            default_fill = th["label"] if shp.kind == "text" else th["idle"]
            e = _Element(kind, shp, parse_color(el.get("fill", default_fill)), stroke, sw, active,
                         label, label_size, input=el["input"])
            box = self._shape_box(shp, sw, el)
            if label and shp.kind != "text":
                box = self._union(box, self._label_box(label, label_size, *shp.center()))
            if kind == "trigger" and shp.kind != "text":
                e.fill_dir = el.get("fill_dir", "up") if el.get("fill_dir") in _FILL_DIRS else "up"
                ox, oy = math.floor(shp.x0 * k), math.floor(shp.y0 * k)
                mw, mh = math.ceil(shp.x1 * k) - ox + 1, math.ceil(shp.y1 * k) - oy + 1
                mask = Image.new("L", (max(1, mw), max(1, mh)), 0)
                self._draw_shape(mask, ImageDraw.Draw(mask), shp, ox, oy, 255, None, 0)  # type: ignore[arg-type]
                e.mask, e.mask_origin = mask, (ox, oy)
                e.extent = mh if e.fill_dir in ("up", "down") else mw
            elif kind == "trigger":
                e.extent = 1
        ss = self.ss
        e.bbox_hi = (math.floor(box[0]), math.floor(box[1]), math.ceil(box[2]), math.ceil(box[3]))
        m = _LANCZOS_SUPPORT + 1
        e.out_rect = (max(0, e.bbox_hi[0] // ss - m), max(0, e.bbox_hi[1] // ss - m),
                      min(self.size[0], -(-e.bbox_hi[2] // ss) + m),
                      min(self.size[1], -(-e.bbox_hi[3] // ss) + m))
        return e

    def _pad_box(self, box: list[float], sw: float) -> list[float]:
        pad = sw * self.k / 2 + 2
        return [box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad]

    @staticmethod
    def _union(a: Sequence[float], b: Sequence[float]) -> list[float]:
        return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]

    def _shape_box(self, shp: _Shape, sw: float, obj: Mapping[str, Any]) -> list[float]:
        k = self.k
        if shp.kind == "text":
            sprite, l, t = self._sprite(shp.text, max(1, _r(shp.size * k)), (255, 255, 255, 255),
                                        shp.anchor, None, _r(sw * k) if "stroke" in obj else 0)
            x, y = _r(shp.x0 * k) + l, _r(shp.y0 * k) + t
            return [x - 1, y - 1, x + sprite.width + 1, y + sprite.height + 1]
        return self._pad_box([shp.x0 * k, shp.y0 * k, shp.x1 * k, shp.y1 * k], sw)

    def _label_box(self, text: str, size: float, cx: float, cy: float, slack: float = 0.0) -> list[float]:
        k = self.k
        sprite, l, t = self._sprite(text, max(1, _r(size * k)), (255, 255, 255, 255), "mm")
        x, y = _r(cx * k) + l, _r(cy * k) + t
        s = slack * k + 2
        return [x - s, y - s, x + sprite.width + s, y + sprite.height + s]

    # -- drawing ------------------------------------------------------------------
    def _sprite(self, text: str, size: int, color: RGBA | None, anchor: str,
                stroke: RGBA | None = None, stroke_w: int = 0) -> tuple[Image.Image, int, int]:
        key = (text, size, color, anchor, stroke, stroke_w)
        hit = self._sprites.get(key)
        if hit is None:
            font = self.fonts.get(size, text)
            sw = stroke_w if stroke else 0
            l, t, r, b = font.getbbox(text, anchor=anchor, stroke_width=sw)
            c = color or (0, 0, 0, 0)
            img = Image.new("RGBA", (max(1, r - l), max(1, b - t)), (c[0], c[1], c[2], 0))
            ImageDraw.Draw(img).text((-l, -t), text, font=font, fill=c, anchor=anchor,
                                     stroke_width=sw, stroke_fill=stroke)
            hit = self._sprites[key] = (img, int(l), int(t))
        return hit

    def _blit_text(self, img: Image.Image, text: str, x: float, y: float, size: float,
                   color: RGBA | None, anchor: str = "mm", stroke: RGBA | None = None,
                   stroke_w: float = 0.0) -> None:
        if color is None or not text:
            return
        sprite, l, t = self._sprite(text, max(1, _r(size * self.k)), color, anchor,
                                    stroke, _r(stroke_w * self.k))
        composite(img, sprite, _r(x) + l, _r(y) + t)

    def _draw_shape(self, img: Image.Image, draw: ImageDraw.ImageDraw, shp: _Shape, ox: float, oy: float,
                    fill: Any, stroke: RGBA | None, sw: float, direct: bool = False) -> None:
        """Draw at hi-res with the canvas origin at (ox, oy). Strokes are centred
        on the outline like SVG strokes."""
        k = self.k
        if shp.kind == "text":
            self._blit_text(img, shp.text, shp.x0 * k - ox, shp.y0 * k - oy, shp.size, fill,
                            shp.anchor, stroke, sw if stroke else 0)
            return
        has_stroke = stroke is not None and sw > 0
        half = sw * k / 2 if has_stroke else 0.0
        width = max(1, _r(sw * k)) if has_stroke else 0
        if fill is None and not has_stroke:
            return
        if (not direct and img.mode == "RGBA"
                and ((isinstance(fill, tuple) and fill[3] < 255) or (has_stroke and stroke[3] < 255))):
            # ImageDraw overwrites RGBA pixels; draw translucent colours on a layer and blend.
            pad = half + 2
            x0 = max(0, math.floor(shp.x0 * k - pad - ox))
            y0 = max(0, math.floor(shp.y0 * k - pad - oy))
            x1 = min(img.width, math.ceil(shp.x1 * k + pad - ox))
            y1 = min(img.height, math.ceil(shp.y1 * k + pad - oy))
            if x1 > x0 and y1 > y0:
                layer = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
                self._draw_shape(layer, ImageDraw.Draw(layer), shp, ox + x0, oy + y0, fill, stroke, sw,
                                 direct=True)
                img.alpha_composite(layer, (x0, y0))
            return
        if shp.kind == "polygon":
            pts = [(x * k - ox, y * k - oy) for x, y in shp.points]
            if len(pts) < 2:
                return
            if fill is not None and len(pts) >= 3:
                draw.polygon(pts, fill=fill)
            if has_stroke:
                draw.line(pts + pts[:2], fill=stroke, width=width, joint="curve")
            return
        box = [_r(shp.x0 * k - half) - ox, _r(shp.y0 * k - half) - oy,
               _r(shp.x1 * k + half) - ox - 1, _r(shp.y1 * k + half) - oy - 1]
        if box[2] < box[0] or box[3] < box[1]:
            return
        outline = stroke if has_stroke else None
        if shp.kind == "ellipse":
            draw.ellipse(box, fill=fill, outline=outline, width=width)
        elif shp.r > 0:
            radius = min(shp.r * k + half, (box[2] - box[0]) / 2, (box[3] - box[1]) / 2)
            draw.rounded_rectangle(box, radius=max(0, _r(radius)), fill=fill, outline=outline, width=width)
        else:
            draw.rectangle(box, fill=fill, outline=outline, width=width)

    def _draw_label(self, img: Image.Image, e: _Element, cx: float, cy: float, lit: bool,
                    ox: float, oy: float) -> None:
        if e.label:
            self._blit_text(img, e.label, cx * self.k - ox, cy * self.k - oy, e.label_size,
                            self._label_active if lit else self._label)

    def _fill_bar(self, img: Image.Image, e: _Element, px: int, ox: int, oy: int) -> None:
        assert e.mask is not None
        mw, mh = e.mask.size
        box = {"up": (0, mh - px, mw, mh), "down": (0, 0, mw, px),
               "left": (mw - px, 0, mw, mh), "right": (0, 0, px, mh)}[e.fill_dir]
        dx, dy = e.mask_origin[0] - ox, e.mask_origin[1] - oy
        x0, y0 = max(box[0], -dx), max(box[1], -dy)
        x1, y1 = min(box[2], img.width - dx), min(box[3], img.height - dy)
        if x1 > x0 and y1 > y0 and e.active is not None:
            img.paste(e.active, (dx + x0, dy + y0, dx + x1, dy + y1), e.mask.crop((x0, y0, x1, y1)))

    def _draw_element(self, img: Image.Image, draw: ImageDraw.ImageDraw, e: _Element,
                      vs: Any, ox: int, oy: int) -> None:
        shp = e.shape
        if e.kind == "stick":
            dx, dy, lit = vs
            self._draw_shape(img, draw, shp, ox, oy, e.fill, e.stroke, e.sw)
            cx, cy = shp.center()
            kx, ky = cx + dx / self.k, cy + dy / self.k
            knob = _Shape("ellipse", kx - e.knob_r, ky - e.knob_r, kx + e.knob_r, ky + e.knob_r)
            self._draw_shape(img, draw, knob, ox, oy, e.active if lit else e.knob_fill, e.stroke, e.sw)
            self._draw_label(img, e, kx, ky, lit, ox, oy)
            return
        if e.kind == "trigger" and e.mask is not None:
            px, lit = vs
            self._draw_shape(img, draw, shp, ox, oy, e.fill, None, 0)
            if px > 0:
                self._fill_bar(img, e, px, ox, oy)
            if e.stroke is not None and e.sw > 0:
                self._draw_shape(img, draw, shp, ox, oy, None, e.active if lit else e.stroke, e.sw)
        else:
            lit = vs[1] if isinstance(vs, tuple) else vs
            if shp.kind == "text":
                self._draw_shape(img, draw, shp, ox, oy, e.active if lit else e.fill, None, 0)
                return
            self._draw_shape(img, draw, shp, ox, oy, e.active if lit else e.fill, e.stroke, e.sw)
        self._draw_label(img, e, *shp.center(), lit, ox, oy)

    # -- incremental rendering -------------------------------------------------------
    def _update(self, key: tuple[Any, ...]) -> Image.Image:
        if self._cur_img is not None and self._cur_key is not None:
            if key == self._cur_key:
                return self._cur_img
            rects = [e.out_rect for e, a, b in zip(self.elements, key, self._cur_key) if a != b]
            rects = _merge_rects(rects)
            if sum((r[2] - r[0]) * (r[3] - r[1]) for r in rects) < 0.6 * self.size[0] * self.size[1]:
                img = self._cur_img.copy()
                for r in rects:
                    if r[2] > r[0] and r[3] > r[1]:
                        img.paste(self._render_region(key, r), r[:2])
                self._cur_key, self._cur_img = key, img
                return img
        img = self._render_region(key, (0, 0, *self.size))
        self._cur_key, self._cur_img = key, img
        return img

    def _render_region(self, key: tuple[Any, ...], rect: tuple[int, int, int, int]) -> Image.Image:
        x0, y0, x1, y1 = rect
        ss = self.ss
        m = _LANCZOS_SUPPORT * ss + 2 if ss > 1 else 0
        hx0, hy0 = max(0, x0 * ss - m), max(0, y0 * ss - m)
        hx1, hy1 = min(self.hi_size[0], x1 * ss + m), min(self.hi_size[1], y1 * ss + m)
        patch = self._body.crop((hx0, hy0, hx1, hy1))
        draw = ImageDraw.Draw(patch, "RGBA")
        for e, vs in zip(self.elements, key):
            b = e.bbox_hi
            if b[2] <= hx0 or b[0] >= hx1 or b[3] <= hy0 or b[1] >= hy1:
                continue
            self._draw_element(patch, draw, e, vs, hx0, hy0)
        if ss == 1:
            return patch
        return patch.resize((x1 - x0, y1 - y0), Image.Resampling.LANCZOS,
                            box=(x0 * ss - hx0, y0 * ss - hy0, x1 * ss - hx0, y1 * ss - hy0))


# --- history lane -----------------------------------------------------------------

def default_row_labels(inputs: Sequence[str], family: str = FAMILY_GENERIC,
                       layout: Mapping[str, Any] | None = None) -> dict[str, str]:
    """Row labels: the layout element's ``label``, then FACE_LABELS, then short names."""
    out: dict[str, str] = {}
    faces = FACE_LABELS.get(family, FACE_LABELS[FAMILY_GENERIC])
    from_layout: dict[str, str] = {}
    for el in (layout or {}).get("elements", []):
        if el.get("type") in ("button", "trigger") and el.get("label"):
            from_layout.setdefault(el["input"], str(el["label"]))
    for name in inputs:
        out[name] = (from_layout.get(name) or faces.get(name) or SHORT_LABELS.get(name)
                     or name.replace("_", " ").title())
    return out


def default_row_colors(layout: Mapping[str, Any]) -> dict[str, str]:
    """Per-input bar colour from each element's ``active`` override."""
    out: dict[str, str] = {}
    for el in layout.get("elements", []):
        if el.get("active"):
            for name in element_inputs(el):
                out.setdefault(name, el["active"])
    return out


class HistoryPainter:
    """Scrolling input-history lane: one row per input, bars while held.

    The lane covers ``seconds`` of time ending at the current time (right edge).
    Frame boundaries (``fps``, aligned to ``origin_ns``) are drawn when a frame
    is at least ``min_frame_px`` wide, and 1 s ticks always. ``mapping`` is the
    same display remap as :class:`LayoutPainter` (source -> row input).
    ``background`` (an opaque colour) pre-composites the lane onto the colour it
    will be shown over, so :meth:`paint` produces a fast, fully opaque RGB lane.
    """

    def __init__(self, inputs: Sequence[str], width: int, height: int, seconds: float = 4.0,
                 fps: float = 60.0, theme: Mapping[str, Any] | None = None, *,
                 labels: Mapping[str, str] | None = None, colors: Mapping[str, Any] | None = None,
                 mapping: Mapping[str, str] | str | None = None, font_path: str | None = None,
                 scale: float = 1.0, family: str = FAMILY_GENERIC, min_frame_px: float = 4.0,
                 background: Any = None) -> None:
        if not inputs:
            raise ValueError("history needs at least one input row")
        self.inputs = list(dict.fromkeys(inputs))  # one row per input, first position wins
        self.width, self.height = max(8, int(width)), max(8, int(height))
        self.seconds = float(seconds)
        if self.seconds <= 0:
            raise ValueError("seconds must be positive")
        self.fps = float(fps)
        self.min_frame_px = float(min_frame_px)
        self.theme = dict(DEFAULT_THEME)
        self.theme.update(theme or {})
        self.mapping: dict[str, str] = parse_map(mapping) if isinstance(mapping, str) else dict(mapping or {})
        self.scale = float(scale)
        backdrop = parse_color(background)
        self._backdrop = backdrop if backdrop is not None and backdrop[3] == 255 else None
        th = self.theme
        # Lane = the theme's idle/label pair, which every layout keeps readable
        # (labels are drawn on idle buttons); bars use the lit colours.
        self._bg = parse_color(th["idle"]) or (61, 67, 77, 255)
        self._label = parse_color(th["label"]) or (255, 255, 255, 255)
        self._label_active = parse_color(th["label_active"]) or (0, 0, 0, 255)
        active = parse_color(th["active"]) or (255, 210, 63, 255)
        self._colors = {n: parse_color((colors or {}).get(n)) or active for n in self.inputs}
        self.labels = dict(default_row_labels(self.inputs, family))
        self.labels.update(labels or {})
        self.fonts = FontBook(font_path)
        self.pad = max(2, round(4 * self.scale))
        n = len(self.inputs)
        # Never smaller than 2 px per row and a 16 px wide lane (degenerate sizes crash Pillow).
        self.width = max(self.width, 2 * self.pad + 24)
        self.height = max(self.height, 2 * self.pad + 2 * n)
        self.row_h = (self.height - 2 * self.pad) / n
        self._font_size = max(6, round(min(self.row_h * 0.9, th["font_size"] * self.scale)))
        widest = max(self._text_width(self.labels[i]) for i in self.inputs)
        self.label_w = min(round(self.width * 0.3), widest + 3 * self.pad)
        self.lane_x0 = self.pad + self.label_w
        self.lane_x1 = self.width - self.pad
        self._chips: dict[str, tuple[Image.Image, int, int]] = {}
        self._base = self._render_base()
        self._iv_src: tuple[Timeline, int] | None = None
        self._iv: dict[str, tuple[list[int], list[int]]] = {}

    # -- geometry ---------------------------------------------------------------
    @property
    def lane_width(self) -> float:
        return float(self.lane_x1 - self.lane_x0)

    def row_span(self, name: str) -> tuple[float, float]:
        """(top, bottom) of an input's row in lane pixels."""
        i = self.inputs.index(name)
        top = self.pad + i * self.row_h
        return top, top + self.row_h

    def x_for(self, t_ns: int, now_ns: int) -> float:
        """Lane x coordinate of time ``t_ns`` when the right edge is ``now_ns``."""
        return self.lane_x1 - (now_ns - t_ns) * self.lane_width / (self.seconds * NS_PER_S)

    # -- rendering ----------------------------------------------------------------
    def _text_width(self, text: str) -> int:
        font = self.fonts.get(self._font_size, text)
        l, _, r, _ = font.getbbox(text)
        return int(r - l)

    def _render_base(self) -> Image.Image:
        """Opaque RGB lane (so translucent lines blend); the rounded outline is ``self._mask``."""
        ss = 2
        w, h = self.width * ss, self.height * ss
        radius = min(round(6 * self.scale * ss), min(w, h) // 4)  # tiny lanes: keep Pillow happy
        shape = Image.new("L", (w, h), 0)
        ImageDraw.Draw(shape).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
        self._mask = shape.resize((self.width, self.height), Image.Resampling.LANCZOS)
        border = parse_color(self.theme["idle_stroke"]) or (0, 0, 0, 255)
        hi = Image.new("RGB", (w, h), border[:3])
        d = ImageDraw.Draw(hi, "RGBA")
        bw = max(1, round(self.scale * ss))
        d.rounded_rectangle([bw, bw, w - 1 - bw, h - 1 - bw], radius=max(0, radius - bw), fill=self._bg)
        lc = self._label
        for i in range(0, len(self.inputs), 2):
            top, bottom = self.row_span(self.inputs[i])
            d.rectangle([self.lane_x0 * ss, round(top * ss), self.lane_x1 * ss - 1,
                         round(bottom * ss) - 1], fill=(lc[0], lc[1], lc[2], 20))
        img = hi.resize((self.width, self.height), Image.Resampling.LANCZOS)
        d = ImageDraw.Draw(img, "RGBA")
        d.line([(self.lane_x0 - 1, self.pad), (self.lane_x0 - 1, self.height - self.pad - 1)],
               fill=(lc[0], lc[1], lc[2], 60))
        d.line([(self.lane_x1, self.pad), (self.lane_x1, self.height - self.pad - 1)],
               fill=(lc[0], lc[1], lc[2], 170), width=max(1, round(self.scale)))
        for name in self.inputs:
            top, bottom = self.row_span(name)
            text = self.labels[name]
            font = self.fonts.get(self._font_size, text)
            d.text((self.pad * 1.5, (top + bottom) / 2), text, font=font, fill=lc, anchor="lm")
        if self._backdrop is not None:
            out = Image.new("RGB", img.size, self._backdrop[:3])
            out.paste(img, (0, 0), self._mask)
            return out
        return img

    def _chip(self, name: str) -> tuple[Image.Image, int, int]:
        hit = self._chips.get(name)
        if hit is None:
            top, bottom = self.row_span(name)
            x0, y0 = round(self.pad * 0.5), round(top) + 1
            w = round(self.lane_x0 - self.pad * 0.5 - 2) - x0
            h = max(2, round(bottom) - 1 - y0)
            color = self._colors[name]
            img = Image.new("RGBA", (max(2, w), h), (color[0], color[1], color[2], 0))
            d = ImageDraw.Draw(img, "RGBA")
            d.rounded_rectangle([0, 0, img.width - 1, h - 1], radius=max(1, h // 4), fill=color)
            text = self.labels[name]
            d.text((self.pad * 1.5 - x0, h / 2), text, font=self.fonts.get(self._font_size, text),
                   fill=self._label_active, anchor="lm")
            hit = self._chips[name] = (img, x0, y0)
        return hit

    def intervals(self, timeline: Timeline, device: int) -> dict[str, tuple[list[int], list[int]]]:
        """Per row: sorted, disjoint (downs, ups) press intervals (ups exclusive)."""
        if self._iv_src is not None and self._iv_src[0] is timeline and self._iv_src[1] == device:
            return self._iv
        rows: dict[str, list[tuple[int, int]]] = {n: [] for n in self.inputs}
        for p in timeline.presses(device, include_triggers=True):
            src = press_name(p.button)
            name = self.mapping.get(src, src)
            if name in rows:
                rows[name].append((p.down_ns, _INF if p.up_ns is None else p.up_ns))
        out: dict[str, tuple[list[int], list[int]]] = {}
        for name, ivs in rows.items():
            ivs.sort()
            downs: list[int] = []
            ups: list[int] = []
            for a, b in ivs:
                if downs and a <= ups[-1]:
                    ups[-1] = max(ups[-1], b)
                else:
                    downs.append(a)
                    ups.append(b)
            out[name] = (downs, ups)
        self._iv_src, self._iv = (timeline, device), out
        return out

    def paint(self, timeline: Timeline | None, device: int | None, t_ns: int,
              image: Image.Image | None = None, dest: tuple[int, int] = (0, 0),
              origin_ns: int = 0) -> Image.Image:
        """Draw the lane with its right edge at ``t_ns``. Returns a new image of
        (width, height) (RGB with a ``background``, else RGBA), or draws onto
        ``image`` at ``dest`` and returns it. ``origin_ns`` aligns the frame grid."""
        img = self._base.copy()
        d = ImageDraw.Draw(img, "RGBA")
        window = self.seconds * NS_PER_S
        t0 = t_ns - window
        ppn = self.lane_width / window
        lx0, lx1 = self.lane_x0, self.lane_x1
        top, bottom = self.pad, self.height - self.pad - 1
        lc = self._label
        frame_px = self.lane_width / (self.seconds * self.fps)
        if frame_px >= self.min_frame_px:
            period = NS_PER_S / self.fps
            j = math.floor((t0 - origin_ns) / period)
            j1 = math.ceil((t_ns - origin_ns) / period)
            grid = (lc[0], lc[1], lc[2], 32)
            while j <= j1:
                x = round(lx1 - (t_ns - (origin_ns + round(j * period))) * ppn)
                if lx0 <= x < lx1:
                    d.line([(x, top), (x, bottom)], fill=grid)
                j += 1
        tick = (lc[0], lc[1], lc[2], 95)
        s = math.ceil((t0 - origin_ns) / NS_PER_S)
        while origin_ns + s * NS_PER_S <= t_ns:
            x = round(lx1 - (t_ns - (origin_ns + s * NS_PER_S)) * ppn)
            if lx0 <= x < lx1:
                d.line([(x, top), (x, bottom)], fill=tick, width=max(1, round(self.scale)))
            s += 1
        if timeline is not None and device is not None:
            iv = self.intervals(timeline, device)
            inset = max(1.0, self.row_h * 0.18)
            for name in self.inputs:
                downs, ups = iv[name]
                if not downs:
                    continue
                rt, rb = self.row_span(name)
                y0, y1 = round(rt + inset), max(round(rt + inset) + 1, round(rb - inset))
                color = self._colors[name]
                i = bisect.bisect_right(ups, t0)
                held = False
                while i < len(downs) and downs[i] <= t_ns:
                    a, b = max(downs[i], t0), min(ups[i], t_ns)
                    x0 = lx1 - (t_ns - a) * ppn
                    x1 = min(float(lx1), max(lx1 - (t_ns - b) * ppn, x0 + 1.0))
                    _hbar(d, x0, x1, y0, y1, color)
                    if ups[i] > t_ns:
                        held = True
                    i += 1
                if held:
                    chip, cx, cy = self._chip(name)
                    composite(img, chip, cx, cy)
        if self._backdrop is None:
            img = img.convert("RGBA")
            img.putalpha(self._mask)
        if image is None:
            return img
        composite(image, img, int(dest[0]), int(dest[1]))
        return image


def _hbar(d: ImageDraw.ImageDraw, x0: float, x1: float, y0: int, y1: int, color: RGBA) -> None:
    """Horizontal bar with anti-aliased (fractional) left/right edges."""
    r, g, b, a = color
    ix0, ix1 = math.ceil(x0), math.floor(x1)
    if ix1 < ix0:  # inside one pixel column
        d.rectangle([ix1, y0, ix1, y1 - 1], fill=(r, g, b, round(a * (x1 - x0))))
        return
    if ix1 > ix0:
        d.rectangle([ix0, y0, ix1 - 1, y1 - 1], fill=color)
    if ix0 - x0 > 0.02:
        d.rectangle([ix0 - 1, y0, ix0 - 1, y1 - 1], fill=(r, g, b, round(a * (ix0 - x0))))
    if x1 - ix1 > 0.02:
        d.rectangle([ix1, y0, ix1, y1 - 1], fill=(r, g, b, round(a * (x1 - ix1))))


__all__ = ["LayoutPainter", "HistoryPainter", "FontBook", "parse_color", "parse_shape",
           "normalize_layout", "digital_state", "composite", "default_row_labels",
           "default_row_colors", "DEFAULT_FONTS", "SYMBOL_FONTS", "MONO_FONTS", "SHORT_LABELS"]
