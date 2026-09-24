"""Command-line interface: ``controllerlog <command>`` (or ``python -m controllerlog``)."""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import __version__
from .model import BUTTONS, NUM_BUTTONS, TRIGGER_PRESS_THRESHOLD

DEFAULT_PORT = 8765
DEFAULT_REC_DIR = Path("recordings")


def _die(msg: str, code: int = 2) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return code


def _parse_meta(pairs: list[str] | None) -> dict[str, str]:
    meta: dict[str, str] = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"error: --meta expects key=value, got {p!r}")
        k, v = p.split("=", 1)
        meta[k.strip()] = v.strip()
    return meta


def _fps(value: str) -> float:
    from .timeline import resolve_fps
    try:
        return resolve_fps(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"bad fps {value!r} (a positive number, num/den, or "
                                         "gb/gba/nes/snes/60)") from None


def _fps_text(value: str) -> str:
    """Validated like --fps but kept as text, so the renderer can resolve rationals exactly."""
    _fps(value)
    try:
        from .render.video import fps_fraction
        fps_fraction(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"bad fps {value!r}") from None
    return value


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a port number: {value!r}") from None
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"port must be 0-65535, got {value}")
    return port


def _is_ctlog_name(name: str | Path) -> bool:
    return str(name).lower().endswith((".ctlog", ".ctlog.gz"))


def _rec_fps(rec) -> float:
    """The recording's native frame rate (meta.fps of converted .gm2/.bk2/.csv), else 60."""
    import math
    v = rec.meta.get("fps")
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0:
        return float(v)
    return 60.0


def _check_device(rec, device: int | None) -> None:
    """A --device that has no input in ``rec`` is an error, not an empty result."""
    if device is None:
        return
    with_input = sorted({e.device for e in rec.input_events() if e.device is not None})
    if device not in with_input:
        raise ValueError(f"device {device} has no input events in this recording "
                         f"(devices with input: {', '.join(map(str, with_input)) or 'none'})")


def _load_any(path: Path, args: argparse.Namespace | None = None):
    """Load a .ctlog/.gm2/.bk2/.csv file as a Recording (plus the native object)."""
    suffixes = "".join(path.suffixes).lower()
    if path.is_dir() and not suffixes.endswith(".gm2"):   # read_gm2 explains folders itself
        raise ValueError(f"{path} is a folder, expected a recording or movie file")
    if suffixes.endswith(".gm2"):
        from .formats import gm2
        mv = gm2.read_gm2(path)
        if mv.truncated:
            print("warning: .gm2 body is truncated (GSE crashed or still writing); "
                  "using the frames that could be recovered", file=sys.stderr)
        return gm2.gm2_to_recording(mv, source=str(path)), mv
    if suffixes.endswith(".bk2"):
        from .formats import bk2
        from . import tas
        movie = bk2.read_bk2(path)
        return tas.to_recording(bk2.to_frame_movie(movie)), movie
    if suffixes.endswith(".csv"):
        from . import tas
        # Without --fps, the table's own time_ms column gives its rate (else 60).
        fm = tas.load_csv(path, getattr(args, "fps", None))
        return tas.to_recording(fm), fm
    from .logfile import read_log
    return read_log(path), None


def _native_frame_movie(native):
    """The FrameMovie of a loaded .gm2/.bk2/.csv (None for a .ctlog recording)."""
    from . import tas
    from .formats import bk2, gm2
    if isinstance(native, tas.FrameMovie):
        return native
    if isinstance(native, gm2.Gm2Movie):
        return gm2.to_frame_movie(native)
    if isinstance(native, bk2.Bk2Movie):
        return bk2.to_frame_movie(native)
    return None


def _default_recording_path(rec_dir: Path, meta: dict[str, str]) -> Path:
    stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    game = meta.get("game")
    safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in game).strip() if game else ""
    return rec_dir / (f"{stamp}_{safe}.ctlog" if safe else f"{stamp}.ctlog")


# --- devices -------------------------------------------------------------------------

