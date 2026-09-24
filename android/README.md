# GC Bridge (Android)

The Android side of Hackmons Controller. One app, four jobs:

1. **Log controller input on the phone** to the same `.ctlog` format the PC tools use, so a
   recording made on the phone opens in `controllerlog view`, `stats`, `render`, `convert` and
   `edit` like one made on the PC.
2. **Show a floating controller overlay** ("display over other apps") whose buttons light up as
   you press them, movable and collapsible, drawn from the same layout files as the PC overlay
   (Xbox, PlayStation, Switch, GameCube, Game Boy, GBA, generic).
3. **Make the Switch 2 GameCube / Pro controller work over USB-C** as a normal Android gamepad
   (the original GC Bridge idea: it sends the controller's init sequence and asks for the
   standard HID report).
4. **Read the Switch 2 GameCube / Pro controller itself**, over USB (full analog triggers, 250
   reports/s) or, experimentally, over Bluetooth LE (no dongle, no pairing).

**Status: gamepad mode and the overlay have been tried on a Galaxy Z TriFold (0.2 showed the
buttons in the wrong places, fixed in 0.2.1).** Recording, button capture, USB capture and the
Bluetooth reader still wait for their first test. Please copy the app's log (Copy all) after
trying each part.

## Install

Download `GCBridge-<version>.apk` from the repository's Releases page (or build it, see below)
and either open it on the phone (allow "install unknown apps" for your browser / file manager
when asked) or install it from the PC:

```powershell
$adb = "$env:USERPROFILE\Android\Sdk\platform-tools\adb.exe"
& $adb install -r GCBridge-0.2.apk
```

If One UI blocks the install, turn off **Auto Blocker** (Settings > Security and privacy) for
the moment. The APK is debug-signed, like any `assembleDebug` build.

## Permissions the app asks for, and when

| Permission | Needed for | How to grant |
|---|---|---|
| USB device access | Start controller, USB capture | Android asks when you plug the controller in or tap the button |
| Display over other apps | the overlay | Show overlay opens the settings page the first time |
| Notifications (Android 13+) | the "recording…" notification with its Marker / Stop buttons | asked on the first Record |
| Accessibility service | button capture while a game is in front | Settings > Accessibility > Installed apps > GC Bridge button capture |
| Bluetooth (Nearby devices) | the Bluetooth reader | asked on Scan & connect |

The accessibility service reads **only gamepad / joystick key events** (see
`InputRouter.onKey`): keyboard typing, the screen and other apps' content are never read, and
no event is blocked or changed. On Android 13+ a sideloaded app can't be enabled as an
accessibility service until you allow restricted settings: **App info > ⋮ > Allow restricted
settings**, then enable it.

## Using it

The screen has these sections, top to bottom.

### Log & overlay

* The live preview draws the controller you used last.
* **Record** starts a `.ctlog` (a foreground service keeps it going while you play); **Stop
  recording** closes it. **Marker** writes a `m` row (a split) into the recording, also
  available from the notification.
* **Show overlay** puts the floating controller over other apps. Drag it to move it; tap it to
  collapse it into a small badge and tap the badge to bring it back. The layout, size and
  opacity are set below the buttons (`auto` picks the layout from the controller family).
* **Recordings…** lists the files, and offers Share (send to your PC, Drive, email...), Copy to
  Downloads (`Download/GC Bridge/`) and Delete. The files live in
  `Android/data/com.controllerlog.gcbridge/files/recordings/`, which is visible from a PC over
  USB file transfer, and are named `gcbridge_<date>_<time>.ctlog`.

What gets logged, by source:

| Source | Buttons | Sticks, triggers, hat D-pad | Works with a game in front? |
|---|---|---|---|
| Any controller Android supports, while the GC Bridge screen is in front | yes | yes | no (the game gets the input, not GC Bridge) |
| Same, with **Button capture** (accessibility) on | yes | no: Android delivers axes only to the focused app, and turns the DS4 / DualSense / Xbox hat D-pad into keys inside that app | **yes** |
| **USB capture** of the Switch 2 GameCube / Pro controller | yes | yes, full analog triggers, 250 Hz | yes, but the game can't see the controller while it runs |
| **Bluetooth capture** (experimental) | yes | yes | yes, but Android never sees a gamepad this way |

For system-wide capture with sticks, the PC route still exists:
`controllerlog live --adb` reads the phone's controllers over ADB (docs/ANDROID.md).

### Button capture everywhere

Tap **Accessibility settings**, enable *GC Bridge button capture*. The status line on the
screen says ON once Android has started the service. Buttons from every gamepad are then
logged and shown on the overlay in any app.

### USB: gamepad mode and capture mode

* **Start controller (gamepad mode)** is what GC Bridge 0.1 did: it runs automatically when
  the controller is plugged in (if you let GC Bridge open for it), sends the init sequence
  with the standard report (0x0A) selected, releases the controller and lets Android's own
  HID driver turn it into a gamepad for every game. The *Nintendo/SDL format (0x05)* radio
  button is a debug option: the kernel ignores that report.
* **Start USB capture** is the new mode: GC Bridge reads the controller itself. It reads the
  stick calibration and trigger rest values from the controller's flash, initialises it in
  Nintendo format, then claims the HID interface and streams every report into the log and
  the overlay, in the background. While it runs Android has **no** gamepad for this
  controller (claiming the interface detaches the kernel driver); Stop USB capture releases
  it, and a replug always brings the gamepad back.

