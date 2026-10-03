package com.controllerlog.gcbridge;

import android.view.InputDevice;
import android.view.KeyEvent;
import android.view.MotionEvent;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * Android key codes / Linux scan codes / MotionEvent axes to the canonical model, following the
 * same rules as {@code controllerlog/input/adb_backend.py} (which reads the same devices through
 * {@code adb getevent}). Pure Java: only compile-time constants of the Android classes are used,
 * so it runs in JVM unit tests.
 */
final class AndroidInput {

    private AndroidInput() {
    }

    static final int VENDOR_SONY = 0x054C;
    static final int VENDOR_NINTENDO = 0x057E;
    static final int VENDOR_MICROSOFT = 0x045E;
    static final int PID_GAMECUBE = 0x2073;
    static final int PID_PRO2 = 0x2069;

    /** Pseudo button indices: the key drives a trigger axis (0 / AXIS_MAX). */
    static final int KEY_LEFT_TRIGGER = 100;
    static final int KEY_RIGHT_TRIGGER = 101;

    // Linux input codes (input-event-codes.h).
    private static final int BTN_SOUTH = 0x130, BTN_EAST = 0x131, BTN_C = 0x132, BTN_NORTH = 0x133,
            BTN_WEST = 0x134, BTN_Z = 0x135, BTN_TL = 0x136, BTN_TR = 0x137, BTN_TL2 = 0x138,
            BTN_TR2 = 0x139, BTN_SELECT = 0x13a, BTN_START = 0x13b, BTN_MODE = 0x13c,
            BTN_THUMBL = 0x13d, BTN_THUMBR = 0x13e, BTN_DPAD_UP = 0x220, BTN_DPAD_DOWN = 0x221,
            BTN_DPAD_LEFT = 0x222, BTN_DPAD_RIGHT = 0x223, BTN_GRIPL = 0x224, BTN_GRIPR = 0x225,
            BTN_GRIPL2 = 0x226, BTN_GRIPR2 = 0x227, BTN_TRIGGER_HAPPY1 = 0x2c0, KEY_BACK = 158,
            KEY_HOMEPAGE = 172, KEY_MENU = 139, KEY_RECORD = 167;

    /** Layout family for the overlay, from the USB ids (and the name for unknown vendors). */
    static String family(int vendorId, int productId, String name) {
        if (vendorId == VENDOR_SONY) {
            return Pad.FAMILY_PLAYSTATION;
        }
        if (vendorId == VENDOR_NINTENDO) {
            return productId == PID_GAMECUBE ? Pad.FAMILY_GAMECUBE : Pad.FAMILY_SWITCH;
        }
        if (vendorId == VENDOR_MICROSOFT) {
            return Pad.FAMILY_XBOX;
        }
        String n = name == null ? "" : name.toLowerCase(Locale.ROOT);
        if (n.contains("xbox")) {
            return Pad.FAMILY_XBOX;
        }
        if (n.contains("playstation") || n.contains("dualsense") || n.contains("dualshock")
                || n.contains("wireless controller")) {
            return Pad.FAMILY_PLAYSTATION;
        }
        if (n.contains("gamecube")) {
            return Pad.FAMILY_GAMECUBE;
        }
        if (n.contains("pro controller") || n.contains("joy-con") || n.contains("nintendo")) {
            return Pad.FAMILY_SWITCH;
        }
        return Pad.FAMILY_GENERIC;
    }

    /**
     * Sony and Nintendo kernel drivers name the face buttons by position (BTN_NORTH = top);
     * xpad and plain HID gamepads use Xbox names (BTN_NORTH = the X button on the left).
     */
    static boolean positionalFace(int vendorId) {
        return vendorId == VENDOR_SONY || vendorId == VENDOR_NINTENDO;
    }

