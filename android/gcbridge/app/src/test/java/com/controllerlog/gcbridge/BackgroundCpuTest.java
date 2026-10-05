package com.controllerlog.gcbridge;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

import java.io.IOException;

public class BackgroundCpuTest {

    private static String source(String file) throws IOException {
        return TestFiles.javaSource(file);
    }

    private static String body(String src, String signature) {
        return TestFiles.body(src, signature);
    }

    @Test
    public void mainScreenListensOnlyWhileVisible() throws IOException {
        String src = source("MainActivity.java");
        String onCreate = body(src, "protected void onCreate(");
        String onStart = body(src, "protected void onStart(");
        String onStop = body(src, "protected void onStop(");
        String onDestroy = body(src, "protected void onDestroy(");
        assertFalse(onCreate.contains("registerInputDeviceListener"));
        assertFalse(onCreate.contains("hub.addListener"));
        assertTrue(onStart.contains("registerInputDeviceListener(this"));
        assertTrue(onStart.contains("hub.addListener(this)"));
        assertTrue(onStop.contains("unregisterInputDeviceListener(this)"));
        assertTrue(onStop.contains("hub.removeListener(this)"));
        assertTrue(onStop.contains("InputRouter.release(this)"));
        assertFalse(onDestroy.contains("registerInputDeviceListener"));
    }

    @Test
    public void deviceChangesAreCoalescedAndQuiet() throws IOException {
        String src = source("MainActivity.java");
        String changed = body(src, "public void onInputDeviceChanged(");
        String added = body(src, "public void onInputDeviceAdded(");
        String removed = body(src, "public void onInputDeviceRemoved(");
        for (String b : new String[]{changed, added, removed}) {
            assertFalse(b.contains("refreshDevices()"));
            assertTrue(b.contains("scheduleDevicesRefresh()"));
        }
        assertTrue(changed.contains("isGameDevice"));
        String append = body(src, "private void appendLog(");
        assertFalse(append.contains("setText"));
        String schedule = body(src, "private void scheduleDevicesRefresh(");
        assertTrue(schedule.contains("if (started)"));
    }

    @Test
    public void routerTrackingIsReleasedByEveryHolder() throws IOException {
        for (String file : new String[]{"CaptureService.java", "KeyCaptureService.java"}) {
            String src = source(file);
            assertTrue(file, src.contains("InputRouter.hold(this, this)"));
            assertTrue(file, src.contains("InputRouter.release(this)"));
        }
    }
}
