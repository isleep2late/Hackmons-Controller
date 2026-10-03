package com.controllerlog.gcbridge;

import android.view.InputDevice;
import android.view.KeyEvent;
import android.view.MotionEvent;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;

final class PadIdentity {

    static final int[] PROBE_KEYS = {
            KeyEvent.KEYCODE_BUTTON_A, KeyEvent.KEYCODE_BUTTON_B, KeyEvent.KEYCODE_BUTTON_C,
            KeyEvent.KEYCODE_BUTTON_X, KeyEvent.KEYCODE_BUTTON_Y, KeyEvent.KEYCODE_BUTTON_Z,
            KeyEvent.KEYCODE_BUTTON_L1, KeyEvent.KEYCODE_BUTTON_R1, KeyEvent.KEYCODE_BUTTON_L2,
            KeyEvent.KEYCODE_BUTTON_R2, KeyEvent.KEYCODE_BUTTON_THUMBL,
            KeyEvent.KEYCODE_BUTTON_THUMBR, KeyEvent.KEYCODE_BUTTON_START,
            KeyEvent.KEYCODE_BUTTON_SELECT, KeyEvent.KEYCODE_BUTTON_MODE,
            KeyEvent.KEYCODE_DPAD_UP, KeyEvent.KEYCODE_DPAD_DOWN, KeyEvent.KEYCODE_DPAD_LEFT,
            KeyEvent.KEYCODE_DPAD_RIGHT,
    };

    static final class Range {
        final int axis;
        final float min;
        final float max;
        final float flat;
        final boolean joystick;

        Range(int axis, float min, float max, float flat, boolean joystick) {
            this.axis = axis;
            this.min = min;
            this.max = max;
            this.flat = flat;
            this.joystick = joystick;
        }
    }

    final String name;
    final int vendorId;
    final int productId;
    final String descriptor;
    final int sources;
    final boolean external;
    final int controllerNumber;
    final List<Range> ranges;
    final Set<Integer> hasKeys;
    final List<int[]> usbIds;

    PadIdentity(String name, int vendorId, int productId, String descriptor, int sources,
                boolean external, int controllerNumber, List<Range> ranges, Set<Integer> hasKeys,
                List<int[]> usbIds) {
        this.name = name == null ? "" : name;
        this.vendorId = vendorId;
        this.productId = productId;
        this.descriptor = descriptor == null ? "" : descriptor;
        this.sources = sources;
        this.external = external;
        this.controllerNumber = controllerNumber;
        this.ranges = ranges == null ? Collections.emptyList() : Collections.unmodifiableList(new ArrayList<>(ranges));
        this.hasKeys = hasKeys == null ? Collections.emptySet()
                : Collections.unmodifiableSet(new LinkedHashSet<>(hasKeys));
        this.usbIds = usbIds == null ? Collections.emptyList() : Collections.unmodifiableList(new ArrayList<>(usbIds));
    }

    static String normalise(String name) {
        if (name == null) {
            return "";
        }
        return name.toLowerCase(Locale.ROOT).trim().replaceAll("\\s+", " ");
    }

    static String deviceKey(int vendorId, int productId, String name) {
        return String.format(Locale.ROOT, "sig:%04x:%04x:%s", vendorId & 0xffff, productId & 0xffff,
                normalise(name));
    }

    String normalisedName() {
        return normalise(name);
    }

    String deviceKey() {
        return deviceKey(vendorId, productId, name);
    }

    String idsKey() {
        return String.format(Locale.ROOT, "%04x:%04x", vendorId & 0xffff, productId & 0xffff);
    }

    boolean hasToken(String token) {
        return containsWord(normalisedName(), token);
    }

    static boolean containsWord(String haystack, String token) {
        if (token.isEmpty()) {
            return false;
        }
        int from = 0;
        while (true) {
            int i = haystack.indexOf(token, from);
            if (i < 0) {
                return false;
            }
            int end = i + token.length();
            boolean startOk = i == 0 || !Character.isLetterOrDigit(haystack.charAt(i - 1));
            boolean endOk = end == haystack.length() || !Character.isLetterOrDigit(haystack.charAt(end));
            if (startOk && endOk) {
                return true;
            }
            from = i + 1;
        }
    }

    boolean hasAxis(int axis) {
        for (Range r : ranges) {
            if (r.axis == axis) {
                return true;
            }
        }
        return false;
    }

