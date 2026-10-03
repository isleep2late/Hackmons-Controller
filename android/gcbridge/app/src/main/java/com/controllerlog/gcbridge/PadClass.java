package com.controllerlog.gcbridge;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

final class PadClass {

    static final String RULE_IGNORED = "ignored";
    static final String RULE_PROFILE = "profile";
    static final String RULE_AYN_GAMECUBE = "ayn-copy-gamecube";
    static final String RULE_AYN_PRO2 = "ayn-copy-pro2";
    static final String RULE_AYN_NINTENDO = "ayn-copy-nintendo";
    static final String RULE_BUILTIN = "builtin-untouched";
    static final String RULE_S2_HIDGENERIC = "s2-hidgeneric";
    static final String RULE_S2_KERNEL = "s2-kernel-driver";
    static final String RULE_GC_CLONE = "name-gamecube-clone";
    static final String RULE_FAMILY = "family";
    static final String RULE_GENERIC = "generic";
    static final String RULE_DEMOTED = "demoted";

    static final String MODEL_GAMECUBE = "gamecube";
    static final String MODEL_PRO2 = "pro2";

    static final String TABLE_NONE = "none";
    static final String TABLE_S2_GC = "S2-GC";
    static final String TABLE_S2_PRO = "S2-Pro";
    static final String TABLE_PROFILE = "profile";

    static final int ROUTES_LEGACY = 0;
    static final int ROUTES_LEGACY_FLIP = 1;
    static final int ROUTES_AYN = 2;
    static final int ROUTES_KERNEL = 3;
    static final int ROUTES_PROFILE = 4;

    static final class Stick {
        final int axis;
        final boolean invert;
        final float neg;
        final float pos;

        Stick(int axis, boolean invert, float neg, float pos) {
            this.axis = axis;
            this.invert = invert;
            this.neg = neg;
            this.pos = pos;
        }
    }

    String rule;
    String reason;
    String model;
    String family;
    String table = TABLE_NONE;
    int routeStyle = ROUTES_LEGACY;
    boolean rawReport;
    boolean ignored;
    int vendorId;
    int productId;
    float leftScale = 1f;
    float rightScale = 1f;
    int rightXAxis = -1;
    int rightYAxis = -1;
    boolean triggersOnZ;
    final Map<String, Integer> profileButtons = new LinkedHashMap<>();
    final Map<String, Stick> profileAxes = new LinkedHashMap<>();
    List<String> unavailable = Collections.emptyList();

    PadClass(String rule, String reason) {
        this.rule = rule;
        this.reason = reason;
    }

    PadClass copy() {
        PadClass c = new PadClass(rule, reason);
        c.model = model;
        c.family = family;
        c.table = table;
        c.routeStyle = routeStyle;
        c.rawReport = rawReport;
        c.ignored = ignored;
        c.vendorId = vendorId;
        c.productId = productId;
        c.leftScale = leftScale;
        c.rightScale = rightScale;
        c.rightXAxis = rightXAxis;
        c.rightYAxis = rightYAxis;
        c.triggersOnZ = triggersOnZ;
        c.profileButtons.putAll(profileButtons);
        c.profileAxes.putAll(profileAxes);
        c.unavailable = new ArrayList<>(unavailable);
        return c;
    }

    boolean flipsLeftY() {
        if (routeStyle == ROUTES_PROFILE) {
            Stick s = profileAxes.get("left_y");
            return s != null && s.invert;
        }
        return routeStyle == ROUTES_AYN || routeStyle == ROUTES_LEGACY_FLIP;
    }

    boolean flipsRightY() {
        if (routeStyle == ROUTES_PROFILE) {
            Stick s = profileAxes.get("right_y");
            return s != null && s.invert;
        }
        return routeStyle == ROUTES_AYN || routeStyle == ROUTES_LEGACY_FLIP;
    }

    String summary() {
        StringBuilder sb = new StringBuilder(rule);
        if (model != null) {
            sb.append(" (").append(model).append(')');
        }
        sb.append(": ").append(reason);
        return sb.toString();
    }
}
