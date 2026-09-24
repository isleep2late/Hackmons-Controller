"""Regenerate the ``getevent`` transcripts in this directory.

The printers below reproduce the exact ``printf`` formats of AOSP
``system/core/toolbox/getevent.c`` (``open_device``, ``print_possible_events``,
``print_input_props``, ``print_event`` and the ``-t`` timestamp prefix), so the
fixtures are byte-for-byte what a phone prints. Device capabilities follow the
kernel drivers that Android GKI kernels use: hid-playstation (DualSense),
hid-nintendo (Switch Pro), hid-generic with the Android HID gamepad usages
(Xbox Wireless Controller over Bluetooth, generic BT pads, DS4 without
hid-sony).

Run: ``python tests/fixtures/adb/make_fixtures.py``
"""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent

EV_KEY, EV_REL, EV_ABS, EV_MSC, EV_SW, EV_LED, EV_SND, EV_REP, EV_FF = 1, 2, 3, 4, 5, 0x11, 0x12, 0x14, 0x15
TYPE_LABEL = {EV_KEY: "KEY", EV_REL: "REL", EV_ABS: "ABS", EV_MSC: "MSC", EV_SW: "SW ",
              EV_LED: "LED", EV_SND: "SND", EV_REP: "REP", EV_FF: "FF "}

# First-defined label per code, as input.h-labels.h would list them.
LABELS = {
    EV_KEY: {0x72: "KEY_VOLUMEDOWN", 0x73: "KEY_VOLUMEUP", 0x74: "KEY_POWER",
             0x9e: "KEY_BACK", 0xa7: "KEY_RECORD", 0xac: "KEY_HOMEPAGE",
             0x110: "BTN_MOUSE", 0x130: "BTN_GAMEPAD", 0x131: "BTN_EAST", 0x132: "BTN_C",
             0x133: "BTN_NORTH", 0x134: "BTN_WEST", 0x135: "BTN_Z", 0x136: "BTN_TL",
             0x137: "BTN_TR", 0x138: "BTN_TL2", 0x139: "BTN_TR2", 0x13a: "BTN_SELECT",
             0x13b: "BTN_START", 0x13c: "BTN_MODE", 0x13d: "BTN_THUMBL", 0x13e: "BTN_THUMBR",
             0x145: "BTN_TOOL_FINGER", 0x14a: "BTN_TOUCH", 0x14d: "BTN_TOOL_DOUBLETAP"},
    EV_ABS: {0x00: "ABS_X", 0x01: "ABS_Y", 0x02: "ABS_Z", 0x03: "ABS_RX", 0x04: "ABS_RY",
             0x05: "ABS_RZ", 0x09: "ABS_GAS", 0x0a: "ABS_BRAKE", 0x10: "ABS_HAT0X",
             0x11: "ABS_HAT0Y", 0x2f: "ABS_MT_SLOT", 0x30: "ABS_MT_TOUCH_MAJOR",
             0x31: "ABS_MT_TOUCH_MINOR", 0x35: "ABS_MT_POSITION_X", 0x36: "ABS_MT_POSITION_Y",
             0x39: "ABS_MT_TRACKING_ID", 0x3a: "ABS_MT_PRESSURE"},
    EV_MSC: {0x04: "MSC_SCAN", 0x05: "MSC_TIMESTAMP"},
    EV_SW: {0x02: "SW_HEADPHONE_INSERT", 0x04: "SW_MICROPHONE_INSERT"},
    EV_LED: {0x08: "LED_MISC"},
    EV_REP: {0x00: "REP_DELAY", 0x01: "REP_PERIOD"},
    EV_FF: {0x50: "FF_RUMBLE", 0x51: "FF_PERIODIC", 0x58: "FF_SQUARE", 0x59: "FF_TRIANGLE",
            0x5a: "FF_SINE", 0x60: "FF_GAIN"},
}
PROP_LABELS = {0: "INPUT_PROP_POINTER", 1: "INPUT_PROP_DIRECT", 2: "INPUT_PROP_BUTTONPAD",
               6: "INPUT_PROP_ACCELEROMETER"}


