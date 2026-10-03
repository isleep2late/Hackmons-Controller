package com.controllerlog.gcbridge;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertSame;

import org.junit.Test;

import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.Map;

public class HubExtraTest {

    private static Map<String, Object> extra(String rule, Object unavailable) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("transport", "android");
        m.put("rule", rule);
        if (unavailable != null) {
            m.put("unavailable", unavailable);
        }
        return m;
    }

    @Test
    public void reconnectWithANewClassReplacesTheDeviceExtra() {
        InputHub hub = InputHub.get();
        String key = "test:extra:" + System.nanoTime();
        InputHub.Device first = hub.connect(key, "NSO GameCube Controller", "android", Pad.FAMILY_GAMECUBE,
                0x2020, 0x0111, "unknown",
                extra(PadClass.RULE_AYN_GAMECUBE, Arrays.asList("Home", "Capture", "C")));
        InputHub.Device again = hub.connect(key, "NSO GameCube Controller", "android", Pad.FAMILY_GAMECUBE,
                0x2020, 0x0111, "unknown", extra(PadClass.RULE_PROFILE, null));
        assertSame(first, again);
        assertEquals(PadClass.RULE_PROFILE, hub.find(key).extra.get("rule"));
        assertFalse(hub.find(key).extra.containsKey("unavailable"));
        assertEquals(0, CaptureService.unavailableOf(hub.find(key)).size());
        hub.disconnect(key);
    }
}
