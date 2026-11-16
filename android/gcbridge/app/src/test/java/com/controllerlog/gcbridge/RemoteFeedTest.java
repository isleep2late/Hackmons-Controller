package com.controllerlog.gcbridge;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

import java.io.IOException;

public class RemoteFeedTest {

    @Test
    public void protocolConstantsAreTheOnesIs2lRemoteExpects() {
        assertEquals("com.isleep2late.remote", RemoteFeed.REMOTE_PACKAGE);
        assertEquals("com.isleep2late.remote.PAD_FEED", RemoteFeed.ACTION_PAD_FEED);
        assertEquals(1, RemoteFeed.MSG_PAD_STATE);
        assertEquals(2, RemoteFeed.MSG_PAD_GONE);
        assertEquals(1, RemoteFeed.PROTOCOL_VERSION);
        assertEquals("v", RemoteFeed.KEY_VERSION);
        assertEquals("buttons", RemoteFeed.KEY_BUTTONS);
        assertEquals("axes", RemoteFeed.KEY_AXES);
        assertEquals("name", RemoteFeed.KEY_NAME);
        assertEquals(4_000_000L, RemoteFeed.MIN_INTERVAL_NS);
        assertEquals(500, RemoteFeed.REPEAT_MS);
    }

    @Test
    public void buttonBitsFollowTheSdlIndices() {
        Pad.State s = new Pad.State();
        assertEquals(0, RemoteFeed.packButtons(s.buttons));
        s.setButton(Pad.SOUTH, true);
        assertEquals(1, RemoteFeed.packButtons(s.buttons));
        s.setButton(Pad.START, true);
        s.setButton(Pad.DPAD_RIGHT, true);
        assertEquals(1 | 1 << 6 | 1 << 14, RemoteFeed.packButtons(s.buttons));
        s.clear();
        s.setButton(Pad.MISC6, true);
        assertEquals(1 << 25, RemoteFeed.packButtons(s.buttons));
        for (int i = 0; i < Pad.NUM_BUTTONS; i++) {
            s.clear();
            s.setButton(i, true);
            assertEquals(Pad.BUTTONS[i], 1 << i, RemoteFeed.packButtons(s.buttons));
        }
        assertEquals(0b101, RemoteFeed.packButtons(new int[]{7, 0, 1}));
    }

    @Test
    public void axesKeepTheSdlOrderAndRanges() {
        Pad.State s = new Pad.State();
        s.setAxis(Pad.LEFT_X, -32768);
        s.setAxis(Pad.LEFT_Y, 32767);
        s.setAxis(Pad.RIGHT_X, 1234);
        s.setAxis(Pad.RIGHT_Y, -5);
        s.setAxis(Pad.LEFT_TRIGGER, 32767);
        s.setAxis(Pad.RIGHT_TRIGGER, 100);
        assertArrayEquals(new int[]{-32768, 32767, 1234, -5, 32767, 100}, RemoteFeed.packAxes(s.axes));
        assertArrayEquals(new int[]{-32768, 32767, 0, 0, 0, 32767},
                RemoteFeed.packAxes(new int[]{-99999, 99999, 0, 0, -20000, 40000}));
        assertArrayEquals(new int[]{5, 0, 0, 0, 0, 0}, RemoteFeed.packAxes(new int[]{5}));
        int[] src = {1, 2, 3, 4, 5, 6};
        int[] packed = RemoteFeed.packAxes(src);
        packed[0] = 99;
        assertEquals(1, src[0]);
    }

    @Test
    public void aSendWaitsOutTheRestOfFourMilliseconds() {
        assertEquals(4, RemoteFeed.delayMs(0));
        assertEquals(4, RemoteFeed.delayMs(1));
        assertEquals(3, RemoteFeed.delayMs(1_000_000));
        assertEquals(1, RemoteFeed.delayMs(3_999_999));
        assertEquals(0, RemoteFeed.delayMs(4_000_000));
        assertEquals(0, RemoteFeed.delayMs(Long.MAX_VALUE));
        assertEquals(4, RemoteFeed.delayMs(-50));
    }

    @Test
    public void changesBetweenTwoSendsAskForOneFlush() {
        RemoteFeed.Throttle t = new RemoteFeed.Throttle();
        long ms = 1_000_000L;
        assertEquals(0, t.request(100 * ms));
        assertEquals(-1, t.request(100 * ms));
        assertEquals(-1, t.request(101 * ms));
        t.flushed(101 * ms);
        assertEquals(3, t.request(102 * ms));
        assertEquals(-1, t.request(103 * ms));
        t.flushed(105 * ms);
        assertEquals(0, t.request(109 * ms));
        t.flushed(109 * ms);
        assertEquals(0, t.request(700 * ms));
        t.reset();
        assertEquals(0, t.request(700 * ms));
    }

    @Test
    public void androidControllersAreSentOnlyAsFarAsTheyAreRead() {
        assertEquals(RemoteFeed.SEND_ALL, RemoteFeed.coverage("android", true));
        assertEquals(RemoteFeed.SEND_NOTHING, RemoteFeed.coverage("android", false));
        for (String reader : new String[]{"gcbridge-usb", "gcbridge-ble"}) {
            assertEquals(RemoteFeed.SEND_ALL, RemoteFeed.coverage(reader, false));
            assertEquals(RemoteFeed.SEND_ALL, RemoteFeed.coverage(reader, true));
        }
    }

    @Test
    public void statusSaysWhereToGetIs2lRemote() {
        assertEquals("off", RemoteFeed.statusText(false, RemoteFeed.LINK_CONNECTED, 9));
        assertEquals("connected, 12 states sent",
                RemoteFeed.statusText(true, RemoteFeed.LINK_CONNECTED, 12));
        String missing = RemoteFeed.statusText(true, RemoteFeed.LINK_NOT_INSTALLED, 0);
        assertTrue(missing.startsWith("is2l Remote not installed"));
        assertTrue(missing.contains("https://github.com/isleep2late/is2l-remote/releases"));
    }

    @Test
    public void manifestMayAskForIs2lRemoteAndEveryHolderLetsGo() throws IOException {
        String manifest = TestFiles.read(TestFiles.root().resolve("android/gcbridge/app/src/main/AndroidManifest.xml"));
        assertTrue(manifest.contains("<package android:name=\"" + RemoteFeed.REMOTE_PACKAGE + "\" />"));
        for (String file : new String[]{"MainActivity.java", "CaptureService.java", "KeyCaptureService.java"}) {
            String src = TestFiles.javaSource(file);
            assertTrue(file, src.contains("RemoteFeed.hold(this, this)"));
            assertTrue(file, src.contains("RemoteFeed.release(this)"));
        }
    }
}