    /**
     * Canonical button for a key event, {@link #KEY_LEFT_TRIGGER} / {@link #KEY_RIGHT_TRIGGER}
     * for digital triggers, or -1. The Linux scan code decides when it is a gamepad code (that
     * is what adb capture on the PC sees); the Android key code is the fallback.
     *
     * @param analogTriggers the device reports trigger axes, so BTN_TL2/TR2 keys are ignored
     */
    static int buttonForKey(int keyCode, int scanCode, int vendorId, int productId,
                            boolean analogTriggers) {
        if (isSwitch2Standard(vendorId, productId)) {
            int r = switch2StandardButton(productId, scanCode);
            if (r != NOT_A_REPORT_BUTTON) {
                return r;
            }
        }
        return genericButton(keyCode, scanCode, positionalFace(vendorId), analogTriggers);
    }

    static int buttonForKey(PadClass c, int keyCode, int scanCode, boolean analogTriggers) {
        if (c.ignored) {
            return -1;
        }
        if (PadClass.TABLE_PROFILE.equals(c.table)) {
            Integer v = scanCode != 0 ? c.profileButtons.get(PadProfile.inputKeyForScan(scanCode)) : null;
            if (v == null) {
                v = c.profileButtons.get(PadProfile.inputKeyForKey(keyCode));
            }
            return v != null ? v : -1;
        }
        if (PadClass.TABLE_S2_GC.equals(c.table) || PadClass.TABLE_S2_PRO.equals(c.table)) {
            int r = switch2StandardButton(PadClass.TABLE_S2_GC.equals(c.table) ? PID_GAMECUBE : PID_PRO2,
                    scanCode);
            if (r != NOT_A_REPORT_BUTTON) {
                return r;
            }
        }
        return genericButton(keyCode, scanCode, positionalFace(c.vendorId), analogTriggers);
    }

    static boolean isSystemVolumeKey(int keyCode) {
        return keyCode == KeyEvent.KEYCODE_VOLUME_UP || keyCode == KeyEvent.KEYCODE_VOLUME_DOWN
                || keyCode == KeyEvent.KEYCODE_VOLUME_MUTE || keyCode == KeyEvent.KEYCODE_MUTE;
    }

