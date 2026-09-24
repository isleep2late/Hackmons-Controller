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

## Why not an Android app?

| Approach | Verdict |
|---|---|
| AccessibilityService key filter | Sees buttons only. No sticks, no analog triggers, and **no D-pad** on DS4/DualSense/Xbox pads (Android converts their hat D-pad inside the focused app). Useless for Game Boy runs. |
| Accessibility motion events (Android 14+) | *Consumes* the events, so the game stops receiving them. |
| Overlay window that takes focus | Steals input from the game. |
| Shizuku / ADB-started helper reading `/dev/input` | Works, and is what a future native app would use. Same technique as the ADB capture above, with an on-phone overlay (`TYPE_APPLICATION_OVERLAY`, not focusable, opacity ≤ 0.8). |
| Custom Bluetooth host stack (BlueRetro-style) | Not possible without root. Classic L2CAP sockets and the HID host API are system-only. |

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
