"""Load and validate controller layouts (see docs/LAYOUTS.md)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .model import AXES, BUTTONS, FAMILY_GENERIC

LAYOUT_DIR = Path(__file__).resolve().parent / "layouts"
DIGITAL_INPUTS = frozenset(BUTTONS) | {"left_trigger", "right_trigger"}
SHAPES = {"rect": ("x", "y", "w", "h"), "circle": ("cx", "cy", "r"),
          "ellipse": ("cx", "cy", "rx", "ry"), "polygon": ("points",),
          "text": ("x", "y", "text")}

DEFAULT_THEME: dict[str, Any] = {
    "body": "#2b2f36", "body_stroke": "#15171b",
    "idle": "#3d434d", "idle_stroke": "#15171b",
    "active": "#ffd23f", "label": "#e8e8e8", "label_active": "#111111",
    "font_size": 14,
}


class LayoutError(ValueError):
    pass


_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# Colours are CSS hex strings (docs/LAYOUTS.md). Checked strictly: layouts are shared as
# files and their colours end up in SVG/HTML, so nothing else may get through.
_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_COLOR_WORDS = {"none", "transparent"}
THEME_COLOR_KEYS = ("body", "body_stroke", "idle", "idle_stroke", "active", "label", "label_active")
COLOR_KEYS = ("fill", "stroke", "idle_stroke", "active", "ring")


def _check_color(value: Any, where: str) -> None:
    if not (isinstance(value, str) and (_COLOR_RE.match(value) or value.lower() in _COLOR_WORDS)):
        raise LayoutError(f"{where}: colour must be a CSS hex string like #ffd23f, got {value!r}")


def _check_colors(obj: dict[str, Any], keys: tuple[str, ...], where: str) -> None:
    for key in keys:
        if key in obj:
            _check_color(obj[key], f"{where}.{key}")


def list_layouts(directory: Path = LAYOUT_DIR) -> list[str]:
    return sorted(p.stem for p in directory.glob("*.json"))


def load_layout(name_or_path: str | Path, directory: Path = LAYOUT_DIR) -> dict[str, Any]:
    """Load a layout by bare name (``"xbox"``) or by explicit file path.

    Strings are only treated as paths when they contain a path separator, so a
    name coming from a URL or CLI can't escape the layouts directory.
    """
    if isinstance(name_or_path, Path) or any(s in str(name_or_path) for s in ("/", "\\")):
        p = Path(name_or_path)
    else:
        name = str(name_or_path)
        if name.endswith(".json"):
            name = name[:-5]
        if not _NAME_RE.match(name):
            raise LayoutError(f"bad layout name {name_or_path!r}")
        p = directory / f"{name}.json"
    if not p.is_file():
        raise LayoutError(f"layout not found: {name_or_path} (available: "
                          f"{', '.join(list_layouts(directory))})")
    layout = json.loads(p.read_text(encoding="utf-8"))
    layout.setdefault("name", p.stem)
    return normalize_layout(layout)


def normalize_layout(layout: dict[str, Any]) -> dict[str, Any]:
    """Validate a layout dict and fill in the theme and default history (in place)."""
    validate_layout(layout)
    theme = dict(DEFAULT_THEME)
    theme.update(layout.get("theme", {}))
    layout["theme"] = theme
    if "history" not in layout:
        layout["history"] = [inp for el in layout["elements"]
                             for inp in element_inputs(el) if inp in DIGITAL_INPUTS]
    return layout


def element_inputs(el: dict[str, Any]) -> list[str]:
    if el["type"] in ("button", "trigger"):
        return [el["input"]]
    if el["type"] == "stick":
        return [el["button"]] if el.get("button") else []
    return []


def _check_shape(obj: dict[str, Any], where: str) -> None:
    shape = obj.get("shape")
    if shape not in SHAPES:
        raise LayoutError(f"{where}: unknown shape {shape!r}")
    for key in SHAPES[shape]:
        if key not in obj:
            raise LayoutError(f"{where}: {shape} needs {key!r}")
    if shape == "polygon" and (not isinstance(obj["points"], list) or len(obj["points"]) < 3):
        raise LayoutError(f"{where}: polygon needs at least 3 points")


def validate_layout(layout: dict[str, Any]) -> None:
    name = layout.get("name", "?")
    size = layout.get("size")
    if not (isinstance(size, list) and len(size) == 2 and all(isinstance(v, (int, float)) for v in size)):
        raise LayoutError(f"{name}: size must be [width, height]")
    theme = layout.get("theme", {})
    if not isinstance(theme, dict):
        raise LayoutError(f"{name}: theme must be an object")
    _check_colors(theme, THEME_COLOR_KEYS, f"{name}.theme")
    for i, shape in enumerate(layout.get("body", [])):
        _check_shape(shape, f"{name}.body[{i}]")
        _check_colors(shape, COLOR_KEYS, f"{name}.body[{i}]")
    elements = layout.get("elements")
    if not isinstance(elements, list) or not elements:
        raise LayoutError(f"{name}: elements must be a non-empty list")
    for i, el in enumerate(elements):
        where = f"{name}.elements[{i}]"
        if not isinstance(el, dict):
            raise LayoutError(f"{where}: must be an object")
        _check_colors(el, COLOR_KEYS, where)
        t = el.get("type")
        if t in ("button", "trigger"):
            if el.get("input") not in DIGITAL_INPUTS:
                raise LayoutError(f"{where}: unknown input {el.get('input')!r}")
            _check_shape(el, where)
            if t == "trigger" and el["input"] not in ("left_trigger", "right_trigger"):
                raise LayoutError(f"{where}: trigger elements need a trigger input")
        elif t == "stick":
            for key in ("x_axis", "y_axis"):
                if el.get(key) not in AXES:
                    raise LayoutError(f"{where}: unknown axis {el.get(key)!r}")
            if el.get("button") and el["button"] not in BUTTONS:
                raise LayoutError(f"{where}: unknown button {el['button']!r}")
            for key in ("cx", "cy", "r"):
                if key not in el:
                    raise LayoutError(f"{where}: stick needs {key!r}")
        else:
            raise LayoutError(f"{where}: unknown element type {t!r}")
    history = layout.get("history", [])
    for inp in history:
        if inp not in DIGITAL_INPUTS:
            raise LayoutError(f"{name}.history: unknown input {inp!r}")
    if len(set(history)) != len(history):
        raise LayoutError(f"{name}.history: duplicate inputs")


def layout_for_family(family: str, directory: Path = LAYOUT_DIR) -> str:
    """Pick the layout whose ``families`` contains ``family`` (falls back to generic)."""
    for name in list_layouts(directory):
        try:
            data = json.loads((directory / f"{name}.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if family in data.get("families", []):
            return name
    if (directory / f"{FAMILY_GENERIC}.json").exists():
        return FAMILY_GENERIC
    names = list_layouts(directory)
    if not names:
        raise LayoutError(f"no layouts in {directory}")
    return names[0]


def parse_map(spec: str | None) -> dict[str, str]:
    """``"south:east,east:south"`` -> ``{"south": "east", "east": "south"}``.

    Keys are *source* inputs (what the controller reports); values are the
    layout inputs they should light. Unmapped inputs pass through unchanged.
    """
    out: dict[str, str] = {}
    if not spec:
        return out
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise LayoutError(f"bad map entry {part!r} (expected src:dst)")
        src, dst = (s.strip() for s in part.split(":", 1))
        for v in (src, dst):
            if v not in DIGITAL_INPUTS:
                raise LayoutError(f"bad map entry {part!r}: unknown input {v!r}")
        out[src] = dst
    return out


def remap_digital(pressed: dict[str, bool], mapping: dict[str, str]) -> dict[str, bool]:
    """Apply a :func:`parse_map` mapping to a {input: pressed} dict."""
    if not mapping:
        return pressed
    out = {k: False for k in pressed}
    for src, down in pressed.items():
        dst = mapping.get(src, src)
        out[dst] = out.get(dst, False) or down
    return out
