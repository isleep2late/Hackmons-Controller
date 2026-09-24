# File formats

ControllerLog keeps its own lossless recording format (`.ctlog`) and converts
to/from the formats speedrun and TAS tools use.

| Format | Direction | What it is | Timing |
|---|---|---|---|
| `.ctlog` | read/write | Our recording: every button/axis change of every controller | host nanoseconds |
| `.gm2` (GSE) | read, edit, write | GSE's emulator-internal input movie | emulated frames |
| `.bk2` (BizHawk) | read/write | BizHawk movie (zip) for TAStudio | emulated frames |
| `.csv` | read/write | Frame table (one row per frame) | frames at a chosen fps |

---

## `.ctlog` — ControllerLog recording (version 1)

UTF-8 JSON Lines. Optionally gzip-compressed when the name ends in `.gz`.

**Line 1: header object**

```json
{"format": "controllerlog", "version": 1, "created_utc": "2026-09-21T19:03:07.123+00:00",
 "time_unit": "ns", "clock": "perf_counter_ns",
 "meta": {"game": "Pokemon Red", "category": "Any% Glitchless", "runner": "you"}}
```

| key | meaning |
|---|---|
| `format` | always `"controllerlog"` |
| `version` | format version; readers reject newer major versions |
| `created_utc` | ISO-8601 wall-clock time the recording started |
| `time_unit` | always `"ns"` |
| `clock` | where timestamps came from: `perf_counter_ns` (live capture), `emulated` (converted from a .gm2/.bk2), `frames` (TAS movie) |
| `meta` | free-form: game, category, runner, notes, source file, `tas_edited`, ... |

**Following lines: one event row each**

```
[t_ns, device, kind, code, value]
```

* `t_ns` — integer nanoseconds since the recording started (monotonic, not wall clock).
* `device` — integer device id inside this file (`null` for markers).
* `kind`:

| kind | code | value | meaning |
|---|---|---|---|
| `"+"` | `null` | device object | controller connected (also written at t=0 for every device present at start) |
| `"-"` | — | — | controller disconnected (row is `[t, dev, "-"]`) |
| `"b"` | button index | `1` down / `0` up | button change |
| `"a"` | axis index | int16 | axis change |
| `"m"` | `null` | string | marker: `split:<name>`, `reset`, `run_start`, `gm2:start`, ... |

