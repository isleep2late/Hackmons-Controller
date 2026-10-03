# Shared controller fixtures (pad-fixture v1)

One JSON file per input device. GC Bridge (Java), isleep2late (C#) and iswitch2late (Kotlin) each
classify the same files with their own code and must reach the same answers. `make_pad_fixtures.py`
writes them; the Thor rows come from the captures of 2026-10-02 on the owner's AYN Thor
(`dumpsys input`, `getevent -lp`, `getevent -l` while pressing every button, AYN's
`Vendor_2020_Product_0111.kl`). Copies elsewhere carry a `SOURCE.txt` with the commit and the
sha256 of each file; change the files here and copy them, never edit a copy.

## The rules (first match wins)

Name tokens are matched against the normalised name (lower case, runs of white space collapsed
to one space, trimmed). A token matches only as a whole word: the characters before and after it
must be the start or end of the name or something other than a letter or digit (so "nso" does
not match inside "console").

* `gc` = `gamecube`
* `pro` = `pro controller`, `switch 2 pro`, `pro 2`
* `nin` = `nintendo`, `nso`, `switch`

| # | rule | condition | result |
|---|---|---|---|
| 1 | `ignored` | device key on the ignore list | not read |
| 2 | `profile` | a saved profile for the device key | the profile |
| 3 | `ayn-copy-gamecube` | 2020:0111 and `gc` | S2-GC table by scan code, flip Y and RZ, left X/Y, right Y = RZ, right X = RX only if declared, scale 0.598 left, 0.547 right, Z/GAS/BRAKE/HAT not routed, digital triggers from slots 5 and 13 |
| 4 | `ayn-copy-pro2` | 2020:0111 and `pro` | S2-Pro table, flip Y and RZ, scale 0.786 |
| 5 | `ayn-copy-nintendo` | 2020:0111 and `nin` only | key codes, unchanged |
| 6 | `builtin-untouched` | 2020:0111, anything else | key codes, unchanged |
| 7 | `s2-hidgeneric` | 057e:2073 or 057e:2069, no HAT, joystick axes within X, Y, RX, RZ | S2 table, flip Y and RZ, scale 1 |
| 8 | `s2-kernel-driver` | 057e:2066/2067/2069/2073 with HAT axes | key codes by position, no flip, right RX/RY, GameCube triggers Z/RZ read as (v+1)/2 |
| 9 | `name-gamecube-clone` | vendor not 057e, `gc`, GAMEPAD or JOYSTICK source, BUTTON_C and BUTTON_Z, no HAT | as rule 7 for the GameCube |
| 10 | `family` | Sony or Microsoft vendor, or the name has xbox / playstation / dualsense / dualshock / wireless controller | unchanged |
| 11 | `generic` | anything else | unchanged |

Rules 3, 4, 7, 9 (and a profile for such a device) are "raw report" classes. One is demoted at
run time (rule `demoted`, read by key code from then on) when it delivers a scan code outside
0x130-0x13F and 0x2C0-0x2C4 other than 102, 114, 115, 158 and 580, any scan code in
0x220-0x223, or non-zero HAT motion. Scan code 0 is not evidence.

Device key: `sig:<vvvv>:<pppp>:<normalised name>` (four lower-case hex digits each).

## File format

```
format "pad-fixture", version 1, id, source, measured (true = from a device), stickPress (0.24)
device   name, vendor, product (decimal), descriptor, sources [KEYBOARD|GAMEPAD|JOYSTICK|MOUSE...],
         external, controllerNumber, axes [{axis, min, max, flat}], hasKeys [Android key names]
context  optional: profiles {device key: profile}, ignore [device keys]
expect   rule, model (gamecube | pro2 | null), family, deviceKey, table (S2-GC | S2-Pro | profile |
         none), flip [axes inverted], leftX, leftY, rightX, rightY (axis names or null),
         fullScale {left, right} (null for profiles), spans (profiles: per stick slot {neg, pos}),
         triggers {left, right, centred}, dropAxes [declared axes nothing reads], analogTriggers,
         unavailable [labels], rawReport. Keys absent from expect are not checked.
presses  [{label, slot, scan, keyCode, expect {gcbridge, is2l, iswitch2late}}]; null = dropped
axisSamples [{axis, raw, canonical, value, expect}]: value is after flip and scale, clamped to
         -1..1 (0..1 for triggers), Android's down-positive convention; expect is isleep2late's
         token at stickPress 0.24 (null = none); canonical null = the axis is not read at all
demotions [{scan, keyCode, demotes} | {hat, value, demotes}]
```

## Profiles (`v` 1)

```
{"v":1, "match":{"key", "vendor", "product", "name", "axes"}, "model", "family", "rule":"profile",
 "buttons":{input: label | [label, label]}, "axes":{slot: {"axis", "invert", "neg", "pos"}},
 "unavailable":[labels], "made"}
```

Inputs: `scan:0x131` (scan code, preferred when non-zero), `key:<Android key code>` (a name such
as `key:BUTTON_A` is also accepted), `axis:<AXIS><+|->` (an axis used as a button, threshold 0.5,
release 0.35; HAT d-pads arrive this way). Axis slots: `left_x`, `left_y`, `right_x`, `right_y`,
`left_trigger`, `right_trigger`; `invert` true means the raw value grows the other way from
Android's convention (stick up positive); `neg`/`pos` are the full-deflection spans of the raw
value on each side (the value is divided by them, then clamped). A list value means one input
was confirmed for two labels; a reader that can give an input only one meaning uses the first.

Labels: model `gamecube` uses A, B, X, Y, Z, L, R, Start, DUp, DDown, DLeft, DRight, ZL, Home,
Capture, C, GR, GL, Minus, LStick, RStick; every other model uses positional labels South, East,
West, North, LeftShoulder, RightShoulder, LeftTrigger, RightTrigger, Back, Start, Guide,
LeftStick, RightStick, DUp, DDown, DLeft, DRight, Misc1, Misc2. Unavailable labels add
MainStickUp/Down/Left/Right and CStickUp/Down/Left/Right (GameCube) or LeftStick*/RightStick*
directions (positional) when a stick step timed out.

| GameCube label | GC Bridge | isleep2late | iswitch2late |
|---|---|---|---|
| A | south | A | BUTTON_A |
| B | west | B | BUTTON_B |
| X | east | X | BUTTON_X |
| Y | north | Y | BUTTON_Y |
| Z | right_shoulder | RightShoulder | BUTTON_R1 |
| L | left_trigger (full) | LeftTrigger | BUTTON_L2 |
| R | right_trigger (full) | RightTrigger | BUTTON_R2 |
| ZL | left_shoulder | LeftShoulder | BUTTON_L1 |
| Start | start | Start | BUTTON_START |
| DUp/DDown/DLeft/DRight | dpad_* | Dpad* | DPAD_* |
| Home | guide | Guide | BUTTON_MODE |
| Capture | misc1 | Misc1 | MEDIA_RECORD |
| C | misc2 | Misc2 | none |
| GR / GL | right_paddle1 / left_paddle1 | Paddle1 / Paddle2 | none |
| Minus / LStick / RStick | back / left_stick / right_stick | Back / LeftStick / RightStick | BUTTON_SELECT / BUTTON_THUMBL / BUTTON_THUMBR |
