# Replay, rendering and TAS work

## What "replay the exact inputs" can and can't mean

| Target | Frame-exact replay? | How |
|---|---|---|
| **GSE (Game Boy / GBA)** | **Yes** | GSE's own `.gm2` replayed in [InputLogPlayer](https://github.com/CasualPokePlayer/InputLogPlayer) (`--rom --bios --gm2 [--dump-av]`) |
| **BizHawk** | **Yes** | `.bk2` movie playback, or TAStudio |
| Live emulator or PC game through a virtual controller | **Best effort** | `controllerlog replay` |

Emulators are deterministic: the same inputs on the same frames from the same
start state always give the same result. PC games generally aren't. They use
variable timesteps, clock-seeded RNG, threads and asynchronous loading, so
replaying a recording through a virtual controller reproduces your *inputs*
with sub-millisecond timing, but not necessarily the same *outcome*. The
exceptions virtualise time itself: libTAS (Linux), Hourglass (old 32-bit
Windows games), and game-specific tools such as CelesteTAS.

`controllerlog replay` reports its own timing accuracy after every run
(mean/p99/max lateness). It schedules with a 1 ms system timer plus a spin-wait
for the last 1.5 ms before each event.

Replay tips:
* Align the start with something visible: a marker (`--from-marker`) or the
  countdown (`--countdown`). Starting on a button press of your real
  controller is available from Python (`replay.ButtonTrigger`), not the CLI.
* Frame-based movies (`.bk2`, `.gm2`, `.csv`, or `.ctlog` with `--frames`) are
  applied once per frame at the movie's frame rate. `--speed` and
  `--from-marker` work for them too: in a `.gm2` every hard reset is a `reset`
  marker, so `--from-marker reset` starts at the first frame after the first
  reset. `--device` picks the pad of a `.ctlog`; `--fps` sets the rate a
  `.ctlog` is quantized at with `--frames` (default: the recording's own rate,
  else 60).
* To replay into GSE through a virtual pad, turn on GSE's *background input*
  for joysticks and bind the virtual Xbox pad. Frame alignment is still not
  guaranteed, because GSE polls once per emulated frame on its own schedule.
  For exact results, edit the `.gm2` and replay it in InputLogPlayer.
* Hide your physical controller with HidHide during replay so you don't get
  double input.

## GSE → BizHawk (`.gm2` → `.bk2`)

```bash
controllerlog convert "%APPDATA%\GSE\Input Log\<log>.gm2" run.bk2 --sha1 <ROM SHA1>
```

GSE samples input once per VBlank-bounded `gambatte_runfor(35112)` call. That
is exactly what BizHawk's default Gambatte mode ("VBlank driven frames") does,
so normal frames convert 1:1: Platform `GB`, Core `Gambatte`, the right
ConsoleMode, the save file as `MovieSaveRam.bin`, and every GSE hard reset as
`Power` on the next frame, including the one a save-file load starts with. In
GBC-in-GBA mode the movie turns `EnableBIOS` off. BizHawk still boots the BIOS
for movies, and this makes its reset stall match GSE's. The converter warns
about what BizHawk can't reproduce exactly:

* **Game Boy Player mode** (GSE's 3.3M-sample reset stall and random fade-out).
  Written as GBC-in-GBA mode. Select the patched `GBC_agb_gambatte.bin` BIOS in
  BizHawk to match GSE.
* Records with a budget other than 35112 samples (the GBP reset fade-out).
* SGB2 logs, which use a different layout and a newer core revision.
* GBA logs from GSE v0.6+, which were recorded on the Mesen core. BizHawk has
  mGBA, so these are transcriptions only.
* Logs that start from a GSE savestate. These are refused, because GSE and
  BizHawk savestates are different formats.
* Core version skew. GSE's gambatte-core is a few commits newer than
  BizHawk's, so rare desyncs are possible. Check by playing the `.bk2` in
  BizHawk and comparing with InputLogPlayer's `--dump-av` video.

## Editing inputs

`controllerlog edit IN OUT` applies an edit script to any movie. IN can be
`.gm2`, `.bk2`, `.csv` or `.ctlog`; a `.ctlog` is quantized to frames first
(at `--fps`, default the recording's own rate, else 60). Script files may be
UTF-8 with or without a BOM, or UTF-16 (PowerShell's `>`).

```text
hold 120-130 east        # GB "A" is the east button (positional naming)
release 125 east
set 200 start,dpad_up    # exactly these buttons on frame 200
clear 300-310
insert 50 3              # 3 neutral frames before frame 50
delete 60 2
copy 10-19 40
axis 70-80 left_x -32768
```

```bash
controllerlog edit run.gm2 run_tas.gm2 --script edits.txt
controllerlog diff run.gm2 run_tas.gm2 --fps gb
```

* **`.gm2` output** keeps GSE's header and start blob, and every original
  frame keeps its own cycle budget. A hard reset belongs to the frame after
  it, so `insert` and `delete` move resets (and GBP fade-out budgets) along
  with the inputs: after `insert 100 2`, every reset after frame 100 is two
  frames later too. Inserted frames get the nominal budget (35112 samples on
  GB/GBC, 280896 cycles on GBA) and no reset. A reset in front of a deleted
  frame moves to the next remaining frame (it is dropped at the end of the
  log). `copy` overwrites inputs only, so the destination keeps its resets.
  The file still plays in InputLogPlayer.
* **`.bk2` output** opens in BizHawk's **TAStudio** (piano roll, greenzone,
  branches). That's the right place for serious TASing, because every edit is
  re-emulated immediately. Editing a `.bk2` changes only player 1's buttons:
  save RAM, `Power` frames (moved with their frames on insert/delete), SHA1,
  sync settings (console mode, BIOS, RTC), comments and other lumps are kept.
  From a `.gm2`, `edit` starts from the same faithful conversion as
  `convert x.gm2 y.bk2`. Only a `.ctlog`/`.csv` source gives a fresh power-on
  movie (pass `--system`).
* Edits are marked in `.ctlog` output (`tas_edited` in the header metadata)
  and `.bk2` output (a "TAS-edited with ControllerLog" comment). `.gm2` and
  `.csv` output carry no marker: an edited `.gm2` keeps GSE's original header
  and looks like a genuine log, so label such files yourself.

## Optimizing a TAS window inside BizHawk (`controllerlog optimize`)

`controllerlog optimize` tries thousands of input variations for a short window of a BizHawk `.bk2` movie. It plays every candidate on BizHawk's own emulator core, scores it with a RAM value you choose, and writes the best one into a new `.bk2`. Because the candidates run on the core and sync settings the movie uses, the inputs it finds sync when you play the new movie in BizHawk.

**What it is not:** it does not "solve" a game or produce theoretically optimal input. It is a local search over one window (tens to a few hundred frames), toward a goal you define with a memory address. Results are guaranteed only on the same core and sync settings they were found on.

### Requirements
- BizHawk (EmuHawk). The bot was written against the BizHawk 2.11 source. The socket message format needs 2.6.2 or newer, and an older EmuHawk is reported as such. Versions before 2.11 are untested.
- The exact ROM the movie was made for.
- A power-on (or save-RAM) `.bk2` made with Gambatte (GB/GBC) or mGBA (GBA). Other platforms may work but are untested. Movies that start from a savestate are refused.
- One RAM address that measures what you want. You can find addresses in RAM maps (TASVideos game resources, pret disassemblies) or with BizHawk's RAM Search / RAM Watch.

### Addresses: domain offsets, not bus addresses
A read is written `DOMAIN:ADDR[:SIZE[:ENDIAN]]`. ADDR is hex. SIZE is `1`, `2` or `4` (`s1`, `s2`, `s4` for signed), and ENDIAN is `le` (the default) or `be`. BizHawk domain offsets start at 0:
- On Game Boy, bus address `0xD361` is `WRAM:0x1361`, or `System Bus:0xD361`.
- On GBA, `0x03001234` is `IWRAM:0x1234`.

If you pass a bus address as a WRAM offset, the tool refuses it and suggests the fix.

### Objectives
- `max READ` / `min READ`: the value at the end of the window.
- `until READ OP VALUE`: the fewest frames until the condition holds. OP is one of `== != < <= > >=`, or `&` (any bit of the mask set). VALUE is decimal or `0x` hex. BizHawk stops a candidate as soon as the condition holds, so each candidate costs only the frames it needs.
- `--tiebreak "max READ"` (repeatable) orders candidates that tie. For `until` with beam search, add a tiebreak such as the player's X position so that candidates that haven't reached the goal yet can still be ranked.

### Methods
- `--method delay`: moves each button press in the window up to `--max-shift` frames earlier or later, one press at a time, and keeps the best position. `--passes` repeats the sweep. Good for frame-perfect presses and RNG manipulation.
- `--method beam`: builds new input from `--actions` (button combinations such as `"" Right Right+A`), each held for `--macro-len` frames. At every step it keeps the `--beam-width` best candidates and drops duplicate game states. By default a state is identified by a hash of WRAM+HRAM or IWRAM+EWRAM; CPU registers are not included, so this is a heuristic.
- `--method mutate`: hill-climbs from your input with random edits. An edit flips a button, moves one end of a held press, or inserts or deletes a frame. An edit is kept if it isn't worse. `--seed` makes runs repeatable.

### Running it
1. **Start the optimizer first.** EmuHawk connects to it while starting up, and fails to start if nothing is listening:
   ```
   controllerlog optimize --movie run.bk2 --out run_opt.bk2 --start 1200 --window 90 --method delay --objective "until WRAM:0x135E:1==92" --tail 60
   ```
   Bad option values are reported before this step.
2. **Start EmuHawk with the command line it prints**, for example:
   ```
   EmuHawk.exe --socket_ip=127.0.0.1 --socket_port=43880 --movie="C:\runs\run.bk2" --lua="C:\ControllerLog\tools\bizhawk\controllerlog_bot.lua" "C:\roms\game.gbc"
   ```
   - `--movie` plays your movie with its own core settings up to `--start`. The bot then stops the movie and takes over the input. EmuHawk plays a frame (or so) before it starts the Lua script, so `--start` must be at least that frame (usually 1). The tool tells you if it isn't.
   - Without `--movie`, the tool replays the movie's input itself, using your current core settings. This only works if those match the movie's sync settings, and if the frames EmuHawk already ran have no input in the movie. `--movie` is recommended.
   - The `--socket_*` flags exist only on the command line.
   - For another run in the same EmuHawk: pause it, restart your movie, then restart `controllerlog_bot.lua` from Tools > Lua Console. The script reconnects to the new optimizer.
   - Alternatively, `--emuhawk PATH --rom PATH --launch` starts EmuHawk for you.
3. Leave the emulator alone while it runs; the bot pauses it between commands. By default emulation is unthrottled (`--speed max`, the same flag EmuHawk's Toggle Throttle hotkey sets). Your throttle and speed settings are restored when the run ends or the script is stopped. Turning off rewind makes it faster. Very long prefixes get proportionally more time to replay.
4. The tool prints the objective before and after (re-checked from the savestate) and writes `--out`. The new movie's comments say `optimized by ControllerLog`, and the number of evaluations is added to the rerecord count. The stale `CycleCount` (running time) header is removed; BizHawk writes a new one when you play the movie to its end and save it. If nothing improved, no file is written.

### What happens to the rest of the movie
- **`until` objectives:** when the new input reaches the goal, the tool finds where the old input reaches it: in the window, or up to 3600 frames further into the movie. It then removes the frames saved, so your original input from after the goal follows immediately. If the old input never reaches the goal in that range, the window is replaced in place, and for beam search the frames after the goal are neutral. Check the rest of the movie in that case.
- **`max`/`min` objectives:** the window is replaced in place. If the game state at the end of the window changed, check the rest of the movie for desyncs (e.g. in TAStudio).

### From Python
```python
from controllerlog.optimize import BizHawkBridge, Objective, LineCodec, beam_search
with BizHawkBridge(port=43880) as bh:          # listen first, then start EmuHawk (here without --movie;
    bh.wait_for_bizhawk()                      #  with --movie, call bh.seek(frame) before saving)
    start = bh.save_state()
    obj = Objective.parse("until WRAM:0x04:1==1", ["max WRAM:0x00:2"])
    res = beam_search(bh, start, 60, ["", "Right", "Right+A"], obj, codec=LineCodec.for_system("gb"))
    print(res.result.hit, res.lines)
```
Every search takes any object with `evaluate` / `branch` / `free_state`, so you can prototype against your own evaluator.

### Honest limits
- It has not yet been run against a real EmuHawk. It was built from BizHawk's source and tested against a model of it (the real Lua script, run under Lua 5.4 and 5.1).
- It searches locally and depends on the objective you choose.
- Only digital buttons are searched. Analog and sensor axes keep the values of the lines being edited, and new frames get each axis's neutral value.
- The bot can't tell that the optimizer has gone away. If the optimizer stops abnormally (crash, killed process, lost connection), restart the Lua script, or EmuHawk, before the next run.

## Rendering input-display videos

```bash
controllerlog render run.gm2 inputs.webm --fps gb --layout gameboy --bg transparent
controllerlog render recordings/run.ctlog inputs.mp4 --from-marker split:1 --frame-counter
```

Frame *n* of the render shows the controller state at exactly `n × 4389/262144 s`
for GB/GBA (59.7275 fps). A `.gm2` rendered at `--fps gb` therefore lines up
one-to-one with InputLogPlayer's `--dump-av` gameplay video, which uses the
same timebase. Composite the two with ffmpeg:

```bash
ffmpeg -i gameplay.mp4 -c:v libvpx-vp9 -i inputs.webm -filter_complex "[0:v][1:v]overlay=x=main_w-overlay_w-16:y=main_h-overlay_h-16" -c:a copy out.mp4
```

`-c:v libvpx-vp9` before `-i inputs.webm` matters: ffmpeg's built-in VP9
decoder ignores the webm's alpha channel, so the transparent parts would come
out black. A `.mov` render keeps its alpha with the default decoder.

For footage captured live with OBS, align using a visible event, or the cycle
counter in GSE's status bar at two points.

## Comparing your controller with GSE's log (`controllerlog align`)

Record the host side while you play (`controllerlog live --record`), then
compare it with the log GSE wrote for the same run:

```bash
controllerlog align recordings/run.ctlog "%APPDATA%\GSE\Input Log\run.gm2" [--map south:east,east:south] [--window 3] [--out retimed.ctlog]
```

It prints the offset and the clock drift between real time and emulated
time, the match rate, and how presses land relative to frames (median and
p5-p95 spread, in frames). It lists the physical presses GSE never
registered, marked "shorter than a frame" when that explains it, and the
emulator presses with no physical press (keyboard, another pad, edits).
Without `--map`, identity and A/B-swapped bindings are both tried. `--out`
writes the recording re-timed onto the emulator clock. To view it next to
the emulator log in the viewer's compare mode, convert the `.gm2` to `.ctlog`
first. Absolute input latency can't be measured this way: GSE stores its
start time in whole seconds, so the offset absorbs any constant delay.
`align` warns when the result shouldn't be trusted (few presses matched).
