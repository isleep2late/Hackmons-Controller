package com.controllerlog.gcbridge;

import static org.junit.Assert.assertTrue;

import org.junit.Test;

import java.io.IOException;

public class SystemKeysTest {

    private static String squash(String s) {
        return s.replaceAll("\\s+", "");
    }

    @Test
    public void activityHandsVolumeKeysToAndroidBeforeAnyGamepadCheck() throws IOException {
        String body = squash(TestFiles.body(TestFiles.javaSource("MainActivity.java"),
                "public boolean dispatchKeyEvent("));
        assertTrue(body, body.startsWith(
                "{if(AndroidInput.isSystemVolumeKey(event.getKeyCode())){returnsuper.dispatchKeyEvent(event);}"));
    }

    @Test
    public void routerNeverTakesVolumeKeys() throws IOException {
        String body = squash(TestFiles.body(TestFiles.javaSource("InputRouter.java"), "static boolean onKey("));
        int volume = body.indexOf("if(AndroidInput.isSystemVolumeKey(e.getKeyCode())){returnfalse;}");
        assertTrue(body, volume >= 0);
        assertTrue(body, volume < body.indexOf("returntrue;"));
        assertTrue(body, volume < body.indexOf("press("));
    }
}
