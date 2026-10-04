package com.controllerlog.gcbridge;

import android.view.MotionEvent;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

final class PadCalibration {

    static final long REST_MS = 300;
    static final long TIMEOUT_MS = 10_000;
    static final long HOLD_MS = 1_000;
    static final float BUTTON_AXIS = 0.5f;
    static final float STICK_START = 0.2f;
    static final float STICK_MIN = 0.3f;
    static final float SETTLED = 0.25f;

    enum Kind { REST, BUTTON, TRIGGER, STICK, LIVE }

    enum LiveChange { NONE, APPLY, RESTORE }

    static final class Step {
        final Kind kind;
        final String label;
        final String prompt;
        final String slot;
        final boolean vertical;
        final List<String> unavailableLabels;

        Step(Kind kind, String label, String prompt, String slot, boolean vertical, List<String> unavailableLabels) {
            this.kind = kind;
            this.label = label;
            this.prompt = prompt;
            this.slot = slot;
            this.vertical = vertical;
            this.unavailableLabels = unavailableLabels;
        }
    }

    private static Step button(String label, String prompt) {
        return new Step(Kind.BUTTON, label, prompt, null, false, Collections.singletonList(label));
    }

    private static Step trigger(String label, String prompt, String slot) {
        return new Step(Kind.TRIGGER, label, prompt, slot, false, Collections.singletonList(label));
    }

    private static Step stick(String prompt, String slot, boolean vertical, String... unavailable) {
        return new Step(Kind.STICK, slot, prompt, slot, vertical, Arrays.asList(unavailable));
    }

    static List<Step> gameCubeSteps() {
        List<Step> s = new ArrayList<>();
        s.add(new Step(Kind.REST, "rest", "Let go of everything", null, false, Collections.emptyList()));
        s.add(button("A", "Press A"));
        s.add(button("B", "Press B"));
        s.add(button("X", "Press X"));
        s.add(button("Y", "Press Y"));
        s.add(button("Z", "Press Z"));
        s.add(trigger("L", "Press L fully", "left_trigger"));
        s.add(trigger("R", "Press R fully", "right_trigger"));
        s.add(button("Start", "Press Start"));
        s.add(button("DUp", "Press D-pad up"));
        s.add(button("DDown", "Press D-pad down"));
        s.add(button("DLeft", "Press D-pad left"));
        s.add(button("DRight", "Press D-pad right"));
        s.add(button("ZL", "Press ZL"));
        s.add(button("Home", "Press Home"));
        s.add(button("Capture", "Press Capture"));
        s.add(button("C", "Press C"));
        s.add(stick("Push the main stick UP and hold", "left_y", true, "MainStickUp", "MainStickDown"));
        s.add(stick("Push the main stick RIGHT and hold", "left_x", false, "MainStickLeft", "MainStickRight"));
        s.add(stick("Push the C-stick UP and hold", "right_y", true, "CStickUp", "CStickDown"));
        s.add(stick("Push the C-stick RIGHT and hold", "right_x", false, "CStickLeft", "CStickRight"));
        s.add(new Step(Kind.LIVE, "live", "Live test: press buttons and move the sticks, then Save", null, false,
                Collections.emptyList()));
        return s;
    }

