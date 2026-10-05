package com.controllerlog.gcbridge;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import android.view.KeyEvent;
import android.view.MotionEvent;

import org.junit.Test;

import java.io.IOException;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;

public class PadClassifierTest {

    private static AndroidInput.AxisRoute stickRoute(List<AndroidInput.AxisRoute> routes, int canonical) {
        for (AndroidInput.AxisRoute r : routes) {
            if (!r.hat && !r.isDigital() && r.canonicalAxis == canonical) {
                return r;
            }
        }
        return null;
    }

    private static AndroidInput.AxisRoute routeFor(List<AndroidInput.AxisRoute> routes, int axis) {
        for (AndroidInput.AxisRoute r : routes) {
            if (!r.hat && !r.isDigital() && r.androidAxis == axis) {
                return r;
            }
        }
        return null;
    }

    private static String axisOf(AndroidInput.AxisRoute r) {
        return r == null ? null : PadIdentity.axisName(r.androidAxis);
    }

    private static Map<String, Object> observe(PadFixtures.Fixture f) {
        PadClass c = f.classify();
        List<AndroidInput.AxisRoute> routes = AndroidInput.routes(c, f.identity);
        Map<String, Object> o = new LinkedHashMap<>();
        o.put("rule", c.rule);
        o.put("model", c.model);
        o.put("family", c.family);
        o.put("deviceKey", f.identity.deviceKey());
        o.put("table", c.table);
        o.put("rawReport", c.rawReport);
        o.put("ignored", c.ignored);
        AndroidInput.AxisRoute lx = stickRoute(routes, Pad.LEFT_X);
        AndroidInput.AxisRoute ly = stickRoute(routes, Pad.LEFT_Y);
        AndroidInput.AxisRoute rx = stickRoute(routes, Pad.RIGHT_X);
        AndroidInput.AxisRoute ry = stickRoute(routes, Pad.RIGHT_Y);
        o.put("leftX", axisOf(lx));
        o.put("leftY", axisOf(ly));
        o.put("rightX", axisOf(rx));
        o.put("rightY", axisOf(ry));
        Set<String> flip = new TreeSet<>();
        for (AndroidInput.AxisRoute r : new AndroidInput.AxisRoute[]{lx, ly, rx, ry}) {
            if (r != null && r.invert) {
                flip.add(PadIdentity.axisName(r.androidAxis));
            }
        }
        o.put("flip", flip);
        Map<String, Object> scale = new LinkedHashMap<>();
        scale.put("left", lx != null ? (double) lx.scalePos : ly != null ? (double) ly.scalePos : 1.0);
        scale.put("right", ry != null ? (double) ry.scalePos : rx != null ? (double) rx.scalePos : 1.0);
        o.put("fullScale", scale);
        Map<String, Object> spans = new LinkedHashMap<>();
        String[] slots = {"left_x", "left_y", "right_x", "right_y"};
        AndroidInput.AxisRoute[] sticks = {lx, ly, rx, ry};
        for (int i = 0; i < slots.length; i++) {
            if (sticks[i] != null && (sticks[i].scaleNeg != 1f || sticks[i].scalePos != 1f)) {
                Map<String, Object> s = new LinkedHashMap<>();
                s.put("neg", (double) sticks[i].scaleNeg);
                s.put("pos", (double) sticks[i].scalePos);
                spans.put(slots[i], s);
            }
        }
        o.put("spans", spans);
        AndroidInput.AxisRoute lt = stickRoute(routes, Pad.LEFT_TRIGGER);
        AndroidInput.AxisRoute rt = stickRoute(routes, Pad.RIGHT_TRIGGER);
        Map<String, Object> trig = new LinkedHashMap<>();
        trig.put("left", lt != null && has(f.identity, lt.androidAxis) ? axisOf(lt) : null);
        trig.put("right", rt != null && has(f.identity, rt.androidAxis) ? axisOf(rt) : null);
        trig.put("centred", (lt != null && lt.centred) || (rt != null && rt.centred));
        o.put("triggers", trig);
        Set<String> used = new HashSet<>();
        for (AndroidInput.AxisRoute r : routes) {
            used.add(PadIdentity.axisName(r.androidAxis));
        }
        Set<String> dropped = new TreeSet<>();
        for (PadIdentity.Range r : f.identity.ranges) {
            if (!used.contains(PadIdentity.axisName(r.axis))) {
                dropped.add(PadIdentity.axisName(r.axis));
            }
        }
        o.put("dropAxes", dropped);
        o.put("analogTriggers", AndroidInput.hasAnalogTriggers(routes));
        o.put("unavailable", new TreeSet<>(c.unavailable));
        return o;
    }

