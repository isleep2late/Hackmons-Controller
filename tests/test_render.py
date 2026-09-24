"""Tests for the offline input-display renderer (controllerlog.render)."""

from __future__ import annotations

import random
import re
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest
from PIL import Image, ImageChops

from controllerlog.layouts import list_layouts, load_layout
from controllerlog.logfile import Recording
from controllerlog.model import AXIS_INDEX, BUTTON_INDEX, NUM_BUTTONS, DeviceInfo, InputEvent, PadState
from controllerlog.render import video
from controllerlog.render.draw import HistoryPainter, LayoutPainter, parse_color
from controllerlog.render.video import (FFmpegNotFoundError, FrameRenderer, all_pressed_state,
                                        find_ffmpeg, fps_fraction, frame_count, frame_time_ns,
                                        iter_frame_states, render_frame, render_recording)
from controllerlog.timeline import Timeline

FIXTURE = Path(__file__).parent / "fixtures" / "render" / "testpad.json"
S = 1_000_000_000
MS = 1_000_000

IDLE = (0x40, 0x40, 0x40)
ACTIVE = (0xff, 0xd0, 0x00)
WELL = (0x10, 0x10, 0x10)


# --- helpers --------------------------------------------------------------------

@pytest.fixture(scope="module")
def layout() -> dict:
    return load_layout(FIXTURE)


def rgb(img: Image.Image, x: float, y: float) -> tuple[int, int, int]:
    return img.convert("RGBA").getpixel((int(x), int(y)))[:3]


def near(c1, c2, tol: int = 10) -> bool:
    return all(abs(a - b) <= tol for a, b in zip(c1[:3], c2[:3]))


def closer(c, a, b) -> bool:
    da = sum((x - y) ** 2 for x, y in zip(c[:3], a[:3]))
    db = sum((x - y) ** 2 for x, y in zip(c[:3], b[:3]))
    return da < db


def max_diff(a: Image.Image, b: Image.Image) -> int:
    """Largest per-channel difference (RGBA getbbox() would only look at alpha)."""
    return max(hi for _, hi in ImageChops.difference(a.convert("RGBA"), b.convert("RGBA")).getextrema())


def state(buttons=(), **axes) -> PadState:
    st = PadState()
    for b in buttons:
        st.buttons[BUTTON_INDEX[b]] = 1
    for name, v in axes.items():
        st.axes[AXIS_INDEX[name]] = v
    return st


def make_recording() -> Recording:
    """2 s: south 0.5-1.0 s, 5 ms east tap at 1.21 s, LT ramp, left stick right, run marker at 0.5 s."""
    dev = DeviceInfo(0, name="Test pad", family="xbox")
    ev = [InputEvent(0, 0, "+", None, dev.to_json()),
          InputEvent(500 * MS, None, "m", None, "run start"),
          InputEvent(500 * MS, 0, "b", BUTTON_INDEX["south"], 1),
          InputEvent(1000 * MS, 0, "b", BUTTON_INDEX["south"], 0),
          InputEvent(1210 * MS, 0, "b", BUTTON_INDEX["east"], 1),
          InputEvent(1215 * MS, 0, "b", BUTTON_INDEX["east"], 0),
          InputEvent(1400 * MS, 0, "a", AXIS_INDEX["left_x"], 32767),
          InputEvent(1500 * MS, None, "m", None, "split:1"),
          InputEvent(1700 * MS, 0, "a", AXIS_INDEX["left_x"], 0)]
    for i in range(11):  # trigger ramp 0.2 .. 0.7 s
        ev.append(InputEvent(200 * MS + i * 50 * MS, 0, "a", AXIS_INDEX["left_trigger"], min(32767, i * 3300)))
    ev.append(InputEvent(900 * MS, 0, "a", AXIS_INDEX["left_trigger"], 0))
    ev.append(InputEvent(2 * S, 0, "b", BUTTON_INDEX["start"], 0))  # sets duration to exactly 2 s
    ev.sort(key=lambda e: e.t_ns)
    return Recording({"format": "controllerlog", "version": 1}, {0: dev}, ev)


