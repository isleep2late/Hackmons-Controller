# GC Bridge (Android)

A small test app for one idea: can an Android phone use the **NSO GameCube controller for
Switch 2** (USB `057E:2073`) or the **Switch 2 Pro Controller** (`057E:2069`) as a normal
gamepad in every game, with no root?

The controller only sends input after it gets an init sequence on its vendor USB interface
(interface 1, bulk endpoints). Linux (and so Android) should already bind its HID interface
(interface 0) to the generic HID driver. GC Bridge sends the init over interface 1 with the
Android USB host API. It asks for the **standard HID gamepad report (id 0x0A)** instead of
Nintendo's vendor report (0x05), so the kernel should turn the stream into a regular gamepad.
It never claims interface 0, except in the optional "Peek" debug action.

**Status: not tested on a phone yet.** The USB protocol was verified on Windows with a real
GameCube controller (see `controllerlog/input/switch2_usb.py`), including switching live
between the 0x05 and 0x0A streams. What Android does with the 0x0A stream is exactly what this
app is meant to find out.

## What the app does

- **Start controller** runs automatically when the controller is plugged in (if you let GC
  Bridge open for it), or when you tap the button. It:
  1. opens the device and claims the vendor interface (class 0xFF),
  2. reads the serial number from flash (a sanity check),
  3. sends the 10 init commands (the 9th one sets the report format),
  4. sets player LED 1,
  5. releases the interface and closes the connection.

  Every command and reply is logged in hex, on screen and in Logcat.
- **Report format**: "Standard gamepad (0x0A)" is the default. "Nintendo/SDL format (0x05)"
  is only for debugging, because the kernel ignores that vendor report.
- **Input devices** lists every Android input device: name, descriptor, vendor:product,
  sources (GAMEPAD / JOYSTICK / DPAD / KEYBOARD...), axis ranges and which gamepad keys it
  reports. The list updates when devices are added, removed or changed, and those events are
  also written to the log.
- **Live gamepad input** shows every axis of the last-used gamepad (X, Y, Z, RX, RY, RZ,
  HAT_X/Y, LTRIGGER, RTRIGGER, BRAKE, GAS, plus any others the device has) with 2 decimals,
  the motion event rate, and the buttons held down.
- **Key events** shows the last 30 key presses, each with its Android key code and Linux scan
  code. For codes in the gamepad range it also guesses which HID button number (1-21) was
  pressed.
- Gamepad keys are captured by the app, so B/BACK can't close it while you test. To leave,
  use the phone's own back gesture or the **Exit** button.
- **Copy all** copies the status, log, events and device list to the clipboard so you can
  paste them somewhere.
- **Peek HID reports** (debug) claims the HID interface for 2 seconds and prints the raw
  reports. It shows whether the controller is streaming at all, and in which format. This
  detaches the kernel HID driver while it runs, so the gamepad disappears for those 2 seconds.
  Android should reattach the driver when Peek releases the interface; if the gamepad doesn't
  come back in **Input devices**, unplug and replug the controller.

## Build

The build needs JDK 21 and the Android SDK (platform android-37.0, build-tools 37.0.0). Both
are already on this PC, inside the Claude desktop app's private storage.

```powershell
cd android\gcbridge   # from the repository root
$env:JAVA_HOME    = "$env:USERPROFILE\Android\jdk-21"   # any JDK 21
$env:ANDROID_HOME = "$env:USERPROFILE\Android\Sdk"
.\gradlew.bat assembleDebug            # -> app\build\outputs\apk\debug\app-debug.apk
.\gradlew.bat testDebugUnitTest        # JVM unit tests for the command bytes
.\gradlew.bat lintDebug                # optional
.\gradlew.bat --stop                   # stop the Gradle daemon when you're done
```

`local.properties` (not meant for version control) holds `sdk.dir` for this PC. Versions:
Android Gradle Plugin 9.4.1, Gradle 9.7.1 (wrapper), compileSdk 37, targetSdk 36, minSdk 29.
The code is plain Java with framework Views and has no library dependencies (only JUnit for
the tests).

The unit tests check the init sequence byte for byte against `INIT_SEQUENCE` in
`switch2_usb.py`. They also parse the Python file directly, so the two copies can't drift
apart. And they check that the format byte (0x05 / 0x0A) is the only difference between the
two modes.

