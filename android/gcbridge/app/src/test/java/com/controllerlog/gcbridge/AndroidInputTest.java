package com.controllerlog.gcbridge;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import android.view.KeyEvent;
import android.view.MotionEvent;

import org.junit.Test;

import java.util.List;

public class AndroidInputTest {

    @Test
    public void familiesFromVendorIds() {
        assertEquals("playstation", AndroidInput.family(0x054C, 0x0CE6, "Wireless Controller"));
        assertEquals("switch", AndroidInput.family(0x057E, 0x2009, "Pro Controller"));
        assertEquals("gamecube", AndroidInput.family(0x057E, 0x2073, ""));
        assertEquals("xbox", AndroidInput.family(0x045E, 0x0B13, ""));
        assertEquals("xbox", AndroidInput.family(0, 0, "Xbox Wireless Controller"));
        assertEquals("generic", AndroidInput.family(0x2DC8, 0x6002, "8BitDo SN30 Pro"));
    }

    @Test
    public void scanCodesFollowAdbBackend() {
        // xpad / plain HID: BTN_NORTH (0x133) is the X button on the left
        assertEquals(Pad.WEST, AndroidInput.buttonForKey(KeyEvent.KEYCODE_BUTTON_X, 0x133, 0x045E, 0, true));
        assertEquals(Pad.NORTH, AndroidInput.buttonForKey(KeyEvent.KEYCODE_BUTTON_Y, 0x134, 0x045E, 0, true));
        // Sony / Nintendo drivers name buttons by position
        assertEquals(Pad.NORTH, AndroidInput.buttonForKey(KeyEvent.KEYCODE_BUTTON_X, 0x133, 0x054C, 0, true));
        assertEquals(Pad.WEST, AndroidInput.buttonForKey(KeyEvent.KEYCODE_BUTTON_Y, 0x134, 0x057E, 0x2009, true));
        assertEquals(Pad.SOUTH, AndroidInput.buttonForKey(KeyEvent.KEYCODE_BUTTON_A, 0x130, 0, 0, true));
        assertEquals(Pad.EAST, AndroidInput.buttonForKey(KeyEvent.KEYCODE_BUTTON_B, 0x131, 0, 0, true));
        assertEquals(Pad.DPAD_LEFT, AndroidInput.buttonForKey(KeyEvent.KEYCODE_DPAD_LEFT, 0x222, 0, 0, true));
        assertEquals(Pad.RIGHT_PADDLE1, AndroidInput.buttonForKey(KeyEvent.KEYCODE_BUTTON_1, 0x2c0, 0, 0, true));
        assertEquals(Pad.MISC3, AndroidInput.buttonForKey(KeyEvent.KEYCODE_BUTTON_5, 0x2c4, 0, 0, true));
        assertEquals(Pad.BACK, AndroidInput.buttonForKey(KeyEvent.KEYCODE_BACK, 158, 0, 0, true));
        assertEquals(Pad.GUIDE, AndroidInput.buttonForKey(KeyEvent.KEYCODE_BUTTON_MODE, 0x13c, 0, 0, true));
    }

    @Test
    public void digitalTriggersOnlyWithoutAnalogAxes() {
        assertEquals(AndroidInput.KEY_LEFT_TRIGGER, AndroidInput.buttonForKey(KeyEvent.KEYCODE_BUTTON_L2, 0x138, 0, 0, false));
        assertEquals(-1, AndroidInput.buttonForKey(KeyEvent.KEYCODE_BUTTON_L2, 0x138, 0, 0, true));
        assertEquals(AndroidInput.KEY_RIGHT_TRIGGER, AndroidInput.buttonForKey(KeyEvent.KEYCODE_BUTTON_R2, 0, 0, 0, false));
    }