    private static int genericButton(int keyCode, int scanCode, boolean positional,
                                     boolean analogTriggers) {
        switch (scanCode) {
            case BTN_SOUTH:
                return Pad.SOUTH;
            case BTN_EAST:
                return Pad.EAST;
            case BTN_NORTH:
                return positional ? Pad.NORTH : Pad.WEST;
            case BTN_WEST:
                return positional ? Pad.WEST : Pad.NORTH;
            case BTN_C:
                return Pad.MISC2;
            case BTN_Z:
                return Pad.MISC1;
            case BTN_TL:
                return Pad.LEFT_SHOULDER;
            case BTN_TR:
                return Pad.RIGHT_SHOULDER;
            case BTN_TL2:
                return analogTriggers ? -1 : KEY_LEFT_TRIGGER;
            case BTN_TR2:
                return analogTriggers ? -1 : KEY_RIGHT_TRIGGER;
            case BTN_SELECT:
            case KEY_BACK:
                return Pad.BACK;
            case BTN_START:
            case KEY_MENU:
                return Pad.START;
            case BTN_MODE:
            case KEY_HOMEPAGE:
                return Pad.GUIDE;
            case BTN_THUMBL:
                return Pad.LEFT_STICK;
            case BTN_THUMBR:
                return Pad.RIGHT_STICK;
            case BTN_DPAD_UP:
                return Pad.DPAD_UP;
            case BTN_DPAD_DOWN:
                return Pad.DPAD_DOWN;
            case BTN_DPAD_LEFT:
                return Pad.DPAD_LEFT;
            case BTN_DPAD_RIGHT:
                return Pad.DPAD_RIGHT;
            case BTN_GRIPL:
                return Pad.LEFT_PADDLE1;
            case BTN_GRIPR:
                return Pad.RIGHT_PADDLE1;
            case BTN_GRIPL2:
                return Pad.LEFT_PADDLE2;
            case BTN_GRIPR2:
                return Pad.RIGHT_PADDLE2;
            case KEY_RECORD:
                return Pad.MISC1;
            default:
                break;
        }
        if (scanCode >= BTN_TRIGGER_HAPPY1 && scanCode < BTN_TRIGGER_HAPPY1 + 8) {
            return happy(scanCode - BTN_TRIGGER_HAPPY1);
        }
        switch (keyCode) {
            case KeyEvent.KEYCODE_BUTTON_A:
                return Pad.SOUTH;
            case KeyEvent.KEYCODE_BUTTON_B:
                return Pad.EAST;
            case KeyEvent.KEYCODE_BUTTON_X:   // Generic.kl: BTN_NORTH
                return positional ? Pad.NORTH : Pad.WEST;
            case KeyEvent.KEYCODE_BUTTON_Y:   // Generic.kl: BTN_WEST
                return positional ? Pad.WEST : Pad.NORTH;
            case KeyEvent.KEYCODE_BUTTON_C:
                return Pad.MISC2;
            case KeyEvent.KEYCODE_BUTTON_Z:
                return Pad.MISC1;
            case KeyEvent.KEYCODE_BUTTON_L1:
                return Pad.LEFT_SHOULDER;
            case KeyEvent.KEYCODE_BUTTON_R1:
                return Pad.RIGHT_SHOULDER;
            case KeyEvent.KEYCODE_BUTTON_L2:
                return analogTriggers ? -1 : KEY_LEFT_TRIGGER;
            case KeyEvent.KEYCODE_BUTTON_R2:
                return analogTriggers ? -1 : KEY_RIGHT_TRIGGER;
            case KeyEvent.KEYCODE_BUTTON_SELECT:
            case KeyEvent.KEYCODE_BACK:
                return Pad.BACK;
            case KeyEvent.KEYCODE_BUTTON_START:
            case KeyEvent.KEYCODE_MENU:
                return Pad.START;
            case KeyEvent.KEYCODE_BUTTON_MODE:
                return Pad.GUIDE;
            case KeyEvent.KEYCODE_BUTTON_THUMBL:
                return Pad.LEFT_STICK;
            case KeyEvent.KEYCODE_BUTTON_THUMBR:
                return Pad.RIGHT_STICK;
            case KeyEvent.KEYCODE_DPAD_UP:
                return Pad.DPAD_UP;
            case KeyEvent.KEYCODE_DPAD_DOWN:
                return Pad.DPAD_DOWN;
            case KeyEvent.KEYCODE_DPAD_LEFT:
                return Pad.DPAD_LEFT;
            case KeyEvent.KEYCODE_DPAD_RIGHT:
                return Pad.DPAD_RIGHT;
            case KeyEvent.KEYCODE_MEDIA_RECORD:
                return Pad.MISC1;
            default:
                break;
        }
        if (keyCode >= KeyEvent.KEYCODE_BUTTON_1 && keyCode < KeyEvent.KEYCODE_BUTTON_1 + 8) {
            return happy(keyCode - KeyEvent.KEYCODE_BUTTON_1);
        }
        return -1;
    }

    private static final int NOT_A_REPORT_BUTTON = Integer.MIN_VALUE;

    /** A Switch 2 GameCube / Pro controller in its standard HID gamepad mode (report 0x0A). */
    static boolean isSwitch2Standard(int vendorId, int productId) {
        return vendorId == VENDOR_NINTENDO && (productId == PID_GAMECUBE || productId == PID_PRO2);
    }