    int[] axes() {
        int[] out = new int[ranges.size()];
        for (int i = 0; i < out.length; i++) {
            out[i] = ranges.get(i).axis;
        }
        return out;
    }

    int[] joystickAxes() {
        List<Integer> list = new ArrayList<>();
        for (Range r : ranges) {
            if (r.joystick && !list.contains(r.axis)) {
                list.add(r.axis);
            }
        }
        int[] out = new int[list.size()];
        for (int i = 0; i < out.length; i++) {
            out[i] = list.get(i);
        }
        return out;
    }

    boolean hasHat() {
        return hasAxis(MotionEvent.AXIS_HAT_X) || hasAxis(MotionEvent.AXIS_HAT_Y);
    }

    boolean hasKey(int keyCode) {
        return hasKeys.contains(keyCode);
    }

    boolean padSource() {
        return (sources & InputDevice.SOURCE_GAMEPAD) == InputDevice.SOURCE_GAMEPAD
                || (sources & InputDevice.SOURCE_JOYSTICK) == InputDevice.SOURCE_JOYSTICK;
    }

    static String axisName(int axis) {
        switch (axis) {
            case MotionEvent.AXIS_X:
                return "X";
            case MotionEvent.AXIS_Y:
                return "Y";
            case MotionEvent.AXIS_Z:
                return "Z";
            case MotionEvent.AXIS_RX:
                return "RX";
            case MotionEvent.AXIS_RY:
                return "RY";
            case MotionEvent.AXIS_RZ:
                return "RZ";
            case MotionEvent.AXIS_HAT_X:
                return "HAT_X";
            case MotionEvent.AXIS_HAT_Y:
                return "HAT_Y";
            case MotionEvent.AXIS_LTRIGGER:
                return "LTRIGGER";
            case MotionEvent.AXIS_RTRIGGER:
                return "RTRIGGER";
            case MotionEvent.AXIS_GAS:
                return "GAS";
            case MotionEvent.AXIS_BRAKE:
                return "BRAKE";
            case MotionEvent.AXIS_THROTTLE:
                return "THROTTLE";
            case MotionEvent.AXIS_RUDDER:
                return "RUDDER";
            case MotionEvent.AXIS_WHEEL:
                return "WHEEL";
            default:
                return "AXIS_" + axis;
        }
    }

    static int axisFromName(String name) {
        if (name == null) {
            return -1;
        }
        switch (name.toUpperCase(Locale.ROOT)) {
            case "X":
                return MotionEvent.AXIS_X;
            case "Y":
                return MotionEvent.AXIS_Y;
            case "Z":
                return MotionEvent.AXIS_Z;
            case "RX":
                return MotionEvent.AXIS_RX;
            case "RY":
                return MotionEvent.AXIS_RY;
            case "RZ":
                return MotionEvent.AXIS_RZ;
            case "HAT_X":
                return MotionEvent.AXIS_HAT_X;
            case "HAT_Y":
                return MotionEvent.AXIS_HAT_Y;
            case "LTRIGGER":
                return MotionEvent.AXIS_LTRIGGER;
            case "RTRIGGER":
                return MotionEvent.AXIS_RTRIGGER;
            case "GAS":
                return MotionEvent.AXIS_GAS;
            case "BRAKE":
                return MotionEvent.AXIS_BRAKE;
            case "THROTTLE":
                return MotionEvent.AXIS_THROTTLE;
            case "RUDDER":
                return MotionEvent.AXIS_RUDDER;
            case "WHEEL":
                return MotionEvent.AXIS_WHEEL;
            default:
                return -1;
        }
    }