# --- LayoutPainter -----------------------------------------------------------------

def test_painter_size_and_transparency(layout):
    p = LayoutPainter(layout)
    img = p.paint(PadState())
    assert img.size == (400, 240) and img.mode == "RGBA"
    assert img.getpixel((0, 0))[3] == 0          # outside the rounded body
    assert img.getpixel((200, 120))[3] == 255
    assert LayoutPainter(layout, scale=1.5).size == (600, 360)


def test_button_idle_and_active_colors(layout):
    p = LayoutPainter(layout)
    idle = p.paint(PadState())
    lit = p.paint(state(["south", "north", "west"]))
    assert near(rgb(idle, 341, 160), IDLE)
    assert near(rgb(lit, 341, 160), (0x00, 0xc0, 0x00))      # element "active" override
    assert near(rgb(lit, 305, 124), ACTIVE)                  # theme active
    assert near(rgb(idle, 341, 88), (0x50, 0x50, 0x80))      # element "fill" override
    assert near(rgb(lit, 341, 88), ACTIVE)
    assert near(rgb(lit, 377, 124), IDLE)                    # east untouched


def test_label_colour_switches(layout):
    p = LayoutPainter(layout)
    box = (324, 154, 337, 167)  # around the "A" label
    idle = p.paint(PadState()).crop(box).convert("L")
    lit = p.paint(state(["south"])).crop(box).convert("L")
    assert idle.getextrema()[1] > 200      # white label on idle
    assert lit.getextrema()[0] < 50        # black label while lit


def test_polygon_ellipse_and_text_buttons(layout):
    p = LayoutPainter(layout)
    idle = p.paint(PadState())
    lit = p.paint(state(["dpad_left", "start", "back"]))
    assert near(rgb(idle, 60, 150), IDLE) and near(rgb(lit, 60, 150), ACTIVE)
    assert near(rgb(idle, 220, 60), IDLE) and near(rgb(lit, 220, 60), ACTIVE)
    text_box = (120, 52, 180, 68)
    colors_idle = {c[:3] for _, c in idle.crop(text_box).getcolors(10000)}
    colors_lit = {c[:3] for _, c in lit.crop(text_box).getcolors(10000)}
    assert any(near(c, ACTIVE, 30) for c in colors_lit)
    assert not any(near(c, ACTIVE, 60) for c in colors_idle)


def test_mapping_swaps_buttons(layout):
    p = LayoutPainter(layout, mapping="south:east,east:south")
    img = p.paint(state(["south"]))
    assert near(rgb(img, 377, 124), (0xe0, 0x00, 0x00))     # east element lit
    assert near(rgb(img, 341, 160), IDLE)                   # south element idle
    p2 = LayoutPainter(layout, mapping={"south": "left_trigger"})
    img = p2.paint(state(["south"]))
    assert near(rgb(img, 75, 12), ACTIVE)                   # mapped digital -> full trigger bar
    assert near(rgb(img, 20, 214), ACTIVE)                  # and the trigger-input button


def test_trigger_fill_up_and_threshold(layout):
    p = LayoutPainter(layout)
    half = p.paint(state(left_trigger=16000))               # just below TRIGGER_PRESS_THRESHOLD
    assert near(rgb(half, 60, 30), ACTIVE)                  # lower part filled
    assert near(rgb(half, 60, 12), IDLE)                    # upper part idle
    assert closer(rgb(half, 75, 6), WELL, ACTIVE)           # outline not lit
    assert near(rgb(half, 20, 214), IDLE)                   # trigger-input button not lit
    pressed = p.paint(state(left_trigger=30000))
    assert closer(rgb(pressed, 75, 6), ACTIVE, WELL)        # lit outline past the threshold
    assert near(rgb(pressed, 20, 214), ACTIVE)
    idle = p.paint(PadState())
    assert near(rgb(idle, 60, 30), IDLE)