    static List<Step> positionalSteps() {
        List<Step> s = new ArrayList<>();
        s.add(new Step(Kind.REST, "rest", "Let go of everything", null, false, Collections.emptyList()));
        s.add(button("South", "Press the bottom face button"));
        s.add(button("East", "Press the right face button"));
        s.add(button("West", "Press the left face button"));
        s.add(button("North", "Press the top face button"));
        s.add(button("LeftShoulder", "Press the left shoulder button (L / L1 / LB)"));
        s.add(button("RightShoulder", "Press the right shoulder button (R / R1 / RB)"));
        s.add(trigger("LeftTrigger", "Press the left trigger fully (ZL / L2 / LT)", "left_trigger"));
        s.add(trigger("RightTrigger", "Press the right trigger fully (ZR / R2 / RT)", "right_trigger"));
        s.add(button("Back", "Press Select / Minus / Back"));
        s.add(button("Start", "Press Start / Plus / Menu"));
        s.add(button("Guide", "Press Home / Guide"));
        s.add(button("LeftStick", "Click the left stick in"));
        s.add(button("RightStick", "Click the right stick in"));
        s.add(button("DUp", "Press D-pad up"));
        s.add(button("DDown", "Press D-pad down"));
        s.add(button("DLeft", "Press D-pad left"));
        s.add(button("DRight", "Press D-pad right"));
        s.add(stick("Push the left stick UP and hold", "left_y", true, "LeftStickUp", "LeftStickDown"));
        s.add(stick("Push the left stick RIGHT and hold", "left_x", false, "LeftStickLeft", "LeftStickRight"));
        s.add(stick("Push the right stick UP and hold", "right_y", true, "RightStickUp", "RightStickDown"));
        s.add(stick("Push the right stick RIGHT and hold", "right_x", false, "RightStickLeft", "RightStickRight"));
        s.add(new Step(Kind.LIVE, "live", "Live test: press buttons and move the sticks, then Save", null, false,
                Collections.emptyList()));
        return s;
    }

    private static final class Answer {
        final String input;
        final Map<String, Object> axis;
        final boolean unavailable;

        Answer(String input, Map<String, Object> axis, boolean unavailable) {
            this.input = input;
            this.axis = axis;
            this.unavailable = unavailable;
        }
    }

    private final PadIdentity identity;
    private final String model;
    private final List<Step> steps;
    private final Answer[] answers;
    private int index;
    private boolean liveApplied;
    private long stepStart;
    private final Map<Integer, Float> latest = new HashMap<>();
    private final Map<Integer, Float> rest = new HashMap<>();
    private final Map<Integer, double[]> restSum = new HashMap<>();
    private final Set<Integer> unarmed = new LinkedHashSet<>();
    private long holdStart = -1;
    private final Map<Integer, Float> peak = new HashMap<>();
    private String pendingInput;
    private Map<String, Object> pendingAxis;
    private String message = "";

    PadCalibration(PadIdentity identity, String model, long nowMs) {
        this(identity, model, nowMs, Collections.emptyMap());
    }

    PadCalibration(PadIdentity identity, String model, long nowMs, Map<Integer, Float> current) {
        this.identity = identity;
        this.model = model;
        this.steps = PadProfile.usesGameCubeLabels(model) ? gameCubeSteps() : positionalSteps();
        this.answers = new Answer[steps.size()];
        this.index = 0;
        this.stepStart = nowMs;
        for (Map.Entry<Integer, Float> e : current.entrySet()) {
            if (e.getKey() != null && e.getValue() != null && !Float.isNaN(e.getValue())) {
                latest.put(e.getKey(), e.getValue());
            }
        }
    }

    String deviceKey() {
        return identity.deviceKey();
    }

    int stepIndex() {
        return index;
    }

    int stepCount() {
        return steps.size();
    }

    Step step() {
        return steps.get(Math.min(index, steps.size() - 1));
    }

    boolean isLive() {
        return step().kind == Kind.LIVE;
    }

    LiveChange liveChange() {
        boolean live = isLive();
        if (live == liveApplied) {
            return LiveChange.NONE;
        }
        liveApplied = live;
        return live ? LiveChange.APPLY : LiveChange.RESTORE;
    }

    boolean liveApplied() {
        return liveApplied;
    }

    boolean awaitingConfirm() {
        return pendingInput != null;
    }

    String message() {
        return message;
    }

    String prompt() {
        if (pendingInput != null) {
            return describeInput(pendingInput) + " is already " + ownerOf(pendingInput) + ". Use it for both?";
        }
        return step().prompt;
    }

