# Hackmons Controller

A speedrunning toolkit for game controllers, inspired by
[BlueRetro](https://github.com/darthcloud/BlueRetro). It lets you:

* connect almost any Bluetooth or USB controller to a PC with software only
  (DualShock 3/4, DualSense, Switch Pro, Joy-Con, Wii Remote, 8BitDo, Xbox,
  **Switch 2 GameCube/Pro over USB-C**, ...), and make any of them look like an
  Xbox controller to PC games;
* log every input with nanosecond timestamps and show a live controller overlay
  (OBS browser source) whose buttons light up as you press them;
* analyse runs: per-button stats, a piano-roll viewer with a frame grid, and
  comparisons between attempts;
* work with **GSE** (Game Boy Speedrun Emulator) `.gm2` input logs and BizHawk
  `.bk2` movies: convert, render input-display videos, TAS-edit, and compare
  your physical presses with what GSE registered;
* replay inputs through a virtual controller, and search for better inputs
  inside BizHawk.

The Python package and command are called **`controllerlog`**. The Android
companion app is **GC Bridge** ([android/README.md](android/README.md)).

```
 Bluetooth / USB controller ──► SDL3 (HID drivers for PS3/4/5, Switch, Wii, 8BitDo, Xbox, …)
 Switch 2 GameCube / Pro ─USB─► switch2_usb (init over USB, 250 Hz reports)
 Android phone + controller ──► adb getevent  ─┐
                                               ▼
                                   ┌──────── Hub ────────┐
                                   ▼          ▼          ▼
                           .ctlog recorder  web overlay  virtual Xbox/DS4 pad
                                   │        (OBS source)  (bridge / replay)
                                   ▼
          stats · viewer · video render · TAS edit · .gm2 / .bk2 / .csv export
```

## Status: what has and hasn't been tested

| Area | Status |
|---|---|
| SDL3 controller capture, hub, recorder, overlay, viewer, renderer | Tested on Windows 11 with ViGEm virtual pads, and in a real browser |
| GSE `.gm2` reader/writer | Checked against 18 real GSE v0.6 logs (GB, GBC, Game Boy Player) |
| Switch 2 **GameCube controller over USB** (`switch2_usb.py`) | Tested with a real controller: init, calibration, 250 Hz stream, sticks, replug. **Button mapping not yet confirmed by pressing** (it follows SDL 3.4's driver) |
| BizHawk `.bk2` writer, GSE → bk2 conversion | Built from BizHawk's source; not yet loaded in a real BizHawk |
| Virtual controller replay / bridge (ViGEmBus) | Tested on Windows (XInput read-back) |
| Android capture over adb | Tested only against a simulated adb |
| **GC Bridge** Android app (USB-C, no PC) | Builds, unit-tested; **not yet run on a phone** |
| Switch 2 over Bluetooth LE (`switch2_ble.py`) | Built from protocol research and sniffer captures; **never run against a controller** |
| BizHawk optimizer (Lua socket bot) | Tested against a model of BizHawk under a real Lua runtime; not against EmuHawk |

There are 650 automated tests (`pytest`); see [Tests](#tests).

## Quick start (Windows)

Needs **Python 3.14+** (reading GSE's zstd-compressed `.gm2` logs uses the
standard library's `compression.zstd`). From the repository folder, in PowerShell:

```bash
py -3.14 -m venv .venv
```
```bash
.venv\Scripts\python -m pip install -e .[all,dev]
```
```bash
.venv\Scripts\python scripts\fetch_sdl3.py
```

* `[all]` adds ffmpeg for video output, `bleak` for the Bluetooth Switch 2
  reader, and `pyusb`/`libusb-package`/`hidapi` for the USB Switch 2 reader.
  `[dev]` adds the test tools.
* `fetch_sdl3.py` downloads the official SDL3 runtime from libsdl-org into `vendor/`.
* Replay and `--bridge` need the ViGEmBus driver
  (<https://github.com/nefarius/ViGEmBus/releases>, install 1.22.0 **before**
  the pip step: otherwise `vgamepad` pops up an installer for the old 1.17
  driver and pip seems frozen until you close it).
* Run commands as `.venv\Scripts\controllerlog <command>`. To type just
  `controllerlog`, activate the venv: in PowerShell first run
  `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force`, then
  `.venv\Scripts\Activate.ps1` (in Command Prompt: `.venv\Scripts\activate.bat`).

Then:

```bash
.venv\Scripts\controllerlog doctor
```
```bash
.venv\Scripts\controllerlog live --record --open
```

`doctor` checks SDL3, ViGEmBus, HidHide, your Bluetooth adapter, adb and ffmpeg
(it takes about 15 s). `live` shows the overlay at
`http://127.0.0.1:8765/?layout=auto` (add it to OBS as a **Browser Source**;
the background is transparent) and records to `recordings\`. In the console:
`m` marker, `s` split, `r` reset, `q` quit.

## Switch 2 GameCube / Pro controller

* **USB-C on a PC:** just plug it in. `live` picks it up automatically (and
  after replugs); `controllerlog switch2 usb` shows its live buttons, sticks and
  report rate. It reads the controller's factory stick calibration and streams
  at 250 Hz. ControllerLog releases the controller's USB command channel after
  start-up, so GSE can still use it at the same time.
* **Bluetooth on a PC:** `controllerlog switch2 scan` / `switch2 test`
  (experimental, untested on hardware; Windows limits it to about 66 Hz).
* **Android:** the controller has a hidden *standard HID gamepad* mode (report
  0x0A) that a small app can switch on over USB. **GC Bridge** does that when
  you plug the controller into your phone, so Android should see a normal
  gamepad in every game. See [android/README.md](android/README.md). Bluetooth
  on Android needs more work (see [Open work](#open-work)).

## Android

Two separate things:

1. **Capture a controller paired to your phone, from the PC** (no app on the
   phone): enable USB or Wireless debugging and run
   `controllerlog live --adb`. See [docs/ANDROID.md](docs/ANDROID.md).
2. **GC Bridge app** (no PC needed): makes the Switch 2 GameCube/Pro controller
   work over USB-C on the phone. Build and install instructions in
   [android/README.md](android/README.md).

## Commands

| command | what it does |
|---|---|
| `devices` | List controllers (SDL3, Switch 2 over USB, ViGEmBus, `--adb` phones) |
| `live` | Capture → live overlay; `--record` writes a `.ctlog`; `--bridge x360\|ds4` mirrors the controller onto a virtual Xbox 360 / DualShock 4 pad; `--adb` adds a phone's controllers; `--switch2 [ADDR]` adds the Bluetooth Switch 2 reader; `--no-switch2-usb` turns off the USB one |
| `view [FILE]` | Recording viewer: piano roll, frame grid, markers, playback, stats, compare two attempts |
| `stats FILE` | Press counts, hold times (frames) and mash rates per button |
| `replay FILE` | Play a `.ctlog`/`.gm2`/`.bk2`/`.csv` back through a virtual controller |
| `render FILE OUT` | Input-display video (`.mp4/.webm/.mov`), GIF/WebP or PNG sequence, frame-aligned |
| `convert IN OUT` | Convert between `.ctlog`, `.gm2`, `.bk2` and `.csv` |
| `edit IN OUT --do "hold 120-130 east"` | TAS-style frame edits (hold, release, set, clear, insert, delete, copy, axis) |
| `diff A B` | Frame-by-frame input differences |
| `align HOST.ctlog RUN.gm2` | Compare a live recording with GSE's log: offset, drift, presses GSE never registered |
| `gm2 info FILE` / `gm2 list` | Inspect GSE input logs (default `%APPDATA%\GSE\Input Log`) |
| `layouts` | List overlay layouts (Xbox, PlayStation, Switch, GameCube, Game Boy, GBA, generic) |
| `doctor` | Read-only setup check |
| `optimize` | Search a window of a BizHawk `.bk2` for better inputs, evaluated in BizHawk |
| `switch2 usb\|scan\|test` | Switch 2 controllers: USB reader, Bluetooth scan/test |

Run `controllerlog <command> --help` for all options.

### Overlay URL parameters

All optional, e.g. `http://127.0.0.1:8765/?layout=gameboy&fps=gb&bg=chroma`:

| parameter | values | default |
|---|---|---|
| `layout=` | `auto` (matches the controller) or a layout name | `auto` |
| `device=` | `auto` (the active pad) or a device id | `auto` |
| `history=` | `1` / `0`: scrolling input-history lane | `1` |
| `seconds=` | length of the history lane | `4` |
| `fps=` | frame grid: `60`, `gb`, `gba`, `nes` or a number | `60` |
| `bg=` | `transparent`, `chroma` (`#00ff00`) or a hex colour | `transparent` |
| `scale=` | size factor | `1` |
| `map=` | display remap `src:dst,...` (see [docs/LAYOUTS.md](docs/LAYOUTS.md)) | none |
| `labels=` / `counts=` | `1` / `0`: button labels / press counters | `1` / `0` |

## GSE (Game Boy Speedrun Emulator) workflow

GSE writes an authoritative `.gm2` log for every session. ControllerLog can't
*record* a `.gm2` itself (GSE decides frame boundaries inside its emulation
loop), so it builds on GSE's own logs:

```bash
controllerlog gm2 list
```
```bash
controllerlog render "<log>.gm2" inputs.mp4 --fps gb --layout gameboy
```
```bash
controllerlog convert "<log>.gm2" run.bk2
```
```bash
controllerlog edit "<log>.gm2" run_tas.gm2 --do "delete 1200 3" --do "hold 1500 east"
```

Edited `.gm2` files keep GSE's header, start save and cycle budgets, so they
still replay in [InputLogPlayer](https://github.com/CasualPokePlayer/InputLogPlayer).
An edited `.gm2` carries no marker that it was edited: label such files
yourself and never submit them as a real-time run. Details:
[docs/TAS.md](docs/TAS.md), [docs/FORMATS.md](docs/FORMATS.md).

## Developing in the cloud

Everything that doesn't touch hardware works on Linux (e.g. a cloud dev box):

```bash
python3.14 -m venv .venv && . .venv/bin/activate
```
```bash
pip install -e ".[video,usb,dev]"
```
```bash
pytest -q
```

* Tests that need SDL3, the ViGEmBus driver or a real controller skip
  themselves when those aren't there. Install SDL3 from your distribution
  (e.g. `libsdl3`) if you want the SDL tests to run.
* `vgamepad` (ViGEmBus) is Windows-only and isn't installed on Linux.
* Building the Android app needs JDK 21 and the Android SDK (platform
  `android-37.0`, build-tools `37.0.0`); set `ANDROID_HOME` and run
  `./gradlew assembleDebug testDebugUnitTest` in `android/gcbridge`. The unit
  tests cross-check the app's USB commands against
  `controllerlog/input/switch2_usb.py`.
* Can't be done from the cloud: anything with the physical controller, the
  phone, ViGEmBus, GSE or BizHawk. Those need the Windows PC.

## Open work

1. **Confirm the Switch 2 GameCube buttons over USB**: press every button with
   `controllerlog switch2 usb` running and check each lands on the right input.
2. **Try GC Bridge on the phone** (Galaxy Z TriFold): does Android create a
   gamepad after the 0x0A switch, and how do the buttons and axes map?
3. **Test the Bluetooth Switch 2 reader on the PC** (`switch2 test`).
4. **Bluetooth on Android**: the controller never offers a standard mode over
   Bluetooth, so GC Bridge would need to speak Nintendo's protocol (port of
   `switch2_ble.py`) and create a virtual gamepad through Shizuku (uhid).
5. Smaller fixes found during install checks: `doctor` doesn't find adb in
   `C:\platform-tools` or `%USERPROFILE%\Android\Sdk`; `live --host 0.0.0.0`
   prints the 127.0.0.1 URL instead of the LAN address; `scripts/fetch_sdl3.py`
   shows a raw traceback on network errors; `doctor` prints nothing for ~15 s.
6. Load generated `.bk2` files in a real BizHawk, and run the optimizer against EmuHawk.

## Docs

* [docs/BLUETOOTH.md](docs/BLUETOOTH.md): which controllers work, how "incompatible" ones are made to work, Switch 2, the full-Bluetooth-stack option
* [docs/ANDROID.md](docs/ANDROID.md): capturing controllers on Android over adb
* [android/README.md](android/README.md): the GC Bridge app
* [docs/TAS.md](docs/TAS.md): replay, BizHawk export, TAS editing, `align`, optimization
* [docs/FORMATS.md](docs/FORMATS.md): `.ctlog`, `.gm2`, `.bk2` and `.csv` specs
* [docs/LAYOUTS.md](docs/LAYOUTS.md): overlay/renderer layout format

## Tests

```bash
.venv\Scripts\python -m pytest
```

On Windows with the ViGEmBus driver installed, the `vigem`/`sdl` tests drive
virtual Xbox/DS4 pads through the real SDL3 stack.

## Credits and license

Protocol and format knowledge comes from reading the sources and docs of
[SDL](https://github.com/libsdl-org/SDL), [GSE](https://github.com/CasualPokePlayer/GSE),
[BizHawk](https://github.com/TASEmulators/BizHawk),
[BlueRetro](https://github.com/darthcloud/BlueRetro) and
[ndeadly's Switch 2 research](https://github.com/ndeadly/switch2_controller_research).
No code from those projects is copied. No license has been chosen for this
project yet.