    private static boolean has(PadIdentity id, int axis) {
        return id.hasAxis(axis);
    }

    private static Object normalise(Object v) {
        if (v instanceof Number) {
            return Math.round(((Number) v).doubleValue() * 1000.0) / 1000.0;
        }
        if (v instanceof List) {
            Set<Object> s = new TreeSet<>();
            for (Object o : (List<?>) v) {
                s.add(normalise(o));
            }
            return s;
        }
        if (v instanceof Set) {
            Set<Object> s = new TreeSet<>();
            for (Object o : (Set<?>) v) {
                s.add(normalise(o));
            }
            return s;
        }
        if (v instanceof Map) {
            Map<String, Object> m = new LinkedHashMap<>();
            for (Map.Entry<?, ?> e : ((Map<?, ?>) v).entrySet()) {
                m.put(String.valueOf(e.getKey()), normalise(e.getValue()));
            }
            return m;
        }
        return v;
    }

    @Test
    public void everyFixtureClassifiesAsExpected() throws IOException {
        int checked = 0;
        for (PadFixtures.Fixture f : PadFixtures.all()) {
            Map<String, Object> want = f.expect();
            assertNotNull(f.id + ": no expect", want);
            Map<String, Object> got = observe(f);
            for (Map.Entry<String, Object> e : want.entrySet()) {
                if (e.getValue() == null && !got.containsKey(e.getKey())) {
                    continue;
                }
                if ("fullScale".equals(e.getKey()) && e.getValue() == null) {
                    continue;
                }
                assertTrue(f.id + ": nothing observed for " + e.getKey(), got.containsKey(e.getKey()));
                Object w = normalise(e.getValue());
                Object g = normalise(got.get(e.getKey()));
                if (w instanceof Map && g instanceof Map) {
                    for (Map.Entry<?, ?> we : ((Map<?, ?>) w).entrySet()) {
                        assertEquals(f.id + ": " + e.getKey() + "." + we.getKey(), we.getValue(),
                                ((Map<?, ?>) g).get(we.getKey()));
                    }
                } else {
                    assertEquals(f.id + ": " + e.getKey(), w, g);
                }
                checked++;
            }
        }
        assertTrue(checked > 100);
    }

    @Test
    public void pressesGiveTheTableColumn() throws IOException {
        int presses = 0;
        for (PadFixtures.Fixture f : PadFixtures.all()) {
            PadClass c = f.classify();
            boolean analog = AndroidInput.hasAnalogTriggers(AndroidInput.routes(c, f.identity));
            for (Map<String, Object> p : f.list("presses")) {
                Map<String, Object> want = Json.asObject(p.get("expect"));
                int idx = AndroidInput.buttonForKey(c, PadFixtures.keyCode(p), PadFixtures.scan(p), analog);
                String got = idx < 0 ? null : InputRouter.canonicalName(idx);
                assertEquals(f.id + " " + Json.str(p, "label", "?"), Json.str(want, "gcbridge", null), got);
                presses++;
            }
        }
        assertTrue(presses >= 40);
    }

    @Test
    public void thorCopyGetsAllThirteenMeasuredPressesRight() throws IOException {
        PadFixtures.Fixture f = PadFixtures.byId("thor-ayn-gc-copy");
        PadClass c = f.classify();
        assertEquals(PadClass.RULE_AYN_GAMECUBE, c.rule);
        assertEquals(13, f.list("presses").size());
        Set<String> outputs = new HashSet<>();
        for (Map<String, Object> p : f.list("presses")) {
            int idx = AndroidInput.buttonForKey(c, PadFixtures.keyCode(p), PadFixtures.scan(p), false);
            assertTrue(Json.str(p, "label", "?"), idx != -1);
            outputs.add(InputRouter.canonicalName(idx));
        }
        assertEquals(13, outputs.size());
    }