    long remainingMs(long nowMs) {
        Kind k = step().kind;
        if (k == Kind.REST || k == Kind.LIVE || pendingInput != null) {
            return -1;
        }
        return Math.max(0, TIMEOUT_MS - (nowMs - stepStart));
    }

    private String ownerOf(String input) {
        for (int i = 0; i < answers.length; i++) {
            if (answers[i] != null && input.equals(answers[i].input)) {
                return steps.get(i).label;
            }
        }
        return "taken";
    }

    static String describeInput(String input) {
        if (input.startsWith("scan:")) {
            return "Scan code " + input.substring(5);
        }
        if (input.startsWith("key:")) {
            try {
                return PadIdentity.keyName(Integer.parseInt(input.substring(4)));
            } catch (NumberFormatException e) {
                return input;
            }
        }
        if (input.startsWith("axis:")) {
            return "Axis " + input.substring(5);
        }
        return input;
    }

    private float restOf(int axis) {
        Float r = rest.get(axis);
        return r != null ? r : 0f;
    }

    private void enter(int i, long nowMs) {
        index = Math.max(0, Math.min(i, steps.size() - 1));
        stepStart = nowMs;
        holdStart = -1;
        peak.clear();
        pendingInput = null;
        pendingAxis = null;
        if (steps.get(index).kind == Kind.REST) {
            restSum.clear();
        }
    }

    private void advance(long nowMs) {
        message = "";
        enter(index + 1, nowMs);
    }

    void skip(long nowMs) {
        Step s = step();
        if (s.kind == Kind.LIVE) {
            return;
        }
        answers[index] = s.kind == Kind.REST ? null : new Answer(null, null, true);
        if (s.kind == Kind.REST) {
            finishRest();
        }
        advance(nowMs);
    }

    void redo(long nowMs) {
        answers[index] = null;
        message = "";
        enter(index, nowMs);
    }

    void back(long nowMs) {
        if (index == 0) {
            return;
        }
        answers[index] = null;
        int prev = index - 1;
        answers[prev] = null;
        message = "";
        enter(prev, nowMs);
    }

    void answerBoth(boolean yes, long nowMs) {
        if (pendingInput == null) {
            return;
        }
        if (yes) {
            answers[index] = new Answer(pendingInput, pendingAxis, false);
            advance(nowMs);
        } else {
            pendingInput = null;
            pendingAxis = null;
            stepStart = nowMs;
            message = "Press another input for " + step().label;
        }
    }

    private boolean taken(String input) {
        for (int i = 0; i < answers.length; i++) {
            if (i != index && answers[i] != null && input.equals(answers[i].input)) {
                return true;
            }
        }
        return false;
    }

    private void capture(String input, Map<String, Object> axis, long nowMs) {
        if (taken(input)) {
            pendingInput = input;
            pendingAxis = axis;
            return;
        }
        answers[index] = new Answer(input, axis, false);
        advance(nowMs);
    }

    void onKey(int scanCode, int keyCode, boolean down, long nowMs) {
        Step s = step();
        if (!down || pendingInput != null || (s.kind != Kind.BUTTON && s.kind != Kind.TRIGGER)) {
            return;
        }
        String input = scanCode != 0 ? PadProfile.inputKeyForScan(scanCode) : PadProfile.inputKeyForKey(keyCode);
        capture(input, null, nowMs);
    }

