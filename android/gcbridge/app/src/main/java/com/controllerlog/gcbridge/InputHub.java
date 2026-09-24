package com.controllerlog.gcbridge;

import java.io.File;
import java.io.IOException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;

/**
 * Process-wide input state: every source (Android input events from the screen or the
 * accessibility service, the USB reader, the Bluetooth reader) publishes into one hub, which
 * keeps a canonical {@link Pad.State} per controller, writes the recording and tells listeners
 * (overlay, main screen). Pure Java and thread-safe; listeners are called on the publishing
 * thread and must be quick (post to the main thread themselves).
 */
public final class InputHub {

    private static final InputHub INSTANCE = new InputHub();

    public static InputHub get() {
        return INSTANCE;
    }

    public interface Listener {
        /** A button or axis of {@code device} changed (its state is already updated). */
        void onInput(Device device);

        /** A controller connected or disconnected, or recording started / stopped. */
        void onStatus();
    }

    /** One controller as the recording sees it. */
    public static final class Device {
        public final int id;
        public final String key;
        public final String name;
        public final String backend;
        public final String family;
        public final int vendorId;
        public final int productId;
        public final String connection;
        public final Map<String, Object> extra;
        public final Pad.State state = new Pad.State();
        public volatile boolean connected = true;
        public volatile long lastInputNs;
        public volatile long inputEvents;

        Device(int id, String key, String name, String backend, String family, int vendorId,
               int productId, String connection, Map<String, Object> extra) {
            this.id = id;
            this.key = key;
            this.name = name;
            this.backend = backend;
            this.family = family;
            this.vendorId = vendorId;
            this.productId = productId;
            this.connection = connection;
            this.extra = extra;
        }

        /** DeviceInfo.to_json() of model.py (the value of a "+" row). */
        public Map<String, Object> toJson() {
            Map<String, Object> d = new LinkedHashMap<>();
            d.put("id", id);
            d.put("name", name);
            d.put("backend", backend);
            d.put("sdl_type", "");
            d.put("family", family);
            d.put("vendor_id", vendorId);
            d.put("product_id", productId);
            d.put("connection", connection);
            if (extra != null && !extra.isEmpty()) {
                d.put("extra", extra);
            }
            return d;
        }

        public String shortName() {
            return name == null || name.isEmpty() ? backend + " controller" : name;
        }
    }

    private final Map<String, Device> devices = new LinkedHashMap<>();
    private final List<Listener> listeners = new CopyOnWriteArrayList<>();
    private int nextId;
    private CtlogWriter writer;
    private File recordingFile;
    private long recordingStartNs;
    private long totalEvents;
    private long lastEventNs;
    private volatile Device active;
    private String lastError;

    private InputHub() {
    }

    public void addListener(Listener l) {
        listeners.add(l);
    }

    public void removeListener(Listener l) {
        listeners.remove(l);
    }

    public static long now() {
        return System.nanoTime();
    }

    // --- devices ---------------------------------------------------------------------------

    /**
     * Registers a controller (or re-activates one seen before, keeping its id) and returns it.
     * The recording gets a "+" row.
     */
    public Device connect(String key, String name, String backend, String family, int vendorId,
                          int productId, String connection, Map<String, Object> extra) {
        Device d;
        synchronized (this) {
            d = devices.get(key);
            if (d == null || !d.name.equals(name) || !d.family.equals(family)) {
                d = new Device(d != null ? d.id : nextId++, key, name, backend, family, vendorId,
                        productId, connection, extra);
                devices.put(key, d);
            }
            d.connected = true;
            d.state.clear();
            if (writer != null) {
                try {
                    writer.device(now() - recordingStartNs, d.id, d.toJson());
                } catch (IOException e) {
                    lastError = e.toString();
                }
            }
        }
        notifyStatus();
        return d;
    }

    public synchronized Device find(String key) {
        return devices.get(key);
    }

    public void disconnect(String key) {
        Device d;
        synchronized (this) {
            d = devices.get(key);
            if (d == null || !d.connected) {
                return;
            }
            d.connected = false;
            releaseAll(d, now());
            if (writer != null) {
                try {
                    writer.disconnect(now() - recordingStartNs, d.id);
                } catch (IOException e) {
                    lastError = e.toString();
                }
            }
            if (active == d) {
                active = null;
            }
        }
        notifyStatus();
    }

    /** All buttons up and axes centred (a controller that went away mid-press). */
    private void releaseAll(Device d, long tNs) {
        for (int i = 0; i < Pad.NUM_BUTTONS; i++) {
            if (d.state.buttons[i] != 0) {
                d.state.buttons[i] = 0;
                record(d, tNs, true, i, 0);
            }
        }
        for (int i = 0; i < Pad.NUM_AXES; i++) {
            if (d.state.axes[i] != 0) {
                d.state.axes[i] = 0;
                record(d, tNs, false, i, 0);
            }
        }
    }