**Button mapping, confirmed on the real controller (gamepad mode).** Android has no kernel
driver for the Switch 2 pads, so its generic HID driver numbers the standard report's 21
buttons in the report's own order: B, A, Y, X, R, ZR, Start, RS, D-down, D-right, D-left, D-up,
L, ZL, Minus, LS, Home, Capture, GR, GL, C. On the GameCube controller the R and L triggers
report their click in the R / L slots and Z sits in the ZR slot. GC Bridge 0.2.1 maps that
order positionally (A = bottom, B = left, X = right, Y = top, D-pad, Start, Z, and the trigger
clicks light the L / R bars) and flips the sticks' Y axis, which the report sends "up =
positive". The PC's `controllerlog live --adb` uses the same table. The same evidence fixed the
Nintendo-report (0x05) readers used by USB capture, Bluetooth capture and the PC: the
GameCube's A, B, X, Y live in the bits of those names, so A is `south` and B is `west`.

### Bluetooth capture (experimental)

Hold the controller's SYNC button until its LEDs run back and forth, tap **Scan & connect**
and grant the Bluetooth permission. The app scans for Nintendo's manufacturer data (company
0x0553), connects without pairing (a pairing attempt makes these controllers disconnect, so
never pair it in Android's Bluetooth settings), asks for a 247-byte MTU and a high connection
priority, subscribes to the report 0x05 notifications, sends the player LED and feature
commands, and reads the calibration blocks. It is a port of `controllerlog/input/switch2_ble.py`,
which has itself never been run against a real controller: expect to send logs. If the log
says the notifications are only 20 bytes, the MTU request was refused and sticks can't be
decoded.

Android itself never sees a gamepad this way (that would need a virtual input device, i.e.
root or Shizuku); the reader only feeds the log and the overlay.

### Diagnostics

The lower half of the screen is unchanged from 0.1: raw axes and key events of the last-used
gamepad, the USB log with every command and reply in hex, the list of Android input devices
with their sources and axes, and **Peek HID reports** (claims the HID interface for 2 seconds
and prints what arrives). **Copy all** puts everything on the clipboard.

## Build

With the Android SDK (JDK 21, platform android-37, build-tools 37.0.0):

```powershell
cd android\gcbridge
$env:JAVA_HOME    = "$env:USERPROFILE\Android\jdk-21"
$env:ANDROID_HOME = "$env:USERPROFILE\Android\Sdk"
.\gradlew.bat assembleDebug testDebugUnitTest lintDebug
.\gradlew.bat --stop
```

The APK is `app\build\outputs\apk\debug\app-debug.apk`. `local.properties` (not in version
control) holds `sdk.dir`. Versions: Android Gradle Plugin 9.4.1, Gradle 9.7.1, compileSdk 37,
targetSdk 36, minSdk 29. Plain Java, framework views only, no library dependencies (JUnit for
the tests). The Gradle build copies `controllerlog/layouts/*.json` into the APK's assets, so
the overlay always matches the PC layouts.

**Without the SDK** (a machine that can't reach dl.google.com, e.g. some cloud dev boxes):

```bash
python scripts/build_apk_nosdk.py --fetch    # aapt2, R8, android.jar, apksig, JUnit -> vendor/android-nosdk
python scripts/build_apk_nosdk.py --test     # -> app/build/outputs/apk/nosdk/app-debug.apk, runs the unit tests
```

The 33 JVM unit tests check, against the Python side of the repository: the init commands
(byte for byte against `switch2_usb.py`), report decoding and calibration (against vectors
generated by `tests/fixtures/switch2/make_java_vectors.py`), the BLE command bytes, the key /
axis mapping, the `.ctlog` writer (a sample file it wrote is read by the Python tests), the
JSON reader and every layout file.

## Layout

```
android/gcbridge/app/src/main/java/com/controllerlog/gcbridge/
  Pad.java               canonical buttons/axes (= controllerlog/model.py) and PadState
  Json.java              tiny JSON reader/writer (layouts, .ctlog rows)
  Layout.java            layout file model + validation; LayoutStore.java loads the assets
  PadView.java           draws a layout for a state (preview and overlay)
  OverlayWindow.java     the floating window: drag, collapse, size/opacity prefs
  AndroidInput.java      key codes / scan codes / axes -> canonical (as adb_backend.py)
  InputRouter.java       KeyEvent / MotionEvent -> InputHub
  InputHub.java          state per controller, listeners, recording
  CtlogWriter.java       the .ctlog writer
  CaptureService.java    foreground service: recording, overlay, USB/BLE readers, notification
  KeyCaptureService.java accessibility service (system-wide buttons)
  RecordingStore.java    recording files, Downloads export; RecordingProvider.java for sharing
  Switch2Protocol.java   commands, report decoding, calibration, BLE packets (pure Java)
  Switch2Usb.java        USB host: gamepad-mode init, Peek, and the capture loop
  Switch2Ble.java        Bluetooth LE reader (experimental)
  InputDiagnostics.java  input device descriptions
  MainActivity.java      the screen
app/src/test/java/...    JUnit tests (JVM)
```