def cmd_devices(args: argparse.Namespace) -> int:
    from .input.sdl3 import SDLError
    from .input.sdl3_backend import list_gamepads
    from .input.sdl3 import hints_with
    out: dict[str, Any] = {}
    try:
        version, pads = list_gamepads(hints=hints_with(enhanced_reports=not args.no_enhanced),
                                      settle_s=args.settle)
        out["sdl"] = {"version": version, "gamepads": pads}
    except SDLError as e:
        out["sdl"] = {"error": str(e)}
    try:
        from .output.virtual_pad import virtual_pad_unavailable_reason
        reason = virtual_pad_unavailable_reason()
        out["vigem"] = "available" if reason is None else f"unavailable ({reason})"
    except Exception as e:  # module import problems
        out["vigem"] = f"unavailable ({e})"
    if args.adb is not None:
        try:
            from .input import adb_backend
            adb = adb_backend.resolve_adb()
            out["adb"] = []
            for serial, state, model in adb_backend.list_adb_devices(adb):
                entry: dict[str, Any] = {"serial": serial, "state": state, "model": model}
                if state != "device":
                    entry["hint"] = ("accept the USB debugging prompt on the phone" if state ==
                                     "unauthorized" else "reconnect the phone / restart adb")
                else:
                    try:
                        inputs = adb_backend.list_input_devices(serial, adb=adb)
                        entry["gamepads"] = [i.name for i in inputs if i.is_gamepad]
                    except Exception as e:
                        entry["error"] = str(e)
                out["adb"].append(entry)
        except Exception as e:
            out["adb"] = {"error": str(e)}
    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return 0
    sdl = out["sdl"]
    if "error" in sdl:
        print(f"SDL3: {sdl['error']}")
    else:
        print(f"SDL {sdl['version']}: {len(sdl['gamepads'])} controller(s)")
        for p in sdl["gamepads"]:
            if "error" in p:
                print(f"  #{p['instance_id']}: could not open ({p['error']})")
                continue
            print(f"  #{p['instance_id']}: {p['name']}  [{p['sdl_type'] or 'unknown type'} -> "
                  f"{p['family']} layout, {p['connection']}, "
                  f"VID {p['vendor_id']:04x} PID {p['product_id']:04x}]")
        if not sdl["gamepads"]:
            print("  (none) - pair a controller in Windows Settings > Bluetooth & devices, "
                  "or plug it in, then run this again")
    try:
        from .input.switch2_usb import Switch2UsbUnavailable, find_controllers
        s2 = find_controllers()
        if s2:
            print(f"Switch 2 over USB: {len(s2)} controller(s)")
            for f in s2:
                print(f"  {f.model}: VID 057e PID {f.product_id:04x}  (read by `live` and "
                      f"`switch2 usb`)")
    except Switch2UsbUnavailable:
        pass
    print(f"Virtual controller (ViGEmBus): {out['vigem']}")
    if "adb" in out:
        print(f"Android (adb): {json.dumps(out['adb'], default=str)}")
    return 0


# --- live ------------------------------------------------------------------------------

class _StatusPrinter:
    """Single console status line: which buttons are held right now."""

    def __init__(self, hub) -> None:
        self.hub = hub
        self.last = ""

    def line(self, recorder) -> str:
        parts = []
        for dev_id, (info, st) in self.hub.snapshot().items():
            held = [BUTTONS[i] for i in range(NUM_BUTTONS) if st.buttons[i]]
            for ax, name in ((4, "left_trigger"), (5, "right_trigger")):
                if st.axes[ax] >= TRIGGER_PRESS_THRESHOLD:
                    held.append(name)
            sticks = []
            for (x, y), name in (((0, 1), "L"), ((2, 3), "R")):
                if abs(st.axes[x]) > 8000 or abs(st.axes[y]) > 8000:
                    sticks.append(f"{name}({st.axes[x] / 32768:+.2f},{st.axes[y] / 32768:+.2f})")
            parts.append(f"#{dev_id} {info.name[:22]}: {' '.join(held + sticks) or '-'}")
        rec = ""
        if recorder is not None:
            rec = f" | REC {recorder.elapsed_ns / 1e9:7.1f}s {recorder.writer.count} ev"
        return (" || ".join(parts) or "waiting for controllers...") + rec

    def print(self, recorder) -> None:
        text = self.line(recorder)
        width = max(40, (os.get_terminal_size().columns if sys.stdout.isatty() else 120) - 1)
        text = text[:width]
        if text != self.last:
            sys.stdout.write("\r" + text.ljust(len(self.last)))
            sys.stdout.flush()
            self.last = text


def _read_key() -> str | None:
    if sys.platform != "win32" or not sys.stdin.isatty():
        return None
    import msvcrt
    if msvcrt.kbhit():
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            msvcrt.getwch()
            return None
        return ch
    return None


