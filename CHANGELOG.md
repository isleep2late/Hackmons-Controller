# Changelog

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
