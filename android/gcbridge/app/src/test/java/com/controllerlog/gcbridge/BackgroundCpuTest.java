package com.controllerlog.gcbridge;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

import java.io.IOException;
import java.nio.file.Path;

public class BackgroundCpuTest {

    private static String source(String file) throws IOException {
        Path root = TestFiles.root();
        assertNotNull(root);
        String text = TestFiles.read(root.resolve("android/gcbridge/app/src/main/java/com/controllerlog/gcbridge")
                .resolve(file));
        StringBuilder sb = new StringBuilder();
        boolean block = false;
        for (String line : text.split("\n")) {
            String t = line.trim();
            if (block) {
                if (t.contains("*/")) {
                    block = false;
                }
                continue;
            }
            if (t.startsWith("/*")) {
                block = !t.contains("*/");
                continue;
            }
            if (t.startsWith("//") || t.startsWith("*")) {
                continue;
            }
            int c = line.indexOf("//");
            sb.append(c >= 0 && !line.substring(0, c).contains("\"") ? line.substring(0, c) : line).append('\n');
        }
        return sb.toString();
    }

    private static String body(String src, String signature) {
        int at = src.indexOf(signature);
        assertTrue("no " + signature, at >= 0);
        int open = src.indexOf('{', at);
        int depth = 0;
        for (int i = open; i < src.length(); i++) {
            char ch = src.charAt(i);
            if (ch == '{') {
                depth++;
            } else if (ch == '}') {
                depth--;
                if (depth == 0) {
                    return src.substring(open, i + 1);
                }
            }
        }
        throw new AssertionError("unbalanced " + signature);
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