    void onAxis(int axis, float value, long nowMs) {
        if (Float.isNaN(value)) {
            return;
        }
        latest.put(axis, value);
        Step s = step();
        if (s.kind == Kind.REST) {
            double[] acc = restSum.computeIfAbsent(axis, k -> new double[2]);
            acc[0] += value;
            acc[1] += 1;
            return;
        }
        if (!rest.containsKey(axis)) {
            boolean seed = !restsAtZero(axis) && Math.abs(value) > SETTLED;
            rest.put(axis, seed ? value : 0f);
            if (seed) {
                unarmed.add(axis);
                return;
            }
        }
        float dev = value - restOf(axis);
        if (unarmed.contains(axis)) {
            if (Math.abs(dev) <= SETTLED) {
                unarmed.remove(axis);
            }
            return;
        }
        if (pendingInput != null) {
            return;
        }
        if (s.kind == Kind.BUTTON || s.kind == Kind.TRIGGER) {
            if (Math.abs(dev) >= BUTTON_AXIS) {
                unarmed.add(axis);
                if (s.kind == Kind.TRIGGER && !isHat(axis)) {
                    Map<String, Object> a = new LinkedHashMap<>();
                    a.put("axis", PadIdentity.axisName(axis));
                    if (dev < 0) {
                        a.put("invert", true);
                    }
                    capture(PadProfile.inputKeyForAxis(axis, dev > 0), a, nowMs);
                } else {
                    capture(PadProfile.inputKeyForAxis(axis, dev > 0), null, nowMs);
                }
            }
        } else if (s.kind == Kind.STICK) {
            if (isHat(axis)) {
                return;
            }
            if (holdStart < 0 && Math.abs(dev) >= STICK_START) {
                holdStart = nowMs;
            }
            if (holdStart >= 0) {
                Float p = peak.get(axis);
                if (p == null || Math.abs(dev) > Math.abs(p)) {
                    peak.put(axis, dev);
                }
            }
        }
    }

    private static boolean isHat(int axis) {
        return axis == MotionEvent.AXIS_HAT_X || axis == MotionEvent.AXIS_HAT_Y;
    }

    private static boolean restsAtZero(int axis) {
        switch (axis) {
            case MotionEvent.AXIS_X:
            case MotionEvent.AXIS_Y:
            case MotionEvent.AXIS_HAT_X:
            case MotionEvent.AXIS_HAT_Y:
            case MotionEvent.AXIS_LTRIGGER:
            case MotionEvent.AXIS_RTRIGGER:
            case MotionEvent.AXIS_BRAKE:
            case MotionEvent.AXIS_GAS:
                return true;
            default:
                return false;
        }
    }

    private void finishRest() {
        rest.clear();
        rest.putAll(latest);
        for (Map.Entry<Integer, double[]> e : restSum.entrySet()) {
            if (e.getValue()[1] > 0) {
                rest.put(e.getKey(), (float) (e.getValue()[0] / e.getValue()[1]));
            }
        }
    }

    void tick(long nowMs) {
        Step s = step();
        if (s.kind == Kind.REST) {
            if (nowMs - stepStart >= REST_MS) {
                finishRest();
                unarmed.clear();
                advance(nowMs);
            }
            return;
        }
        if (s.kind == Kind.LIVE || pendingInput != null) {
            return;
        }
        if (s.kind == Kind.STICK && holdStart >= 0) {
            if (nowMs - holdStart < HOLD_MS) {
                return;
            }
            int best = -1;
            float bestDev = 0f;
            for (Map.Entry<Integer, Float> e : peak.entrySet()) {
                if (Math.abs(e.getValue()) > Math.abs(bestDev)) {
                    best = e.getKey();
                    bestDev = e.getValue();
                }
            }
            if (best < 0 || Math.abs(bestDev) < STICK_MIN) {
                message = "Push further";
                holdStart = -1;
                peak.clear();
                stepStart = nowMs;
                return;
            }
            float span = Math.round(Math.abs(bestDev) * 1000f) / 1000f;
            Map<String, Object> a = new LinkedHashMap<>();
            a.put("axis", PadIdentity.axisName(best));
            boolean invert = s.vertical ? bestDev > 0 : bestDev < 0;
            if (invert) {
                a.put("invert", true);
            }
            a.put("neg", (double) span);
            a.put("pos", (double) span);
            unarmed.add(best);
            answers[index] = new Answer(PadProfile.inputKeyForAxis(best, bestDev > 0), a, false);
            advance(nowMs);
            return;
        }
        if (nowMs - stepStart >= TIMEOUT_MS) {
            answers[index] = new Answer(null, null, true);
            message = step().label + ": no input in 10 s, marked unavailable";
            enter(index + 1, nowMs);
        }
    }