## Install

1. On the phone, enable Developer options: Settings > About phone > Software information >
   tap **Build number** 7 times.
2. Settings > Developer options: turn on **USB debugging**. Because the TriFold has only one
   USB-C port, which the controller will use, also turn on **Wireless debugging**.
3. Install from the PC. Pick one of the two ways below.

   Over USB, before plugging in the controller:

   ```powershell
   $adb = "$env:USERPROFILE\Android\Sdk\platform-tools\adb.exe"
   & $adb install -r android\gcbridge\app\build\outputs\apk\debug\app-debug.apk
   ```

   Or over Wi-Fi (same network). In Wireless debugging, tap **Pair device with pairing code**,
   then:

   ```powershell
   & $adb pair <ip>:<pairing-port>      # enter the 6-digit code
   & $adb connect <ip>:<port>           # the port shown on the Wireless debugging screen
   & $adb install -r ...\app-debug.apk
   ```

   You can also copy the APK to the phone and open it there (allow "install unknown apps" for
   the file manager when asked).

   If the phone blocks the install or USB debugging, check **Auto Blocker** (Settings >
   Security and privacy > Auto Blocker). Recent One UI versions may turn it on by default, and
   it blocks apps from outside the Play Store and Galaxy Store. Turn it off to install.

## Use

1. Connect the controller to the phone with a USB-C **data** cable. Some cables only charge.
2. Android asks whether to open GC Bridge for the controller. Tick "always" and tap OK. The
   app opens, gets USB permission and sends the init right away. If you open the app from the
   launcher instead, tap **Start controller** and allow USB access.
3. Check **Input devices** for an entry with `057e:2073`. It should be marked `*`, with
   GAMEPAD / JOYSTICK in its sources. If it's there even before Start, the kernel bound the
   HID interface.
4. Press buttons and move the sticks. Watch **Live gamepad input** and **Key events**, then
   try a game.
5. Send the results: tap **Copy all** and paste them somewhere, or watch over Wi-Fi adb:

   ```powershell
   & $adb logcat -s GCBridge
   ```

   Motion events are logged at most 10 times per second; every key event is logged.

The controller needs the init again after every replug. That happens automatically if you
ticked "always open GC Bridge".

## What to expect / how to read the results

- **The best case**: a gamepad appears, and after Start the buttons and sticks produce events.
  Games that support standard Android gamepads should then just work.
- **The standard report has only 4 axes**: X/Y (left stick) and Rx/Rz (right stick). Android
  and most games expect the right stick on Z/RZ, so the right stick's horizontal axis may show
  up as RX and be ignored by some games.
- **The GameCube controller's analog L/R are probably not in the standard report.** They will
  likely show up only as buttons (the full-press click), not as LTRIGGER / RTRIGGER values.
- **Buttons 17-21** of the report become `BTN_TRIGGER_HAPPY` codes in Linux. Android may show
  these as unknown key codes.
- **No input device at all**: the kernel didn't create one for interface 0, or Android filtered
  it out. Use **Peek HID reports** to check whether `id 0x0A` reports arrive (about 250 per
  second).
  - If they arrive, the controller side works and the problem is on the Android/kernel side.
  - If nothing arrives, the init didn't take. The USB log shows which command failed.
- **In 0x05 mode**, no gamepad input is expected. That mode is only there to compare with the
  Windows behaviour.

## Layout

```
android/gcbridge/
  settings.gradle.kts, build.gradle.kts, gradle.properties, gradlew(.bat), gradle/wrapper/
  app/build.gradle.kts
  app/src/main/AndroidManifest.xml              USB host feature, attach intent-filter
  app/src/main/res/xml/device_filter.xml        vendor 1406, products 8307 / 8297
  app/src/main/java/com/controllerlog/gcbridge/
    Switch2Protocol.java   command bytes, report decoding (pure Java, unit-tested)
    Switch2Usb.java        USB host code: init over interface 1, optional HID peek
    InputDiagnostics.java  input device / source / axis descriptions
    MainActivity.java      UI, USB permission, device listener, key/motion capture
  app/src/test/java/com/controllerlog/gcbridge/Switch2ProtocolTest.java
```
