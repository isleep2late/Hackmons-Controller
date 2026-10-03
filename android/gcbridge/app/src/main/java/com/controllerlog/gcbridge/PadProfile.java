package com.controllerlog.gcbridge;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

final class PadProfile {

    private PadProfile() {
    }

    static final List<String> GAMECUBE_LABELS = Collections.unmodifiableList(Arrays.asList(
            "A", "B", "X", "Y", "Z", "L", "R", "Start", "DUp", "DDown", "DLeft", "DRight", "ZL",
            "Home", "Capture", "C", "GR", "GL", "Minus", "LStick", "RStick"));
    static final List<String> POSITIONAL_LABELS = Collections.unmodifiableList(Arrays.asList(
            "South", "East", "West", "North", "LeftShoulder", "RightShoulder", "LeftTrigger",
            "RightTrigger", "Back", "Start", "Guide", "LeftStick", "RightStick", "DUp", "DDown",
            "DLeft", "DRight", "Misc1", "Misc2"));
    static final List<String> AXIS_SLOTS = Collections.unmodifiableList(Arrays.asList(
            "left_x", "left_y", "right_x", "right_y", "left_trigger", "right_trigger"));

    static boolean usesGameCubeLabels(String model) {
        return PadClass.MODEL_GAMECUBE.equals(model);
    }

    static int labelToCanonical(String model, String label) {
        if (label == null) {
            return -1;
        }
        if (usesGameCubeLabels(model)) {
            switch (label) {
                case "A":
                    return Pad.SOUTH;
                case "B":
                    return Pad.WEST;
                case "X":
                    return Pad.EAST;
                case "Y":
                    return Pad.NORTH;
                case "Z":
                    return Pad.RIGHT_SHOULDER;
                case "L":
                    return AndroidInput.KEY_LEFT_TRIGGER;
                case "R":
                    return AndroidInput.KEY_RIGHT_TRIGGER;
                case "ZL":
                    return Pad.LEFT_SHOULDER;
                case "Home":
                    return Pad.GUIDE;
                case "Capture":
                    return Pad.MISC1;
                case "C":
                    return Pad.MISC2;
                case "GR":
                    return Pad.RIGHT_PADDLE1;
                case "GL":
                    return Pad.LEFT_PADDLE1;
                case "Minus":
                    return Pad.BACK;
                case "LStick":
                    return Pad.LEFT_STICK;
                case "RStick":
                    return Pad.RIGHT_STICK;
                default:
                    break;
            }
        } else {
            switch (label) {
                case "South":
                    return Pad.SOUTH;
                case "East":
                    return Pad.EAST;
                case "West":
                    return Pad.WEST;
                case "North":
                    return Pad.NORTH;
                case "LeftShoulder":
                    return Pad.LEFT_SHOULDER;
                case "RightShoulder":
                    return Pad.RIGHT_SHOULDER;
                case "LeftTrigger":
                    return AndroidInput.KEY_LEFT_TRIGGER;
                case "RightTrigger":
                    return AndroidInput.KEY_RIGHT_TRIGGER;
                case "Back":
                    return Pad.BACK;
                case "Guide":
                    return Pad.GUIDE;
                case "LeftStick":
                    return Pad.LEFT_STICK;
                case "RightStick":
                    return Pad.RIGHT_STICK;
                case "Misc1":
                    return Pad.MISC1;
                case "Misc2":
                    return Pad.MISC2;
                default:
                    break;
            }
        }
        switch (label) {
            case "Start":
                return Pad.START;
            case "DUp":
                return Pad.DPAD_UP;
            case "DDown":
                return Pad.DPAD_DOWN;
            case "DLeft":
                return Pad.DPAD_LEFT;
            case "DRight":
                return Pad.DPAD_RIGHT;
            default:
                return -1;
        }
    }

    static String unavailableInput(String label) {
        switch (label) {
            case "Home":
            case "Guide":
                return "guide";
            case "Capture":
            case "Misc1":
                return "misc1";
            case "C":
            case "Misc2":
                return "misc2";
            case "GR":
                return "right_paddle1";
            case "GL":
                return "left_paddle1";
            default:
                return null;
        }
    }

    static Set<String> unavailableInputs(List<?> labels) {
        Set<String> out = new LinkedHashSet<>();
        if (labels != null) {
            for (Object o : labels) {
                String in = o == null ? null : unavailableInput(String.valueOf(o));
                if (in != null) {
                    out.add(in);
                }
            }
        }
        return out;
    }

    static String inputKeyForScan(int scanCode) {
        return String.format(Locale.ROOT, "scan:0x%03x", scanCode);
    }

    static String inputKeyForKey(int keyCode) {
        return "key:" + keyCode;
    }

