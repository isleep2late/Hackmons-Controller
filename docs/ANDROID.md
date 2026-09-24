# Android

Android's own Bluetooth stack already handles most controllers natively.
DualShock 4, DualSense, Switch Pro, Joy-Con, Xbox (BT) and 8BitDo all work
through the kernel drivers `hid-playstation`, `hid-sony`, `hid-nintendo` and
`xpad`, which are built into every GKI kernel since Android 12. So the
problem on Android isn't connecting a controller. It's **seeing its inputs
while a game or emulator has focus**. Android doesn't let a normal app read
another app's controller input.

## What works: capture over ADB (no app to install)

ADB's shell user belongs to the `input` group, so it can read `/dev/input/event*`.
ControllerLog runs `getevent` over ADB from your PC. That captures the
controller paired to the phone **system-wide**, including while GSE Android,
an emulator or any game is running. Events flow into the same pipeline:
overlay, recording, stats, rendering.

1. On the phone, enable **Developer options → USB debugging**. On Android 11+
   you can use **Wireless debugging** instead.
2. On the PC, install [Android SDK Platform-Tools](https://developer.android.com/tools/releases/platform-tools)
   (`adb`), connect the phone, and accept the debugging prompt.
3. Pair your controller to the phone as usual.
4. Run:

```bash
controllerlog devices --adb               # phones and the gamepads each one exposes
controllerlog live --adb --record         # overlay + recording from the phone's controller
controllerlog live --adb --no-sdl         # only the phone, not PC controllers
```

Notes:

* `getevent` doesn't flush its output when it's piped, so ControllerLog runs
  `adb shell -tt` (a PTY). Without it, events would arrive in delayed bursts.
* Timestamps come from the phone's `CLOCK_MONOTONIC`. They're mapped onto the
  PC's clock with a running minimum-offset estimate, which filters out USB and
  Wi-Fi jitter and tracks clock drift.
* Button naming is positional, as everywhere in ControllerLog. The Nintendo
  **B** button arrives as `BTN_SOUTH`, i.e. `south`. Mainline `hid-nintendo`
  has been positional in every version from 5.16 onward. For vendor kernels
  with patched drivers, the ADB backend has a `nintendo_swap` option and
  per-device profile overrides.
* A hat-based D-pad (DS4, DualSense, Xbox) is read from `ABS_HAT0X/Y`, so the
  D-pad works, which accessibility-service approaches can't manage (see below).

## The GC Bridge app

[android/README.md](../android/README.md) describes the app. In short, it logs input on the
phone to `.ctlog`, shows a floating overlay, and reads the Switch 2 GameCube / Pro controller
over USB or (experimentally) Bluetooth LE. What each approach on Android can and can't see:

| Approach | Used by GC Bridge for | Limits |
|---|---|---|
| The app's own screen in front | everything: buttons, sticks, triggers, hat D-pads, of any controller Android supports | only while GC Bridge is the focused app |
| AccessibilityService key filter ("Button capture") | buttons system-wide, while a game is in front | no sticks, no analog triggers, and **no D-pad** on DS4/DualSense/Xbox pads (Android converts their hat D-pad inside the focused app) |
| Accessibility motion events (Android 14+) | not used | *consumes* the events, so the game stops receiving them |
| Overlay window that takes focus | not used | would steal input from the game; GC Bridge's overlay is not focusable |
| Reading the controller itself over USB / BLE ("USB capture", "Bluetooth capture") | full input, in the background | only Switch 2 controllers; Android has no gamepad for it while GC Bridge holds it |
| Shizuku / ADB-started helper reading `/dev/input` | not yet | would give system-wide sticks without a PC, same technique as the ADB capture above |
| Custom Bluetooth host stack (BlueRetro-style) | no | not possible without root: classic L2CAP sockets and the HID host API are system-only |

**Showing the overlay on the phone:** start `controllerlog live --host 0.0.0.0`
on the PC and open `http://<pc-ip>:8765/` in the phone's browser, or in a
floating-window browser over the game. There is no password: while it runs,
anyone on the same network can open the overlay, the live input stream and
every recording in `--dir` (including controller serials), so only do this on
a network you trust (not venue or event Wi-Fi). The server answers only to IP
addresses, `localhost` and the PC's own name, which blocks DNS-rebinding
attacks from web pages; add other host names with `--allow-host NAME`.

**GSE on Android** writes its own `.gm2` logs, in
`Android/data/org.psr.gsr/files/Input Log/` on typical devices. Copy them to the
PC, e.g. `adb pull`, and use every `.gm2` feature (stats, render, convert, edit).