def cmd_live(args: argparse.Namespace) -> int:
    from .hub import Hub, Recorder
    from .layouts import parse_map

    meta = _parse_meta(args.meta)
    hub = Hub()
    backends = []
    if not args.no_sdl:
        from .input.sdl3 import hints_with
        from .input.sdl3_backend import SDL3Backend
        backends.append(SDL3Backend(hub, hints=hints_with(enhanced_reports=not args.no_enhanced)))
    if args.adb is not None:
        from .input.adb_backend import AdbBackend
        backends.append(AdbBackend(hub, serial=args.adb or None, device_filter=args.adb_filter))
    if not args.no_sdl and not args.no_switch2_usb:
        # Switch 2 GameCube / Pro over USB-C (SDL's official build can't read them).
        from .input.switch2_usb import Switch2UsbBackend, usb_available
        if usb_available():
            backends.append(Switch2UsbBackend(hub))
    if args.switch2 is not None:
        from .input.switch2_ble import Switch2BleBackend
        backends.append(Switch2BleBackend(hub, address=args.switch2 or None,
                                          model=args.switch2_model,
                                          orientation=args.switch2_orientation,
                                          deadzone=args.switch2_deadzone))
    if not backends:
        return _die("nothing to capture: remove --no-sdl or add --adb")
    for b in backends:
        b.start()
    for b in backends:
        b.ready.wait(10)
        if b.error:
            for other in backends:
                other.stop()
            return _die(f"{b.name} backend failed: {b.error}")

    recorder = None
    server = None
    bridge = None
    try:
        if args.record is not None:
            path = Path(args.record) if args.record else _default_recording_path(Path(args.dir), meta)
            recorder = Recorder(hub, path, meta=meta)
            print(f"Recording to {path}")
        if not args.no_overlay:
            from .overlay.server import OverlayServer
            server = OverlayServer(hub, host=args.host, port=args.port,
                                   recordings_dir=Path(args.dir), **_host_kw(args))
            server.start()
            print(f"Overlay:  {server.overlay_url(layout='auto')}    "
                  f"(OBS: add a Browser Source with this URL)")
            print(f"Viewer:   {server.viewer_url()}")
            if args.open:
                import webbrowser
                webbrowser.open(server.overlay_url(layout="auto"))
        if args.bridge:
            from .bridge import Bridge
            bridge = Bridge(hub, source_device=args.bridge_device, target=args.bridge,
                            remap=parse_map(args.bridge_map) or None)
            bridge.start()
            print(f"Bridge:   mirroring your controller to a virtual {args.bridge.upper()} pad "
                  f"(hide the real one from games with HidHide to avoid double input)")
        keys = "m=marker  s=split  r=reset  q=quit" if sys.platform == "win32" else "Ctrl+C to quit"
        print(f"Keys: {keys}")
        status = _StatusPrinter(hub)
        splits = 0
        while True:
            for b in backends:
                if b.error:
                    raise RuntimeError(f"{b.name} backend stopped: {b.error}")
            key = _read_key()
            if key:
                k = key.lower()
                if k == "q":
                    break
                label = {"m": "marker", "r": "reset"}.get(k)
                if k == "s":
                    splits += 1
                    label = f"split:{splits}"
                if label:
                    hub.mark(label)
                    sys.stdout.write(f"\r[{label}]".ljust(len(status.last)) + "\n")
            if not args.quiet:
                status.print(recorder)
            time.sleep(0.03)
    except KeyboardInterrupt:
        pass
    finally:
        print()
        if recorder is not None:
            recorder.writer.flush()  # the run's tail is on disk even if cleanup is cut short
        try:
            if bridge is not None:
                bridge.stop()
            if server is not None:
                server.stop()
            for b in backends:
                b.stop()
        finally:  # a second Ctrl+C during the (slow) stops above still closes the recording
            if recorder is not None:
                recorder.close()
                print(f"Saved {recorder.path} ({recorder.writer.count} events)")
    return 0


def _host_kw(args: argparse.Namespace) -> dict[str, Any]:
    """Extra OverlayServer arguments from --allow-host."""
    hosts = getattr(args, "allow_host", None)
    return {"allowed_hosts": hosts} if hosts else {}


# --- analysis ---------------------------------------------------------------------------

def cmd_stats(args: argparse.Namespace) -> int:
    from .timeline import Timeline
    rec, _ = _load_any(Path(args.file), args)
    _check_device(rec, args.device)
    tl = Timeline(rec)
    devices = [args.device] if args.device is not None else tl.devices
    fps = args.fps or _rec_fps(rec)
    result = {}
    for d in devices:
        result[d] = tl.stats(d, fps=fps)
    if args.json:
        print(json.dumps({"fps": fps, "duration_s": rec.duration_ns / 1e9,
                          "devices": {str(k): v for k, v in result.items()}}, indent=2))
        return 0
    print(f"{args.file}: {rec.duration_ns / 1e9:.3f} s, {len(rec.events)} events, "
          f"frame rate {fps:.4f} fps")
    for m in rec.markers():
        print(f"  marker {m.t_ns / 1e9:10.3f}s  frame {int(m.t_ns * fps // 1e9):7d}  {m.value}")
    for d, stats in result.items():
        info = rec.devices.get(d)
        print(f"\nDevice #{d}: {info.name if info else '?'}")
        print(f"  {'input':16s} {'presses':>8s} {'hold min':>9s} {'mean':>7s} {'max':>8s} "
              f"{'total':>9s} {'peak/s':>7s}   (hold times in frames)")
        for name, s in stats.items():
            print(f"  {name:16s} {s['presses']:8d} {s['hold_frames_min']:9.2f} "
                  f"{s['hold_frames_mean']:7.2f} {s['hold_frames_max']:8.2f} "
                  f"{s['held_frames_total']:9.1f} {s['peak_mash_hz']:7.1f}")
    return 0


def cmd_view(args: argparse.Namespace) -> int:
    from .hub import Hub
    from .overlay.server import OverlayServer
    rec_dir = Path(args.dir) if args.dir else None
    name = args.file
    convert_hint = ("the viewer opens .ctlog recordings; convert other files first with "
                    "`controllerlog convert IN OUT.ctlog`")
    if name:
        p = Path(name)
        in_dir = ((rec_dir or DEFAULT_REC_DIR) / name).is_file()
        # A path (or a file in the current folder) rather than a name inside --dir:
        # serve its folder, so the viewer's ?file= is a plain name the server accepts.
        if any(s in name for s in "/\\:") or (p.is_file() and not in_dir):
            if not p.is_file():
                return _die(f"no such recording: {name}")
            if not _is_ctlog_name(p.name):
                return _die(convert_hint)
            parent = p.resolve().parent
            if rec_dir is not None and rec_dir.resolve() != parent:
                return _die(f"{name} is not inside --dir {rec_dir}; drop --dir, or give just the "
                            "recording's name")
            rec_dir, name = parent, p.name
        else:
            if not _is_ctlog_name(name):
                return _die(convert_hint)
            if not in_dir:
                return _die(f"no recording {name!r} in {rec_dir or DEFAULT_REC_DIR}")
    rec_dir = rec_dir or DEFAULT_REC_DIR
    if rec_dir.exists() and not rec_dir.is_dir():
        return _die(f"--dir {rec_dir} is not a folder")
    rec_dir.mkdir(parents=True, exist_ok=True)
    hub = Hub()
    server = OverlayServer(hub, host=args.host, port=args.port, recordings_dir=rec_dir,
                           **_host_kw(args))
    server.start()
    url = server.viewer_url(name) if name else server.viewer_url()
    print(f"Recording viewer: {url}   (serving {rec_dir.resolve()}; Ctrl+C to stop)")
    if not args.no_open:
        import webbrowser
        webbrowser.open(url)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


