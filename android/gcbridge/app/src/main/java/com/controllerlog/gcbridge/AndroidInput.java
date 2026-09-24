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
        if (n.contains("pro controller") || n.contains("joy-con") || n.contains("nintendo")) {
            return Pad.FAMILY_SWITCH;
        }
        if (n.contains("gamecube")) {
            return Pad.FAMILY_GAMECUBE;
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
    static int buttonForKey(int keyCode, int scanCode, int vendorId, boolean analogTriggers) {
        boolean positional = positionalFace(vendorId);
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

        AxisRoute(int androidAxis, int canonicalAxis, boolean trigger, boolean hat) {
            this.androidAxis = androidAxis;
            this.canonicalAxis = canonicalAxis;
            this.trigger = trigger;
            this.hat = hat;
        }
    }

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
        List<AxisRoute> out = new ArrayList<>();
        if (has(present, MotionEvent.AXIS_X)) {
            out.add(new AxisRoute(MotionEvent.AXIS_X, Pad.LEFT_X, false, false));
        }
        if (has(present, MotionEvent.AXIS_Y)) {
            out.add(new AxisRoute(MotionEvent.AXIS_Y, Pad.LEFT_Y, false, false));
        }
        if (has(present, MotionEvent.AXIS_Z) && has(present, MotionEvent.AXIS_RZ)) {
            out.add(new AxisRoute(MotionEvent.AXIS_Z, Pad.RIGHT_X, false, false));
            out.add(new AxisRoute(MotionEvent.AXIS_RZ, Pad.RIGHT_Y, false, false));
        } else if (has(present, MotionEvent.AXIS_RX) && has(present, MotionEvent.AXIS_RY)) {
            out.add(new AxisRoute(MotionEvent.AXIS_RX, Pad.RIGHT_X, false, false));
            out.add(new AxisRoute(MotionEvent.AXIS_RY, Pad.RIGHT_Y, false, false));
        } else if (has(present, MotionEvent.AXIS_RX) && has(present, MotionEvent.AXIS_RZ)) {
            out.add(new AxisRoute(MotionEvent.AXIS_RX, Pad.RIGHT_X, false, false));
            out.add(new AxisRoute(MotionEvent.AXIS_RZ, Pad.RIGHT_Y, false, false));
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
