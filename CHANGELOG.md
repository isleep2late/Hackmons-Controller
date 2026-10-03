# Changelog

## v0.2.2

GC Bridge only (the APK, `releases/v0.2.2/`); the PC bundles stay at 0.2.1.

* **AYN Thor**: the NSO GameCube pad reaches Android there only as AYN's copy (ids 2020:0111, the
  same as the Thor's built-in "Odin Controller", with the pad's name). 0.2.1 drew it as a Switch
  pad and read its buttons by key code, so every button was wrong, D-down and D-right did
  nothing and the sticks were upside down. 0.2.2 recognises the copy by those ids plus
  "GameCube" in the name, reads it in the pad's own report order, flips both sticks' Y and
  scales them by the pad's calibration spans (the copy reaches only about 0.6 of full range).
  Home, Capture, C and the C-stick's left/right are not delivered by that copy; the overlay
  dims them and USB capture still reads them. The built-in controls are left as they were.
  Measured on the Thor: device dumps and a press of every button (2026-10-02).
* One set of classification rules shared with isleep2late and iswitch2late, pinned by JSON
  fixtures in `tests/fixtures/pads/` built from those Thor captures; "gamecube" in a name is
  now tested before "nintendo".
* New **Controllers** section: the rule and reason per controller, **Ignore**, **Draw as**,
  **Set up this controller** (guided calibration with a live test, saved as a profile;
  export and import as JSON), **Export diagnostics** (clipboard, file, logcat). A device that
  starts sending codes its raw report never has is read by key code from then on, with a log
  line.
* The volume keys always reach Android, even from a gamepad.
* Background CPU: Android killed 0.2.1 in the background on the Thor ("excessive cpu 7450
  during 300064", limit 2 %). Cause found in the Thor log: the main screen kept its input-device
  listener while in the background, and on the Thor's two screens Android re-sends "input
  device changed" for the touch screens and AYN's virtual mouse on every display change (every
  few seconds while another app runs); each one rewrote a log of up to 60,000 characters and
  re-listed every input device, about 90 ms of main-thread time each (76 callbacks in the 150 s before the
  kill). The screen now listens only while it is visible, coalesces device refreshes, logs
  changes of gamepads only and redraws the log at most every 100 ms. Not yet measured on the
  Thor itself.
* `scripts/build_apk_nosdk.py --variant dev` builds "GC Bridge DEV"
  (`com.controllerlog.gcbridge.dev`, own recordings provider and Downloads folder, no USB
  attach chooser) to install beside the normal app.
* `controllerlog live --adb`: the same recognition, flip and scale for AYN's copy.

## v0.2.1

* The APK and both portable bundles are committed under `releases/v0.2.1/` for direct download.
* GC Bridge: correct button order and stick direction for the Switch 2 GameCube / Pro
  controller in gamepad mode (Android's generic HID driver numbers the report's buttons B, A,
  Y, X, R, ZR, Start, ... and the report's stick Y grows upwards). Confirmed by pressing on the
  GameCube controller.
* `controllerlog live --adb` gets the same table for these controllers.
* USB / Bluetooth readers (PC and app): the GameCube's A, B, X, Y bits are the ones named A,
  B, X, Y, so A is now `south` and B `west` (they were read like a Pro Controller's before).

## v0.2.0

### GC Bridge 0.2 (Android)

* Records any controller Android supports to `.ctlog`, the format the PC tools read
  (`controllerlog view` / `stats` / `render` / `convert` / `edit`). Record, Marker and Stop on
  the screen and in the notification; Recordings list with Share and Copy to Downloads.
* Floating controller overlay over other apps (drag, tap to collapse, size and opacity),
  drawn from the same layout files as the PC overlay.
* "Button capture": an accessibility service that keeps logging gamepad buttons while a game
  is in front. Sticks and hat D-pads are only seen while GC Bridge is in front, over USB
  capture or over Bluetooth capture (Android gives axes to the focused app only).
* USB capture: GC Bridge reads the Switch 2 GameCube / Pro controller itself (calibration from
  flash, Nintendo report, full analog triggers, 250 reports/s) in the background.
* Experimental Bluetooth LE reader for the same controllers (no pairing).
* Gamepad mode (the 0.1 behaviour: standard HID report over USB-C) is unchanged.
* 33 JVM unit tests cross-check the app against the Python side.
* Not yet run on a phone.

### PC

* Portable Windows and Linux programs (`controllerlog-0.2.0-windows-x64.zip`,
  `controllerlog-0.2.0-linux-x64.tar.gz`): own Python 3.14, all dependencies, SDL3; double-click
  `live.cmd` / `doctor.cmd` / `view.cmd` (Windows) or run `./live` (Linux).
* The Bluetooth Switch 2 reader maps the GameCube face buttons like SDL 3.4 and the USB reader
  (to be confirmed on hardware).
* `scripts/build_bundle.py` (portable programs) and `scripts/build_apk_nosdk.py` (APK without
  the Android SDK); CI and Release workflows.
* MIT license.

### Files in this release

| File | What it is |
|---|---|
| `GCBridge-0.2.apk` | the Android app (debug-signed; open it on the phone or `adb install -r`) |
| `controllerlog-0.2.0-windows-x64.zip` | portable Windows program, unzip anywhere |
| `controllerlog-0.2.0-linux-x64.tar.gz` | portable Linux program (x86-64) |