    List<String> summary() {
        List<String> out = new ArrayList<>();
        for (int i = 0; i < steps.size(); i++) {
            Step s = steps.get(i);
            if (s.kind == Kind.REST || s.kind == Kind.LIVE) {
                continue;
            }
            Answer a = answers[i];
            String v = a == null ? (i == index ? "<- now" : "") : a.unavailable ? "unavailable"
                    : a.axis != null && s.kind == Kind.STICK ? String.format(Locale.ROOT, "%s%s span %s",
                    a.axis.get("axis"), Boolean.TRUE.equals(a.axis.get("invert")) ? " inverted" : "", a.axis.get("pos"))
                    : describeInput(a.input);
            out.add(s.label + ": " + v);
        }
        return out;
    }

    Map<String, Object> profile(String made) {
        Map<String, Object> p = new LinkedHashMap<>();
        p.put("v", 1);
        Map<String, Object> match = new LinkedHashMap<>();
        match.put("key", identity.deviceKey());
        match.put("vendor", String.format(Locale.ROOT, "%04x", identity.vendorId & 0xffff));
        match.put("product", String.format(Locale.ROOT, "%04x", identity.productId & 0xffff));
        match.put("name", identity.name);
        List<String> axes = new ArrayList<>();
        for (PadIdentity.Range r : identity.ranges) {
            if (r.joystick && !axes.contains(PadIdentity.axisName(r.axis))) {
                axes.add(PadIdentity.axisName(r.axis));
            }
        }
        match.put("axes", axes);
        p.put("match", match);
        p.put("model", model != null ? model : "generic");
        p.put("family", PadProfile.usesGameCubeLabels(model) ? Pad.FAMILY_GAMECUBE
                : PadClass.MODEL_PRO2.equals(model) ? Pad.FAMILY_SWITCH : Pad.FAMILY_GENERIC);
        p.put("rule", PadClass.RULE_PROFILE);
        Map<String, Object> buttons = new LinkedHashMap<>();
        Map<String, Object> axisMap = new LinkedHashMap<>();
        List<String> unavailable = new ArrayList<>();
        for (int i = 0; i < steps.size(); i++) {
            Step s = steps.get(i);
            Answer a = answers[i];
            if (s.kind == Kind.REST || s.kind == Kind.LIVE) {
                continue;
            }
            if (a == null || a.unavailable) {
                for (String u : s.unavailableLabels) {
                    if (!unavailable.contains(u)) {
                        unavailable.add(u);
                    }
                }
                continue;
            }
            if (s.kind == Kind.STICK) {
                axisMap.put(s.slot, a.axis);
            } else if (s.kind == Kind.TRIGGER && a.axis != null) {
                axisMap.put(s.slot, a.axis);
            } else {
                Object prior = buttons.get(a.input);
                if (prior == null) {
                    buttons.put(a.input, s.label);
                } else {
                    List<Object> both = new ArrayList<>();
                    if (prior instanceof List) {
                        both.addAll((List<?>) prior);
                    } else {
                        both.add(prior);
                    }
                    both.add(s.label);
                    buttons.put(a.input, both);
                }
            }
        }
        p.put("buttons", buttons);
        Map<String, Object> ordered = new LinkedHashMap<>();
        for (String slot : PadProfile.AXIS_SLOTS) {
            if (axisMap.containsKey(slot)) {
                ordered.put(slot, axisMap.get(slot));
            }
        }
        p.put("axes", ordered);
        p.put("unavailable", unavailable);
        if (made != null) {
            p.put("made", made);
        }
        return p;
    }
}