    /**
     * Switch 2 controllers in standard HID mode (what "Start controller" switches on): no kernel
     * driver knows them, so hid-generic numbers the report's 21 buttons in the report's own
     * order, 1-16 as BTN_SOUTH.. (0x130..) and 17-21 as BTN_TRIGGER_HAPPY1.. (0x2c0..). That
     * order is B, A, Y, X, R, ZR, +, RS, down, right, left, up, L, ZL, -, LS, Home, Capture,
     * GR, GL, C, confirmed by pressing on the NSO GameCube controller (whose R / L triggers sit
     * in the R / L slots and whose Z is in the ZR slot). Mapped positionally: the GameCube's A
     * is the bottom button, B the left one.
     */
    static int switch2StandardButton(int productId, int scanCode) {
        int n;
        if (scanCode >= 0x130 && scanCode <= 0x13f) {
            n = scanCode - 0x130 + 1;
        } else if (scanCode >= BTN_TRIGGER_HAPPY1 && scanCode <= BTN_TRIGGER_HAPPY1 + 4) {
            n = scanCode - BTN_TRIGGER_HAPPY1 + 17;
        } else {
            return NOT_A_REPORT_BUTTON;
        }
        boolean gc = productId == PID_GAMECUBE;
        switch (n) {
            case 1:                                       // B
                return gc ? Pad.WEST : Pad.SOUTH;
            case 2:                                       // A
                return gc ? Pad.SOUTH : Pad.EAST;
            case 3:                                       // Y
                return gc ? Pad.NORTH : Pad.WEST;
            case 4:                                       // X
                return gc ? Pad.EAST : Pad.NORTH;
            case 5:                                       // R (GameCube: the trigger's click)
                return gc ? KEY_RIGHT_TRIGGER : Pad.RIGHT_SHOULDER;
            case 6:                                       // ZR (GameCube: Z)
                return gc ? Pad.RIGHT_SHOULDER : KEY_RIGHT_TRIGGER;
            case 7:
                return Pad.START;
            case 8:
                return Pad.RIGHT_STICK;
            case 9:
                return Pad.DPAD_DOWN;
            case 10:
                return Pad.DPAD_RIGHT;
            case 11:
                return Pad.DPAD_LEFT;
            case 12:
                return Pad.DPAD_UP;
            case 13:                                      // L (GameCube: the trigger's click)
                return gc ? KEY_LEFT_TRIGGER : Pad.LEFT_SHOULDER;
            case 14:                                      // ZL (no such button on the GameCube)
                return gc ? Pad.LEFT_SHOULDER : KEY_LEFT_TRIGGER;
            case 15:
                return Pad.BACK;
            case 16:
                return Pad.LEFT_STICK;
            case 17:
                return Pad.GUIDE;
            case 18:
                return Pad.MISC1;
            case 19:
                return Pad.RIGHT_PADDLE1;
            case 20:
                return Pad.LEFT_PADDLE1;
            case 21:
                return Pad.MISC2;
            default:
                return -1;
        }
    }

    /** The standard report's stick Y grows upwards; Android expects down, so flip it. */
    static boolean invertsStickY(int vendorId, int productId) {
        return isSwitch2Standard(vendorId, productId);
    }

    /** BTN_TRIGGER_HAPPY1.. as adb_backend.COMMON_KEYS: paddles, then misc3..misc6. */
    private static int happy(int n) {
        switch (n) {
            case 0:
                return Pad.RIGHT_PADDLE1;
            case 1:
                return Pad.LEFT_PADDLE1;
            case 2:
                return Pad.RIGHT_PADDLE2;
            case 3:
                return Pad.LEFT_PADDLE2;
            case 4:
                return Pad.MISC3;
            case 5:
                return Pad.MISC4;
            case 6:
                return Pad.MISC5;
            case 7:
                return Pad.MISC6;
            default:
                return -1;
        }
    }

    /** Where one MotionEvent axis goes. */
    static final class AxisRoute {
        final int androidAxis;
        final int canonicalAxis;   // Pad.LEFT_X.. or -1 for the hat axes
        final boolean trigger;      // 0..1 instead of -1..1
        final boolean hat;          // AXIS_HAT_X / AXIS_HAT_Y: d-pad buttons
        final boolean invert;       // the device reports this axis the other way round
        final float scaleNeg;
        final float scalePos;
        final boolean centred;
        final int digitalButton;
        final boolean digitalPositive;

        AxisRoute(int androidAxis, int canonicalAxis, boolean trigger, boolean hat) {
            this(androidAxis, canonicalAxis, trigger, hat, false);
        }

        AxisRoute(int androidAxis, int canonicalAxis, boolean trigger, boolean hat, boolean invert) {
            this(androidAxis, canonicalAxis, trigger, hat, invert, 1f, 1f, false, NO_BUTTON, true);
        }

