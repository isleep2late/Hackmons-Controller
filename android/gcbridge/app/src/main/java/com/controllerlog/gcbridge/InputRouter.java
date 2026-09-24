package com.controllerlog.gcbridge;

import android.view.InputDevice;
import android.view.InputEvent;
import android.view.KeyEvent;
import android.view.MotionEvent;

import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Turns Android input events (from the main screen while it is in front, or from the
 * accessibility service system-wide) into hub updates. Main thread only.
 */
final class InputRouter {

    private InputRouter() {
    }

    private static final class Entry {
        InputHub.Device device;
        List<AndroidInput.AxisRoute> routes;
        boolean analogTriggers;
        int vendorId;
        int hatX;
        int hatY;
    }

    private static final Map<Integer, Entry> ENTRIES = new HashMap<>();

    static boolean isGameDevice(InputDevice d) {
        if (d == null) {
            return false;
        }
        int s = d.getSources();
        return (s & InputDevice.SOURCE_GAMEPAD) == InputDevice.SOURCE_GAMEPAD
                || (s & InputDevice.SOURCE_JOYSTICK) == InputDevice.SOURCE_JOYSTICK
                || d.getVendorId() == AndroidInput.VENDOR_NINTENDO;
    }

    private static Entry entry(int deviceId, InputDevice d) {
        Entry e = ENTRIES.get(deviceId);
        if (e != null) {
            return e;
        }
        e = new Entry();
        if (d == null) {
            d = InputDevice.getDevice(deviceId);
        }
        String name = d != null ? d.getName() : "input device #" + deviceId;
        int vid = d != null ? d.getVendorId() : 0;
        int pid = d != null ? d.getProductId() : 0;
        String key = "android:" + (d != null && d.getDescriptor() != null && !d.getDescriptor().isEmpty()
                ? d.getDescriptor() : "id" + deviceId);
        int[] axes = new int[0];
        if (d != null) {
            List<InputDevice.MotionRange> ranges = d.getMotionRanges();
            axes = new int[ranges.size()];
            for (int i = 0; i < axes.length; i++) {
                axes[i] = ranges.get(i).getAxis();
            }
        }
        e.routes = AndroidInput.routes(axes);
        e.analogTriggers = AndroidInput.hasAnalogTriggers(e.routes);
        e.vendorId = vid;
        Map<String, Object> extra = new LinkedHashMap<>();
        extra.put("transport", "android");
        extra.put("android_id", deviceId);
        if (d != null) {
            extra.put("descriptor", d.getDescriptor());
            extra.put("sources", String.format(java.util.Locale.ROOT, "0x%08x", d.getSources()));
        }
        e.device = InputHub.get().connect(key, name, "android", AndroidInput.family(vid, pid, name),
                vid, pid, "unknown", extra);
        ENTRIES.put(deviceId, e);
        return e;
    }

    static void deviceRemoved(int deviceId) {
        Entry e = ENTRIES.remove(deviceId);
        if (e != null) {
            InputHub.get().disconnect(e.device.key);
        }
    }

    static void deviceChanged(int deviceId) {
        ENTRIES.remove(deviceId);   // re-read its axes on the next event
    }

    static long eventNs(InputEvent e) {
        return e.getEventTime() * 1_000_000L;
    }

    /** @return true if the key belonged to a controller and was passed to the hub */
    static boolean onKey(KeyEvent e) {
        InputDevice d = e.getDevice();
        if (!AndroidInput.isGameSource(e.getSource()) && !isGameDevice(d)) {
            return false;
        }
        int action = e.getAction();
        if (action != KeyEvent.ACTION_DOWN && action != KeyEvent.ACTION_UP) {
            return false;
        }
        if (action == KeyEvent.ACTION_DOWN && e.getRepeatCount() > 0) {
            return true;
        }
        Entry en = entry(e.getDeviceId(), d);
        int idx = AndroidInput.buttonForKey(e.getKeyCode(), e.getScanCode(), en.vendorId, en.analogTriggers);
        if (idx < 0) {
            return false;
        }
        boolean down = action == KeyEvent.ACTION_DOWN;
        long t = eventNs(e);
        if (idx == AndroidInput.KEY_LEFT_TRIGGER) {
            InputHub.get().axis(en.device, Pad.LEFT_TRIGGER, down ? Pad.AXIS_MAX : 0, t);
        } else if (idx == AndroidInput.KEY_RIGHT_TRIGGER) {
            InputHub.get().axis(en.device, Pad.RIGHT_TRIGGER, down ? Pad.AXIS_MAX : 0, t);
        } else {
            InputHub.get().button(en.device, idx, down, t);
        }
        return true;
    }

    /** @return true if the motion event came from a controller and was passed to the hub */
    static boolean onMotion(MotionEvent e) {
        InputDevice d = e.getDevice();
        boolean joystick = e.isFromSource(InputDevice.SOURCE_CLASS_JOYSTICK)
                || (isGameDevice(d) && !e.isFromSource(InputDevice.SOURCE_CLASS_POINTER));
        if (!joystick) {
            return false;
        }
        if (e.getActionMasked() != MotionEvent.ACTION_MOVE) {
            return true;
        }
        Entry en = entry(e.getDeviceId(), d);
        InputHub hub = InputHub.get();
        long t = eventNs(e);
        for (AndroidInput.AxisRoute r : en.routes) {
            float v = e.getAxisValue(r.androidAxis);
            if (r.hat) {
                int dir = v < -0.5f ? -1 : v > 0.5f ? 1 : 0;
                if (r.androidAxis == MotionEvent.AXIS_HAT_X) {
                    if (dir != en.hatX) {
                        en.hatX = dir;
                        hub.button(en.device, Pad.DPAD_LEFT, dir < 0, t);
                        hub.button(en.device, Pad.DPAD_RIGHT, dir > 0, t);
                    }
                } else if (dir != en.hatY) {
                    en.hatY = dir;
                    hub.button(en.device, Pad.DPAD_UP, dir < 0, t);
                    hub.button(en.device, Pad.DPAD_DOWN, dir > 0, t);
                }
            } else {
                hub.axis(en.device, r.canonicalAxis, AndroidInput.axisValue(v, r.trigger), t);
            }
        }
        return true;
    }
}