    @Test
    public void keyCodesAreTheFallback() {
        assertEquals(Pad.SOUTH, AndroidInput.buttonForKey(KeyEvent.KEYCODE_BUTTON_A, 0, 0, 0, true));
        assertEquals(Pad.WEST, AndroidInput.buttonForKey(KeyEvent.KEYCODE_BUTTON_X, 0, 0x045E, 0, true));
        assertEquals(Pad.NORTH, AndroidInput.buttonForKey(KeyEvent.KEYCODE_BUTTON_X, 0, 0x054C, 0, true));
        assertEquals(Pad.START, AndroidInput.buttonForKey(KeyEvent.KEYCODE_BUTTON_START, 0, 0, 0, true));
        assertEquals(Pad.MISC1, AndroidInput.buttonForKey(KeyEvent.KEYCODE_MEDIA_RECORD, 0, 0, 0, true));
        assertEquals(-1, AndroidInput.buttonForKey(KeyEvent.KEYCODE_VOLUME_UP, 115, 0, 0, true));
    }

    /** The order confirmed by pressing on the NSO GameCube controller in standard HID mode. */
    @Test
    public void switch2GameCubeStandardReportButtons() {
        int gc = 0x2073, vid = 0x057E;
        int[][] want = {
                {0x130, Pad.WEST}, {0x131, Pad.SOUTH}, {0x132, Pad.NORTH}, {0x133, Pad.EAST},
                {0x134, AndroidInput.KEY_RIGHT_TRIGGER}, {0x135, Pad.RIGHT_SHOULDER}, {0x136, Pad.START},
                {0x138, Pad.DPAD_DOWN}, {0x139, Pad.DPAD_RIGHT}, {0x13a, Pad.DPAD_LEFT}, {0x13b, Pad.DPAD_UP},
                {0x13c, AndroidInput.KEY_LEFT_TRIGGER}, {0x13e, Pad.BACK},
                {0x2c0, Pad.GUIDE}, {0x2c1, Pad.MISC1}, {0x2c4, Pad.MISC2},
        };
        for (int[] w : want) {
            assertEquals("scan 0x" + Integer.toHexString(w[0]), w[1],
                    AndroidInput.buttonForKey(KeyEvent.KEYCODE_UNKNOWN, w[0], vid, gc, false));
        }
        // the trigger clicks are the only trigger information the standard report has
        assertEquals(AndroidInput.KEY_RIGHT_TRIGGER, AndroidInput.buttonForKey(KeyEvent.KEYCODE_BUTTON_Y, 0x134, vid, gc, true));
        // Pro Controller 2: same slots, Nintendo's positional layout, real R/ZR and L/ZL
        int pro = 0x2069;
        assertEquals(Pad.SOUTH, AndroidInput.buttonForKey(0, 0x130, vid, pro, false));
        assertEquals(Pad.EAST, AndroidInput.buttonForKey(0, 0x131, vid, pro, false));
        assertEquals(Pad.RIGHT_SHOULDER, AndroidInput.buttonForKey(0, 0x134, vid, pro, false));
        assertEquals(AndroidInput.KEY_RIGHT_TRIGGER, AndroidInput.buttonForKey(0, 0x135, vid, pro, false));
        assertEquals(Pad.LEFT_SHOULDER, AndroidInput.buttonForKey(0, 0x13c, vid, pro, false));
        assertEquals(AndroidInput.KEY_LEFT_TRIGGER, AndroidInput.buttonForKey(0, 0x13d, vid, pro, false));
        // other Nintendo pads (hid-nintendo, e.g. the Switch 1 Pro Controller) keep the kernel's names
        assertEquals(Pad.SOUTH, AndroidInput.buttonForKey(KeyEvent.KEYCODE_BUTTON_A, 0x130, vid, 0x2009, true));
        assertTrue(AndroidInput.invertsStickY(vid, gc));
        assertFalse(AndroidInput.invertsStickY(vid, 0x2009));
        assertFalse(AndroidInput.invertsStickY(0x054C, 0x0CE6));
    }

