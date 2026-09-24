"""Align a host-side controller recording with an emulator's own log (e.g. GSE .gm2).

The host log (``controllerlog live --record``) has exact physical press times;
the emulator log has the frames the game actually received. Aligning them
answers speedrunner questions neither log can alone:

* which presses the emulator never registered (taps shorter than a frame,
  presses during lag), and which emulator inputs had no physical press
  (keyboard, another pad, TAS edits);
* how consistently presses land relative to frames (timing jitter, in frames);
* the drift between real time and emulated time.

Absolute input latency isn't measurable this way: GSE's log start time is
stored in whole seconds, so the alignment offset absorbs any constant delay.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from statistics import median

from .logfile import Recording
from .model import BUTTON_INDEX, BUTTONS
from .timeline import NS_PER_S, Timeline

DEFAULT_BUTTONS = ("east", "south", "back", "start", "dpad_up", "dpad_down", "dpad_left",
                   "dpad_right", "left_shoulder", "right_shoulder")


@dataclass
class MatchedPress:
    button: str
    host_ns: int      # physical press time (host clock, recording-relative)
    emu_ns: int       # emulator press time (emulated clock)
    residual_ns: int  # emu - (host*scale + offset)


@dataclass
class Alignment:
    offset_ns: int                 # emu_time ≈ host_time * scale + offset
    scale: float                   # emulated seconds per host second (≈1.0)
    matched: list[MatchedPress] = field(default_factory=list)
    host_only: list[tuple[str, int, int]] = field(default_factory=list)  # (button, down, hold_ns)
    emu_only: list[tuple[str, int]] = field(default_factory=list)
    mapping: dict[str, str] = field(default_factory=dict)
    host_presses: int = 0
    emu_presses: int = 0
    warnings: list[str] = field(default_factory=list)   # reasons not to trust this alignment

    def to_emu(self, host_ns: int) -> int:
        return round(host_ns * self.scale) + self.offset_ns

    @property
    def match_rate(self) -> float:
        return len(self.matched) / self.host_presses if self.host_presses else 0.0

    def residual_frames(self, fps: float) -> list[float]:
        return [m.residual_ns * fps / NS_PER_S for m in self.matched]

    def summary(self, fps: float) -> dict[str, object]:
        res = sorted(self.residual_frames(fps))
        def pct(p: float) -> float:
            return round(res[min(len(res) - 1, int(p / 100 * (len(res) - 1)))], 3) if res else 0.0
        return {
            "offset_s": round(self.offset_ns / NS_PER_S, 6),
            "clock_ratio": round(self.scale, 9),
            "drift_ms_per_hour": round((self.scale - 1.0) * 3600e3, 2),
            "mapping": self.mapping,
            "host_presses": self.host_presses,
            "emu_presses": self.emu_presses,
            "matched": len(self.matched),
            "match_rate": round(self.match_rate, 4),
            "residual_frames": {"p5": pct(5), "median": pct(50), "p95": pct(95),
                                "spread_p5_p95": round(pct(95) - pct(5), 3)},
            "host_presses_not_registered": len(self.host_only),
            "emu_presses_without_host_press": len(self.emu_only),
            "warnings": list(self.warnings),
        }


def _onsets(rec: Recording, device: int, buttons: tuple[str, ...]) -> dict[str, list[tuple[int, int]]]:
    """button -> [(down_ns, hold_ns)] using press intervals."""
    tl = Timeline(rec)
    out: dict[str, list[tuple[int, int]]] = {b: [] for b in buttons}
    for p in tl.presses(device, include_triggers=False):
        name = p.name
        if name in out:
            out[name].append((p.down_ns, p.duration_ns(tl.end_ns)))
    return out


def _coarse_offset(host: dict[str, list[tuple[int, int]]], emu: dict[str, list[tuple[int, int]]],
                   bin_ns: int, max_pairs: int = 400_000) -> int | None:
    """Mode of (emu - host) onset differences per button: an event-train cross-correlation."""
    votes: dict[int, int] = {}
    pairs = 0
    for b, hs in host.items():
        es = emu.get(b, [])
        if not hs or not es:
            continue
        step_h = max(1, len(hs) * len(es) // max(1, max_pairs // max(1, len(host))))
        for i in range(0, len(hs), step_h):
            h = hs[i][0]
            for e, _ in es:
                k = (e - h) // bin_ns
                votes[k] = votes.get(k, 0) + 1
                pairs += 1
    if not votes:
        return None
    # Smooth over neighbouring bins so a jittery peak isn't split. The best 3-bin sum is at
    # least the largest count, so only bins next to a count >= a third of it can win.
    thr = max(votes.values()) / 3
    near = {k + d for k, v in votes.items() if v >= thr for d in (-1, 0, 1)}
    best = max((k for k in votes if k in near),
               key=lambda k: votes.get(k - 1, 0) + votes[k] + votes.get(k + 1, 0))
    return best * bin_ns + bin_ns // 2


SEGMENT_NS = 120 * NS_PER_S   # host time per segment for the initial drift estimate
MAX_DRIFT = 1e-3              # largest |clock ratio - 1| the initial estimate searches (3.6 s/hour)


def _times(onsets: dict[str, list[tuple[int, int]]]) -> dict[str, list[int]]:
    return {b: [t for t, _ in lst] for b, lst in onsets.items()}


def _window(onsets: dict[str, list[tuple[int, int]]], times: dict[str, list[int]],
            lo: int, hi: int) -> dict[str, list[tuple[int, int]]]:
    """Onsets with lo <= time < hi (lists are sorted by time; ``times`` from :func:`_times`)."""
    return {b: lst[bisect.bisect_left(times[b], lo):bisect.bisect_left(times[b], hi)]
            for b, lst in onsets.items()}


def _theil_sen(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Robust line fit y = a*x + b (median of pairwise slopes), tolerant of bad segments."""
    slopes = [(y2 - y1) / (x2 - x1) for i, (x1, y1) in enumerate(points)
              for x2, y2 in points[i + 1:] if x2 != x1]
    a = median(slopes) if slopes else 0.0
    return a, median(y - a * x for x, y in points)


