package com.controllerlog.gcbridge;

import android.view.InputDevice;
import android.view.KeyEvent;
import android.view.MotionEvent;

import java.util.List;
import java.util.Locale;

/** Text descriptions of Android input devices for the diagnostics screen. */
final class InputDiagnostics {

    private InputDiagnostics() {
    }

    private static final int SOURCE_SENSOR = 0x04000000; // InputDevice.SOURCE_SENSOR, API 31

    private static final int[] SOURCE_BITS = {
            InputDevice.SOURCE_GAMEPAD, InputDevice.SOURCE_JOYSTICK, InputDevice.SOURCE_DPAD,
            InputDevice.SOURCE_KEYBOARD, InputDevice.SOURCE_MOUSE,
            InputDevice.SOURCE_MOUSE_RELATIVE, InputDevice.SOURCE_TOUCHSCREEN,
            InputDevice.SOURCE_TOUCHPAD, InputDevice.SOURCE_STYLUS,
            InputDevice.SOURCE_BLUETOOTH_STYLUS, InputDevice.SOURCE_TRACKBALL,
            InputDevice.SOURCE_TOUCH_NAVIGATION, InputDevice.SOURCE_ROTARY_ENCODER,
            InputDevice.SOURCE_HDMI, SOURCE_SENSOR,
    };
    private static final String[] SOURCE_NAMES = {
            "GAMEPAD", "JOYSTICK", "DPAD", "KEYBOARD", "MOUSE", "MOUSE_RELATIVE", "TOUCHSCREEN",
            "TOUCHPAD", "STYLUS", "BLUETOOTH_STYLUS", "TRACKBALL", "TOUCH_NAVIGATION",
            "ROTARY_ENCODER", "HDMI", "SENSOR",
    };

    /** Gamepad-ish key codes to probe with InputDevice.hasKeys. */
    private static final int[] GAME_KEYS = {
            KeyEvent.KEYCODE_BUTTON_A, KeyEvent.KEYCODE_BUTTON_B, KeyEvent.KEYCODE_BUTTON_C,
            KeyEvent.KEYCODE_BUTTON_X, KeyEvent.KEYCODE_BUTTON_Y, KeyEvent.KEYCODE_BUTTON_Z,
            KeyEvent.KEYCODE_BUTTON_L1, KeyEvent.KEYCODE_BUTTON_R1, KeyEvent.KEYCODE_BUTTON_L2,
            KeyEvent.KEYCODE_BUTTON_R2, KeyEvent.KEYCODE_BUTTON_THUMBL,
            KeyEvent.KEYCODE_BUTTON_THUMBR, KeyEvent.KEYCODE_BUTTON_START,
            KeyEvent.KEYCODE_BUTTON_SELECT, KeyEvent.KEYCODE_BUTTON_MODE,
            KeyEvent.KEYCODE_BUTTON_1, KeyEvent.KEYCODE_BUTTON_2, KeyEvent.KEYCODE_BUTTON_3,
            KeyEvent.KEYCODE_BUTTON_4, KeyEvent.KEYCODE_BUTTON_5, KeyEvent.KEYCODE_BUTTON_6,
            KeyEvent.KEYCODE_BUTTON_7, KeyEvent.KEYCODE_BUTTON_8, KeyEvent.KEYCODE_BUTTON_9,
            KeyEvent.KEYCODE_BUTTON_10, KeyEvent.KEYCODE_BUTTON_11, KeyEvent.KEYCODE_BUTTON_12,
            KeyEvent.KEYCODE_BUTTON_13, KeyEvent.KEYCODE_BUTTON_14, KeyEvent.KEYCODE_BUTTON_15,
            KeyEvent.KEYCODE_BUTTON_16, KeyEvent.KEYCODE_DPAD_UP, KeyEvent.KEYCODE_DPAD_DOWN,
            KeyEvent.KEYCODE_DPAD_LEFT, KeyEvent.KEYCODE_DPAD_RIGHT,
            KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_BACK,
    };

    /** Axes always shown for a gamepad, in addition to whatever the device reports. */
    static final int[] STANDARD_AXES = {
            MotionEvent.AXIS_X, MotionEvent.AXIS_Y, MotionEvent.AXIS_Z, MotionEvent.AXIS_RX,
            MotionEvent.AXIS_RY, MotionEvent.AXIS_RZ, MotionEvent.AXIS_HAT_X,
            MotionEvent.AXIS_HAT_Y, MotionEvent.AXIS_LTRIGGER, MotionEvent.AXIS_RTRIGGER,
            MotionEvent.AXIS_BRAKE, MotionEvent.AXIS_GAS,
    };

