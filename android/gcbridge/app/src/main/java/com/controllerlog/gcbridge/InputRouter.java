package com.controllerlog.gcbridge;

import android.content.Context;
import android.hardware.input.InputManager;
import android.os.Handler;
import android.os.Looper;
import android.view.InputDevice;
import android.view.InputEvent;
import android.view.KeyEvent;
import android.view.MotionEvent;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;

/**
 * Turns Android input events (from the main screen while it is in front, or from the
 * accessibility service system-wide) into hub updates. Main thread only.
 */
final class InputRouter {

    private InputRouter() {
    }

    static final int MAX_KEY_LOG = 60;

    static final class Entry {
        final int deviceId;
        InputHub.Device device;
        PadIdentity identity;
        PadClass cls;
        String fingerprint;
        List<AndroidInput.AxisRoute> routes;
        boolean analogTriggers;
        int hatX;
        int hatY;
        float[] lastMotion = new float[0];
        boolean[] digitalDown = new boolean[0];
        final Set<Integer> learned = new TreeSet<>();
        final List<String> demotions = new ArrayList<>();

        Entry(int deviceId) {
            this.deviceId = deviceId;
        }
    }

    private static final Map<Integer, Entry> ENTRIES = new HashMap<>();
    private static final ArrayDeque<String> KEY_LOG = new ArrayDeque<>();
    private static PadSettings settings = PadSettings.EMPTY;
    private static boolean settingsLoaded;

    private static final Set<Object> HOLDERS = new HashSet<>();
    private static InputManager tracker;
    private static final InputManager.InputDeviceListener TRACKER = new InputManager.InputDeviceListener() {
        @Override
        public void onInputDeviceAdded(int deviceId) {
        }

        @Override
        public void onInputDeviceRemoved(int deviceId) {
            deviceRemoved(deviceId);
        }

        @Override
        public void onInputDeviceChanged(int deviceId) {
            deviceChanged(deviceId);
        }
    };

    static void hold(Context ctx, Object holder) {
        ensureSettings(ctx);
        if (HOLDERS.add(holder) && HOLDERS.size() == 1) {
            tracker = ctx.getApplicationContext().getSystemService(InputManager.class);
            if (tracker != null) {
                tracker.registerInputDeviceListener(TRACKER, new Handler(Looper.getMainLooper()));
            }
            reconcile();
        }
    }

    static void release(Object holder) {
        if (HOLDERS.remove(holder) && HOLDERS.isEmpty() && tracker != null) {
            tracker.unregisterInputDeviceListener(TRACKER);
            tracker = null;
        }
    }

    static boolean tracking() {
        return !HOLDERS.isEmpty();
    }

    static void ensureSettings(Context ctx) {
        if (!settingsLoaded) {
            settings = PadSettings.load(ctx);
            settingsLoaded = true;
        }
    }

    static void reloadSettings(Context ctx) {
        setSettings(PadSettings.load(ctx));
        settingsLoaded = true;
    }

    static void setSettings(PadSettings s) {
        settings = s == null ? PadSettings.EMPTY : s;
        for (Entry e : ENTRIES.values()) {
            if (e.device != null) {
                InputHub.get().disconnect(e.device.key);
            }
        }
        ENTRIES.clear();
    }

    static PadSettings settings() {
        return settings;
    }

    static boolean isGameDevice(InputDevice d) {
        if (d == null) {
            return false;
        }
        int s = d.getSources();
        return (s & InputDevice.SOURCE_GAMEPAD) == InputDevice.SOURCE_GAMEPAD
                || (s & InputDevice.SOURCE_JOYSTICK) == InputDevice.SOURCE_JOYSTICK
                || d.getVendorId() == AndroidInput.VENDOR_NINTENDO;
    }