# --- replay / render ------------------------------------------------------------------

def _first_frame_at(native, fm, t_ns: int) -> int:
    """Index of the first frame of ``fm`` starting at or after recording time ``t_ns``."""
    from . import tas
    from .formats import gm2
    if isinstance(native, gm2.Gm2Movie):  # GSE frames have their own (budget-based) times
        return next((f.index for f in native.frames() if f.t_ns >= t_ns), len(fm))
    return tas.frames_before(t_ns, fm.fps, 0)


def cmd_replay(args: argparse.Namespace) -> int:
    from .replay import find_marker, replay_frames, replay_recording
    path = Path(args.file)
    rec, native = _load_any(path, args)
    _check_device(rec, args.device)
    status = (lambda s: print(s)) if not args.quiet else None
    fm = _native_frame_movie(native)
    if fm is not None or args.frames:
        from . import tas
        if fm is not None:  # .gm2 / .bk2 / .csv: already one entry per emulated frame
            if args.from_marker is not None:
                m = find_marker(rec, args.from_marker)
                fm.frames = fm.frames[_first_frame_at(native, fm, m.t_ns):]
        else:
            fm = tas.from_recording(rec, device=args.device, fps=args.fps or _rec_fps(rec),
                                    mode="any", start_marker=args.from_marker)
        report = replay_frames(fm.frames, fm.fps, target=args.target, countdown_s=args.countdown,
                               on_status=status, speed=args.speed)
    else:
        report = replay_recording(rec, device=args.device, target=args.target, speed=args.speed,
                                  start_marker=args.from_marker, countdown_s=args.countdown,
                                  on_status=status)
    print(f"Replay finished: {report.summary()}")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    from .layouts import parse_map
    from .render.video import render_recording
    args.fps_text = args.fps          # the renderer resolves names/rationals exactly
    args.fps = _fps(args.fps)
    rec, _ = _load_any(Path(args.file), args)
    _check_device(rec, args.device)
    last = [0.0]

    def progress(done: int, total: int) -> None:
        now = time.monotonic()
        if now - last[0] > 0.25 or done == total:
            last[0] = now
            sys.stdout.write(f"\rrendering {done}/{total} frames ({done * 100 // max(1, total)}%)")
            sys.stdout.flush()

    extra: dict[str, Any] = {}
    if args.end_marker:
        extra["end_marker"] = args.end_marker
    if args.duration is not None:
        extra["duration_ns"] = round(args.duration * 1e9)
    if args.codec:
        extra["codec"] = args.codec
    if args.crf is not None:
        extra["crf"] = args.crf
    out = render_recording(rec, Path(args.out), layout=args.layout, device=args.device,
                           fps=args.fps_text or args.fps, start_marker=args.from_marker,
                           start_ns=round(args.start * 1e9),
                           end_ns=round(args.end * 1e9) if args.end is not None else None,
                           scale=args.scale, background=args.bg, history=not args.no_history,
                           history_seconds=args.history_seconds,
                           frame_counter=args.frame_counter, mapping=parse_map(args.map) or None,
                           mode=args.mode, progress=progress, **extra)
    print(f"\nWrote {out}")
    return 0


# --- conversion / TAS ------------------------------------------------------------------

def _to_frame_movie(path: Path, args: argparse.Namespace):
    from . import tas
    rec, native = _load_any(path, args)
    fm = _native_frame_movie(native)
    if fm is not None:  # .gm2 / .bk2 (native kept for writing back) or .csv
        return fm, (None if isinstance(native, tas.FrameMovie) else native)
    _check_device(rec, args.device)
    return tas.from_recording(rec, device=args.device, fps=args.fps or _rec_fps(rec),
                              mode=args.mode, start_marker=args.from_marker,
                              system=args.system), None