    static String sources(int sources) {
        StringBuilder sb = new StringBuilder(String.format(Locale.ROOT, "0x%08x", sources));
        String sep = " ";
        for (int i = 0; i < SOURCE_BITS.length; i++) {
            if ((sources & SOURCE_BITS[i]) == SOURCE_BITS[i]) {
                sb.append(sep).append(SOURCE_NAMES[i]);
                sep = "|";
            }
        }
        return sb.toString();
    }

    static boolean isGameDevice(InputDevice d) {
        if (d == null) {
            return false;
        }
        int s = d.getSources();
        return (s & InputDevice.SOURCE_GAMEPAD) == InputDevice.SOURCE_GAMEPAD
                || (s & InputDevice.SOURCE_JOYSTICK) == InputDevice.SOURCE_JOYSTICK
                || d.getVendorId() == Switch2Protocol.NINTENDO_VID;
    }

    static String axisName(int axis) {
        String s = MotionEvent.axisToString(axis);
        return s.startsWith("AXIS_") ? s.substring(5) : s;
    }

    static String keyName(int keyCode) {
        String s = KeyEvent.keyCodeToString(keyCode);
        return s.startsWith("KEYCODE_") ? s.substring(8) : s;
    }

    static String shortSummary(InputDevice d) {
        return String.format(Locale.ROOT, "#%d \"%s\" %04x:%04x %s", d.getId(), d.getName(),
                d.getVendorId(), d.getProductId(), sources(d.getSources()));
    }

    static String describe(InputDevice d) {
        StringBuilder sb = new StringBuilder();
        sb.append(isGameDevice(d) ? "* " : "  ").append(shortSummary(d)).append('\n');
        sb.append("    descriptor ").append(d.getDescriptor()).append('\n');
        sb.append(String.format(Locale.ROOT,
                "    external=%b virtual=%b controllerNumber=%d keyboardType=%s%n",
                d.isExternal(), d.isVirtual(), d.getControllerNumber(),
                keyboardType(d.getKeyboardType())));
        List<InputDevice.MotionRange> ranges = d.getMotionRanges();
        if (!ranges.isEmpty()) {
            sb.append("    motion ranges:\n");
            for (InputDevice.MotionRange r : ranges) {
                sb.append(String.format(Locale.ROOT,
                        "      %-10s %-11s min=%.2f max=%.2f flat=%.3f fuzz=%.3f res=%.2f%n",
                        axisName(r.getAxis()), sourceClass(r.getSource()), r.getMin(),
                        r.getMax(), r.getFlat(), r.getFuzz(), r.getResolution()));
            }
        }
        if ((d.getSources() & InputDevice.SOURCE_CLASS_BUTTON) != 0 || isGameDevice(d)) {
            boolean[] has = d.hasKeys(GAME_KEYS);
            StringBuilder keys = new StringBuilder();
            for (int i = 0; i < GAME_KEYS.length; i++) {
                if (has[i]) {
                    keys.append(' ').append(keyName(GAME_KEYS[i]));
                }
            }
            if (keys.length() > 0) {
                sb.append("    has keys:").append(keys).append('\n');
            }
        }
        return sb.toString();
    }

    private static String keyboardType(int t) {
        switch (t) {
            case InputDevice.KEYBOARD_TYPE_ALPHABETIC:
                return "alphabetic";
            case InputDevice.KEYBOARD_TYPE_NON_ALPHABETIC:
                return "non-alphabetic";
            default:
                return "none";
        }
    }

    private static String sourceClass(int source) {
        if ((source & InputDevice.SOURCE_JOYSTICK) == InputDevice.SOURCE_JOYSTICK) {
            return "JOYSTICK";
        }
        if ((source & InputDevice.SOURCE_TOUCHSCREEN) == InputDevice.SOURCE_TOUCHSCREEN) {
            return "TOUCHSCREEN";
        }
        if ((source & InputDevice.SOURCE_MOUSE) == InputDevice.SOURCE_MOUSE) {
            return "MOUSE";
        }
        if ((source & InputDevice.SOURCE_TOUCHPAD) == InputDevice.SOURCE_TOUCHPAD) {
            return "TOUCHPAD";
        }
        return String.format(Locale.ROOT, "0x%x", source);
    }
}
