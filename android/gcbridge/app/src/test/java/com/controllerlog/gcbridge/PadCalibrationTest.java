package com.controllerlog.gcbridge;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import android.view.MotionEvent;

import org.junit.Test;

import java.io.IOException;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;

public class PadCalibrationTest {

    private long t;

    private void tick(PadCalibration c, long ms) {
        long end = t + ms;
        while (t < end) {
            t += 50;
            c.tick(t);
        }
    }

    private void hold(PadCalibration c, int axis, float value) {
        c.onAxis(axis, value * 0.4f, t);
        t += 20;
        c.onAxis(axis, value, t);
        for (int i = 0; i < 25; i++) {
            t += 50;
            c.onAxis(axis, value, t);
            c.tick(t);
        }
        c.onAxis(axis, 0f, t);
    }

    private PadCalibration thorRun(PadFixtures.Fixture f) {
        t = 1000;
        PadCalibration c = new PadCalibration(f.identity, PadClass.MODEL_GAMECUBE, t);
        assertEquals(PadCalibration.Kind.REST, c.step().kind);
        tick(c, 400);
        Map<String, Map<String, Object>> byLabel = new HashMap<>();
        for (Map<String, Object> p : f.list("presses")) {
            byLabel.put(Json.str(p, "label", ""), p);
        }
        for (String label : Arrays.asList("A", "B", "X", "Y", "Z", "L", "R", "Start", "DUp", "DDown", "DLeft",
                "DRight", "ZL")) {
            assertEquals(label, c.step().label);
            Map<String, Object> p = byLabel.get(label);
            assertNotNull(label, p);
            t += 300;
            c.onKey(PadFixtures.scan(p), PadFixtures.keyCode(p), true, t);
            t += 100;
            c.onKey(PadFixtures.scan(p), PadFixtures.keyCode(p), false, t);
        }
        for (String label : Arrays.asList("Home", "Capture", "C")) {
            assertEquals(label, c.step().label);
            tick(c, PadCalibration.TIMEOUT_MS + 100);
        }
        assertEquals("left_y", c.step().slot);
        hold(c, MotionEvent.AXIS_Y, 0.5889f);
        assertEquals("left_x", c.step().slot);
        hold(c, MotionEvent.AXIS_X, 0.6186f);
        assertEquals("right_y", c.step().slot);
        hold(c, MotionEvent.AXIS_RZ, 0.5166f);
        assertEquals("right_x", c.step().slot);
        tick(c, PadCalibration.TIMEOUT_MS + 100);
        assertTrue(c.isLive());
        return c;
    }

    @Test
    public void thorPressSequenceGivesTheBuiltInRule3Mapping() throws IOException {
        PadFixtures.Fixture f = PadFixtures.byId("thor-ayn-gc-copy");
        Map<String, Object> profile = thorRun(f).profile("test");
        Map<String, Object> roundTrip = Json.asObject(Json.parse(Json.write(profile)));
        Map<String, Map<String, Object>> profiles = Collections.singletonMap(f.identity.deviceKey(), roundTrip);
        PadClass fromProfile = PadClassifier.classify(f.identity, profiles, null);
        PadClass rule3 = f.classify();
        assertEquals(PadClass.RULE_PROFILE, fromProfile.rule);
        assertEquals(PadClass.RULE_AYN_GAMECUBE, rule3.rule);
        assertTrue(fromProfile.rawReport);
        for (Map<String, Object> p : f.list("presses")) {
            int a = AndroidInput.buttonForKey(fromProfile, PadFixtures.keyCode(p), PadFixtures.scan(p), false);
            int b = AndroidInput.buttonForKey(rule3, PadFixtures.keyCode(p), PadFixtures.scan(p), false);
            assertEquals(Json.str(p, "label", "?"), b, a);
            assertEquals(Json.str(Json.asObject(p.get("expect")), "gcbridge", null), InputRouter.canonicalName(a));
        }
        assertEquals(rule3.flipsLeftY(), fromProfile.flipsLeftY());
        assertEquals(rule3.flipsRightY(), fromProfile.flipsRightY());
        List<AndroidInput.AxisRoute> pr = AndroidInput.routes(fromProfile, f.identity);
        List<AndroidInput.AxisRoute> rr = AndroidInput.routes(rule3, f.identity);
        assertEquals(rr.size(), pr.size());
        for (AndroidInput.AxisRoute r : rr) {
            AndroidInput.AxisRoute q = null;
            for (AndroidInput.AxisRoute x : pr) {
                if (x.androidAxis == r.androidAxis) {
                    q = x;
                }
            }
            assertNotNull(PadIdentity.axisName(r.androidAxis), q);
            assertEquals(r.canonicalAxis, q.canonicalAxis);
            assertEquals(r.invert, q.invert);
            assertEquals(r.scalePos, q.scalePos, 0.05f);
        }
        assertEquals(new TreeSet<>(rule3.unavailable), new TreeSet<>(fromProfile.unavailable));
        Map<String, Object> axes = Json.asObject(roundTrip.get("axes"));
        assertNull(axes.get("right_x"));
        assertEquals(0.589, Json.num(Json.asObject(axes.get("left_y")), "pos", 0), 0.001);
        assertEquals("gamecube", Json.str(roundTrip, "family", null));
        assertEquals(f.identity.deviceKey(), PadProfile.matchKey(roundTrip));
    }