def _save_frame_movie(fm, out: Path, args: argparse.Namespace, native=None) -> None:
    from . import tas
    suffix = "".join(out.suffixes).lower()
    if suffix.endswith(".csv"):
        tas.save_csv(fm, out)
    elif suffix.endswith(".bk2"):
        from .formats import bk2, gm2
        sha1 = getattr(args, "sha1", None) or ""
        if isinstance(native, bk2.Bk2Movie):
            # Edit the source movie in place: save RAM, Power frames, SHA1, sync settings,
            # comments and extra lumps all survive; only player-1 buttons change.
            movie = bk2.apply_frame_movie(native, fm, sha1=sha1 or None)
        elif isinstance(native, gm2.Gm2Movie):
            # Start from the faithful conversion (save RAM, resets as Power, RTC, BIOS mode).
            base, warnings = bk2.from_gm2(native, sha1=sha1)
            for w in warnings:
                print(f"warning: {w}", file=sys.stderr)
            movie = bk2.apply_frame_movie(base, fm)
        else:
            movie = bk2.from_frame_movie(fm, system=args.system if args.system != "generic"
                                         else fm.system,
                                         game_name=fm.meta.get("game", ""), sha1=sha1)
        bk2.write_bk2(out, movie)
    elif suffix.endswith(".gm2"):
        from .formats import gm2
        template = native if isinstance(native, gm2.Gm2Movie) else None
        if template is None:
            if getattr(args, "template", None):
                template = gm2.read_gm2(args.template)
                fm = fm.copy()
                fm.origin = None  # frames aren't linked to the template: write back by position
        if template is None:
            raise SystemExit("error: writing .gm2 needs --template <GSE .gm2> (for the header and "
                             "start save/savestate); GSE logs can't be synthesized in sync from "
                             "scratch - see docs/FORMATS.md")
        gm2.write_gm2(out, gm2.apply_frame_movie(template, fm))
    elif suffix.endswith(".ctlog") or suffix.endswith(".ctlog.gz"):
        tas.to_recording(fm).save(out)
    else:
        raise SystemExit(f"error: don't know how to write {out.name} (use .ctlog/.csv/.bk2/.gm2)")


def cmd_convert(args: argparse.Namespace) -> int:
    src, out = Path(args.input), Path(args.output)
    in_suffix = "".join(src.suffixes).lower()
    out_suffix = "".join(out.suffixes).lower()
    out_ctlog = _is_ctlog_name(out_suffix)
    if _is_ctlog_name(in_suffix) and out_ctlog:
        # Re-save / (de)compress a recording as is: no quantization, every device and marker.
        from .logfile import read_log
        read_log(src).save(out)
    elif in_suffix.endswith((".gm2", ".bk2")) and out_ctlog:
        rec, _ = _load_any(src, args)
        rec.save(out)
    elif in_suffix.endswith(".gm2") and out_suffix.endswith(".bk2"):
        from .formats import bk2, gm2
        movie, warnings = bk2.from_gm2(gm2.read_gm2(src), sha1=args.sha1 or "")
        bk2.write_bk2(out, movie)
        for w in warnings:
            print(f"warning: {w}", file=sys.stderr)
        if not args.sha1:
            print("note: pass --sha1 <ROM SHA1> so BizHawk can confirm the ROM", file=sys.stderr)
    else:
        fm, native = _to_frame_movie(src, args)
        _save_frame_movie(fm, out, args, native)
    print(f"Wrote {out}")
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    from . import tas
    src, out = Path(args.input), Path(args.output)
    # The script file first, then each --do; with no file the first --do is line 1.
    parts = ([tas.read_text(args.script).rstrip("\r\n")] if args.script else []) + list(args.do or [])
    script = "\n".join(parts)
    if not script.strip():
        return _die("give edits with --script FILE or --do 'hold 10-20 east'")
    fm, native = _to_frame_movie(src, args)
    edited = tas.apply_script(fm, script)
    _save_frame_movie(edited, out, args, native)
    changes = tas.diff(fm, edited)
    print(f"Wrote {out}: {len(edited)} frames, {len(changes)} frame(s) differ from the input"
          + (f" (length {len(fm)} -> {len(edited)})" if len(fm) != len(edited) else ""))
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    from . import tas
    a, _ = _to_frame_movie(Path(args.a), args)
    b, _ = _to_frame_movie(Path(args.b), args)
    diffs = tas.diff(a, b)
    print(f"A: {len(a)} frames, B: {len(b)} frames, {len(diffs)} frame(s) differ")
    for d in diffs[: args.limit]:
        print(f"  frame {d.frame:7d} ({d.frame / a.fps:9.3f}s): "
              f"only A: {','.join(d.only_a) or '-':24s} only B: {','.join(d.only_b) or '-'}")
    if len(diffs) > args.limit:
        print(f"  ... {len(diffs) - args.limit} more (use --limit)")
    return 0