    static PadIdentity identityOf(InputDevice d, int deviceId) {
        if (d == null) {
            return new PadIdentity("input device #" + deviceId, 0, 0, "", 0, false, 0, null, null, null);
        }
        List<PadIdentity.Range> ranges = new ArrayList<>();
        for (InputDevice.MotionRange r : d.getMotionRanges()) {
            ranges.add(new PadIdentity.Range(r.getAxis(), r.getMin(), r.getMax(), r.getFlat(),
                    (r.getSource() & InputDevice.SOURCE_CLASS_JOYSTICK) != 0));
        }
        Set<Integer> keys = new LinkedHashSet<>();
        if (isGameDevice(d)) {
            boolean[] has = d.hasKeys(PadIdentity.PROBE_KEYS);
            for (int i = 0; i < has.length && i < PadIdentity.PROBE_KEYS.length; i++) {
                if (has[i]) {
                    keys.add(PadIdentity.PROBE_KEYS[i]);
                }
            }
        }
        return new PadIdentity(d.getName(), d.getVendorId(), d.getProductId(), d.getDescriptor(),
                d.getSources(), d.isExternal(), d.getControllerNumber(), ranges, keys, null);
    }

    static PadClass classOf(PadIdentity id) {
        return PadClassifier.classify(id, settings.profiles, settings.ignore);
    }

    private static String fingerprint(InputDevice d) {
        if (d == null) {
            return "";
        }
        StringBuilder sb = new StringBuilder(PadIdentity.deviceKey(d.getVendorId(), d.getProductId(), d.getName()));
        sb.append('|').append(d.getSources());
        for (InputDevice.MotionRange r : d.getMotionRanges()) {
            sb.append('|').append(r.getAxis()).append(':').append(r.getSource());
        }
        return sb.toString();
    }

    private static Entry entry(int deviceId, InputDevice d) {
        Entry e = ENTRIES.get(deviceId);
        if (e != null) {
            return e;
        }
        e = new Entry(deviceId);
        if (d == null) {
            d = InputDevice.getDevice(deviceId);
        }
        e.identity = identityOf(d, deviceId);
        e.fingerprint = fingerprint(d);
        e.cls = classOf(e.identity);
        applyRoutes(e);
        if (!e.cls.ignored) {
            connect(e, d);
        }
        ENTRIES.put(deviceId, e);
        return e;
    }

    private static void applyRoutes(Entry e) {
        e.routes = AndroidInput.routes(e.cls, e.identity);
        e.analogTriggers = AndroidInput.hasAnalogTriggers(e.routes);
        e.lastMotion = new float[e.routes.size()];
        e.digitalDown = new boolean[e.routes.size()];
    }

    private static void connect(Entry e, InputDevice d) {
        String key = "android:" + (!e.identity.descriptor.isEmpty() ? e.identity.descriptor : "id" + e.deviceId);
        Map<String, Object> extra = new LinkedHashMap<>();
        extra.put("transport", "android");
        extra.put("android_id", e.deviceId);
        if (d != null) {
            extra.put("descriptor", d.getDescriptor());
            extra.put("sources", String.format(Locale.ROOT, "0x%08x", d.getSources()));
        }
        fillClassExtra(extra, e);
        String drawAs = settings.drawAs.get(e.identity.deviceKey());
        String family = drawAs != null ? drawAs : e.cls.family != null ? e.cls.family : Pad.FAMILY_GENERIC;
        e.device = InputHub.get().connect(key, e.identity.name, "android", family,
                e.identity.vendorId, e.identity.productId, "unknown", extra);
    }

    private static void fillClassExtra(Map<String, Object> extra, Entry e) {
        extra.put("rule", e.cls.rule);
        if (e.cls.model != null) {
            extra.put("model", e.cls.model);
        } else {
            extra.remove("model");
        }
        extra.put("device_key", e.identity.deviceKey());
        if (!e.cls.unavailable.isEmpty()) {
            extra.put("unavailable", new ArrayList<>(e.cls.unavailable));
        } else {
            extra.remove("unavailable");
        }
    }

    static void deviceRemoved(int deviceId) {
        Entry e = ENTRIES.remove(deviceId);
        if (e != null && e.device != null) {
            InputHub.get().disconnect(e.device.key);
        }
    }

