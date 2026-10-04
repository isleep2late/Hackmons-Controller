package com.controllerlog.gcbridge;

import android.view.KeyEvent;
import android.view.MotionEvent;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

final class PadClassifier {

    private PadClassifier() {
    }

    static final int VENDOR_AYN = 0x2020;
    static final int PRODUCT_AYN = 0x0111;
    static final int PID_JOYCON2_L = 0x2066;
    static final int PID_JOYCON2_R = 0x2067;

    static final float GC_LEFT_SPAN = 0.598f;
    static final float GC_RIGHT_SPAN = 0.547f;
    static final float PRO2_SPAN = 0.786f;

    static final String[] GC_TOKENS = {"gamecube"};
    static final String[] PRO_TOKENS = {"switch 2 pro"};
    static final String[] NIN_TOKENS = {"nintendo", "nso", "switch"};
    static final String[] FAMILY_TOKENS = {"xbox", "playstation", "dualsense", "dualshock",
            "wireless controller"};

    static final int[] SYSTEM_SCANS = {114, 115, 102, 158, 580};

    static final List<String> GC_UNAVAILABLE = Collections.unmodifiableList(Arrays.asList(
            "Home", "Capture", "C", "CStickLeft", "CStickRight"));
    static final List<String> GC_UNAVAILABLE_WITH_RX = Collections.unmodifiableList(Arrays.asList(
            "Home", "Capture", "C"));
    static final List<String> PRO2_UNAVAILABLE = Collections.unmodifiableList(Arrays.asList(
            "Home", "Capture", "GR", "GL", "C", "RightStickLeft", "RightStickRight"));
    static final List<String> PRO2_UNAVAILABLE_WITH_RX = Collections.unmodifiableList(Arrays.asList(
            "Home", "Capture", "GR", "GL", "C"));

    static boolean hasAny(PadIdentity id, String[] tokens) {
        for (String t : tokens) {
            if (id.hasToken(t)) {
                return true;
            }
        }
        return false;
    }

    static PadClass classify(PadIdentity id, Map<String, Map<String, Object>> profiles,
                             Set<String> ignore) {
        String key = id.deviceKey();
        PadClass base = classifyBuiltIn(id);
        if (ignore != null && ignore.contains(key)) {
            PadClass c = base.copy();
            c.rule = PadClass.RULE_IGNORED;
            c.reason = "on the ignore list: its keys and axes are not logged";
            c.ignored = true;
            return c;
        }
        if (profiles != null) {
            Map<String, Object> profile = profiles.get(key);
            if (profile != null) {
                return PadProfile.toClass(profile, base, id);
            }
        }
        return base;
    }

    static PadClass classifyBuiltIn(PadIdentity id) {
        int vid = id.vendorId;
        int pid = id.productId;
        boolean gc = hasAny(id, GC_TOKENS);
        boolean pro = hasAny(id, PRO_TOKENS);
        boolean nin = hasAny(id, NIN_TOKENS);
        PadClass c;
        if (vid == VENDOR_AYN && pid == PRODUCT_AYN) {
            if (gc) {
                c = new PadClass(PadClass.RULE_AYN_GAMECUBE, "AYN's copy of the NSO GameCube pad "
                        + "(2020:0111 with a GameCube name): raw report order, stick Y flipped, sticks "
                        + "scaled; Home, Capture, C and C-stick left/right are not delivered by this "
                        + "connection");
                aynCopy(c, id, PadClass.MODEL_GAMECUBE, Pad.FAMILY_GAMECUBE, PadClass.TABLE_S2_GC,
                        GC_LEFT_SPAN, GC_RIGHT_SPAN);
                c.unavailable = new ArrayList<>(c.rightXAxis >= 0 ? GC_UNAVAILABLE_WITH_RX : GC_UNAVAILABLE);
            } else if (pro) {
                c = new PadClass(PadClass.RULE_AYN_PRO2, "AYN's copy of a Switch 2 Pro pad "
                        + "(2020:0111 with a Pro name): raw report order, stick Y flipped, sticks "
                        + "scaled (not yet measured on a device)");
                aynCopy(c, id, PadClass.MODEL_PRO2, Pad.FAMILY_SWITCH, PadClass.TABLE_S2_PRO,
                        PRO2_SPAN, PRO2_SPAN);
                c.unavailable = new ArrayList<>(c.rightXAxis >= 0 ? PRO2_UNAVAILABLE_WITH_RX : PRO2_UNAVAILABLE);
            } else if (nin) {
                c = new PadClass(PadClass.RULE_AYN_NINTENDO, "AYN's ids with a Nintendo name of no "
                        + "known model: read by key code; use \"Set up this controller\"");
                legacy(c, id);
            } else {
                c = new PadClass(PadClass.RULE_BUILTIN, "AYN's ids without a Nintendo name (the "
                        + "built-in controls): unchanged");
                legacy(c, id);
            }
        } else if (vid == AndroidInput.VENDOR_NINTENDO
                && (pid == AndroidInput.PID_GAMECUBE || pid == AndroidInput.PID_PRO2)
                && !id.hasHat() && axesWithin(id, MotionEvent.AXIS_X, MotionEvent.AXIS_Y,
                MotionEvent.AXIS_RX, MotionEvent.AXIS_RZ)) {
            boolean isGc = pid == AndroidInput.PID_GAMECUBE;
            c = new PadClass(PadClass.RULE_S2_HIDGENERIC, "Switch 2 " + (isGc ? "GameCube" : "Pro")
                    + " pad on Android's generic HID driver: raw report order, stick Y flipped");
            c.model = isGc ? PadClass.MODEL_GAMECUBE : PadClass.MODEL_PRO2;
            c.family = isGc ? Pad.FAMILY_GAMECUBE : Pad.FAMILY_SWITCH;
            c.table = isGc ? PadClass.TABLE_S2_GC : PadClass.TABLE_S2_PRO;
            c.routeStyle = PadClass.ROUTES_LEGACY_FLIP;
            c.rawReport = true;
        } else if (vid == AndroidInput.VENDOR_NINTENDO
                && (pid == PID_JOYCON2_L || pid == PID_JOYCON2_R || pid == AndroidInput.PID_PRO2
                || pid == AndroidInput.PID_GAMECUBE) && id.hasHat()) {
            boolean isGc = pid == AndroidInput.PID_GAMECUBE;
            c = new PadClass(PadClass.RULE_S2_KERNEL, "Switch 2 pad on a kernel driver (HAT axes "
                    + "present): key codes by position, no flip");
            c.model = isGc ? PadClass.MODEL_GAMECUBE : pid == AndroidInput.PID_PRO2 ? PadClass.MODEL_PRO2 : null;
            c.family = isGc ? Pad.FAMILY_GAMECUBE : Pad.FAMILY_SWITCH;
            c.routeStyle = PadClass.ROUTES_KERNEL;
            c.triggersOnZ = isGc && id.hasAxis(MotionEvent.AXIS_Z) && id.hasAxis(MotionEvent.AXIS_RZ);
        } else if (vid != AndroidInput.VENDOR_NINTENDO && gc && id.padSource()
                && id.hasKey(KeyEvent.KEYCODE_BUTTON_C) && id.hasKey(KeyEvent.KEYCODE_BUTTON_Z)
                && !id.hasHat()) {
            c = new PadClass(PadClass.RULE_GC_CLONE, "GameCube-named pad with C and Z buttons and no "
                    + "HAT: read as the raw report of the NSO GameCube pad, stick Y flipped");
            c.model = PadClass.MODEL_GAMECUBE;
            c.family = Pad.FAMILY_GAMECUBE;
            c.table = PadClass.TABLE_S2_GC;
            c.routeStyle = PadClass.ROUTES_LEGACY_FLIP;
            c.rawReport = true;
        } else if (vid == AndroidInput.VENDOR_SONY || vid == AndroidInput.VENDOR_MICROSOFT
                || hasAny(id, FAMILY_TOKENS)) {
            c = new PadClass(PadClass.RULE_FAMILY, "Xbox or PlayStation pad: unchanged");
            legacy(c, id);
        } else {
            c = new PadClass(PadClass.RULE_GENERIC, "no rule matched: read by key code, unchanged");
            legacy(c, id);
        }
        c.vendorId = vid;
        c.productId = pid;
        return c;
    }