def test_trigger_fill_right(layout):
    p = LayoutPainter(layout)
    img = p.paint(state(right_trigger=32767 // 4))
    assert near(rgb(img, 297, 20), ACTIVE)
    assert near(rgb(img, 332, 20), IDLE)
    full = p.paint(state(right_trigger=32767))
    assert near(rgb(full, 355, 20), ACTIVE)


def test_stick_knob_moves_and_lights(layout):
    p = LayoutPainter(layout)
    idle = p.paint(PadState())
    assert near(rgb(idle, 168, 110), IDLE)                  # knob at rest
    assert near(rgb(idle, 180, 80), WELL)                   # ring well above it
    right = p.paint(state(left_x=32767))
    assert near(rgb(right, 168, 110), WELL)
    assert near(rgb(right, 208, 110), IDLE)
    up = p.paint(state(left_y=-32768))
    assert near(rgb(up, 180, 80), IDLE)
    clicked = p.paint(state(["left_stick"], left_x=32767))
    assert near(rgb(clicked, 196, 124), ACTIVE)
    # default travel = r - knob_r = 14 for the right stick
    moved = p.paint(state(right_x=32767))
    assert near(rgb(moved, 230 + 14 + 12, 180), IDLE)
    assert near(rgb(moved, 230 - 12, 180), WELL)


def test_incremental_equals_full_render(layout):
    rnd = random.Random(7)
    inc = LayoutPainter(layout)
    for _ in range(40):
        st = PadState([int(rnd.random() < 0.3) for _ in range(NUM_BUTTONS)],
                      [rnd.randint(-32768, 32767) for _ in range(4)] + [rnd.randint(0, 32767) for _ in range(2)])
        if rnd.random() < 0.3:
            st = PadState()
        assert max_diff(inc.paint(st), LayoutPainter(layout).paint(st)) <= 2


def test_visual_key_ignores_invisible_changes(layout):
    p = LayoutPainter(layout)
    assert p.visual_key(PadState()) == p.visual_key(state(["misc1"]))  # misc1 not drawn
    assert p.visual_key(PadState()) != p.visual_key(state(["south"]))


def test_paint_onto_image_with_dest(layout):
    p = LayoutPainter(layout)
    canvas = Image.new("RGBA", (500, 300), (0, 255, 0, 255))
    out = p.paint(state(["south"]), canvas, dest=(50, 30))
    assert out is canvas
    assert near(rgb(canvas, 391, 190), (0x00, 0xc0, 0x00))
    assert rgb(canvas, 10, 10) == (0, 255, 0)


def test_all_shipped_layouts_render_and_light_up():
    names = list_layouts()
    if not names:
        pytest.skip("no layouts installed")
    for name in names:
        lay = load_layout(name)
        p = LayoutPainter(lay)
        idle = p.paint(PadState())
        for e in p.elements:
            if e.kind == "stick":
                if not e.button:
                    continue
                st = state([e.button])
            elif e.input in ("left_trigger", "right_trigger"):
                st = state(**{e.input: 32767})
            else:
                st = state([e.input])
            lit = p.paint(st)
            assert max_diff(idle.crop(e.out_rect), lit.crop(e.out_rect)) > 60, \
                f"{name}: {e.input or e.button} does not light up"
        fr = FrameRenderer(lay, background="transparent")
        assert fr.render(all_pressed_state()).size == fr.size


def test_parse_color():
    assert parse_color("#fff") == (255, 255, 255, 255)
    assert parse_color("#11223344") == (0x11, 0x22, 0x33, 0x44)
    assert parse_color("none") is None


# --- HistoryPainter ---------------------------------------------------------------------

def test_history_bar_in_right_row(layout):
    rec = make_recording()
    tl = Timeline(rec)
    inputs = layout["history"]
    hp = HistoryPainter(inputs, 360, 14 * len(inputs) + 8, seconds=4.0, fps=60.0, theme=layout["theme"],
                        colors={"south": "#00c000"})
    now = 2 * S
    img = hp.paint(tl, 0, now)
    x = hp.x_for(750 * MS, now)
    top, bottom = hp.row_span("south")
    assert near(rgb(img, x, (top + bottom) / 2), (0x00, 0xc0, 0x00))
    t2, b2 = hp.row_span("east")
    assert not near(rgb(img, x, (t2 + b2) / 2), (0x00, 0xc0, 0x00), 60)
    assert not near(rgb(img, hp.x_for(1500 * MS, now), (top + bottom) / 2), (0x00, 0xc0, 0x00), 60)
    # 5 ms tap still visible (minimum 1 px), trigger ramp shows in the LT row past the threshold.
    # The 1 px bar can straddle two output pixels after the LANCZOS downscale (Pillow-version
    # dependent), so accept it in either neighbour.
    xt, yt = hp.x_for(1212 * MS, now), sum(hp.row_span("east")) / 2
    assert any(near(rgb(img, xt + dx, yt), ACTIVE, 90) for dx in (-1, 0, 1))
    lt = hp.row_span("left_trigger")
    assert near(rgb(img, hp.x_for(600 * MS, now), sum(lt) / 2), ACTIVE)


def test_history_mapping_and_held_chip(layout):
    rec = make_recording()
    tl = Timeline(rec)
    inputs = layout["history"]
    hp = HistoryPainter(inputs, 360, 14 * len(inputs) + 8, theme=layout["theme"], mapping={"south": "east"})
    img = hp.paint(tl, 0, 800 * MS)                    # south held -> shows in east row
    east = sum(hp.row_span("east")) / 2
    assert near(rgb(img, hp.x_for(700 * MS, 800 * MS), east), ACTIVE)
    chip = img.crop((0, int(hp.row_span("east")[0]) + 2, hp.lane_x0 - 2, int(hp.row_span("east")[1]) - 2))
    assert any(near(c[:3], ACTIVE, 20) for _, c in chip.getcolors(10000))


@pytest.mark.parametrize("background", ["chroma", "transparent"])
def test_history_lane_is_opaque_over_any_background(layout, background):
    fr = FrameRenderer(layout, background=background, history=True, frame_counter=True)
    img = fr.render(PadState(), frame=3, t_ns=S).convert("RGBA")
    x0, y0 = fr.history_dest
    hp = fr.history
    for name in hp.inputs[:4]:                       # striped and plain rows
        top, bottom = hp.row_span(name)
        px = img.getpixel((x0 + (hp.lane_x0 + hp.lane_x1) // 2, y0 + int((top + bottom) / 2)))
        assert px[3] == 255 and not (px[1] > 200 and px[0] < 80), px   # no chroma/alpha holes
    corner = img.getpixel((x0, y0))
    assert corner[3] <= 8 if background == "transparent" else near(corner, (0, 255, 0), 20)
    cx, cy, cw, ch = fr._counter
    assert img.getpixel((cx + cw - 3, cy + ch // 2))[3] == 255     # counter box in its own strip
    top = fr.controller_dest[1]
    assert top >= ch and near(rgb(img, 341, 160 + top), IDLE)    # controller moved below it


def test_translucent_layout_colours_blend(layout):
    lay = dict(layout)
    lay["body"] = list(layout["body"]) + [{"shape": "rect", "x": 150, "y": 190, "w": 40, "h": 20,
                                           "fill": "#ff000080", "stroke": "none"}]
    img = LayoutPainter(lay).paint(PadState())
    px = img.getpixel((170, 200))
    assert px[3] == 255 and near(px, (0x90, 0x10, 0x10), 12)   # 50 % red over #202020


def test_history_frame_grid_when_zoomed(layout):
    def line_columns(seconds: float) -> int:
        hp = HistoryPainter(["south"], 400, 30, seconds=seconds, fps=60.0, theme=layout["theme"])
        img = hp.paint(None, None, S + 5 * MS)
        row = [img.getpixel((x, 15)) for x in range(hp.lane_x0 + 1, hp.lane_x1 - 1)]
        bg = max(set(row), key=row.count)
        return sum(1 for c in row if c != bg)
    # 0.25 s = 15 frames in view -> frame grid; 4 s -> only the four 1 s ticks
    assert line_columns(0.25) >= line_columns(4.0) + 8


# --- timing ------------------------------------------------------------------------------

def test_exact_frame_timing():
    assert fps_fraction("gb") == fps_fraction(4194304 / 70224) == Fraction(262144, 4389)
    assert fps_fraction(59.94) == Fraction(2997, 50)
    assert frame_time_ns(262144, 0, "gb") == 4389 * S
    assert frame_time_ns(1, 0, 60) == 16_666_667
    assert frame_time_ns(3, 10, 60) == 50_000_010
    assert frame_count(0, 2 * S, 30) == 61
    assert frame_count(0, 2 * S - 1, 30) == 60
    assert frame_count(5, 5, 60) == 1
    with pytest.raises(ValueError):
        frame_count(10, 5, 60)


@pytest.mark.parametrize("mode", ["sample", "any"])
def test_frame_states_match_timeline(mode):
    rec = make_recording()
    ours = [st for _, st in iter_frame_states(rec, 0, 60, 0, 121, mode)]
    ref = Timeline(rec).frames(0, 60.0, mode=mode, count=121)
    assert [s.to_json() for s in ours] == [s.to_json() for s in ref]


def test_any_mode_catches_subframe_tap():
    rec = make_recording()
    east = BUTTON_INDEX["east"]
    sample = [st.buttons[east] for _, st in iter_frame_states(rec, 0, 30, 0, 61, "sample")]
    anym = [st.buttons[east] for _, st in iter_frame_states(rec, 0, 30, 0, 61, "any")]
    assert sum(sample) == 0 and sum(anym) == 1


# --- render_recording ------------------------------------------------------------------------

def test_png_sequence(tmp_path, layout):
    rec = make_recording()
    calls = []
    out = render_recording(rec, tmp_path / "frames", layout=layout, fps=30, history=False,
                           progress=lambda d, t: calls.append((d, t)))
    files = sorted(out.glob("frame_*.png"))
    assert len(files) == 61 and files[0].name == "frame_000000.png" and files[-1].name == "frame_000060.png"
    assert calls[-1] == (61, 61)
    f15 = Image.open(files[15])                    # t = 0.5 s: south just pressed
    assert f15.size == (400, 240)
    assert near(rgb(f15, 341, 160), (0x00, 0xc0, 0x00))
    assert rgb(f15, 0, 0) == (0, 255, 0)           # chroma background
    assert near(rgb(Image.open(files[14]), 341, 160), IDLE)
    assert files[0].read_bytes() == files[1].read_bytes()   # unchanged state -> identical frame


def test_png_sequence_with_history_and_counter(tmp_path, layout):
    rec = make_recording()
    out = render_recording(rec, tmp_path / "h", layout=layout, fps=10, background="transparent",
                           history=True, frame_counter=True)
    files = sorted(out.glob("*.png"))
    assert len(files) == 21
    img = Image.open(files[10])
    assert img.mode == "RGBA" and img.height > 240
    assert img.getpixel((img.width - 1, 0))[3] == 0
    assert files[0].read_bytes() != files[1].read_bytes()   # counter changes every frame


def test_gif_frame_count_and_duration(tmp_path, layout):
    rec = make_recording()
    out = render_recording(rec, tmp_path / "c.gif", layout=layout, fps=25, history=False,
                           frame_counter=True)
    with Image.open(out) as im:
        assert im.n_frames == 51
        total = 0
        for i in range(im.n_frames):
            im.seek(i)
            total += im.info["duration"]
    assert total == 51 * 40
    out2 = render_recording(rec, tmp_path / "plain.gif", layout=layout, fps=25, history=False)
    with Image.open(out2) as im:
        n = im.n_frames
        total = 0
        for i in range(n):
            im.seek(i)
            total += im.info["duration"]
    assert 3 <= n < 51 and total == 51 * 40


def test_webp_output(tmp_path, layout):
    rec = make_recording()
    out = render_recording(rec, tmp_path / "a.webp", layout=layout, fps=30, history=False,
                           background="transparent")
    with Image.open(out) as im:
        total = 0
        for i in range(im.n_frames):
            im.seek(i)
            im.load()
            total += im.info["duration"]
        assert im.mode == "RGBA"
    assert total == round(61 * 1000 / 30)


def test_start_marker_and_offset(tmp_path, layout):
    rec = make_recording()
    out = render_recording(rec, tmp_path / "m", layout=layout, fps=10, history=False,
                           start_marker="run start")
    files = sorted(out.glob("*.png"))
    assert len(files) == 16                           # 0.5 s .. 2.0 s at 10 fps
    assert near(rgb(Image.open(files[0]), 341, 160), (0x00, 0xc0, 0x00))
    out = render_recording(rec, tmp_path / "m2", layout=layout, fps=10, history=False,
                           start_marker="run", start_ns=-100 * MS, end_marker="split")
    files = sorted(out.glob("*.png"))
    assert len(files) == 12                           # 0.4 s .. 1.5 s
    assert near(rgb(Image.open(files[0]), 341, 160), IDLE)
    with pytest.raises(ValueError, match="not found"):
        render_recording(rec, tmp_path / "x", layout=layout, start_marker="nope")


def test_default_layout_from_family_and_render_frame(tmp_path):
    if "xbox" not in list_layouts():
        pytest.skip("xbox layout not installed")
    rec = make_recording()
    img = render_frame(rec, 700 * MS, background="#000000", history=False)
    assert img.size == tuple(load_layout("xbox")["size"])


def test_unsupported_output_and_transparent_mp4(tmp_path, layout):
    rec = make_recording()
    with pytest.raises(ValueError, match="unsupported output"):
        render_recording(rec, tmp_path / "x.avi", layout=layout)
    with pytest.raises(ValueError, match="alpha"):
        render_recording(rec, tmp_path / "x.mp4", layout=layout, background="transparent")


def test_missing_ffmpeg_message(tmp_path, layout, monkeypatch):
    monkeypatch.setattr(video, "find_ffmpeg", lambda explicit=None: None)
    with pytest.raises(FFmpegNotFoundError, match="imageio-ffmpeg"):
        render_recording(make_recording(), tmp_path / "x.mp4", layout=layout)


def test_long_animation_warns(tmp_path, layout):
    rec = make_recording()
    with pytest.warns(RuntimeWarning) as caught:
        render_recording(rec, tmp_path / "long.webp", layout=layout, fps=1000, history=False,
                         duration_ns=2 * S, scale=0.25)
    messages = [str(w.message) for w in caught]
    assert any("memory" in m for m in messages)            # long animation
    assert any("shorter than 11 ms" in m for m in messages)  # 1 ms frames merged


# --- review regressions -----------------------------------------------------------------------

def splits_recording() -> Recording:
    dev = DeviceInfo(0, family="xbox")
    ev = [InputEvent(0, 0, "+", None, dev.to_json()),
          InputEvent(1 * S, None, "m", None, "split:1"),
          InputEvent(3 * S, None, "m", None, "split:2"),
          InputEvent(6 * S, None, "m", None, "split:3"),
          InputEvent(9 * S, 0, "b", 0, 1)]
    return Recording({}, {0: dev}, ev)


def test_end_marker_is_searched_after_the_start_marker():
    rec = splits_recording()
    # "split" is a prefix of the start marker itself: the clip must run to the NEXT split
    assert video.resolve_range(rec, start_marker="split:2", end_marker="split") == (3 * S, 6 * S)
    # a lead-in must not make the start marker count as the end
    assert video.resolve_range(rec, start_ns=-500 * MS, start_marker="split:2",
                               end_marker="split") == (2500 * MS, 6 * S)
    assert video.resolve_range(rec, end_marker="split") == (0, 1 * S)
    assert video.resolve_range(rec, start_ns=1 * S, end_marker="split") == (1 * S, 3 * S)
    with pytest.raises(ValueError, match="not found"):          # no split after the last one
        video.resolve_range(rec, start_marker="split:3", end_marker="split")


def test_frame_counter_fits_long_runs(layout):
    long_ns = 5 * 3600 * S + 7 * S                       # a 5 h run: > 999999 frames at 60 fps
    fr = FrameRenderer(layout, history=False, frame_counter=True, duration_ns=long_ns)
    x, y, w, h = fr._counter
    last = frame_count(0, long_ns, 60) - 1
    assert last > 999_999
    l, _, r, _ = fr._mono.getbbox(fr._counter_text(last, long_ns), anchor="lm")
    assert r - l + 2 <= w                                # sized up front: never clipped
    # without the hint the box starts small and widens instead of clipping the text
    small = FrameRenderer(layout, background="#000000", history=False, frame_counter=True)
    sx, sy, sw, sh = small._counter
    img = small.render(PadState(), frame=last, t_ns=long_ns)
    tl, _, tr, _ = small._mono.getbbox(small._counter_text(last, long_ns), anchor="lm")
    assert tr - tl > sw - 4
    assert rgb(img, sx + tr - tl, sy + sh // 2) != (0, 0, 0)  # box extends past its default width
    assert rgb(img, sx + tr - tl + 20, sy + sh // 2) == (0, 0, 0)


def test_counter_hint_passed_by_render_recording(tmp_path, layout, monkeypatch):
    seen = {}
    orig = video.FrameRenderer.__init__

    def spy(self, *a, **k):
        seen.update(k)
        orig(self, *a, **k)
    monkeypatch.setattr(video.FrameRenderer, "__init__", spy)
    render_recording(make_recording(), tmp_path / "c", layout=layout, fps=10, history=False,
                     frame_counter=True, start_ns=500 * MS)
    assert seen["duration_ns"] == 1500 * MS


def test_out_of_range_stick_values_stay_in_bounds(layout):
    p = LayoutPainter(layout)
    p.paint(PadState())
    wild = state(left_x=65535, left_y=-70000)            # buggy backend / import values
    assert max_diff(p.paint(wild), LayoutPainter(layout).paint(wild)) <= 2
    assert p.visual_key(wild) == p.visual_key(state(left_x=32767, left_y=-32768))


def test_degenerate_layout_shapes_and_tiny_lanes(layout):
    from controllerlog.layouts import LayoutError
    lay = dict(layout)
    lay["body"] = list(layout["body"]) + [{"shape": "polygon", "points": []}]
    with pytest.raises(LayoutError):  # validation rejects polygons with < 3 points up front
        LayoutPainter(lay)
    for w, h in ((8, 8), (300, 8), (8, 200), (1, 1)):
        hp = HistoryPainter(["south", "east", "dpad_up"], w, h)
        assert hp.lane_x1 > hp.lane_x0
        hp.paint(None, None, S)
    tiny = {"size": [10, 10], "elements": [{"type": "button", "input": "south", "shape": "circle",
                                            "cx": 5, "cy": 5, "r": 4}]}
    fr = FrameRenderer(tiny, history=True, frame_counter=True)
    assert fr.render(all_pressed_state(), frame=5, t_ns=S).size == fr.size


def test_duplicate_history_rows_are_merged(layout):
    from controllerlog.layouts import LayoutError
    lay = dict(layout)
    lay["history"] = ["south", "east", "south"]
    with pytest.raises(LayoutError):  # layouts with duplicate history rows are invalid
        FrameRenderer(lay, history=True)
    assert HistoryPainter(["south", "south"], 200, 30).inputs == ["south"]  # painter still dedups


def test_stick_label_defaults_to_knob_size(layout):
    lay = dict(layout)
    lay["elements"] = [{"type": "stick", "x_axis": "left_x", "y_axis": "left_y", "cx": 50, "cy": 50,
                        "r": 20, "knob_r": 8, "label": "L"},
                       {"type": "stick", "x_axis": "right_x", "y_axis": "right_y", "cx": 150, "cy": 50,
                        "r": 20, "knob_r": 8, "label": "R", "label_size": 11}]
    p = LayoutPainter(lay)
    assert [e.label_size for e in p.elements] == [8, 11]   # same rule as the web overlay


def test_rational_fps_strings():
    assert fps_fraction("60000/1001") == Fraction(60000, 1001)
    assert frame_time_ns(60000, 0, "60000/1001") == 1001 * S
    assert frame_time_ns(1, 0, "60000/1001") == 16_683_333          # 16683333.33 ns


# --- ffmpeg (only when available) -------------------------------------------------------------

FFMPEG = find_ffmpeg()
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="no ffmpeg (pip install imageio-ffmpeg)")


def probe(path: Path) -> tuple[str, float, int]:
    """(ffmpeg -i stderr, duration seconds, decoded frame count)."""
    info = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", info)
    assert m, info
    dur = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    dec = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path), "-map", "0:v:0", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    frames = int(re.findall(r"frame=\s*(\d+)", dec)[-1])
    return info, dur, frames


@needs_ffmpeg
def test_mp4_end_to_end(tmp_path, layout):
    rec = make_recording()
    out = render_recording(rec, tmp_path / "run.mp4", layout=layout, fps=30, history=True)
    info, dur, frames = probe(out)
    assert "h264" in info and "yuv420p" in info
    assert frames == 61
    assert abs(dur - 61 / 30) < 0.05
    size = re.search(r", (\d+)x(\d+)", info)
    assert size and int(size[1]) % 2 == 0 and int(size[2]) % 2 == 0
    assert out.stat().st_size > 1000
    assert "bt709" in info
    png = tmp_path / "f15.png"                       # frame 15 = 0.5 s: south pressed
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(out), "-vf", "select=eq(n\\,15)", "-frames:v", "1",
                    str(png)], check=True)
    img = Image.open(png).convert("RGB")
    assert near(img.getpixel((1, 1)), (0, 255, 0), 12)          # chroma key survives yuv420p
    assert near(rgb(img, 341, 160), (0x00, 0xc0, 0x00), 16)


@needs_ffmpeg
def test_mp4_gameboy_rate(tmp_path, layout):
    rec = make_recording()
    out = render_recording(rec, tmp_path / "gb.mp4", layout=layout, fps="gb", history=False)
    info, dur, frames = probe(out)
    assert frames == frame_count(0, 2 * S, "gb") == 120
    assert "59.73 fps" in info


@needs_ffmpeg
def test_transparent_webm_and_mov(tmp_path, layout):
    rec = make_recording()
    webm = render_recording(rec, tmp_path / "a.webm", layout=layout, fps=20, background="transparent",
                            history=False)
    info, _, frames = probe(webm)
    assert "vp9" in info and "alpha_mode" in info and frames == 41
    mov = render_recording(rec, tmp_path / "a.mov", layout=layout, fps=20, background="transparent",
                           history=False)
    info, _, frames = probe(mov)
    assert "qtrle" in info and "argb" in info and frames == 41
    # decode one frame back and check the south button is lit at 0.5 s (frame 10)
    png = tmp_path / "f10.png"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(mov), "-vf", "select=eq(n\\,10)", "-frames:v", "1",
                    str(png)], check=True)
    img = Image.open(png).convert("RGBA")
    assert img.getpixel((0, 0))[3] == 0
    assert near(rgb(img, 341, 160), (0x00, 0xc0, 0x00))


@needs_ffmpeg
def test_interrupted_video_leaves_no_partial_file(tmp_path, layout, monkeypatch):
    def stop(done: int, total: int) -> None:
        if done > 20:
            raise KeyboardInterrupt
    out = tmp_path / "cut.mp4"
    with pytest.raises(KeyboardInterrupt):
        render_recording(make_recording(), out, layout=layout, fps=60, progress=stop)
    assert not out.exists()                          # unplayable (no moov atom): removed
    # a file ffmpeg never opened (it failed first) is left alone
    keep = tmp_path / "keep.mp4"
    keep.write_bytes(b"previous render")
    monkeypatch.setattr(video, "codec_args", lambda *a, **k: ["-c:v", "no_such_encoder_xyz"])
    with pytest.raises(RuntimeError, match="ffmpeg"):
        render_recording(make_recording(), keep, layout=layout, fps=30)
    assert keep.read_bytes() == b"previous render"