def cmd_align(args: argparse.Namespace) -> int:
    from .analysis import align, shifted
    from .layouts import parse_map
    if args.out and not _is_ctlog_name(args.out):
        return _die("--out writes a .ctlog recording; use a .ctlog or .ctlog.gz name")
    host, _ = _load_any(Path(args.host), args)
    _check_device(host, args.device)
    emu, _ = _load_any(Path(args.emulator), args)
    al = align(host, emu, host_device=args.device, fps=args.fps,
               mapping=parse_map(args.map) or None, window_frames=args.window)
    s = al.summary(args.fps)
    if args.json:
        print(json.dumps(s, indent=2))
    else:
        for w in al.warnings:
            print(f"warning: {w}", file=sys.stderr)
        r = s["residual_frames"]
        print(f"Offset {s['offset_s']:+.4f} s, clock ratio {s['clock_ratio']:.6f} "
              f"({s['drift_ms_per_hour']:+.1f} ms/hour drift)")
        if al.mapping:
            print(f"Button mapping used (host -> emulator): {al.mapping}")
        print(f"Matched {s['matched']} of {s['host_presses']} physical presses "
              f"({s['match_rate'] * 100:.1f}%); emulator saw {s['emu_presses']} presses")
        print(f"Press timing vs frames: median {r['median']:+.2f}, p5..p95 spread {r['spread_p5_p95']:.2f} frames")
        frame_ms = 1000 / args.fps
        if al.host_only:
            print(f"\n{len(al.host_only)} physical press(es) the emulator never registered:")
            for b, t, hold in al.host_only[: args.limit]:
                why = "shorter than a frame" if hold < frame_ms * 1e6 else "not seen (lag/pause/binding?)"
                print(f"  {t / 1e9:10.3f}s  {b:14s} held {hold / 1e6:6.1f} ms  ({why})")
        if al.emu_only:
            print(f"\n{len(al.emu_only)} emulator press(es) with no matching physical press "
                  f"(keyboard, another device, or edits):")
            for b, t in al.emu_only[: args.limit]:
                print(f"  frame {int(t * args.fps // 1e9):7d}  {b}")
    if args.out:
        shifted(host, al).save(args.out)
        # With --json, stdout is only the JSON document.
        print(f"\nWrote {args.out}: the host recording re-timed onto the emulator clock. To "
              f"compare them in `controllerlog view`, convert the emulator log to .ctlog first "
              f"(`controllerlog convert EMU.gm2 emu.ctlog`)",
              file=sys.stderr if args.json else sys.stdout)
    return 0