def dev(path, name, bus=0, vendor=0, product=0, version=0, location="", uniq="",
        keys=(), pressed=(), abs=None, events=None, props=()):
    return dict(path=path, name=name, bus=bus, vendor=vendor, product=product, version=version,
                location=location, uniq=uniq, keys=list(keys), pressed=set(pressed),
                abs=dict(abs or {}), events=dict(events or {}), props=list(props))


def print_possible_events(d, labels: bool) -> str:
    out = ["  events:\n"]
    types = {EV_KEY: d["keys"], EV_ABS: sorted(d["abs"]), **d["events"]}
    for t in sorted(types):
        codes = sorted(types[t])
        count = 0
        for code in codes:
            down = "*" if t in (EV_KEY, EV_LED, EV_SND, EV_SW) and code in d["pressed"] else " "
            if count == 0:
                out.append(f"    {TYPE_LABEL[t]} ({t:04x}):")
            elif (count & (0x3 if labels else 0x7)) == 0 or t == EV_ABS:
                out.append("\n               ")
            if labels:
                lab = LABELS.get(t, {}).get(code)
                if lab:
                    out.append(f" {lab[:20]}{down}{'':{max(0, 20 - len(lab))}}")
                else:
                    out.append(f" {code:04x}{down}                ")
            else:
                out.append(f" {code:04x}{down}")
            if t == EV_ABS:
                v, mn, mx, fz, fl, res = d["abs"][code]
                out.append(f" : value {v}, min {mn}, max {mx}, fuzz {fz}, flat {fl}, resolution {res}")
            count += 1
        if count:
            out.append("\n")
    return "".join(out)


def print_input_props(d) -> str:
    out = ["  input props:\n"]
    for p in sorted(d["props"]):
        out.append(f"    {PROP_LABELS.get(p, f'{p:04x}')}\n")
    if not d["props"]:
        out.append("    <none>\n")
    return "".join(out)


def print_info(devs, *, full: bool = True, labels: bool = False) -> str:
    """``getevent -i`` (full=True) or ``getevent -p`` (full=False), optionally ``-l``."""
    out = []
    for n, d in enumerate(devs, 1):
        out.append(f"add device {n}: {d['path']}\n")
        if full:
            out.append(f"  bus:      {d['bus']:04x}\n  vendor    {d['vendor']:04x}\n"
                       f"  product   {d['product']:04x}\n  version   {d['version']:04x}\n")
        out.append(f"  name:     \"{d['name']}\"\n")
        if full:
            out.append(f"  location: \"{d['location']}\"\n  id:       \"{d['uniq']}\"\n")
            out.append("  version:  1.0.1\n")
        out.append(print_possible_events(d, labels))
        out.append(print_input_props(d))
    return "".join(out)


def ts(t: float) -> str:
    sec = int(t)
    usec = round((t - sec) * 1_000_000)
    return f"[{sec:8d}.{usec:06d}] "


def ev(t: float, path: str, typ: int, code: int, value: int) -> str:
    return f"{ts(t)}{path}: {typ:04x} {code:04x} {value & 0xffffffff:08x}\n"


def syn(t: float, path: str) -> str:
    return ev(t, path, 0, 0, 0)


def stream_header(devs, start: int = 1) -> str:
    return "".join(f"add device {n}: {d['path']}\n  name:     \"{d['name']}\"\n"
                   for n, d in enumerate(devs, start))


# --- phone devices -------------------------------------------------------------

GPIO = dev("/dev/input/event0", "gpio_keys", bus=0x19, vendor=1, product=1, version=0x100,
           location="gpio-keys/input0", keys=[0x73])
POWER = dev("/dev/input/event1", "s2mpg12-power-keys", location="s2mpg12-power-keys/input0",
            keys=[0x72, 0x74])
