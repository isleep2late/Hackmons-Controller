package com.controllerlog.gcbridge;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.Locale;

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
    public void pinnedCertificateIsTheIs2lRemoteReleaseOne() {
        assertEquals("84b56217735286469bdc3db3eab919c275bf7d5a85a41b4fa13170e2758d866d",
                RemoteFeed.REMOTE_CERT_SHA256);
        assertEquals("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
                RemoteFeed.sha256Hex("abc".getBytes(StandardCharsets.US_ASCII)));
        assertEquals("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                RemoteFeed.sha256Hex(new byte[0]));
    }

    @Test
    public void onlyThePinnedCertificateIsAccepted() {
        byte[] good = "abc".getBytes(StandardCharsets.US_ASCII);
        byte[] other = "abd".getBytes(StandardCharsets.US_ASCII);
        String pin = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";
        assertTrue(RemoteFeed.signedWith(pin, new byte[][]{good}));
        assertTrue(RemoteFeed.signedWith(pin.toUpperCase(Locale.ROOT), new byte[][]{good}));
        assertTrue(RemoteFeed.signedWith("BA:78:16:BF:8F:01:CF:EA:41:41:40:DE:5D:AE:22:23:"
                + "B0:03:61:A3:96:17:7A:9C:B4:10:FF:61:F2:00:15:AD", new byte[][]{good}));
        assertTrue(RemoteFeed.signedWith(pin, new byte[][]{good, good}));
        assertFalse(RemoteFeed.signedWith(pin, new byte[][]{other}));
        assertFalse(RemoteFeed.signedWith(pin, new byte[][]{good, other}));
        assertFalse(RemoteFeed.signedWith(pin, new byte[][]{good, null}));
        assertFalse(RemoteFeed.signedWith(pin, new byte[0][]));
        assertFalse(RemoteFeed.signedWith(pin, null));
        assertFalse(RemoteFeed.signedWith(null, new byte[][]{good}));
        assertFalse(RemoteFeed.signedWith("", new byte[][]{good}));
        assertFalse(RemoteFeed.signedWith(pin.substring(0, 62), new byte[][]{good}));
        assertFalse(RemoteFeed.signedWith(RemoteFeed.REMOTE_CERT_SHA256, new byte[][]{good}));
    }

    @Test
    public void aWrongSignatureIsSaidInPlainWordsAndChecksComeBeforeTheBinding() throws IOException {
        String refused = RemoteFeed.statusText(true, RemoteFeed.LINK_UNTRUSTED, 0);
        assertTrue(refused.startsWith("The is2l Remote on this device is not the official release"));
        assertTrue(refused.contains("nothing is sent to it"));
        assertTrue(refused.contains(RemoteFeed.RELEASES_URL));
        assertEquals("off", RemoteFeed.statusText(false, RemoteFeed.LINK_UNTRUSTED, 0));
        String src = TestFiles.javaSource("RemoteFeed.java");
        int check = src.indexOf("if (!trusted()) {\n            refuse();\n            return;");
        int bind = src.indexOf("app.bindService(");
        assertTrue(check > 0);
        assertTrue(bind > check);
        assertEquals(bind, src.lastIndexOf("app.bindService("));
        int connected = src.indexOf("public void onServiceConnected");
        int recheck = src.indexOf("if (!trusted()) {", connected);
        int messenger = src.indexOf("messenger = new Messenger(service)");
        assertTrue(connected > 0 && recheck > connected && messenger > recheck);
        assertEquals(messenger, src.lastIndexOf("messenger = new Messenger("));
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