    static void deviceChanged(int deviceId) {
        Entry e = ENTRIES.get(deviceId);
        if (e == null) {
            return;
        }
        InputDevice d = InputDevice.getDevice(deviceId);
        if (d == null) {
            deviceRemoved(deviceId);
        } else if (!fingerprint(d).equals(e.fingerprint)) {
            ENTRIES.remove(deviceId);
        }
    }

    private static void reconcile() {
        for (Integer id : new ArrayList<>(ENTRIES.keySet())) {
            deviceChanged(id);
        }
    }

    static long eventNs(InputEvent e) {
        return e.getEventTime() * 1_000_000L;
    }

    private static void demote(Entry e, String why) {
        PadClass before = e.cls;
        e.cls = PadClassifier.demote(before, why);
        e.demotions.add(why);
        applyRoutes(e);
        if (e.device != null) {
            fillClassExtra(e.device.extra, e);
            InputHub.get().connect(e.device.key, e.device.name, e.device.backend, e.device.family,
                    e.device.vendorId, e.device.productId, e.device.connection, e.device.extra);
        }
        MainActivity.log("Controller \"" + e.identity.name + "\" " + e.cls.reason);
    }

    private static void learn(Entry e, int scanCode, int keyCode) {
        if (scanCode != 0) {
            e.learned.add(scanCode);
        }
        String why = PadClassifier.demotionReason(e.cls, scanCode, keyCode);
        if (why != null) {
            demote(e, why);
        }
    }

    private static void press(Entry en, int idx, boolean down, long t) {
        if (idx == AndroidInput.KEY_LEFT_TRIGGER) {
            InputHub.get().axis(en.device, Pad.LEFT_TRIGGER, down ? Pad.AXIS_MAX : 0, t);
        } else if (idx == AndroidInput.KEY_RIGHT_TRIGGER) {
            InputHub.get().axis(en.device, Pad.RIGHT_TRIGGER, down ? Pad.AXIS_MAX : 0, t);
        } else {
            InputHub.get().button(en.device, idx, down, t);
        }
    }

    private static void logKey(KeyEvent e, String outcome) {
        String line = String.format(Locale.ROOT, "%d dev=#%d %s %s key=%d scan=0x%03x src=0x%08x -> %s",
                e.getEventTime(), e.getDeviceId(), e.getAction() == KeyEvent.ACTION_DOWN ? "down" : "up",
                PadIdentity.keyName(e.getKeyCode()), e.getKeyCode(), e.getScanCode(), e.getSource(), outcome);
        KEY_LOG.addLast(line);
        while (KEY_LOG.size() > MAX_KEY_LOG) {
            KEY_LOG.removeFirst();
        }
    }

