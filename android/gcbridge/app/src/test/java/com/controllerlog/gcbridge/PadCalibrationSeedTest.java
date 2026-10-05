package com.controllerlog.gcbridge;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;

import android.view.MotionEvent;

import org.junit.Test;

import java.io.IOException;
import java.util.HashMap;
import java.util.Map;

public class PadCalibrationSeedTest {

    @Test
    public void valuesSeenBeforeTheSetUpAreTheRestWhenTheRestStepIsSilent() throws IOException {
        PadFixtures.Fixture f = PadFixtures.byId("kernel-driver-gc");
        Map<Integer, Float> seen = new HashMap<>();
        for (PadIdentity.Range r : f.identity.ranges) {
            seen.put(r.axis, 0f);
        }
        seen.put(MotionEvent.AXIS_Z, -1f);
        seen.put(MotionEvent.AXIS_RZ, -1f);
        long t = 0;
        PadCalibration c = new PadCalibration(f.identity, PadClass.MODEL_GAMECUBE, t, seen);
        seen.put(MotionEvent.AXIS_Z, 0.5f);
        while (t < 400) {
            c.tick(t += 50);
        }
        int scan = 0x130;
        for (String label : new String[]{"A", "B", "X", "Y", "Z"}) {
            assertEquals(label, c.step().label);
            c.onKey(scan++, 0, true, t += 100);
        }
        assertEquals("L", c.step().label);
        for (PadIdentity.Range r : f.identity.ranges) {
            c.onAxis(r.axis, r.axis == MotionEvent.AXIS_Z ? 1f : r.axis == MotionEvent.AXIS_RZ ? -1f : 0f, t += 1);
        }
        assertEquals("R", c.step().label);
        Map<String, Object> axes = Json.asObject(c.profile(null).get("axes"));
        Map<String, Object> lt = Json.asObject(axes.get("left_trigger"));
        assertEquals("Z", Json.str(lt, "axis", null));
        assertNull(lt.get("invert"));
        assertNull(axes.get("right_trigger"));
    }
}
