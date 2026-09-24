"""Offline input-display renderer: controller layouts drawn with Pillow, encoded to
PNG sequences, GIF/WebP or video (ffmpeg)."""

from __future__ import annotations

from .draw import HistoryPainter, LayoutPainter, normalize_layout, parse_color
from .video import (FFmpegNotFoundError, FrameRenderer, find_ffmpeg, fps_fraction, frame_count,
                    frame_time_ns, render_frame, render_recording)

__all__ = ["LayoutPainter", "HistoryPainter", "FrameRenderer", "render_recording", "render_frame",
           "find_ffmpeg", "FFmpegNotFoundError", "fps_fraction", "frame_time_ns", "frame_count",
           "normalize_layout", "parse_color"]