    static String canonicalName(int idx) {
        if (idx == AndroidInput.KEY_LEFT_TRIGGER) {
            return "left_trigger";
        }
        if (idx == AndroidInput.KEY_RIGHT_TRIGGER) {
            return "right_trigger";
        }
        return idx >= 0 && idx < Pad.NUM_BUTTONS ? Pad.BUTTONS[idx] : "-";
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
        if (AndroidInput.isSystemVolumeKey(e.getKeyCode())) {
            return false;
        }
        if (action == KeyEvent.ACTION_DOWN && e.getRepeatCount() > 0) {
            return true;
        }
        Entry en = entry(e.getDeviceId(), d);
        if (en.cls.ignored) {
            logKey(e, "ignored device");
            return false;
        }
        learn(en, e.getScanCode(), e.getKeyCode());
        int idx = AndroidInput.buttonForKey(en.cls, e.getKeyCode(), e.getScanCode(), en.analogTriggers);
        logKey(e, idx < 0 ? "unmapped (" + en.cls.rule + ")" : canonicalName(idx) + " (" + en.cls.rule + ")");
        if (idx < 0) {
            return false;
        }
        press(en, idx, action == KeyEvent.ACTION_DOWN, eventNs(e));
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
        if (en.cls.ignored) {
            return false;
        }
        if (en.cls.rawReport && en.identity.hasHat()) {
            String why = PadClassifier.hatDemotionReason(en.cls, e.getAxisValue(MotionEvent.AXIS_HAT_X),
                    e.getAxisValue(MotionEvent.AXIS_HAT_Y));
            if (why != null) {
                demote(en, why);
            }
        }
        InputHub hub = InputHub.get();
        long t = eventNs(e);
        for (int i = 0; i < en.routes.size(); i++) {
            AndroidInput.AxisRoute r = en.routes.get(i);
            float v = e.getAxisValue(r.androidAxis);
            en.lastMotion[i] = v;
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
            } else if (r.isDigital()) {
                float sv = r.digitalPositive ? v : -v;
                boolean was = en.digitalDown[i];
                boolean now = was ? sv >= AndroidInput.DIGITAL_RELEASE : sv >= AndroidInput.DIGITAL_PRESS;
                if (now != was) {
                    en.digitalDown[i] = now;
                    press(en, r.digitalButton, now, t);
                }
            } else {
                hub.axis(en.device, r.canonicalAxis, AndroidInput.axisValue(r.apply(v), r.trigger), t);
            }
        }
        return true;
    }

    static PadClass classOfHubDevice(InputHub.Device device) {
        if (device == null) {
            return null;
        }
        for (Entry e : ENTRIES.values()) {
            if (e.device == device) {
                return e.cls;
            }
        }
        return null;
    }

    static List<Entry> entries() {
        List<Entry> out = new ArrayList<>(ENTRIES.values());
        out.sort((a, b) -> Integer.compare(a.deviceId, b.deviceId));
        return out;
    }

    static Entry entryFor(int deviceId) {
        return ENTRIES.get(deviceId);
    }

    static List<String> keyLog() {
        return new ArrayList<>(KEY_LOG);
    }

    static String describeRoutes(List<AndroidInput.AxisRoute> routes) {
        StringBuilder sb = new StringBuilder();
        for (AndroidInput.AxisRoute r : routes) {
            if (sb.length() > 0) {
                sb.append(", ");
            }
            sb.append(PadIdentity.axisName(r.androidAxis)).append("->");
            if (r.hat) {
                sb.append("dpad");
            } else if (r.isDigital()) {
                sb.append(canonicalName(r.digitalButton)).append(r.digitalPositive ? "(+)" : "(-)");
            } else {
                sb.append(Pad.AXES[r.canonicalAxis]);
            }
            if (r.invert) {
                sb.append(" flip");
            }
            if (r.scaleNeg != 1f || r.scalePos != 1f) {
                sb.append(r.scaleNeg == r.scalePos
                        ? String.format(Locale.ROOT, " /%.3f", r.scalePos)
                        : String.format(Locale.ROOT, " /%.3f|%.3f", r.scaleNeg, r.scalePos));
            }
            if (r.centred) {
                sb.append(" (v+1)/2");
            }
        }
        return sb.length() == 0 ? "none" : sb.toString();
    }

    static String lastMotion(Entry e) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < e.routes.size() && i < e.lastMotion.length; i++) {
            sb.append(String.format(Locale.ROOT, "%s=%+.3f ", PadIdentity.axisName(e.routes.get(i).androidAxis),
                    e.lastMotion[i]));
        }
        return sb.toString().trim();
    }

    static String describeLearned(Entry e) {
        StringBuilder sb = new StringBuilder();
        for (int s : e.learned) {
            sb.append(String.format(Locale.ROOT, "0x%03x ", s));
        }
        return sb.length() == 0 ? "none" : sb.toString().trim();
    }

    static int[] ids() {
        int[] out = new int[ENTRIES.size()];
        int i = 0;
        for (Integer k : ENTRIES.keySet()) {
            out[i++] = k;
        }
        Arrays.sort(out);
        return out;
    }
}