    public synchronized List<Device> devices() {
        return new ArrayList<>(devices.values());
    }

    public synchronized List<Device> connectedDevices() {
        List<Device> out = new ArrayList<>();
        for (Device d : devices.values()) {
            if (d.connected) {
                out.add(d);
            }
        }
        return out;
    }

    /** The controller that produced input most recently (what the overlay shows). */
    public Device activeDevice() {
        return active;
    }

    // --- input -----------------------------------------------------------------------------

    public void button(Device d, int index, boolean down, long tNs) {
        boolean changed;
        synchronized (this) {
            changed = d.connected && d.state.setButton(index, down);
            if (changed) {
                record(d, tNs, true, index, down ? 1 : 0);
                touched(d, tNs);
            }
        }
        if (changed) {
            notifyInput(d);
        }
    }

    public void axis(Device d, int index, int value, long tNs) {
        boolean changed;
        synchronized (this) {
            changed = d.connected && d.state.setAxis(index, value);
            if (changed) {
                record(d, tNs, false, index, d.state.axes[index]);
                touched(d, tNs);
            }
        }
        if (changed) {
            notifyInput(d);
        }
    }

    /** Applies a complete state (USB / Bluetooth reports), recording only what changed. */
    public void state(Device d, int[] buttons, int[] axes, long tNs) {
        boolean changed = false;
        synchronized (this) {
            if (!d.connected) {
                return;
            }
            for (int i = 0; i < Pad.NUM_BUTTONS && i < buttons.length; i++) {
                if (d.state.setButton(i, buttons[i] != 0)) {
                    record(d, tNs, true, i, buttons[i] != 0 ? 1 : 0);
                    changed = true;
                }
            }
            for (int i = 0; i < Pad.NUM_AXES && i < axes.length; i++) {
                if (d.state.setAxis(i, axes[i])) {
                    record(d, tNs, false, i, d.state.axes[i]);
                    changed = true;
                }
            }
            if (changed) {
                touched(d, tNs);
            }
        }
        if (changed) {
            notifyInput(d);
        }
    }

    private void touched(Device d, long tNs) {
        d.lastInputNs = tNs;
        d.inputEvents++;
        totalEvents++;
        lastEventNs = tNs;
        active = d;
    }

    private void record(Device d, long tNs, boolean button, int code, int value) {
        if (writer == null) {
            return;
        }
        try {
            if (button) {
                writer.button(tNs - recordingStartNs, d.id, code, value != 0);
            } else {
                writer.axis(tNs - recordingStartNs, d.id, code, value);
            }
        } catch (IOException e) {
            lastError = e.toString();
        }
    }

    public void marker(String label) {
        synchronized (this) {
            if (writer != null) {
                try {
                    writer.marker(now() - recordingStartNs, label);
                } catch (IOException e) {
                    lastError = e.toString();
                }
            }
        }
        notifyStatus();
    }

    // --- recording -------------------------------------------------------------------------

    public void startRecording(File file, Map<String, Object> headerExtra, Map<String, Object> meta)
            throws IOException {
        synchronized (this) {
            if (writer != null) {
                stopRecordingLocked();
            }
            CtlogWriter w = new CtlogWriter(file, headerExtra, meta);
            long start = now();
            for (Device d : devices.values()) {
                if (d.connected) {
                    w.device(0, d.id, d.toJson());
                }
            }
            writer = w;
            recordingFile = file;
            recordingStartNs = start;
            lastError = null;
        }
        notifyStatus();
    }

    public File stopRecording() {
        File f;
        synchronized (this) {
            f = stopRecordingLocked();
        }
        notifyStatus();
        return f;
    }

    private File stopRecordingLocked() {
        File f = recordingFile;
        if (writer != null) {
            try {
                writer.close();
            } catch (IOException e) {
                lastError = e.toString();
            }
        }
        writer = null;
        recordingFile = null;
        return f;
    }

    /** Periodic housekeeping: flush the file after a quiet spell. */
    public synchronized void tick() {
        if (writer != null) {
            try {
                writer.flushIfDue();
            } catch (IOException e) {
                lastError = e.toString();
            }
        }
    }

    public synchronized boolean isRecording() {
        return writer != null;
    }

    public synchronized File recordingFile() {
        return recordingFile;
    }

    public synchronized long recordingDurationNs() {
        return writer == null ? 0 : now() - recordingStartNs;
    }

    public synchronized int recordingRows() {
        return writer == null ? 0 : writer.count();
    }

    public synchronized long totalEvents() {
        return totalEvents;
    }

    public synchronized long lastEventNs() {
        return lastEventNs;
    }

    public synchronized String lastError() {
        return lastError;
    }

    // --- listeners -------------------------------------------------------------------------

    private void notifyInput(Device d) {
        for (Listener l : listeners) {
            l.onInput(d);
        }
    }

    private void notifyStatus() {
        for (Listener l : listeners) {
            l.onStatus();
        }
    }
}