TOUCH = dev("/dev/input/event2", "fts_ts", bus=0x1c, keys=[0x14a],
            abs={0x2f: (0, 0, 9, 0, 0, 0), 0x30: (0, 0, 1079, 0, 0, 0),
                 0x31: (0, 0, 1079, 0, 0, 0), 0x35: (0, 0, 1079, 0, 0, 0),
                 0x36: (0, 0, 2399, 0, 0, 0), 0x39: (0, 0, 65535, 0, 0, 0),
                 0x3a: (0, 0, 1023, 0, 0, 0)},
            props=[1])
PHONE = [GPIO, POWER, TOUCH]

# --- DualSense via hid-playstation ------------------------------------------------

DS_MAC = "a0:ab:51:12:34:56"
DS_KEYS = [0x130, 0x131, 0x133, 0x134, 0x136, 0x137, 0x138, 0x139, 0x13a, 0x13b, 0x13c, 0x13d, 0x13e]
DUALSENSE = dev("/dev/input/event4", "DualSense Wireless Controller", bus=5, vendor=0x054c,
                product=0x0ce6, version=0x8100, location="3c:28:6d:aa:bb:cc", uniq=DS_MAC,
                keys=DS_KEYS,
                abs={0x00: (128, 0, 255, 0, 0, 0), 0x01: (127, 0, 255, 0, 0, 0),
                     0x02: (0, 0, 255, 0, 0, 0), 0x03: (129, 0, 255, 0, 0, 0),
                     0x04: (128, 0, 255, 0, 0, 0), 0x05: (0, 0, 255, 0, 0, 0),
                     0x10: (0, -1, 1, 0, 0, 0), 0x11: (0, -1, 1, 0, 0, 0)},
                events={EV_FF: [0x50, 0x51, 0x58, 0x59, 0x5a, 0x60]})
DS_TOUCH = dev("/dev/input/event5", "DualSense Wireless Controller Touchpad", bus=5,
               vendor=0x054c, product=0x0ce6, version=0x8100, location="3c:28:6d:aa:bb:cc",
               uniq=DS_MAC, keys=[0x110, 0x145, 0x14a, 0x14d],
               abs={0x00: (0, 0, 1919, 0, 0, 0), 0x01: (0, 0, 1079, 0, 0, 0),
                    0x2f: (0, 0, 1, 0, 0, 0), 0x35: (0, 0, 1919, 0, 0, 0),
                    0x36: (0, 0, 1079, 0, 0, 0), 0x39: (0, 0, 65535, 0, 0, 0)},
               props=[0, 2])
DS_MOTION = dev("/dev/input/event6", "DualSense Wireless Controller Motion Sensors", bus=5,
                vendor=0x054c, product=0x0ce6, version=0x8100, location="3c:28:6d:aa:bb:cc",
                uniq=DS_MAC,
                abs={0x00: (-120, -32768, 32768, 16, 0, 8192), 0x01: (8150, -32768, 32768, 16, 0, 8192),
                     0x02: (210, -32768, 32768, 16, 0, 8192),
                     0x03: (3, -2097152, 2097152, 16, 0, 1024), 0x04: (-7, -2097152, 2097152, 16, 0, 1024),
                     0x05: (1, -2097152, 2097152, 16, 0, 1024)},
                events={EV_MSC: [0x05]}, props=[6])
DS_JACK = dev("/dev/input/event7", "DualSense Wireless Controller Headset Jack", bus=5,
              vendor=0x054c, product=0x0ce6, version=0x8100, location="3c:28:6d:aa:bb:cc",
              uniq=DS_MAC, events={EV_SW: [0x02, 0x04]})
DS_ALL = [DUALSENSE, DS_TOUCH, DS_MOTION, DS_JACK]

# --- Switch Pro Controller via hid-nintendo (Android GKI backport naming) ----------