        AxisRoute(int androidAxis, int canonicalAxis, boolean trigger, boolean hat, boolean invert,
                  float scaleNeg, float scalePos, boolean centred, int digitalButton,
                  boolean digitalPositive) {
            this.androidAxis = androidAxis;
            this.canonicalAxis = canonicalAxis;
            this.trigger = trigger;
            this.hat = hat;
            this.invert = invert;
            this.scaleNeg = scaleNeg > 0f ? scaleNeg : 1f;
            this.scalePos = scalePos > 0f ? scalePos : 1f;
            this.centred = centred;
            this.digitalButton = digitalButton;
            this.digitalPositive = digitalPositive;
        }

        static AxisRoute scaled(int androidAxis, int canonicalAxis, boolean invert, float neg, float pos) {
            return new AxisRoute(androidAxis, canonicalAxis, false, false, invert, neg, pos, false,
                    NO_BUTTON, true);
        }

        static AxisRoute digital(int androidAxis, int button, boolean positive) {
            return new AxisRoute(androidAxis, -1, false, false, false, 1f, 1f, false, button, positive);
        }

        boolean isDigital() {
            return digitalButton != NO_BUTTON;
        }

        float apply(float v) {
            if (Float.isNaN(v)) {
                return v;
            }
            if (centred) {
                v = (v + 1f) / 2f;
            }
            float s = v < 0f ? scaleNeg : scalePos;
            if (s != 1f) {
                v = v / s;
            }
            return invert ? -v : v;
        }
    }

    static final int NO_BUTTON = Integer.MIN_VALUE;
    static final float DIGITAL_PRESS = 0.5f;
    static final float DIGITAL_RELEASE = 0.35f;

    private static boolean has(int[] present, int axis) {
        for (int a : present) {
            if (a == axis) {
                return true;
            }
        }
        return false;
    }

    /**
     * Axis routing for a device that reports {@code present} axes. Android's key layout files
     * already normalise known pads (right stick on Z/RZ, triggers on LTRIGGER/RTRIGGER or
     * BRAKE/GAS); unknown HID pads may put the right stick on RX/RY or RX/RZ.
     */
    static List<AxisRoute> routes(int[] present) {
        return routes(present, false);
    }

    /** @param invertY flip both sticks' Y (see {@link #invertsStickY}) */
    static List<AxisRoute> routes(int[] present, boolean invertY) {
        List<AxisRoute> out = new ArrayList<>();
        if (has(present, MotionEvent.AXIS_X)) {
            out.add(new AxisRoute(MotionEvent.AXIS_X, Pad.LEFT_X, false, false));
        }
        if (has(present, MotionEvent.AXIS_Y)) {
            out.add(new AxisRoute(MotionEvent.AXIS_Y, Pad.LEFT_Y, false, false, invertY));
        }
        if (has(present, MotionEvent.AXIS_Z) && has(present, MotionEvent.AXIS_RZ)) {
            out.add(new AxisRoute(MotionEvent.AXIS_Z, Pad.RIGHT_X, false, false));
            out.add(new AxisRoute(MotionEvent.AXIS_RZ, Pad.RIGHT_Y, false, false, invertY));
        } else if (has(present, MotionEvent.AXIS_RX) && has(present, MotionEvent.AXIS_RY)) {
            out.add(new AxisRoute(MotionEvent.AXIS_RX, Pad.RIGHT_X, false, false));
            out.add(new AxisRoute(MotionEvent.AXIS_RY, Pad.RIGHT_Y, false, false, invertY));
        } else if (has(present, MotionEvent.AXIS_RX) && has(present, MotionEvent.AXIS_RZ)) {
            out.add(new AxisRoute(MotionEvent.AXIS_RX, Pad.RIGHT_X, false, false));
            out.add(new AxisRoute(MotionEvent.AXIS_RZ, Pad.RIGHT_Y, false, false, invertY));
        }
        if (has(present, MotionEvent.AXIS_LTRIGGER) || has(present, MotionEvent.AXIS_RTRIGGER)) {
            out.add(new AxisRoute(MotionEvent.AXIS_LTRIGGER, Pad.LEFT_TRIGGER, true, false));
            out.add(new AxisRoute(MotionEvent.AXIS_RTRIGGER, Pad.RIGHT_TRIGGER, true, false));
        } else if (has(present, MotionEvent.AXIS_BRAKE) || has(present, MotionEvent.AXIS_GAS)) {
            out.add(new AxisRoute(MotionEvent.AXIS_BRAKE, Pad.LEFT_TRIGGER, true, false));
            out.add(new AxisRoute(MotionEvent.AXIS_GAS, Pad.RIGHT_TRIGGER, true, false));
        }
        if (has(present, MotionEvent.AXIS_HAT_X)) {
            out.add(new AxisRoute(MotionEvent.AXIS_HAT_X, -1, false, true));
        }
        if (has(present, MotionEvent.AXIS_HAT_Y)) {
            out.add(new AxisRoute(MotionEvent.AXIS_HAT_Y, -1, false, true));
        }
        return out;
    }