    @Test
    public void silentStepTimesOutAsUnavailable() throws IOException {
        PadFixtures.Fixture f = PadFixtures.byId("thor-ayn-gc-copy");
        t = 0;
        PadCalibration c = new PadCalibration(f.identity, PadClass.MODEL_GAMECUBE, t);
        tick(c, 400);
        assertEquals("A", c.step().label);
        tick(c, PadCalibration.TIMEOUT_MS - 500);
        assertEquals("A", c.step().label);
        tick(c, 600);
        assertEquals("B", c.step().label);
        List<?> unavailable = Json.asArray(c.profile(null).get("unavailable"));
        assertTrue(unavailable.contains("A"));
    }

    @Test
    public void reusedInputAsksAndCanBeRefusedOrShared() throws IOException {
        PadFixtures.Fixture f = PadFixtures.byId("thor-ayn-gc-copy");
        t = 0;
        PadCalibration c = new PadCalibration(f.identity, PadClass.MODEL_GAMECUBE, t);
        tick(c, 400);
        c.onKey(0x131, 97, true, t += 100);
        assertEquals("B", c.step().label);
        c.onKey(0x131, 97, true, t += 100);
        assertTrue(c.awaitingConfirm());
        assertTrue(c.prompt().contains("already A"));
        c.answerBoth(false, t += 100);
        assertFalse(c.awaitingConfirm());
        assertEquals("B", c.step().label);
        c.onKey(0x131, 97, true, t += 100);
        c.answerBoth(true, t += 100);
        assertEquals("X", c.step().label);
        Object shared = Json.asObject(c.profile(null).get("buttons")).get("scan:0x131");
        assertEquals(Arrays.asList("A", "B"), shared);
        c.back(t += 100);
        assertEquals("B", c.step().label);
        c.onKey(0x130, 96, true, t += 100);
        assertEquals("X", c.step().label);
        assertEquals("B", Json.asObject(c.profile(null).get("buttons")).get("scan:0x130"));
        assertEquals("A", Json.asObject(c.profile(null).get("buttons")).get("scan:0x131"));
    }

    @Test
    public void weakStickPushAsksForMore() throws IOException {
        PadFixtures.Fixture f = PadFixtures.byId("thor-ayn-gc-copy");
        t = 0;
        PadCalibration c = new PadCalibration(f.identity, PadClass.MODEL_GAMECUBE, t);
        tick(c, 400);
        while (c.step().kind != PadCalibration.Kind.STICK) {
            c.skip(t += 10);
        }
        hold(c, MotionEvent.AXIS_Y, 0.25f);
        assertEquals("left_y", c.step().slot);
        assertEquals("Push further", c.message());
        hold(c, MotionEvent.AXIS_Y, 0.6f);
        assertEquals("left_x", c.step().slot);
    }

    @Test
    public void triggerOnAnAxisBecomesAnAxisAnswer() throws IOException {
        PadFixtures.Fixture f = PadFixtures.byId("xbox360");
        t = 0;
        PadCalibration c = new PadCalibration(f.identity, null, t);
        tick(c, 400);
        while (!"LeftTrigger".equals(c.step().label)) {
            c.skip(t += 10);
        }
        c.onAxis(MotionEvent.AXIS_LTRIGGER, 0.9f, t += 10);
        assertEquals("RightTrigger", c.step().label);
        c.onAxis(MotionEvent.AXIS_LTRIGGER, 0.95f, t += 10);
        assertEquals("RightTrigger", c.step().label);
        c.onAxis(MotionEvent.AXIS_RTRIGGER, 1f, t += 10);
        Map<String, Object> axes = Json.asObject(c.profile(null).get("axes"));
        assertEquals("LTRIGGER", Json.str(Json.asObject(axes.get("left_trigger")), "axis", null));
        assertEquals("RTRIGGER", Json.str(Json.asObject(axes.get("right_trigger")), "axis", null));
        assertEquals("generic", c.profile(null).get("model"));
    }
}