SP_MAC = "98:b6:e9:01:02:03"
SWITCH_PRO = dev("/dev/input/event8", "Nintendo Switch Pro Controller", bus=5, vendor=0x057e,
                 product=0x2009, version=0x8001, location="3c:28:6d:aa:bb:cc", uniq=SP_MAC,
                 keys=[0x130, 0x131, 0x133, 0x134, 0x135, 0x136, 0x137, 0x138, 0x139,
                       0x13a, 0x13b, 0x13c, 0x13d, 0x13e],
                 abs={0x00: (0, -32767, 32767, 250, 500, 0), 0x01: (0, -32767, 32767, 250, 500, 0),
                      0x03: (0, -32767, 32767, 250, 500, 0), 0x04: (0, -32767, 32767, 250, 500, 0),
                      0x10: (0, -1, 1, 0, 0, 0), 0x11: (0, -1, 1, 0, 0, 0)},
                 events={EV_FF: [0x50, 0x51, 0x58, 0x59, 0x5a, 0x60]})
SWITCH_IMU = dev("/dev/input/event9", "Nintendo Switch Pro Controller IMU", bus=5, vendor=0x057e,
                 product=0x2009, version=0x8001, location="3c:28:6d:aa:bb:cc", uniq=SP_MAC,
                 abs={0x00: (-310, -32767, 32767, 10, 0, 4096), 0x01: (25, -32767, 32767, 10, 0, 4096),
                      0x02: (4090, -32767, 32767, 10, 0, 4096), 0x03: (-12, -32767, 32767, 10, 0, 14247),
                      0x04: (4, -32767, 32767, 10, 0, 14247), 0x05: (2, -32767, 32767, 10, 0, 14247)},
                 events={EV_MSC: [0x05]}, props=[6])

# --- Xbox Wireless Controller over Bluetooth (hid-generic, Android HID usages) -----

XBOX = dev("/dev/input/event5", "Xbox Wireless Controller", bus=5, vendor=0x045e, product=0x0b13,
           version=0x0513, location="3c:28:6d:aa:bb:cc", uniq="44:16:22:aa:bb:01",
           keys=[0x9e, 0xa7, 0xac, *range(0x130, 0x13f)], pressed=[0x137],
           abs={0x00: (32768, 0, 65535, 255, 4095, 0), 0x01: (30000, 0, 65535, 255, 4095, 0),
                0x02: (32768, 0, 65535, 255, 4095, 0), 0x05: (32768, 0, 65535, 255, 4095, 0),
                0x09: (0, 0, 1023, 3, 63, 0), 0x0a: (1023, 0, 1023, 3, 63, 0),
                0x10: (0, -1, 1, 0, 0, 0), 0x11: (0, -1, 1, 0, 0, 0)},
           events={EV_MSC: [0x04], EV_FF: [0x50, 0x51, 0x58, 0x59, 0x5a, 0x60]})

# --- generic Android-mode BT gamepad (Z/RZ right stick, BRAKE/GAS triggers) --------

GENERIC = dev("/dev/input/event6", "Wireless Gamepad", bus=5, vendor=0x2563, product=0x0575,
              version=0x0100, location="3c:28:6d:aa:bb:cc", uniq="e4:17:d8:00:11:22",
              keys=list(range(0x130, 0x13f)),
              abs={0x00: (128, 0, 255, 0, 15, 0), 0x01: (128, 0, 255, 0, 15, 0),
                   0x02: (128, 0, 255, 0, 15, 0), 0x05: (128, 0, 255, 0, 15, 0),
                   0x09: (0, 0, 255, 0, 15, 0), 0x0a: (0, 0, 255, 0, 15, 0),
                   0x10: (0, -1, 1, 0, 0, 0), 0x11: (0, -1, 1, 0, 0, 0)},
              events={EV_MSC: [0x04]})
GENERIC_CC = dev("/dev/input/event7", "Wireless Gamepad Consumer Control", bus=5, vendor=0x2563,
                 product=0x0575, version=0x0100, location="3c:28:6d:aa:bb:cc",
                 uniq="e4:17:d8:00:11:22", keys=[0x72, 0x73, 0x9e, 0xac],
                 events={EV_MSC: [0x04]})

# --- DualShock 4 on hid-generic (no hid-sony): raw HID button order ---------------

