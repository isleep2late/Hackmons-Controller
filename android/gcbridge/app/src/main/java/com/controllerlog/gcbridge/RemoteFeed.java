package com.controllerlog.gcbridge;

import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.ServiceConnection;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.os.DeadObjectException;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.os.Message;
import android.os.Messenger;
import android.os.RemoteException;
import android.os.SystemClock;
import android.util.Log;

import java.util.Arrays;
import java.util.HashSet;
import java.util.Set;

/**
 * Sends the active controller's state to is2l Remote (a separate app, package
 * {@link #REMOTE_PACKAGE}), which carries it to the PC as a virtual controller. GC Bridge binds
 * to is2l Remote's exported service and posts {@link Message}s through its {@link Messenger}:
 * {@link #MSG_PAD_STATE} with the whole state on every change (at most one per 4 ms, the
 * latest state always wins) and again every 500 ms, {@link #MSG_PAD_GONE} when the feed stops.
 *
 * <p>Process-wide like {@link InputHub}. It runs while the setting is on and somebody holds it:
 * the screen while visible, {@link CaptureService} and {@link KeyCaptureService} while they
 * run, so it lasts in the background exactly as long as a source that works there.
 */
final class RemoteFeed implements InputHub.Listener {

    static final String REMOTE_PACKAGE = "com.isleep2late.remote";
    static final String ACTION_PAD_FEED = "com.isleep2late.remote.PAD_FEED";
    static final String RELEASES_URL = "https://github.com/isleep2late/is2l-remote/releases";
    static final String PREF_ENABLED = "remote_feed";

    static final int MSG_PAD_STATE = 1;
    static final int MSG_PAD_GONE = 2;
    static final int PROTOCOL_VERSION = 1;
    static final String KEY_VERSION = "v";
    static final String KEY_BUTTONS = "buttons";
    static final String KEY_AXES = "axes";
    static final String KEY_NAME = "name";

    static final long MIN_INTERVAL_NS = 4_000_000L;
    static final long REPEAT_MS = 500;
    static final long REBIND_MS = 3000;
    static final int TRIGGER_MAX = 32767;

    static final int LINK_IDLE = 0;
    static final int LINK_NOT_INSTALLED = 1;
    static final int LINK_NO_FEED = 2;
    static final int LINK_CONNECTING = 3;
    static final int LINK_CONNECTED = 4;

    // --- pure logic (no Android classes; RemoteFeedTest) ---------------------------------------

    /** Bit i is set when button i of {@link Pad#BUTTONS} is down. */
    static int packButtons(int[] buttons) {
        int mask = 0;
        for (int i = 0; i < buttons.length && i < Integer.SIZE; i++) {
            if (buttons[i] != 0) {
                mask |= 1 << i;
            }
        }
        return mask;
    }

    /** left_x, left_y, right_x, right_y in -32768..32767 (y down), both triggers in 0..32767. */
    static int[] packAxes(int[] axes) {
        int[] out = new int[Pad.NUM_AXES];
        for (int i = 0; i < Pad.NUM_AXES && i < axes.length; i++) {
            boolean trigger = i == Pad.LEFT_TRIGGER || i == Pad.RIGHT_TRIGGER;
            out[i] = trigger ? Math.max(0, Math.min(TRIGGER_MAX, axes[i])) : Pad.clampAxis(axes[i]);
        }
        return out;
    }

    /** How long a send must still wait when the last one was {@code sinceLastNs} ago. */
    static long delayMs(long sinceLastNs) {
        if (sinceLastNs >= MIN_INTERVAL_NS) {
            return 0;
        }
        long left = MIN_INTERVAL_NS - Math.max(0, sinceLastNs);
        return (left + 999_999L) / 1_000_000L;
    }

    /**
     * The coalescing decision: any number of changes between two sends ask for one flush, and
     * the flush reads the state when it runs, so the newest state is the one that goes out.
     */
    static final class Throttle {
        private boolean scheduled;
        private boolean sent;
        private long lastNs;

        /** @return -1 when a flush is already on its way, else the delay to schedule one with */
        synchronized long request(long nowNs) {
            if (scheduled) {
                return -1;
            }
            scheduled = true;
            return sent ? delayMs(nowNs - lastNs) : 0;
        }

        synchronized void flushed(long nowNs) {
            scheduled = false;
            sent = true;
            lastNs = nowNs;
        }

        synchronized void reset() {
            scheduled = false;
            sent = false;
        }
    }

    static final int SEND_ALL = 0;
    static final int SEND_NOTHING = 2;

    /**
     * Whether a controller is being read in full right now. Android's own controllers (backend
     * "android") deliver sticks and triggers only to the focused GC Bridge screen. Without it
     * the feed must let go instead of repeating a frozen state: Button capture would still have
     * the buttons, but a feed without sticks would take over from is2l Remote, which reads the
     * same controller completely by itself. The USB and Bluetooth readers always have everything.
     */
    static int coverage(String backend, boolean screenFocused) {
        return !"android".equals(backend) || screenFocused ? SEND_ALL : SEND_NOTHING;
    }