    static String keyName(int keyCode) {
        switch (keyCode) {
            case KeyEvent.KEYCODE_BUTTON_A:
                return "BUTTON_A";
            case KeyEvent.KEYCODE_BUTTON_B:
                return "BUTTON_B";
            case KeyEvent.KEYCODE_BUTTON_C:
                return "BUTTON_C";
            case KeyEvent.KEYCODE_BUTTON_X:
                return "BUTTON_X";
            case KeyEvent.KEYCODE_BUTTON_Y:
                return "BUTTON_Y";
            case KeyEvent.KEYCODE_BUTTON_Z:
                return "BUTTON_Z";
            case KeyEvent.KEYCODE_BUTTON_L1:
                return "BUTTON_L1";
            case KeyEvent.KEYCODE_BUTTON_R1:
                return "BUTTON_R1";
            case KeyEvent.KEYCODE_BUTTON_L2:
                return "BUTTON_L2";
            case KeyEvent.KEYCODE_BUTTON_R2:
                return "BUTTON_R2";
            case KeyEvent.KEYCODE_BUTTON_THUMBL:
                return "BUTTON_THUMBL";
            case KeyEvent.KEYCODE_BUTTON_THUMBR:
                return "BUTTON_THUMBR";
            case KeyEvent.KEYCODE_BUTTON_START:
                return "BUTTON_START";
            case KeyEvent.KEYCODE_BUTTON_SELECT:
                return "BUTTON_SELECT";
            case KeyEvent.KEYCODE_BUTTON_MODE:
                return "BUTTON_MODE";
            case KeyEvent.KEYCODE_DPAD_UP:
                return "DPAD_UP";
            case KeyEvent.KEYCODE_DPAD_DOWN:
                return "DPAD_DOWN";
            case KeyEvent.KEYCODE_DPAD_LEFT:
                return "DPAD_LEFT";
            case KeyEvent.KEYCODE_DPAD_RIGHT:
                return "DPAD_RIGHT";
            case KeyEvent.KEYCODE_MEDIA_RECORD:
                return "MEDIA_RECORD";
            case KeyEvent.KEYCODE_VOLUME_UP:
                return "VOLUME_UP";
            case KeyEvent.KEYCODE_VOLUME_DOWN:
                return "VOLUME_DOWN";
            case KeyEvent.KEYCODE_VOLUME_MUTE:
                return "VOLUME_MUTE";
            case KeyEvent.KEYCODE_BACK:
                return "BACK";
            case KeyEvent.KEYCODE_HOME:
                return "HOME";
            case KeyEvent.KEYCODE_APP_SWITCH:
                return "APP_SWITCH";
            case KeyEvent.KEYCODE_UNKNOWN:
                return "UNKNOWN";
            default:
                return "KEYCODE_" + keyCode;
        }
    }

    static int keyFromName(String name) {
        if (name == null) {
            return -1;
        }
        for (int code : new int[]{KeyEvent.KEYCODE_BUTTON_A, KeyEvent.KEYCODE_BUTTON_B,
                KeyEvent.KEYCODE_BUTTON_C, KeyEvent.KEYCODE_BUTTON_X, KeyEvent.KEYCODE_BUTTON_Y,
                KeyEvent.KEYCODE_BUTTON_Z, KeyEvent.KEYCODE_BUTTON_L1, KeyEvent.KEYCODE_BUTTON_R1,
                KeyEvent.KEYCODE_BUTTON_L2, KeyEvent.KEYCODE_BUTTON_R2,
                KeyEvent.KEYCODE_BUTTON_THUMBL, KeyEvent.KEYCODE_BUTTON_THUMBR,
                KeyEvent.KEYCODE_BUTTON_START, KeyEvent.KEYCODE_BUTTON_SELECT,
                KeyEvent.KEYCODE_BUTTON_MODE, KeyEvent.KEYCODE_DPAD_UP, KeyEvent.KEYCODE_DPAD_DOWN,
                KeyEvent.KEYCODE_DPAD_LEFT, KeyEvent.KEYCODE_DPAD_RIGHT,
                KeyEvent.KEYCODE_MEDIA_RECORD, KeyEvent.KEYCODE_VOLUME_UP,
                KeyEvent.KEYCODE_VOLUME_DOWN, KeyEvent.KEYCODE_VOLUME_MUTE, KeyEvent.KEYCODE_BACK,
                KeyEvent.KEYCODE_HOME, KeyEvent.KEYCODE_APP_SWITCH, KeyEvent.KEYCODE_UNKNOWN}) {
            if (keyName(code).equals(name)) {
                return code;
            }
        }
        if (name.startsWith("KEYCODE_")) {
            try {
                return Integer.parseInt(name.substring(8));
            } catch (NumberFormatException e) {
                return -1;
            }
        }
        return -1;
    }

    static int sourceFromName(String name) {
        switch (name) {
            case "KEYBOARD":
                return InputDevice.SOURCE_KEYBOARD;
            case "GAMEPAD":
                return InputDevice.SOURCE_GAMEPAD;
            case "JOYSTICK":
                return InputDevice.SOURCE_JOYSTICK;
            case "DPAD":
                return InputDevice.SOURCE_DPAD;
            case "MOUSE":
                return InputDevice.SOURCE_MOUSE;
            case "TOUCHSCREEN":
                return InputDevice.SOURCE_TOUCHSCREEN;
            default:
                return 0;
        }
    }
}