    @Test
    public void axisSamplesFollowFlipAndScale() throws IOException {
        int samples = 0;
        for (PadFixtures.Fixture f : PadFixtures.all()) {
            PadClass c = f.classify();
            List<AndroidInput.AxisRoute> routes = AndroidInput.routes(c, f.identity);
            for (Map<String, Object> s : f.list("axisSamples")) {
                int axis = PadIdentity.axisFromName(Json.str(s, "axis", null));
                float raw = (float) Json.num(s, "raw", 0);
                String canonical = Json.str(s, "canonical", null);
                AndroidInput.AxisRoute r = routeFor(routes, axis);
                String label = f.id + " " + Json.str(s, "axis", "?") + "=" + raw;
                if (canonical == null) {
                    assertNull(label + " should not be routed", r);
                    continue;
                }
                assertNotNull(label + " has no route", r);
                assertEquals(label, canonical, Pad.AXES[r.canonicalAxis]);
                float v = r.apply(raw);
                float clamped = Math.max(r.trigger ? 0f : -1f, Math.min(1f, v));
                assertEquals(label, Json.num(s, "value", Double.NaN), clamped, 0.0015);
                assertEquals(label, Math.round(clamped * Pad.AXIS_MAX), AndroidInput.axisValue(v, r.trigger), 1);
                samples++;
            }
        }
        assertTrue(samples >= 30);
    }

    @Test
    public void demotionFollowsTheSpec() throws IOException {
        int rows = 0;
        for (PadFixtures.Fixture f : PadFixtures.all()) {
            PadClass c = f.classify();
            for (Map<String, Object> d : f.list("demotions")) {
                boolean want = Boolean.TRUE.equals(d.get("demotes"));
                String why;
                if (d.containsKey("hat")) {
                    float v = (float) Json.num(d, "value", 0);
                    boolean x = "HAT_X".equals(Json.str(d, "hat", ""));
                    why = PadClassifier.hatDemotionReason(c, x ? v : 0f, x ? 0f : v);
                } else {
                    why = PadClassifier.demotionReason(c, PadFixtures.scan(d), PadFixtures.keyCode(d));
                }
                assertEquals(f.id + " " + d, want, why != null);
                rows++;
            }
        }
        assertTrue(rows >= 10);
    }

    @Test
    public void demotedCopyFallsBackToKeyCodes() throws IOException {
        PadFixtures.Fixture f = PadFixtures.byId("thor-ayn-gc-copy");
        PadClass c = f.classify();
        String why = PadClassifier.demotionReason(c, 0x220, KeyEvent.KEYCODE_DPAD_UP);
        assertNotNull(why);
        PadClass d = PadClassifier.demote(c, why);
        assertEquals(PadClass.RULE_DEMOTED, d.rule);
        assertFalse(d.rawReport);
        assertEquals(Pad.DPAD_UP, AndroidInput.buttonForKey(d, KeyEvent.KEYCODE_DPAD_UP, 0x220, false));
        assertNull(PadClassifier.demotionReason(d, 0x110, 0));
        assertFalse(AndroidInput.routes(d, f.identity).isEmpty());
        assertEquals(PadClass.RULE_AYN_GAMECUBE, c.rule);
    }

    @Test
    public void odinAndCopyStayApart() throws IOException {
        PadFixtures.Fixture copy = PadFixtures.byId("thor-ayn-gc-copy");
        PadFixtures.Fixture odin = PadFixtures.byId("thor-odin-builtin");
        assertEquals(copy.identity.vendorId, odin.identity.vendorId);
        assertEquals(copy.identity.productId, odin.identity.productId);
        assertNotEquals(copy.identity.deviceKey(), odin.identity.deviceKey());
        PadClass c = copy.classify();
        PadClass o = odin.classify();
        assertTrue(c.flipsLeftY());
        assertFalse(o.flipsLeftY());
        Set<String> ignoreCopy = new HashSet<>();
        ignoreCopy.add(copy.identity.deviceKey());
        assertTrue(PadClassifier.classify(copy.identity, null, ignoreCopy).ignored);
        assertFalse(PadClassifier.classify(odin.identity, null, ignoreCopy).ignored);
        Map<String, Map<String, Object>> profiles = PadFixtures.byId("thor-ayn-gc-copy-profile").profiles;
        assertEquals(PadClass.RULE_PROFILE, PadClassifier.classify(copy.identity, profiles, null).rule);
        assertEquals(PadClass.RULE_BUILTIN, PadClassifier.classify(odin.identity, profiles, null).rule);
    }