    static String statusText(boolean enabled, int link, long statesSent) {
        if (!enabled) {
            return "off";
        }
        switch (link) {
            case LINK_NOT_INSTALLED:
                return "is2l Remote not installed. Get it from " + RELEASES_URL;
            case LINK_NO_FEED:
                return "is2l Remote is installed but has no controller feed (update is2l Remote)";
            case LINK_CONNECTED:
                return "connected, " + statesSent + " states sent";
            case LINK_CONNECTING:
                return "connecting to is2l Remote";
            default:
                return "on, waiting";
        }
    }

    // --- singleton -----------------------------------------------------------------------------

    private static RemoteFeed instance;

    static synchronized RemoteFeed get(Context ctx) {
        if (instance == null) {
            instance = new RemoteFeed(ctx.getApplicationContext());
        }
        return instance;
    }

    private static synchronized RemoteFeed peek() {
        return instance;
    }

    /** Keeps the feed alive for {@code holder} (a visible screen or a running service). */
    static void hold(Context ctx, Object holder) {
        get(ctx).setHeld(holder, true);
    }

    static void release(Object holder) {
        RemoteFeed f = peek();
        if (f != null) {
            f.setHeld(holder, false);
        }
    }

    /** The GC Bridge screen gained or lost the input focus (see {@link #coverage}). */
    static void screenFocus(boolean focused) {
        RemoteFeed f = peek();
        if (f != null && f.screenFocused != focused) {
            f.screenFocused = focused;
            f.worker.post(f.updater);
        }
    }

    static boolean connected() {
        RemoteFeed f = peek();
        return f != null && f.enabled && f.link == LINK_CONNECTED;
    }

    private final Context app;
    private final SharedPreferences prefs;
    private final InputHub hub = InputHub.get();
    private final Handler worker;
    private final Set<Object> holders = new HashSet<>();
    private final Throttle throttle = new Throttle();
    private final Pad.State snapshot = new Pad.State();

    private volatile boolean enabled;
    private volatile boolean held;
    private volatile boolean screenFocused;
    private volatile boolean running;
    private volatile int link = LINK_IDLE;
    private volatile long statesSent;

    // Worker thread only.
    private boolean listening;
    private boolean bound;
    private Messenger messenger;
    private long lastBindMs;
    private boolean repeatDue;
    private boolean haveLast;
    private int lastButtons;
    private int[] lastAxes = new int[Pad.NUM_AXES];
    private String lastName = "";

    private final Runnable updater = this::update;
    private final Runnable flusher = this::flush;
    private final Runnable ticker = this::tick;

    private final ServiceConnection connection = new ServiceConnection() {
        @Override
        public void onServiceConnected(ComponentName name, IBinder service) {
            worker.post(() -> {
                if (!bound) {
                    return;
                }
                messenger = new Messenger(service);
                link = LINK_CONNECTED;
                haveLast = false;
                repeatDue = true;
                requestFlush();
                MainActivity.log("is2l Remote: connected");
            });
        }

        @Override
        public void onServiceDisconnected(ComponentName name) {
            // is2l Remote's process went away; Android reconnects this binding by itself
            worker.post(() -> {
                messenger = null;
                haveLast = false;
                if (bound) {
                    link = LINK_CONNECTING;
                    MainActivity.log("is2l Remote: connection lost, waiting for it to come back");
                }
            });
        }

        @Override
        public void onBindingDied(ComponentName name) {
            worker.post(() -> {
                if (bound) {
                    unbind();
                    bind();
                }
            });
        }

        @Override
        public void onNullBinding(ComponentName name) {
            worker.post(() -> {
                if (bound) {
                    unbind();
                    link = LINK_NO_FEED;
                }
            });
        }
    };

    private RemoteFeed(Context app) {
        this.app = app;
        prefs = app.getSharedPreferences(PadSettings.PREFS, Context.MODE_PRIVATE);
        enabled = prefs.getBoolean(PREF_ENABLED, false);
        HandlerThread thread = new HandlerThread("gcbridge-remote");
        thread.start();
        worker = new Handler(thread.getLooper());
    }

    boolean isEnabled() {
        return enabled;
    }

    void setEnabled(boolean on) {
        prefs.edit().putBoolean(PREF_ENABLED, on).apply();
        enabled = on;
        worker.post(updater);
    }

    String status() {
        return statusText(enabled, link, statesSent);
    }

    private void setHeld(Object holder, boolean on) {
        synchronized (holders) {
            if (on) {
                holders.add(holder);
            } else {
                holders.remove(holder);
            }
            held = !holders.isEmpty();
        }
        worker.post(updater);
    }

    // --- hub listener (any thread) -------------------------------------------------------------

    @Override
    public void onInput(InputHub.Device device) {
        if (running && device == hub.activeDevice()) {
            requestFlush();
        }
    }

    @Override
    public void onStatus() {
        if (running) {
            requestFlush();
        }
    }