    @Test
    public void switch2StandardSticksAreFlippedVertically() {
        List<AndroidInput.AxisRoute> r = AndroidInput.routes(new int[]{MotionEvent.AXIS_X, MotionEvent.AXIS_Y,
                MotionEvent.AXIS_RX, MotionEvent.AXIS_RZ}, true);
        assertFalse(route(r, MotionEvent.AXIS_X).invert);
        assertTrue(route(r, MotionEvent.AXIS_Y).invert);
        assertFalse(route(r, MotionEvent.AXIS_RX).invert);
        assertTrue(route(r, MotionEvent.AXIS_RZ).invert);
        assertFalse(route(AndroidInput.routes(new int[]{MotionEvent.AXIS_Y}), MotionEvent.AXIS_Y).invert);
    }

    @Test
    public void axisRoutingPrefersAndroidsNormalisedLayout() {
        List<AndroidInput.AxisRoute> r = AndroidInput.routes(new int[]{
                MotionEvent.AXIS_X, MotionEvent.AXIS_Y, MotionEvent.AXIS_Z, MotionEvent.AXIS_RZ,
                MotionEvent.AXIS_LTRIGGER, MotionEvent.AXIS_RTRIGGER, MotionEvent.AXIS_HAT_X,
                MotionEvent.AXIS_HAT_Y});
        assertEquals(8, r.size());
        assertEquals(Pad.RIGHT_X, route(r, MotionEvent.AXIS_Z).canonicalAxis);
        assertEquals(Pad.RIGHT_Y, route(r, MotionEvent.AXIS_RZ).canonicalAxis);
        assertTrue(route(r, MotionEvent.AXIS_LTRIGGER).trigger);
        assertTrue(route(r, MotionEvent.AXIS_HAT_X).hat);
        assertTrue(AndroidInput.hasAnalogTriggers(r));

        // Switch 2 GameCube standard report (X/Y + Rx/Rz), no triggers
        r = AndroidInput.routes(new int[]{MotionEvent.AXIS_X, MotionEvent.AXIS_Y,
                MotionEvent.AXIS_RX, MotionEvent.AXIS_RZ});
        assertEquals(Pad.RIGHT_X, route(r, MotionEvent.AXIS_RX).canonicalAxis);
        assertEquals(Pad.RIGHT_Y, route(r, MotionEvent.AXIS_RZ).canonicalAxis);
        assertFalse(AndroidInput.hasAnalogTriggers(r));

        r = AndroidInput.routes(new int[]{MotionEvent.AXIS_X, MotionEvent.AXIS_Y,
                MotionEvent.AXIS_RX, MotionEvent.AXIS_RY, MotionEvent.AXIS_BRAKE, MotionEvent.AXIS_GAS});
        assertEquals(Pad.RIGHT_Y, route(r, MotionEvent.AXIS_RY).canonicalAxis);
        assertEquals(Pad.LEFT_TRIGGER, route(r, MotionEvent.AXIS_BRAKE).canonicalAxis);
    }

    private static AndroidInput.AxisRoute route(List<AndroidInput.AxisRoute> routes, int axis) {
        for (AndroidInput.AxisRoute r : routes) {
            if (r.androidAxis == axis) {
                return r;
            }
        }
        throw new AssertionError("no route for axis " + axis);
    }

    @Test
    public void axisValuesScaleToInt16() {
        assertEquals(32767, AndroidInput.axisValue(1f, false));
        assertEquals(-32767, AndroidInput.axisValue(-1f, false));
        assertEquals(0, AndroidInput.axisValue(0f, false));
        assertEquals(16384, AndroidInput.axisValue(0.5f, true));
        assertEquals(0, AndroidInput.axisValue(-0.5f, true));
        assertEquals(32767, AndroidInput.axisValue(7f, false));
        assertEquals(0, AndroidInput.axisValue(Float.NaN, false));
    }
}