    static List<AxisRoute> routes(PadClass c, PadIdentity id) {
        int[] present = id.axes();
        switch (c.routeStyle) {
            case PadClass.ROUTES_LEGACY_FLIP:
                return routes(present, true);
            case PadClass.ROUTES_AYN:
                return aynRoutes(c, id);
            case PadClass.ROUTES_KERNEL:
                return kernelRoutes(c, present);
            case PadClass.ROUTES_PROFILE:
                return profileRoutes(c);
            default:
                return routes(present, false);
        }
    }

    private static List<AxisRoute> aynRoutes(PadClass c, PadIdentity id) {
        List<AxisRoute> out = new ArrayList<>();
        if (id.hasAxis(MotionEvent.AXIS_X)) {
            out.add(AxisRoute.scaled(MotionEvent.AXIS_X, Pad.LEFT_X, false, c.leftScale, c.leftScale));
        }
        if (id.hasAxis(MotionEvent.AXIS_Y)) {
            out.add(AxisRoute.scaled(MotionEvent.AXIS_Y, Pad.LEFT_Y, true, c.leftScale, c.leftScale));
        }
        if (c.rightXAxis >= 0) {
            out.add(AxisRoute.scaled(c.rightXAxis, Pad.RIGHT_X, false, c.rightScale, c.rightScale));
        }
        if (c.rightYAxis >= 0) {
            out.add(AxisRoute.scaled(c.rightYAxis, Pad.RIGHT_Y, true, c.rightScale, c.rightScale));
        }
        return out;
    }

    private static List<AxisRoute> kernelRoutes(PadClass c, int[] present) {
        List<AxisRoute> out = new ArrayList<>();
        if (has(present, MotionEvent.AXIS_X)) {
            out.add(new AxisRoute(MotionEvent.AXIS_X, Pad.LEFT_X, false, false));
        }
        if (has(present, MotionEvent.AXIS_Y)) {
            out.add(new AxisRoute(MotionEvent.AXIS_Y, Pad.LEFT_Y, false, false));
        }
        if (has(present, MotionEvent.AXIS_RX) && has(present, MotionEvent.AXIS_RY)) {
            out.add(new AxisRoute(MotionEvent.AXIS_RX, Pad.RIGHT_X, false, false));
            out.add(new AxisRoute(MotionEvent.AXIS_RY, Pad.RIGHT_Y, false, false));
        } else if (!c.triggersOnZ && has(present, MotionEvent.AXIS_Z) && has(present, MotionEvent.AXIS_RZ)) {
            out.add(new AxisRoute(MotionEvent.AXIS_Z, Pad.RIGHT_X, false, false));
            out.add(new AxisRoute(MotionEvent.AXIS_RZ, Pad.RIGHT_Y, false, false));
        }
        if (c.triggersOnZ) {
            out.add(new AxisRoute(MotionEvent.AXIS_Z, Pad.LEFT_TRIGGER, true, false, false, 1f, 1f, true,
                    NO_BUTTON, true));
            out.add(new AxisRoute(MotionEvent.AXIS_RZ, Pad.RIGHT_TRIGGER, true, false, false, 1f, 1f, true,
                    NO_BUTTON, true));
        } else if (has(present, MotionEvent.AXIS_LTRIGGER) || has(present, MotionEvent.AXIS_RTRIGGER)) {
            out.add(new AxisRoute(MotionEvent.AXIS_LTRIGGER, Pad.LEFT_TRIGGER, true, false));
            out.add(new AxisRoute(MotionEvent.AXIS_RTRIGGER, Pad.RIGHT_TRIGGER, true, false));
        } else if (has(present, MotionEvent.AXIS_BRAKE) || has(present, MotionEvent.AXIS_GAS)) {
            out.add(new AxisRoute(MotionEvent.AXIS_BRAKE, Pad.LEFT_TRIGGER, true, false));
            out.add(new AxisRoute(MotionEvent.AXIS_GAS, Pad.RIGHT_TRIGGER, true, false));
        }
        if (has(present, MotionEvent.AXIS_HAT_X)) {
            out.add(new AxisRoute(MotionEvent.AXIS_HAT_X, -1, false, true));
        }
        if (has(present, MotionEvent.AXIS_HAT_Y)) {
            out.add(new AxisRoute(MotionEvent.AXIS_HAT_Y, -1, false, true));
        }
        return out;
    }

