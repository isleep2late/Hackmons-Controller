# Controller layout format (`controllerlog/layouts/*.json`)

One JSON file describes one drawable controller. The **same file** drives the
live web overlay (rendered as SVG in the browser) and the offline video
renderer (rendered with Pillow), so only simple primitives are allowed. Every
shape can be drawn by both SVG and `PIL.ImageDraw`.

```jsonc
{
  "name": "xbox",                      // file stem, used in ?layout=xbox
  "title": "Xbox-style controller",
  "families": ["xbox"],                // auto-selected for these DeviceInfo.family values
  "size": [520, 340],                  // canvas / viewBox width, height (px)
  "theme": {                           // optional; these are the defaults
    "body": "#2b2f36", "body_stroke": "#15171b",
    "idle": "#3d434d", "idle_stroke": "#15171b",
    "active": "#ffd23f", "label": "#e8e8e8", "label_active": "#111111",
    "font_size": 14
  },
  "body": [ /* shapes drawn first, never light up */ ],
  "elements": [ /* inputs */ ],
  "history": ["dpad_up", "dpad_down", "dpad_left", "dpad_right",
              "south", "east", "west", "north", "left_shoulder", "right_shoulder",
              "left_trigger", "right_trigger", "back", "start"]
}
```

## Shapes

All coordinates are in canvas pixels. Colors are CSS hex strings (`#rgb`,
`#rgba`, `#rrggbb` or `#rrggbbaa`; `none` and `transparent` are also accepted).
Anything else in a colour field (`fill`, `stroke`, `idle_stroke`, `active`,
`ring` and the theme colours) makes the layout invalid, so a shared layout file
can't inject markup into the overlay or viewer.

| `shape`     | fields                                  | notes |
|-------------|-----------------------------------------|-------|
| `rect`      | `x, y, w, h`, optional `r` (corner radius) | |
| `circle`    | `cx, cy, r`                             | |
| `ellipse`   | `cx, cy, rx, ry`                        | |
| `polygon`   | `points: [[x,y], ...]`                  | closed automatically |
| `text`      | `x, y, text`, optional `size`, `anchor` (`"middle"` default, `"start"`, `"end"`) | `y` is the text's vertical centre |

Any shape may carry `fill`, `stroke`, `stroke_width` to override the theme.

## Elements

```jsonc
// Digital button: lights up while pressed.
{"type": "button", "input": "south", "shape": "circle", "cx": 410, "cy": 190, "r": 17,
 "label": "A", "active": "#5fd35f"}

// Trigger: fill bar proportional to 0..32767, plus lit outline past the press threshold.
{"type": "trigger", "input": "left_trigger", "shape": "rect", "x": 90, "y": 12, "w": 70, "h": 22,
 "r": 6, "label": "LT", "fill_dir": "right"}          // fill_dir: up | down | left | right

// Analog stick: ring + knob displaced by the axes, knob lights when the stick is clicked.
{"type": "stick", "x_axis": "left_x", "y_axis": "left_y", "button": "left_stick",
 "cx": 150, "cy": 150, "r": 38, "knob_r": 20, "travel": 22}
```

* `input` / `button` use canonical names from `controllerlog/model.py`
  (`BUTTONS` plus `left_trigger` / `right_trigger` for digital use of triggers).
* A `button` element whose `input` is a trigger lights past
  `TRIGGER_PRESS_THRESHOLD` (used for Game Boy L/R or GameCube-style drawings).
* `label` is optional; drawn centred on the shape in `theme.label`
  (`theme.label_active` while lit).
* `active` optionally overrides the lit colour for that element.

Nintendo naming is positional: `south` is the bottom face button (Nintendo
**B**), `east` is the right face button (Nintendo **A**). Game Boy / GBA
layouts therefore draw **A = `east`**, **B = `south`**, **Select = `back`**,
**Start = `start`**, **L = `left_shoulder`**, **R = `right_shoulder`**. GSE and
bk2 imports use the same mapping, so their logs light the right buttons.

## Defaults and optional keys

Both renderers (web overlay `web/js/layout-svg.js` and `render/draw.py`)
implement these the same way. Rely on them in custom layouts.

| key | where | default / meaning |
|---|---|---|
| `theme.stroke_width` | theme | `2`. Strokes are centred on the outline, as in SVG |
| `stroke_width` | any shape/element | overrides `theme.stroke_width` |
| `fill` | button / trigger | idle fill (default `theme.idle`; text-shaped buttons default to `theme.label`) |
| `stroke` (alias `idle_stroke`) | button / trigger | idle outline (default `theme.idle_stroke`) |
| `active` | button / trigger / stick | lit colour (default `theme.active`) |
| `label_size` | any element | label font size (default `theme.font_size`; stick knob labels `min(font_size, knob_r)`) |
| `fill_dir` | trigger | `up` |
| `knob_r` | stick | `0.55 × r` |
| `travel` | stick | `r − knob_r` |
| `fill` / `ring` | stick | knob colour (default `theme.idle`) / ring colour (default `theme.body_stroke`) |
| polygon labels | polygon buttons | drawn at the polygon's area centroid |
| `text` shapes in `body` | body | coloured `theme.label` unless `fill` is given |

A polygon needs at least 3 points. `history` must not repeat an input.

## Remapping at display time

Overlay URL and renderer accept `map=src:dst,src:dst` which renames inputs
before drawing, e.g. showing an Xbox pad on the Game Boy layout with Xbox A as
GB A: `?layout=gameboy&map=south:east,east:south`.

## `history`

Ordered list of inputs shown as rows in the scrolling input-history
(piano-roll) lane and in rendered timelines. Defaults to every input that has
an element if omitted.