DS4_GENERIC = dev("/dev/input/event5", "Wireless Controller", bus=5, vendor=0x054c, product=0x09cc,
                  version=0x0100, location="3c:28:6d:aa:bb:cc", uniq="1c:66:6d:10:20:30",
                  keys=list(range(0x130, 0x13e)),
                  abs={0x00: (128, 0, 255, 0, 15, 0), 0x01: (128, 0, 255, 0, 15, 0),
                       0x02: (127, 0, 255, 0, 15, 0), 0x03: (0, 0, 255, 0, 15, 0),
                       0x04: (0, 0, 255, 0, 15, 0), 0x05: (128, 0, 255, 0, 15, 0),
                       0x10: (0, -1, 1, 0, 0, 0), 0x11: (0, -1, 1, 0, 0, 0)},
                  events={EV_MSC: [0x04]})


def dualsense_stream() -> str:
    g, tp, mo, tc = DUALSENSE["path"], DS_TOUCH["path"], DS_MOTION["path"], TOUCH["path"]
    sp, imu = SWITCH_PRO["path"], SWITCH_IMU["path"]
    s = [stream_header(PHONE + DS_ALL)]
    t = 5000.0
    # motion sensors and the phone touchscreen stream too; the backend must ignore them
    s += [ev(t, mo, 3, 0, -118), ev(t, mo, 4, 5, 1000), syn(t, mo)]
    s += [ev(t + 0.02, tc, 3, 0x39, 42), ev(t + 0.02, tc, 3, 0x35, 500), syn(t + 0.02, tc)]
    s += [ev(t + 0.10, g, 3, 0, 0xff), ev(t + 0.10, g, 3, 1, 0x80), syn(t + 0.10, g)]
    s += [ev(t + 0.30, g, 1, 0x130, 1), syn(t + 0.30, g)]
    s += [ev(t + 0.32, mo, 3, 1, 8100), ev(t + 0.32, mo, 4, 5, 321000), syn(t + 0.32, mo)]
    s += [ev(t + 0.45, g, 1, 0x130, 0), syn(t + 0.45, g)]
    s += [ev(t + 0.50, g, 3, 5, 0x80), ev(t + 0.50, g, 1, 0x139, 1), syn(t + 0.50, g)]
    s += [ev(t + 0.55, g, 3, 5, 0xff), syn(t + 0.55, g)]
    s += [ev(t + 0.60, g, 3, 5, 0), ev(t + 0.60, g, 1, 0x139, 0), syn(t + 0.60, g)]
    s += [ev(t + 0.65, g, 3, 0x11, -1), syn(t + 0.65, g)]
    s += [ev(t + 0.70, g, 3, 0x11, 0), syn(t + 0.70, g)]
    s += [ev(t + 0.75, tp, 1, 0x110, 1), ev(t + 0.75, tp, 3, 0x35, 900), syn(t + 0.75, tp)]
    s += [ev(t + 0.80, tp, 1, 0x110, 0), syn(t + 0.80, tp)]
    s += [stream_header([SWITCH_PRO, SWITCH_IMU], start=8)]
    s += [ev(t + 0.90, sp, 3, 0, -32767), ev(t + 0.90, sp, 3, 1, 32767), syn(t + 0.90, sp)]
    s += [ev(t + 0.95, sp, 1, 0x131, 1), syn(t + 0.95, sp)]
    s += [ev(t + 0.96, imu, 3, 2, 4000), syn(t + 0.96, imu)]
    s += [ev(t + 1.00, sp, 1, 0x131, 0), syn(t + 1.00, sp)]
    s += [ev(t + 1.05, sp, 1, 0x138, 1), syn(t + 1.05, sp)]
    s += [ev(t + 1.10, sp, 1, 0x138, 0), ev(t + 1.10, sp, 3, 0, 0), ev(t + 1.10, sp, 3, 1, 0),
          syn(t + 1.10, sp)]
    s += [f"remove device 9: {imu}\n", f"remove device 8: {sp}\n"]
    s += [ev(t + 1.20, g, 1, 0x13c, 1), syn(t + 1.20, g)]
    s += ["#hang\n"]
    return "".join(s)