    static String inputKeyForAxis(int axis, boolean positive) {
        return "axis:" + PadIdentity.axisName(axis) + (positive ? "+" : "-");
    }

    static String normaliseInputKey(String key) {
        if (key == null) {
            return null;
        }
        String k = key.trim();
        if (k.startsWith("scan:")) {
            String v = k.substring(5).toLowerCase(Locale.ROOT);
            try {
                int n = v.startsWith("0x") ? Integer.parseInt(v.substring(2), 16) : Integer.parseInt(v);
                return inputKeyForScan(n);
            } catch (NumberFormatException e) {
                return null;
            }
        }
        if (k.startsWith("key:")) {
            String v = k.substring(4);
            int code = PadIdentity.keyFromName(v);
            if (code < 0) {
                try {
                    code = Integer.parseInt(v);
                } catch (NumberFormatException e) {
                    return null;
                }
            }
            return inputKeyForKey(code);
        }
        if (k.startsWith("axis:") && k.length() > 6) {
            char sign = k.charAt(k.length() - 1);
            int axis = PadIdentity.axisFromName(k.substring(5, k.length() - 1));
            if (axis < 0 || (sign != '+' && sign != '-')) {
                return null;
            }
            return inputKeyForAxis(axis, sign == '+');
        }
        if (k.startsWith("hat:") && k.length() > 5) {
            return normaliseInputKey("axis:" + k.substring(4));
        }
        return null;
    }

    static PadClass toClass(Map<String, Object> profile, PadClass base, PadIdentity id) {
        PadClass c = new PadClass(PadClass.RULE_PROFILE, "saved calibration profile"
                + (Json.has(profile, "made") ? " (made " + Json.str(profile, "made", "") + ")" : ""));
        c.model = Json.str(profile, "model", base.model);
        c.family = Json.str(profile, "family", base.family != null ? base.family : Pad.FAMILY_GENERIC);
        c.table = PadClass.TABLE_PROFILE;
        c.routeStyle = PadClass.ROUTES_PROFILE;
        c.rawReport = base.rawReport;
        c.vendorId = id.vendorId;
        c.productId = id.productId;
        Map<String, Object> buttons = Json.asObject(profile.get("buttons"));
        if (buttons != null) {
            for (Map.Entry<String, Object> e : buttons.entrySet()) {
                String key = normaliseInputKey(e.getKey());
                Object value = e.getValue();
                List<Object> many = Json.asArray(value);
                if (many != null && !many.isEmpty()) {
                    value = many.get(0);
                }
                int canonical = labelToCanonical(c.model, value instanceof String ? (String) value : null);
                if (key != null && canonical >= 0) {
                    c.profileButtons.put(key, canonical);
                }
            }
        }
        Map<String, Object> axes = Json.asObject(profile.get("axes"));
        if (axes != null) {
            for (String slot : AXIS_SLOTS) {
                Map<String, Object> a = Json.asObject(axes.get(slot));
                if (a == null) {
                    continue;
                }
                int axis = PadIdentity.axisFromName(Json.str(a, "axis", null));
                if (axis < 0) {
                    continue;
                }
                boolean invert = Boolean.TRUE.equals(a.get("invert"));
                float neg = (float) Json.num(a, "neg", 1.0);
                float pos = (float) Json.num(a, "pos", 1.0);
                c.profileAxes.put(slot, new PadClass.Stick(axis, invert, neg > 0 ? neg : 1f, pos > 0 ? pos : 1f));
            }
        }
        List<String> unavailable = new ArrayList<>();
        List<Object> u = Json.asArray(profile.get("unavailable"));
        if (u != null) {
            for (Object o : u) {
                if (o instanceof String) {
                    unavailable.add((String) o);
                }
            }
        }
        c.unavailable = unavailable;
        return c;
    }

    static Map<String, Map<String, Object>> parseProfiles(String json) {
        Map<String, Map<String, Object>> out = new LinkedHashMap<>();
        if (json == null || json.isEmpty()) {
            return out;
        }
        try {
            Map<String, Object> all = Json.asObject(Json.parse(json));
            if (all != null) {
                for (Map.Entry<String, Object> e : all.entrySet()) {
                    Map<String, Object> p = Json.asObject(e.getValue());
                    if (p != null) {
                        out.put(e.getKey(), p);
                    }
                }
            }
        } catch (IllegalArgumentException e) {
            return out;
        }
        return out;
    }

    static String matchKey(Map<String, Object> profile) {
        Map<String, Object> match = Json.asObject(profile.get("match"));
        return Json.str(match, "key", null);
    }

    static String fileName(Map<String, Object> profile, String date) {
        String model = Json.str(profile, "model", "pad");
        return (model == null || model.isEmpty() ? "pad" : model) + "-" + date + ".json";
    }
}