def cmd_gm2(args: argparse.Namespace) -> int:
    from .formats import gm2
    if args.gm2_cmd == "list":
        d = Path(args.dir) if args.dir else gm2.default_log_dir()
        files = sorted(d.glob("*.gm2"), key=lambda p: p.stat().st_mtime, reverse=True) if d.exists() else []
        print(f"{d}: {len(files)} input log(s)")
        for p in files[: args.limit]:
            print(f"  {p.name}  ({p.stat().st_size:,} bytes)")
        return 0
    mv = gm2.read_gm2(args.file)
    h = mv.header
    frames = mv.frames()
    info = {
        "file": args.file, "gm2_version": h.version, "platform": h.platform_name,
        "rom_name": h.rom_name, "emulator": f"GSE {h.emu_version}",
        "started": _dt.datetime.fromtimestamp(h.start_timestamp, _dt.timezone.utc).isoformat()
        if h.start_timestamp else None,
        "starts_from_savestate": h.starts_from_savestate, "start_blob_bytes": len(mv.blob),
        "records": len(mv.records), "frames": len(frames),
        "resets": sum(1 for _, b in mv.records if b & gm2.HARD_RESET),
        "duration_s": round(mv.duration_ns / 1e9, 3), "truncated": mv.truncated,
    }
    if args.json:
        print(json.dumps(info, indent=2))
    else:
        for k, v in info.items():
            print(f"{k:22s} {v}")
        if args.frames:
            for fr in frames[: args.frames]:
                print(f"  frame {fr.index:7d}  {fr.t_ns / 1e9:9.3f}s  "
                      f"{'RESET ' if fr.reset_before else ''}{' '.join(fr.names()) or '.'}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import format_checks, run_checks
    checks = run_checks()
    if args.json:
        print(json.dumps([c.__dict__ for c in checks], indent=2))
    else:
        print(format_checks(checks))
    return 0


def cmd_layouts(args: argparse.Namespace) -> int:
    from .layouts import list_layouts, load_layout
    for name in list_layouts():
        try:
            lay = load_layout(name)
            print(f"{name:12s} {lay.get('title', '')}  (families: {', '.join(lay.get('families', []))})")
        except Exception as e:
            print(f"{name:12s} INVALID: {e}")
    return 0


# --- parser ---------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="controllerlog", description=(
        "Connect, log, display, replay and TAS-edit game controller input."))
    p.add_argument("--version", action="version", version=f"controllerlog {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="cmd", required=True)

    from .input.switch2_ble import DEFAULT_DEADZONE, MODELS, ORIENTATIONS, _deadzone_arg

    def frames_opts(sp):
        sp.add_argument("--fps", type=_fps, default=None,
                        help="frame rate: number or gb/gba/nes/snes (default: the file's native "
                             "rate, else 60)")
        sp.add_argument("--device", type=int, default=None, help="device id (default: most active)")
        sp.add_argument("--from-marker", default=None, help="start at the first marker with this label/prefix")
        sp.add_argument("--mode", choices=("any", "sample"), default="any",
                        help="frame quantization: any = keep sub-frame taps (default), sample = state at frame start")
        sp.add_argument("--system", default="generic", help="target system for bk2 export: gb, gbc, gba, nes, snes...")

    sp = sub.add_parser("devices", help="list connected controllers and what each backend can see")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--settle", type=float, default=1.0, help="seconds to wait for controller handshakes")
    sp.add_argument("--adb", nargs="?", const="", default=None, help="also list Android devices via adb")
    sp.add_argument("--no-enhanced", action="store_true",
                    help="don't switch PlayStation/Switch pads to enhanced reports while listing "
                         "(as `live --no-enhanced`; keeps DirectInput working for non-SDL games)")
    sp.set_defaults(func=cmd_devices)

    sp = sub.add_parser("live", help="capture controllers: live overlay, optional recording and virtual-pad bridge")
    sp.add_argument("--record", nargs="?", const="", default=None, metavar="FILE",
                    help="record to FILE (default: recordings/<timestamp>.ctlog)")
    sp.add_argument("--dir", default=str(DEFAULT_REC_DIR), help="recordings folder (default %(default)s)")
    sp.add_argument("--meta", action="append", metavar="KEY=VALUE", help="recording metadata, e.g. game=Tetris")
    sp.add_argument("--port", type=_port, default=DEFAULT_PORT)
    sp.add_argument("--host", default="127.0.0.1",
                    help="use 0.0.0.0 to view the overlay from a phone/tablet on your LAN "
                         "(no password: anyone on that network can then read your recordings "
                         "and live input)")
    sp.add_argument("--allow-host", action="append", metavar="NAME",
                    help="extra host name the overlay answers to (repeatable); localhost, IP "
                         "addresses and this PC's name always work")
    sp.add_argument("--no-overlay", action="store_true", help="don't start the web overlay")
    sp.add_argument("--open", action="store_true", help="open the overlay in your browser")
    sp.add_argument("--no-sdl", action="store_true", help="don't capture local controllers")
    sp.add_argument("--no-switch2-usb", action="store_true",
                    help="don't read Switch 2 GameCube/Pro controllers plugged in over USB")
    sp.add_argument("--no-enhanced", action="store_true",
                    help="don't switch PlayStation/Switch pads to enhanced reports (use when a non-SDL "
                         "game reads the same pad through DirectInput; lowers DS4 report rate)")
    sp.add_argument("--adb", nargs="?", const="", default=None, metavar="SERIAL",
                    help="also capture controllers paired to an Android device over adb")
    sp.add_argument("--adb-filter", default=None, help="only Android input devices whose name contains this")
    sp.add_argument("--switch2", nargs="?", const="", default=None, metavar="ADDRESS",
                    help="also read a Switch 2 controller over Bluetooth LE (experimental; "
                         "hold its sync button; needs: pip install bleak)")
    sp.add_argument("--switch2-model", choices=sorted(MODELS), default=None,
                    help="Switch 2 model hint (as `switch2 test --model`)")
    sp.add_argument("--switch2-orientation", choices=ORIENTATIONS, default="horizontal",
                    help="single Joy-Con 2: sideways (default) or upright (vertical)")
    sp.add_argument("--switch2-deadzone", type=_deadzone_arg, default=DEFAULT_DEADZONE,
                    help="Switch 2 radial stick deadzone, 0 <= d < 1 (default %(default)s)")
    sp.add_argument("--bridge", choices=("x360", "ds4"), default=None,
                    help="mirror your controller onto a virtual Xbox 360 / DualShock 4 (for games that don't support it)")
    sp.add_argument("--bridge-device", type=int, default=None, help="which device to mirror (default: most recent)")
    sp.add_argument("--bridge-map", default=None, help="remap before mirroring, e.g. south:east,east:south")
    sp.add_argument("--quiet", action="store_true", help="no live status line")
    sp.set_defaults(func=cmd_live)

    sp = sub.add_parser("view", help="open the recording viewer (timeline, stats, playback, compare)")
    sp.add_argument("file", nargs="?", default=None,
                    help="recording to open: a .ctlog path, or a name inside --dir")
    sp.add_argument("--dir", default=None,
                    help=f"recordings folder to serve (default: the file's folder, else {DEFAULT_REC_DIR})")
    sp.add_argument("--port", type=_port, default=DEFAULT_PORT)
    sp.add_argument("--host", default="127.0.0.1",
                    help="use 0.0.0.0 for LAN access (no password: anyone on that network can "
                         "then read your recordings)")
    sp.add_argument("--allow-host", action="append", metavar="NAME",
                    help="extra host name the viewer answers to (repeatable)")
    sp.add_argument("--no-open", action="store_true")
    sp.set_defaults(func=cmd_view)

    sp = sub.add_parser("stats", help="per-button press counts, hold times and mash rates")
    sp.add_argument("file")
    sp.add_argument("--fps", type=_fps, default=None,
                    help="frame rate for hold times (default: the file's native rate, else 60)")
    sp.add_argument("--device", type=int, default=None)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser("replay", help="play a recording/movie back through a virtual controller")
    sp.add_argument("file", help=".ctlog, .gm2, .bk2 or .csv")
    sp.add_argument("--target", choices=("x360", "ds4"), default="x360")
    sp.add_argument("--speed", type=float, default=1.0)
    sp.add_argument("--countdown", type=float, default=3.0, help="seconds before playback starts")
    sp.add_argument("--frames", action="store_true",
                    help="quantize a .ctlog to frames first (frame-locked playback); "
                         ".gm2/.bk2/.csv always play frame by frame")
    sp.add_argument("--fps", type=_fps, default=None,
                    help="frame rate for --frames (default: the recording's native rate, else 60)")
    sp.add_argument("--device", type=int, default=None, help="recording device to replay")
    sp.add_argument("--from-marker", default=None,
                    help="start at the first marker with this label/prefix (.gm2: e.g. reset)")
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(func=cmd_replay)

    sp = sub.add_parser("render", help="render an input-display video/PNG sequence from a recording")
    sp.add_argument("file", help=".ctlog, .gm2, .bk2 or .csv")
    sp.add_argument("out", help="output: .mp4/.webm/.mov (needs ffmpeg), .gif/.webp, or a folder for PNGs")
    sp.add_argument("--layout", default=None, help="layout name (default: matches the controller)")
    sp.add_argument("--fps", type=_fps_text, default="60",
                    help="number, 60000/1001, or gb/gba/nes/snes (exact rates)")
    sp.add_argument("--device", type=int, default=None)
    sp.add_argument("--from-marker", default=None, help="start at this marker (--start is then an offset)")
    sp.add_argument("--start", type=float, default=0.0, help="start time in seconds (negative = lead-in before the marker)")
    sp.add_argument("--end", type=float, default=None, help="end time in seconds (absolute)")
    sp.add_argument("--duration", type=float, default=None, help="clip length in seconds")
    sp.add_argument("--end-marker", default=None, help="stop at the next marker with this label/prefix")
    sp.add_argument("--codec", default=None, help="override the video codec (e.g. prores for .mov)")
    sp.add_argument("--crf", type=int, default=None, help="video quality (lower = better)")
    sp.add_argument("--scale", type=float, default=1.0)
    sp.add_argument("--bg", default="chroma", help="chroma (green), transparent, or #rrggbb")
    sp.add_argument("--no-history", action="store_true", help="controller only, no input-history lane")
    sp.add_argument("--history-seconds", type=float, default=4.0)
    sp.add_argument("--frame-counter", action="store_true", help="burn in frame number and time")
    sp.add_argument("--map", default=None, help="display remap, e.g. south:east,east:south")
    sp.add_argument("--mode", choices=("sample", "any"), default="sample")
    sp.set_defaults(func=cmd_render)

    sp = sub.add_parser("convert", help="convert between .ctlog, .gm2, .bk2 and .csv")
    sp.add_argument("input")
    sp.add_argument("output")
    frames_opts(sp)
    sp.add_argument("--template", default=None, help="GSE .gm2 to take header/start blob from when writing .gm2")
    sp.add_argument("--sha1", default=None, help="ROM SHA1 for the bk2 header")
    sp.set_defaults(func=cmd_convert)

    sp = sub.add_parser("edit", help="TAS-edit a movie with an edit script (see controllerlog/tas.py)")
    sp.add_argument("input")
    sp.add_argument("output")
    sp.add_argument("--script", default=None, help="file with edit commands")
    sp.add_argument("--do", action="append", metavar="COMMAND", help="inline edit command, repeatable")
    frames_opts(sp)
    sp.add_argument("--template", default=None)
    sp.add_argument("--sha1", default=None)
    sp.set_defaults(func=cmd_edit)

    sp = sub.add_parser("diff", help="frame-by-frame input differences between two runs/movies")
    sp.add_argument("a")
    sp.add_argument("b")
    frames_opts(sp)
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_diff)

    sp = sub.add_parser("align", help="compare a host recording with the emulator's own log "
                                      "(dropped presses, timing jitter, drift)")
    sp.add_argument("host", help="controllerlog recording (.ctlog) made while playing")
    sp.add_argument("emulator", help="the emulator's log of the same run (.gm2 / .bk2)")
    sp.add_argument("--fps", type=_fps, default=_fps("gb"))
    sp.add_argument("--device", type=int, default=None, help="host device id")
    sp.add_argument("--map", default=None, help="host->emulator button map, e.g. south:east,east:south "
                                               "(default: try identity and A/B swapped)")
    sp.add_argument("--window", type=float, default=3.0, help="max match distance in frames")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--out", default=None, help="write the re-timed host recording here (.ctlog)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_align)

    sp = sub.add_parser("gm2", help="inspect GSE input logs")
    gsub = sp.add_subparsers(dest="gm2_cmd", required=True)
    g = gsub.add_parser("info", help="header and summary of a .gm2")
    g.add_argument("file")
    g.add_argument("--json", action="store_true")
    g.add_argument("--frames", type=int, default=0, help="also print the first N frames")
    g = gsub.add_parser("list", help="list GSE input logs (default: %%APPDATA%%\\GSE\\Input Log)")
    g.add_argument("--dir", default=None)
    g.add_argument("--limit", type=int, default=30)
    sp.set_defaults(func=cmd_gm2)

    sp = sub.add_parser("layouts", help="list controller layouts")
    sp.set_defaults(func=cmd_layouts)

    sp = sub.add_parser("doctor", help="check SDL3, ViGEmBus, HidHide, Bluetooth adapter, adb, ffmpeg (read-only)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_doctor)

    from .optimize import add_cli as add_optimize_cli
    add_optimize_cli(sub)
    from .input.switch2_ble import add_cli as add_switch2_cli
    add_switch2_cli(sub)
    return p


def main(argv: list[str] | None = None) -> int:
    # Redirected output on Windows uses the ANSI code page (cp1252): a Japanese ROM name or
    # controller name must print as '?' rather than abort the command halfway.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError) as e:
        if args.verbose:
            raise
        return _die(str(e), 1)