Button indices (same as SDL3's `SDL_GamepadButton`):
`0 south, 1 east, 2 west, 3 north, 4 back, 5 guide, 6 start, 7 left_stick,
8 right_stick, 9 left_shoulder, 10 right_shoulder, 11 dpad_up, 12 dpad_down,
13 dpad_left, 14 dpad_right, 15 misc1, 16 right_paddle1, 17 left_paddle1,
18 right_paddle2, 19 left_paddle2, 20 touchpad, 21-25 misc2..misc6`.

Positions, not labels: `south` is the bottom face button — Xbox **A**, PlayStation
**Cross**, Nintendo **B**. `east` is Xbox **B**, PlayStation **Circle**, Nintendo **A**.

Axis indices: `0 left_x, 1 left_y, 2 right_x, 3 right_y` (−32768..32767, **y negative = up**),
`4 left_trigger, 5 right_trigger` (0..32767).

Device object:

```json
{"id": 0, "name": "DualSense Wireless Controller", "backend": "sdl3", "sdl_type": "ps5",
 "family": "playstation", "vendor_id": 1356, "product_id": 3302, "connection": "wireless"}
```

Guarantees:

* Rows are written in time order per device. A reader should still sort stably by `t_ns`.
* The writer flushes every 250 ms, also when input goes idle after a burst. A crash
  loses at most that much, and a truncated **last** line is ignored. A malformed line
  anywhere else is an error. A `.ctlog.gz` left unfinished by a crash (no gzip
  end-of-stream marker) is read up to the last flushed data.
* The first rows at `t=0` give the complete starting state (connected devices and
  every non-neutral input), so each file is self-contained.

---

## `.gm2` — GSE input log

Implemented in `controllerlog/formats/gm2.py`. The spec below was derived from
GSE's source (`GSE.Emu/EmuInputLog.cs`, GSE v0.6 / GM2 v2). Where GSE puts them:
`%APPDATA%\GSE\Input Log\<yyyy-MM-ddTHH-mm-ss>-<RomName>.gm2`, or `<exe dir>\Input Log\`
for a portable install. GSE starts a new file on every ROM load, savestate load and save load.
The file GSE is currently writing is **locked**: other programs can't read it until GSE
closes it, which happens on the next ROM/state/save load or when GSE exits. GSE also
flushes only every 128 KiB of input (~3 minutes of GB frames). A hard kill before the
first flush leaves an empty file, and a later one loses the last partial block, which
the reader tolerates.

**Header**. Little-endian, except for the magic. v2 is 1024 bytes, v1 is 560 bytes.

| offset | size | field |
|---|---|---|
| 0 | 8 | magic `GSEMOVIE` |
| 8 | 4 | version (2 = GSE v0.5+, 1 = GSE v0.4) |
| 12 | 4 | platform: 0 GB, 1 GBC, 2 GBC-in-GBA / GB Player, 3 SGB2, 4 GBA |
| 16 | 4 | reset stall (GBP 3309568, SGB2 4194304, else 0) |
| 20 | 4 | flags: bit0 starts from savestate, bit1 zstd body, bit2 GBA RTC disabled |
| 24 | 8 | start timestamp (Unix seconds) |
| 32 | 8 | GB RTC dividers |
| 40 | 4 | start blob size |
| 44 | 256 | ROM name (u8 length + 255 bytes UTF-8) |
| 300 | 256 | emulator version (same encoding) |
| 560 | 8 | GBA RTC time (v2 only) |

**Body** (zstd-compressed when flag bit 1 is set; GSE always sets it):
the start blob (power-on save file, or savestate when bit 0 is set), followed by
8-byte records `(u32 cycles, u32 buttons)` until EOF.

* Button bits: `0 A, 1 B, 2 Select, 3 Start, 4 Right, 5 Left, 6 Up, 7 Down, 8 R, 9 L`.
  GSE never logs Left+Right or Up+Down together.
* A hard reset is the record `(0, 0x80000000)`.
* For GB-family logs, `cycles` is the *requested* budget in 2^21 Hz samples, logged
  before the frame runs. It is normally 35112, which is one frame at 59.7275 fps.
  The exact elapsed time per record can only be recovered by emulating.
* For GBA logs, `cycles` is the actual CPU cycles at 2^24 Hz, logged after the frame
  ran. Summing them gives exact emulated time.

**Mapping to canonical buttons.** Nintendo positional: A → `east`, B → `south`,
Select → `back`, Start → `start`, d-pad → `dpad_*`, L → `left_shoulder`,
R → `right_shoulder`.

**Why ControllerLog doesn't *record* `.gm2` from a controller.** A `.gm2`'s frame
boundaries come from inside GSE's emulation loop. GSE polls input once per
emulated frame and embeds the starting save or savestate. An external logger
can't see those boundaries, so a synthesized `.gm2` wouldn't stay in sync with a
real run. The supported workflow is the reverse:

1. Run GSE as usual. GSE writes the authoritative `.gm2`.
2. Optionally run `controllerlog live --record` at the same time. This captures the
   host side: analog values, exact press timing and every controller.
   `controllerlog align` then compares the two (see docs/TAS.md).
3. Import the `.gm2` for frame-exact display, analysis, video rendering and TAS
   edits. The header and start blob are kept, and every original frame keeps its
   cycle field. A hard reset stays attached to the frame after it, so `insert` and
   `delete` move resets and short (GBP fade-out) budgets along with the inputs;
   inserted frames get the nominal budget (35112 GB samples, 280896 GBA cycles).
   The result still replays in InputLogPlayer. Nothing in the file marks it as
   edited.

---

## `.bk2` — BizHawk movie

See `controllerlog/formats/bk2.py` and docs/TAS.md. Converting or editing a
`.bk2` into a `.bk2` rewrites only player 1's buttons and keeps everything else
(save RAM, `Power` frames, SHA1, sync settings, comments, other lumps).
Gambatte movies all say `Platform GB`; `IsCGBMode 1` plus the sync settings'
`ConsoleMode` tell GB, GBC and GBC-in-GBA apart.

## `.csv` — frame table

```
frame,time_ms,east,south,dpad_right
0,0.000,0,0,0
1,16.743,1,0,1
```

`frame` and `time_ms` are informational on import, except that without `--fps`
they give the table's frame rate (snapped to a known rate such as `gb` when
within 0.1 %; 60 if there are fewer than two rows). Every other column must be a
canonical button name (0/1) or an axis name (integer value). UTF-8 with or without
a BOM (Excel's "CSV UTF-8") and UTF-16 are accepted.