    @Test
    public void familyTestsGameCubeBeforeNintendo() {
        assertEquals("gamecube", AndroidInput.family(0x2020, 0x0111, "Nintendo Nintendo GameCube Controller"));
        assertEquals("gamecube", AndroidInput.family(0, 0, "Nintendo Co., Ltd. NSO GameCube Controller"));
        assertEquals("switch", AndroidInput.family(0x2020, 0x0111, "Nintendo Switch Pro Controller"));
        assertEquals("generic", AndroidInput.family(0x2020, 0x0111, "Odin Controller"));
    }

    @Test
    public void tokensMatchWholeWordsOnly() {
        assertTrue(PadIdentity.containsWord("nintendo co., ltd. nso gamecube controller", "nso"));
        assertFalse(PadIdentity.containsWord("odin console gamepad", "nso"));
        assertTrue(PadIdentity.containsWord("nintendo switch 2 pro controller", "pro controller"));
        assertFalse(PadIdentity.containsWord("supergamecubex", "gamecube"));
        assertEquals("sig:2020:0111:nintendo nintendo gamecube controller",
                PadIdentity.deviceKey(0x2020, 0x0111, "  Nintendo   Nintendo GameCube\tController "));
    }

    @Test
    public void legacyPathOf021ScramblesTheThorCopy() throws IOException {
        PadFixtures.Fixture f = PadFixtures.byId("thor-ayn-gc-copy");
        int vid = f.identity.vendorId;
        int pid = f.identity.productId;
        List<AndroidInput.AxisRoute> legacy = AndroidInput.routes(f.identity.axes(), AndroidInput.invertsStickY(vid, pid));
        boolean analog = AndroidInput.hasAnalogTriggers(legacy);
        assertTrue(analog);
        int right = 0;
        List<String> lost = new ArrayList<>();
        for (Map<String, Object> p : f.list("presses")) {
            String want = Json.str(Json.asObject(p.get("expect")), "gcbridge", null);
            int idx = AndroidInput.buttonForKey(PadFixtures.keyCode(p), PadFixtures.scan(p), vid, pid, analog);
            if (idx < 0) {
                lost.add(Json.str(p, "label", "?"));
            } else if (InputRouter.canonicalName(idx).equals(want)) {
                right++;
            }
        }
        assertEquals(0, right);
        assertEquals(2, lost.size());
        assertTrue(lost.contains("DDown") && lost.contains("DRight"));
        assertFalse(routeFor(legacy, MotionEvent.AXIS_Y).invert);
    }

    @Test
    public void volumeKeysAreSystemKeys() {
        assertTrue(AndroidInput.isSystemVolumeKey(KeyEvent.KEYCODE_VOLUME_UP));
        assertTrue(AndroidInput.isSystemVolumeKey(KeyEvent.KEYCODE_VOLUME_DOWN));
        assertTrue(AndroidInput.isSystemVolumeKey(KeyEvent.KEYCODE_VOLUME_MUTE));
        assertTrue(AndroidInput.isSystemVolumeKey(KeyEvent.KEYCODE_MUTE));
        assertFalse(AndroidInput.isSystemVolumeKey(KeyEvent.KEYCODE_BUTTON_A));
        assertFalse(AndroidInput.isSystemVolumeKey(KeyEvent.KEYCODE_BACK));
    }

    @Test
    public void unavailableLabelsDimTheirOverlayElements() {
        Set<String> dim = PadProfile.unavailableInputs(PadClassifier.GC_UNAVAILABLE);
        assertEquals(new TreeSet<>(java.util.Arrays.asList("guide", "misc1", "misc2")), new TreeSet<>(dim));
    }
}