    private static void aynCopy(PadClass c, PadIdentity id, String model, String family, String table,
                                float left, float right) {
        c.model = model;
        c.family = family;
        c.table = table;
        c.routeStyle = PadClass.ROUTES_AYN;
        c.rawReport = true;
        c.leftScale = left;
        c.rightScale = right;
        c.rightYAxis = id.hasAxis(MotionEvent.AXIS_RZ) ? MotionEvent.AXIS_RZ : -1;
        c.rightXAxis = id.hasAxis(MotionEvent.AXIS_RX) ? MotionEvent.AXIS_RX : -1;
    }

    private static void legacy(PadClass c, PadIdentity id) {
        c.family = AndroidInput.family(id.vendorId, id.productId, id.name);
        c.routeStyle = PadClass.ROUTES_LEGACY;
    }

    private static boolean axesWithin(PadIdentity id, int... allowed) {
        for (int a : id.joystickAxes()) {
            boolean ok = false;
            for (int b : allowed) {
                ok |= a == b;
            }
            if (!ok) {
                return false;
            }
        }
        return true;
    }

    static String demotionReason(PadClass c, int scanCode, int keyCode) {
        if (c == null || !c.rawReport || scanCode == 0) {
            return null;
        }
        if (scanCode >= 0x220 && scanCode <= 0x223) {
            return String.format(Locale.ROOT, "delivered a D-pad key code (%s, scan 0x%03x)",
                    PadIdentity.keyName(keyCode), scanCode);
        }
        if ((scanCode >= 0x130 && scanCode <= 0x13f) || (scanCode >= 0x2c0 && scanCode <= 0x2c4)) {
            return null;
        }
        for (int s : SYSTEM_SCANS) {
            if (s == scanCode) {
                return null;
            }
        }
        return String.format(Locale.ROOT, "delivered scan code 0x%03x, which the raw report never has",
                scanCode);
    }

    static String hatDemotionReason(PadClass c, float hatX, float hatY) {
        if (c == null || !c.rawReport) {
            return null;
        }
        if ((!Float.isNaN(hatX) && hatX != 0f) || (!Float.isNaN(hatY) && hatY != 0f)) {
            return "delivered HAT motion, which the raw report never has";
        }
        return null;
    }

    static PadClass demote(PadClass c, String why) {
        PadClass d = c.copy();
        d.rule = PadClass.RULE_DEMOTED;
        d.reason = why + "; read by key code from now on (was " + c.rule + ")";
        d.table = PadClass.TABLE_NONE;
        d.routeStyle = PadClass.ROUTES_LEGACY;
        d.rawReport = false;
        d.profileButtons.clear();
        d.profileAxes.clear();
        d.unavailable = new ArrayList<>();
        d.leftScale = 1f;
        d.rightScale = 1f;
        return d;
    }
}