    private void requestFlush() {
        long delay = throttle.request(System.nanoTime());
        if (delay >= 0) {
            worker.postDelayed(flusher, delay);
        }
    }

    // --- worker thread -------------------------------------------------------------------------

    private void update() {
        boolean want = enabled && held;
        if (want == running) {
            if (running) {
                requestFlush();   // a holder or the focus changed: what may be sent can differ
            }
            return;
        }
        running = want;
        if (want) {
            if (!listening) {
                hub.addListener(this);
                listening = true;
            }
            bind();
            worker.postDelayed(ticker, REPEAT_MS);
        } else {
            if (listening) {
                hub.removeListener(this);
                listening = false;
            }
            worker.removeCallbacks(ticker);
            worker.removeCallbacks(flusher);
            throttle.reset();
            sendGone();
            unbind();
            link = LINK_IDLE;
        }
    }

    private void tick() {
        if (!running) {
            return;
        }
        if (!bound) {
            if (SystemClock.uptimeMillis() - lastBindMs >= REBIND_MS) {
                bind();
            }
        } else if (messenger != null) {
            repeatDue = true;
            requestFlush();
        }
        worker.postDelayed(ticker, REPEAT_MS);
    }

    private boolean installed() {
        try {
            app.getPackageManager().getPackageInfo(REMOTE_PACKAGE, 0);
            return true;
        } catch (PackageManager.NameNotFoundException e) {
            return false;
        }
    }

    private void bind() {
        lastBindMs = SystemClock.uptimeMillis();
        if (!installed()) {
            if (link != LINK_NOT_INSTALLED) {
                MainActivity.log("is2l Remote is not installed: " + RELEASES_URL);
            }
            link = LINK_NOT_INSTALLED;
            return;
        }
        Intent intent = new Intent(ACTION_PAD_FEED).setPackage(REMOTE_PACKAGE);
        boolean ok;
        try {
            ok = app.bindService(intent, connection, Context.BIND_AUTO_CREATE);
        } catch (RuntimeException e) {   // SecurityException & co.
            Log.w(MainActivity.TAG, "RemoteFeed bind: " + e);
            ok = false;
        }
        if (ok) {
            bound = true;
            link = LINK_CONNECTING;
        } else {
            // a refused bind still has to be released
            try {
                app.unbindService(connection);
            } catch (RuntimeException e) {
                Log.d(MainActivity.TAG, "RemoteFeed unbind after a refused bind: " + e);
            }
            if (link != LINK_NO_FEED) {
                MainActivity.log("is2l Remote has no controller feed service (update is2l Remote)");
            }
            link = LINK_NO_FEED;
        }
    }

    private void unbind() {
        if (bound) {
            try {
                app.unbindService(connection);
            } catch (RuntimeException e) {
                Log.d(MainActivity.TAG, "RemoteFeed unbind: " + e);
            }
        }
        bound = false;
        messenger = null;
        haveLast = false;
    }

    private void flush() {
        throttle.flushed(System.nanoTime());
        if (running) {
            sendCurrent();
        }
    }

    private void sendCurrent() {
        Messenger m = messenger;
        if (m == null) {
            return;
        }
        InputHub.Device d = hub.activeDevice();
        int cover = d == null || !d.connected ? SEND_NOTHING
                : coverage(d.backend, screenFocused);
        if (cover == SEND_NOTHING) {
            repeatDue = false;
            sendGone();
            return;
        }
        synchronized (hub) {   // the hub changes states under its own lock
            snapshot.set(d.state);
        }
        int buttons = packButtons(snapshot.buttons);
        int[] axes = packAxes(snapshot.axes);
        String name = d.name == null ? "" : d.name;
        boolean repeat = repeatDue;
        repeatDue = false;
        if (haveLast && !repeat && buttons == lastButtons && Arrays.equals(axes, lastAxes)
                && name.equals(lastName)) {
            return;
        }
        Bundle data = new Bundle();
        data.putInt(KEY_VERSION, PROTOCOL_VERSION);
        data.putInt(KEY_BUTTONS, buttons);
        data.putIntArray(KEY_AXES, axes);
        data.putString(KEY_NAME, name);
        Message msg = Message.obtain(null, MSG_PAD_STATE);
        msg.setData(data);
        if (deliver(m, msg)) {
            lastButtons = buttons;
            lastAxes = axes;
            lastName = name;
            haveLast = true;
            statesSent++;
        }
    }

    private void sendGone() {
        Messenger m = messenger;
        if (m == null || !haveLast) {
            return;
        }
        haveLast = false;
        deliver(m, Message.obtain(null, MSG_PAD_GONE));
    }

    private boolean deliver(Messenger m, Message msg) {
        try {
            m.send(msg);
            return true;
        } catch (DeadObjectException e) {
            // is2l Remote died with the binding still open: start over
            MainActivity.log("is2l Remote: the feed service died, reconnecting");
            unbind();
            if (running) {
                bind();
            }
            return false;
        } catch (RemoteException | RuntimeException e) {
            Log.w(MainActivity.TAG, "RemoteFeed send: " + e);
            return false;
        }
    }
}