    private static List<AxisRoute> profileRoutes(PadClass c) {
        List<AxisRoute> out = new ArrayList<>();
        String[] slots = {"left_x", "left_y", "right_x", "right_y"};
        int[] canonical = {Pad.LEFT_X, Pad.LEFT_Y, Pad.RIGHT_X, Pad.RIGHT_Y};
        for (int i = 0; i < slots.length; i++) {
            PadClass.Stick s = c.profileAxes.get(slots[i]);
            if (s != null) {
                out.add(AxisRoute.scaled(s.axis, canonical[i], s.invert, s.neg, s.pos));
            }
        }
        PadClass.Stick lt = c.profileAxes.get("left_trigger");
        if (lt != null) {
            out.add(new AxisRoute(lt.axis, Pad.LEFT_TRIGGER, true, false, lt.invert, lt.neg, lt.pos, false,
                    NO_BUTTON, true));
        }
        PadClass.Stick rt = c.profileAxes.get("right_trigger");
        if (rt != null) {
            out.add(new AxisRoute(rt.axis, Pad.RIGHT_TRIGGER, true, false, rt.invert, rt.neg, rt.pos, false,
                    NO_BUTTON, true));
        }
        for (java.util.Map.Entry<String, Integer> e : c.profileButtons.entrySet()) {
            String k = e.getKey();
            if (!k.startsWith("axis:") || k.length() < 7) {
                continue;
            }
            int axis = PadIdentity.axisFromName(k.substring(5, k.length() - 1));
            if (axis >= 0) {
                out.add(AxisRoute.digital(axis, e.getValue(), k.endsWith("+")));
            }
        }
        return out;
    }

    static boolean hasAnalogTriggers(List<AxisRoute> routes) {
        for (AxisRoute r : routes) {
            if (r.trigger) {
                return true;
            }
        }
        return false;
    }

    /** -1..1 stick value (or 0..1 trigger) to the canonical int16 / 0..32767 range. */
    static int axisValue(float v, boolean trigger) {
        if (Float.isNaN(v)) {
            return 0;
        }
        float c = Math.max(trigger ? 0f : -1f, Math.min(1f, v));
        return Pad.clampAxis(Math.round(c * Pad.AXIS_MAX));
    }

    /** Gamepad / joystick sources only: keyboards (even ones with a D-pad) are never logged. */
    static boolean isGameSource(int source) {
        return (source & InputDevice.SOURCE_GAMEPAD) == InputDevice.SOURCE_GAMEPAD
                || (source & InputDevice.SOURCE_JOYSTICK) == InputDevice.SOURCE_JOYSTICK;
    }
}