def _segment_starts(host: dict[str, list[tuple[int, int]]], emu: dict[str, list[tuple[int, int]]],
                    bin_ns: int, coarse: int) -> list[tuple[float, int]]:
    """Initial (scale, offset) guesses for long runs, where clock drift smears the global offset.

    The host run is cut into segments. Each gets its own coarse offset against the
    emulator onsets within the largest plausible drift (:data:`MAX_DRIFT`) of an
    anchor offset, and a robust line through (segment time, offset) gives the drift.
    Anchors: the global estimate, and the offsets of a few single segments against
    the whole emulator log (drift barely smears those).
    """
    times = sorted(t for lst in host.values() for t, _ in lst)
    if not times:
        return []
    t0, span = times[0], times[-1] - times[0]
    n = int(span // SEGMENT_NS)
    if n < 3:
        return []
    margin = round(span * MAX_DRIFT) + 2 * NS_PER_S
    host_t, emu_t = _times(host), _times(emu)
    segments = []
    for s in range(n):
        lo, hi = t0 + span * s // n, t0 + span * (s + 1) // n + (1 if s == n - 1 else 0)
        seg = _window(host, host_t, lo, hi)
        if sum(len(v) for v in seg.values()) >= 3:
            segments.append((lo, hi, seg))
    if len(segments) < 3:
        return []
    anchors = [coarse]
    for lo, hi, seg in (segments[0], segments[len(segments) // 2], segments[-1]):
        off = _coarse_offset(seg, emu, bin_ns, max_pairs=100_000)
        if off is not None and all(abs(off - a) > margin for a in anchors):
            anchors.append(off)
    starts = []
    for anchor in anchors:
        points = []
        for lo, hi, seg in segments:
            near = _window(emu, emu_t, lo + anchor - margin, hi + anchor + margin)
            off = _coarse_offset(seg, near, bin_ns)
            if off is not None:
                points.append(((lo + hi) / 2 / NS_PER_S, off / NS_PER_S))
        if len(points) >= 3:
            slope, icpt = _theil_sen(points)
            starts.append((1.0 + slope, round(icpt * NS_PER_S)))
    return starts


def _match(host: dict[str, list[tuple[int, int]]], emu: dict[str, list[tuple[int, int]]],
           offset: int, scale: float, window_ns: int) -> tuple[list[MatchedPress], list, list]:
    matched: list[MatchedPress] = []
    host_only: list[tuple[str, int, int]] = []
    emu_only: list[tuple[str, int]] = []
    for b in host.keys() | emu.keys():
        hs = host.get(b, [])
        es = [e for e, _ in emu.get(b, [])]
        used = [False] * len(es)
        for h, hold in hs:
            target = round(h * scale) + offset
            i = bisect.bisect_left(es, target - window_ns)
            best = None
            while i < len(es) and es[i] <= target + window_ns:
                if not used[i] and (best is None or abs(es[i] - target) < abs(es[best] - target)):
                    best = i
                i += 1
            if best is None:
                host_only.append((b, h, hold))
            else:
                used[best] = True
                matched.append(MatchedPress(b, h, es[best], es[best] - target))
        emu_only.extend((b, es[i]) for i, u in enumerate(used) if not u)
    matched.sort(key=lambda m: m.host_ns)
    host_only.sort(key=lambda x: x[1])
    emu_only.sort(key=lambda x: x[1])
    return matched, host_only, emu_only


def _fit(matched: list[MatchedPress]) -> tuple[float, int]:
    """Least-squares emu = scale*host + offset over matched presses."""
    n = len(matched)
    xs = [m.host_ns / NS_PER_S for m in matched]
    ys = [m.emu_ns / NS_PER_S for m in matched]
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return 1.0, round((my - mx) * NS_PER_S)
    scale = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return scale, round((my - scale * mx) * NS_PER_S)


def align(host: Recording, emu: Recording, host_device: int | None = None,
          emu_device: int | None = None, fps: float = 4194304 / 70224,
          buttons: tuple[str, ...] = DEFAULT_BUTTONS, mapping: dict[str, str] | None = None,
          window_frames: float = 3.0, try_ab_swap: bool = True) -> Alignment:
    """Align two recordings of the same run.

    ``mapping`` renames host buttons to emulator buttons (host -> emu), e.g.
    ``{"south": "east", "east": "south"}`` when your emulator binds the Xbox
    A button to GB A. With ``try_ab_swap`` both the identity and the A/B-swapped
    mapping are tried and the better one is kept.
    """
    hdev = host.primary_device() if host_device is None else host_device
    edev = emu.primary_device() if emu_device is None else emu_device
    if hdev is None or edev is None:
        raise ValueError("both recordings need input from at least one device")
    frame_ns = NS_PER_S / fps
    emu_on = _onsets(emu, edev, buttons)
    candidates = [dict(mapping or {})]
    if try_ab_swap and not mapping:
        candidates.append({"south": "east", "east": "south", "west": "north", "north": "west"})
    best: Alignment | None = None
    all_host = tuple(dict.fromkeys(list(buttons) + [b for m in candidates for b in m]))
    host_raw = _onsets(host, hdev, tuple(b for b in all_host if b in BUTTON_INDEX))
    for cand in candidates:
        host_on: dict[str, list[tuple[int, int]]] = {b: [] for b in buttons}
        for src, lst in host_raw.items():
            dst = cand.get(src, src)
            if dst in host_on:
                host_on[dst].extend(lst)
        for lst in host_on.values():
            lst.sort()
        coarse = _coarse_offset(host_on, emu_on, bin_ns=round(frame_ns))
        if coarse is None:
            continue
        window = round(window_frames * frame_ns)
        starts = [(1.0, coarse)] + _segment_starts(host_on, emu_on, round(frame_ns), coarse)
        for scale, offset in starts:
            matched, host_only, emu_only = _match(host_on, emu_on, offset, scale, window)
            for _ in range(3):  # refine drift + offset from the matches, then re-match
                if len(matched) < 3:
                    break
                scale, offset = _fit(matched)
                matched, host_only, emu_only = _match(host_on, emu_on, offset, scale, window)
            al = Alignment(offset, scale, matched, host_only, emu_only, cand,
                           sum(len(v) for v in host_on.values()), sum(len(v) for v in emu_on.values()))
            if best is None or len(al.matched) > len(best.matched):
                best = al
    if best is None:
        raise ValueError("no common buttons were pressed in both recordings")
    best.warnings = _sanity(best, frame_ns)
    return best


def _sanity(al: Alignment, frame_ns: float) -> list[str]:
    """Warnings for an alignment that shouldn't be trusted."""
    out = []
    if al.match_rate < 0.5:
        out.append(f"only {al.match_rate * 100:.0f}% of the physical presses matched: the "
                   "recordings may not be of the same run, the button mapping may be wrong "
                   "(try --map), or the clock drift is too large to align; don't trust the "
                   "offset/drift figures")
    if len(al.matched) >= 20:
        # A leftover slope of the residuals against host time = drift the fit didn't absorb.
        k = len(al.matched) // 4
        first = median(m.residual_ns for m in al.matched[:k])
        last = median(m.residual_ns for m in al.matched[-k:])
        if abs(last - first) > frame_ns:
            out.append(f"press timing drifts by {(last - first) / frame_ns:+.1f} frames between "
                       "the start and end of the run after alignment: the drift estimate is off")
    return out


def shifted(host: Recording, al: Alignment) -> Recording:
    """Copy of the host recording re-timed onto the emulator's clock (for side-by-side viewing)."""
    from .model import InputEvent
    out = Recording(header={**host.header, "meta": {**host.meta, "aligned_to_emulator": True,
                                                    "alignment_offset_s": al.offset_ns / NS_PER_S,
                                                    "alignment_scale": al.scale}},
                    devices=dict(host.devices))
    for ev in host.events:
        out.events.append(InputEvent(max(0, al.to_emu(ev.t_ns)), ev.device, ev.kind, ev.code, ev.value))
    out.sort()
    return out


__all__ = ["Alignment", "MatchedPress", "align", "shifted", "DEFAULT_BUTTONS", "BUTTONS"]
