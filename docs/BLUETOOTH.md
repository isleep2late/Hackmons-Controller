# Bluetooth controllers: what works, and how "incompatible" ones are made to work

**Short version.** Most modern Bluetooth controllers already *pair* with
Windows 11's built-in Bluetooth. What makes them "incompatible" is almost
always the layer above: games expect an Xbox (XInput) controller, and Windows
exposes a DualShock, Switch Pro or Wii Remote as some other HID device.
ControllerLog fixes that layer in software:

1. **Read.** SDL3's own HID drivers talk to each controller family directly
   over the Bluetooth HID channel. ControllerLog turns on the drivers SDL leaves
   **off** by default on Windows (DualShock 3, Wii Remote / Wii U Pro, Steam
   Controller). It also asks DualShock 4 pads for 1 ms reports; SDL's default
   is 4 ms.
2. **Log and display** every input: the overlay, recordings, stats and renders.
3. **Re-emit** (optional). `controllerlog live --bridge x360` mirrors the
   controller onto a virtual Xbox 360 pad through ViGEmBus, so XInput-only
   games accept it. This is the same idea as DS4Windows and BetterJoy, but it
   works for every family SDL supports. Use
   [HidHide](https://github.com/nefarius/HidHide) to hide the real pad from the
   game so it doesn't see double input.

Run `controllerlog doctor` to check your setup and `controllerlog devices` to
see what SDL detects.

## Compatibility with only the PC's built-in Bluetooth

Report rates are measured over Bluetooth on Windows 11. A rate near 60 Hz means
roughly one report per frame, so a press shorter than about 15 ms can be
missed, and which frame a press lands on is ambiguous at ±1 frame.

| Controller | Pairs in Windows? | Works in ControllerLog | Notes | BT report rate |
|---|---|---|---|---|
| DualShock 4 | yes (Share + PS) | **yes** | 1 ms reports enabled | ~730 Hz |
| DualSense / Edge | yes (Create + PS) | **yes** | | ~800 Hz |
| Switch Pro | yes (sync button) | **yes** | Full-report mode (IMU) | ~66 Hz |
| Joy-Con L/R | yes, each separately | **yes** | Combined into one pad | ~60 Hz |
| NSO NES / SNES / N64 / Genesis | yes | **yes** | | ~60 Hz |
| Xbox One S / Series / Elite 2 / Adaptive | yes | **yes** | Already XInput; no bridge needed | ~133 Hz (BLE) |
| 8BitDo (S / D / X modes) | yes | **yes** | S mode appears as a Switch Pro | 60–120 Hz |
| Stadia (Bluetooth-mode firmware) | yes | **yes** | Needs Google's BT firmware update | – |
| Steam Controller 2015 (BLE firmware) and 2026 | yes | **yes** | 2026 model supported in SDL 3.4.16 | ~134 Hz |
| Amazon Luna | yes | **yes** | | – |
| Wii Remote (+ Nunchuk / Classic), Wii U Pro | usually (press **1+2**, skip the PIN) | **yes** | SDL's Wii driver is enabled by ControllerLog. It is off by default because it breaks DolphinBar users | ~100 Hz |
| Generic Android / "HID gamepad" | yes | usually | Unknown pads appear with SDL's generic mapping | 60–133 Hz |
| **DualShock 3 / Sixaxis** | **no** | with drivers | Windows rejects DS3 connections at the L2CAP level. Install **BthPS3 + DsHidMini** (signed kernel drivers by Nefarius; BthPS3 v3 also supports PCIe Intel radios). The pad is paired over USB, not the Windows dialog. Put DsHidMini in **SXS** mode (read by SDL's sixaxis driver, which ControllerLog enables) or **XInput** mode. Its DS4Windows mode (7331:0001) isn't recognized by SDL. Wired over USB, a DS3 works without drivers | – |
| **Switch 2 Pro / Joy-Con 2 / NSO GameCube (Switch 2)** | connects, but no gamepad appears | experimental | Bluetooth LE with Nintendo's proprietary GATT protocol, not HID. SDL 3.4 supports these over **USB** only, and on Windows that path also needs a libusb-1.0 runtime. ControllerLog's experimental Bluetooth LE reader is `controllerlog switch2`; see "Switch 2 controllers" below | ~16–70 Hz on Windows |
| Original Xbox One pad (1537/1697), Elite Series 1, Xbox 360 wireless | – | **impossible over BT** | Not Bluetooth radios (Xbox Wireless / 360 RF). They need Microsoft's adapter or a USB cable | – |
| 2.4 GHz dongle modes (8BitDo, Logitech, Steam Puck), Wii U GamePad, WaveBird | – | **impossible over BT** | Not Bluetooth | – |

### Running alongside a game

* **Enhanced reports.** SDL switches PlayStation and Switch pads to their
  full-rate "enhanced" report mode. On PlayStation pads this **breaks
  DirectInput for non-SDL games** until the pad is power-cycled. If a
  DirectInput game reads the same DS4/DualSense, run `controllerlog live
  --no-enhanced` (and `controllerlog devices --no-enhanced`: listing opens the
  pads too). GSE, Steam Input, SDL-based and XInput games are unaffected.
* **Background input** is enabled, so logging continues while the game has focus.
* **Right after connecting a controller:** Xbox/XInput pads only report on
  change, so a button already held while the pad connects is logged when it
  next changes. SDL also treats a trigger's first reported value as its rest
  position, so the very first half-press on a freshly connected pad can go
  unlogged. Press everything once after connecting, before starting a run.
* Several SDL processes (GSE and ControllerLog, for example) can read the same
  Bluetooth pad at once. Exclusive-mode tools (DS4Windows "hide", HidHide
  without a whitelist entry) can block that. Whitelist ControllerLog's
  `python.exe` in HidHide.

## Why not write a Bluetooth driver like BlueRetro?

BlueRetro runs its **own** Bluetooth host stack on an ESP32. That's how it
handles DS3, Switch 2, Wii and odd HID pads. On Windows, the Bluetooth
**L2CAP** interfaces you'd need for that (the channels classic HID runs on)
are **kernel-only**. User-mode programs get RFCOMM, SDP queries and Bluetooth
LE GATT. That's enough for the Switch 2 reader below, but not for classic HID.
That leaves three software-only options:

1. **Use the Windows stack plus SDL3's HID drivers.** This is the default and
   covers everything marked "yes" above.
2. **Signed third-party kernel drivers for the one big gap.** BthPS3 + DsHidMini
   cover the DS3.
3. **Expert "full stack" mode: take the adapter away from Windows.** Bind the
   built-in radio to WinUSB with [Zadig](https://zadig.akeo.ie/) and run a
   user-space host stack such as [Google Bumble](https://github.com/google/bumble)
   (Python, Apache-2.0) on it. That's exactly BlueRetro's position: every packet
   is yours, including DS3 pairing, Switch 2 over BLE at full rate and raw Wii
   reports.

### Expert mode: costs and how to undo it

`controllerlog doctor` tells you whether your radio can be taken over.
USB-attached Intel AX210/AX211/BE200 radios can; PCIe-attached `IBTPCIBUS`
radios cannot. Before trying:

* **Windows has no Bluetooth at all while the adapter is taken over.** Your
  Bluetooth keyboard, mouse and earbuds disconnect. Have wired ones ready.
  `doctor` lists what would drop.
* Controllers have to be paired again with the new stack. Windows' stored link
  keys aren't visible to it.
* Bumble lists the AX211 (8087:0033) in its Intel driver but has only tested
  the AX210 and BE200. Its firmware loader also accepts only certain hardware
  variants (0x17/0x19/0x1C), and some AX211-class parts report 0x18. Run
  `bumble-intel-util info` after the Zadig swap, before building on it. It
  needs Intel firmware files (`bumble-intel-fw-download`).
* Installing a driver is a system change. ControllerLog will never do it for
  you. You run Zadig yourself.

**To undo:** Device Manager → *Universal Serial Bus devices* → "Intel(R)
Wireless Bluetooth(R)" (provider libwdi) → **Uninstall device**, tick *Attempt
to remove the driver*, then *Action → Scan for hardware changes*. Windows
rebinds its Intel driver. From the command line: `pnputil /enum-drivers`, find
the libwdi WinUSB entry, then `pnputil /delete-driver oemNN.inf /uninstall
/force` and `pnputil /scan-devices`.

ControllerLog doesn't ship a Bumble backend yet. The Hub/Backend interface
(`controllerlog/input/base.py`) is where one would plug in. BlueRetro's
per-family init sequences (`main/bluetooth/hidp/{ps3,sw,sw2,wii}.c`) and
mapping tables (`main/adapter/wireless/*.c`) are Apache-2.0 and small enough to
port.

## Switch 2 controllers over Bluetooth, no dongle (experimental)

`controllerlog switch2` reads these controllers directly over Bluetooth LE:

- Nintendo Switch 2 **Pro Controller** (057E:2069)
- **Joy-Con 2** R (2066) and L (2067)
- **NSO GameCube controller** for Switch 2 (2073)

It does not take over your Bluetooth adapter, so your Bluetooth keyboard, mouse and headphones keep working.

These controllers use Nintendo's own Bluetooth LE protocol, not standard HID. That is why Windows shows no gamepad, and why SDL 3.4 only supports them over USB. ControllerLog talks to them directly as a user-mode Bluetooth client, using the `bleak` library (WinRT on Windows).

### Prerequisites
- Windows 11 is recommended. Windows 10 works, but at a much lower report rate (see below).
- A Bluetooth adapter with LE support.
- `pip install bleak`. It pulls in the `winrt-*` packages and works on Python 3.14.
- **Do not pair the controller in Windows Settings.** Windows' standard (SMP) pairing makes Switch 2 controllers disconnect. If the controller is already listed there, remove it.
- Turn your Switch 2 console off or keep it out of range, so it doesn't grab the controller.

### Pairing mode
Hold the controller's small **SYNC** button until the player LEDs run back and forth. It is on the top edge of the Pro and GameCube controllers, and on the rail side of a Joy-Con 2.

ControllerLog does *not* write any pairing data to the controller, so your console pairing is left alone. The trade-off is that the controller never reconnects to the PC by itself:
- Without an address, ControllerLog only picks controllers in pairing mode, so press SYNC each time.
- With an address (`switch2 test ADDRESS` or `live --switch2 ADDRESS`), it will also connect when the controller advertises a reconnect to its console, for example after you press a button. That can race a nearby console, and it has not been confirmed on hardware yet.

### Use
```
controllerlog switch2 scan                 # lists controllers: address, model, signal, mode
controllerlog switch2 test                 # first controller in pairing mode; live buttons + Hz
controllerlog switch2 test 98:E2:55:C2:16:88 --seconds 30
controllerlog switch2 test --model joycon_r --orientation vertical
controllerlog live --switch2 [ADDRESS]     # record, display and bridge it like any other pad
controllerlog live --switch2 --switch2-model joycon_r --switch2-orientation vertical --switch2-deadzone 0
```
`live` reads one Switch 2 controller (`--switch2` takes a single address). Its `--switch2-model`, `--switch2-orientation` and `--switch2-deadzone` options match `switch2 test`'s `--model`, `--orientation` and `--deadzone`; `--player`, `--report` and `--no-throughput` exist only on `switch2 test`.
`test` prints one live line: the measured report rate, the Bluetooth connection interval, battery voltage, the pressed buttons as labelled on the controller, and all six axes. It exits with 1 if no controller connected. Add `-v` (or `-vv`) after `switch2` for connection logs.

### What you get
- Buttons are stored **by position**, the same way SDL maps these controllers over USB:
  - Nintendo A = east, B = south, X = north, Y = west.
  - ZL/ZR become full-scale trigger values.
  - Capture = misc1, C = misc2, GL/GR = paddles.
- GameCube: A = south, B = west, X = east, Y = north, Z = right shoulder, ZL = left shoulder. The analog L/R go on the trigger axes, using the controller's own rest calibration, and the full-pull clicks are misc3/misc4.
- A single Joy-Con defaults to **sideways**, like SDL's mini-gamepad mode: stick on the left, SL/SR as shoulders, sticks rotated to match. R/ZR (or L/ZL) become paddles. Use `--orientation vertical` (`switch2 test`) or `--switch2-orientation vertical` (`live`) for an upright Joy-Con.
- Stick calibration is read from the controller's own memory: factory data, or your user calibration if you ran one. If it can't be read, typical defaults are used. There is a 3% radial deadzone; set `--deadzone 0` (`switch2 test`) or `--switch2-deadzone 0` (`live`) for raw calibrated values.

### Honest limits
- **Report rate.** The controller sends one report per Bluetooth connection event.
  - Windows 10 uses a fixed 60 ms interval, about 16-17 Hz.
  - On Windows 11, ControllerLog requests Windows' "ThroughputOptimized" connection parameters, a 15 ms interval (12 × 1.25 ms), so at most about 66 Hz. `test` shows the interval Windows actually granted.
  - A Switch 2 console gets 200 Hz using a special 5 ms interval. Windows gives user programs no way to request that.
  - Faster rates need a Bluetooth stack that can set connection parameters directly, for example a second, dedicated adapter driven by a user-space stack. ControllerLog does not do that.
  - For per-frame accuracy at 60 fps, USB is better. SDL reads Switch 2 pads over USB only when it is built with libusb (GSE's SDL is; the official SDL3.dll may not be).
- **Timing.** Timestamps are taken when Windows delivers each report. Expect jitter of about one connection interval (about 15 ms).
- **Input only.** No rumble, gyro, NFC or Joy-Con mouse. Two Joy-Cons are two separate controllers, and `live` reads only one Switch 2 controller at a time.
- **GameCube face buttons.** The GameCube controller reports its own A, B, X, Y in the bits of those names (confirmed through its standard HID report on Android), so A is `south`, B `west`, X `east`, Y `north` here, over USB and in the Android app. SDL 3.4's `HandleGameCubeState` reads the bits like a Pro Controller's instead, so a GameCube pad through SDL shows its A as `east`.
- **Android.** The GC Bridge app carries a port of this reader (`Switch2Ble.java`), see [android/README.md](../android/README.md); Android's connection interval is negotiated with `CONNECTION_PRIORITY_HIGH` (11.25-15 ms).
- **Untested on hardware.** This is experimental, and it was built without a Switch 2 controller on hand. The report format, button layout, calibration handling and advertisements were checked against published sniffer captures of real controllers, but connecting from Windows has not been confirmed yet. Please report what `switch2 test` shows on your machine, including the Hz and connection interval.

## Roadmap for the remaining gaps

* **Switch 2 on hardware**: confirm the experimental reader against real controllers.
* **Bumble backend** for expert mode (above).
* **Device-clock timestamps.** DS4 and DualSense reports carry their own
  microsecond counters. Logging them next to host time would remove Bluetooth
  scheduling jitter from press timing.