def crash_streams() -> tuple[str, str]:
    """Stream that dies with a button held, then a healthy restart."""
    g = DUALSENSE["path"]
    a = [stream_header(PHONE + DS_ALL)]
    a += [ev(7000.10, g, 1, 0x131, 1), syn(7000.10, g)]
    a += [ev(7000.20, g, 3, 0, 0xff), syn(7000.20, g)]
    a += ["#stderr could not get evdev event, No such device\n", "#exit 1\n"]
    b = [stream_header(PHONE + DS_ALL)]
    b += [ev(7001.00, g, 1, 0x133, 1), syn(7001.00, g)]
    b += [ev(7001.10, g, 1, 0x133, 0), syn(7001.10, g)]
    b += ["#hang\n"]
    return "".join(a), "".join(b)


DEVICES_ONE = """List of devices attached
R5CT1234ABC            device usb:1-1 product:dm1qxeea model:SM_S911B device:dm1q transport_id:1

"""
DEVICES_MANY = """* daemon not running; starting now at tcp:5037
* daemon started successfully
List of devices attached
R5CT1234ABC            device usb:1-1 product:dm1qxeea model:SM_S911B device:dm1q transport_id:1
192.168.1.50:37845     device product:panther model:Pixel_7 device:panther transport_id:3
0123456789ABCDEF       unauthorized usb:1-2 transport_id:4
emulator-5554          offline transport_id:5
ZY22XXXXXX             no permissions (missing udev rules? user is in the plugdev group); see [http://developer.android.com/tools/device.html] usb:1-3 transport_id:6
adb-R5CT1234ABC-AbCdEf._adb-tls-connect._tcp device product:dm1qxeea model:SM_S911B device:dm1q transport_id:7

"""
DEVICES_UNAUTHORIZED = """List of devices attached
0123456789ABCDEF       unauthorized usb:1-2 transport_id:4

"""
DEVICES_NONE = "List of devices attached\n\n"

# Older/third-party getevent builds print the ids on one line.
OLD_STYLE = """add device 1: /dev/input/event3
  bus: 0005, vendor 054c, product 09cc, version 8100
  name:     "Sony Interactive Entertainment Wireless Controller"
  events:
    KEY (0001): 0130  0131  0133  0134  0136  0137  0138  0139
                013a  013b  013c  013d  013e
    ABS (0003): 0000  : value 125, min 0, max 255, fuzz 0, flat 0
                0001  : value 130, min 0, max 255, fuzz 0, flat 0
                0002  : value 0, min 0, max 255, fuzz 0, flat 0
                0003  : value 127, min 0, max 255, fuzz 0, flat 0
                0004  : value 127, min 0, max 255, fuzz 0, flat 0
                0005  : value 0, min 0, max 255, fuzz 0, flat 0
                0010  : value 0, min -1, max 1, fuzz 0, flat 0
                0011  : value 0, min -1, max 1, fuzz 0, flat 0
  input props:
    <none>
"""


def main() -> None:
    files = {
        "dualsense_phone_info.txt": print_info(PHONE + DS_ALL),
        "switchpro_info.txt": print_info([SWITCH_PRO, SWITCH_IMU]),
        "switchpro_info_labels.txt": print_info([SWITCH_PRO, SWITCH_IMU], full=False, labels=True),
        "xbox_bt_info.txt": print_info(PHONE + [XBOX]),
        "generic_bt_info.txt": print_info(PHONE + [GENERIC, GENERIC_CC]),
        "ds4_hidgeneric_info.txt": print_info([DS4_GENERIC]),
        "old_style_info.txt": OLD_STYLE,
        "dualsense_stream.txt": dualsense_stream(),
        "crash_stream_a.txt": crash_streams()[0],
        "crash_stream_b.txt": crash_streams()[1],
        "devices_one.txt": DEVICES_ONE,
        "devices_many.txt": DEVICES_MANY,
        "devices_unauthorized.txt": DEVICES_UNAUTHORIZED,
        "devices_none.txt": DEVICES_NONE,
    }
    for name, text in files.items():
        (HERE / name).write_text(text, encoding="utf-8", newline="\n")
        print("wrote", name)


if __name__ == "__main__":
    main()
