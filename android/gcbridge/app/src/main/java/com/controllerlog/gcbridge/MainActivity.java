package com.controllerlog.gcbridge;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlertDialog;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Insets;
import android.net.Uri;
import android.hardware.input.InputManager;
import android.hardware.usb.UsbDevice;
import android.hardware.usb.UsbManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.provider.Settings;
import android.util.Log;
import android.view.InputDevice;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowInsets;
import android.widget.ArrayAdapter;
import android.widget.AdapterView;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.RadioGroup;
import android.widget.ScrollView;
import android.widget.SeekBar;
import android.widget.Spinner;
import android.widget.TextView;
import android.widget.Toast;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.lang.ref.WeakReference;
import java.time.LocalDate;
import java.time.LocalTime;
import java.time.format.DateTimeFormatter;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * One screen: start the controller over USB (vendor interface only), then watch what Android
 * makes of it - input devices, key events and joystick axes.
 */
public class MainActivity extends Activity implements InputManager.InputDeviceListener,
        InputHub.Listener {

    static final String TAG = "GCBridge";

    private static final String ACTION_USB_PERMISSION =
            "com.controllerlog.gcbridge.USB_PERMISSION";
    private static final String PREFS = "gcbridge";
    private static final String KEY_FORMAT = "report_format";
    private static final String STATE_LOG = "usb_log";
    private static final String STATE_KEYS = "key_history";
    private static final int MAX_SAVED_LOG_CHARS = 20_000;

    private static final int ACTION_NONE = 0;
    private static final int ACTION_START = 1;
    private static final int ACTION_PEEK = 2;
    private static final int ACTION_CAPTURE = 3;
    private static final int REQ_BLE = 10;
    private static final int REQ_NOTIFICATIONS = 11;
    private static final int REQ_IMPORT_PROFILE = 12;
    private static final long STATUS_INTERVAL_MS = 1000;
    private static final long DEVICE_REFRESH_MS = 250;
    private static final long LOG_UI_INTERVAL_MS = 100;
    private static final long CALIB_TICK_MS = 100;
    private static final int MAX_PROFILE_BYTES = 1 << 20;
    static final String EXTRA_HARNESS_DIAG = "gcbridge.harness.diag";

    private static final int MAX_KEY_HISTORY = 30;
    private static final int MAX_LOG_CHARS = 60_000;
    private static final long MOTION_LOG_INTERVAL_MS = 100;
    private static final long LIVE_UI_INTERVAL_MS = 33;
    private static final int PEEK_MILLIS = 2000;
    private static final DateTimeFormatter TIME =
            DateTimeFormatter.ofPattern("HH:mm:ss.SSS", Locale.ROOT);

    // Process-wide USB state, touched on the main thread only (except WORKER itself). It is not
    // per activity: a screen recreated in the middle of a Start or Peek (a density, dark-mode or
    // font change isn't in configChanges) keeps receiving that operation's log lines, keeps its
    // buttons disabled until it ends, and can't start a second operation on the same device.
    private static final ExecutorService WORKER =
            Executors.newSingleThreadExecutor(r -> new Thread(r, "gcbridge-usb"));
    private static final Handler MAIN = new Handler(Looper.getMainLooper());
    private static WeakReference<MainActivity> current = new WeakReference<>(null);
    private static boolean busy;
    private static int pendingAction = ACTION_NONE;
    /** A Start requested (by a USB attach) while busy; runs when the current operation ends. */
    private static UsbDevice queuedStart;

    private UsbManager usb;
    private InputManager inputManager;
    private SharedPreferences prefs;
    private final Handler main = new Handler(Looper.getMainLooper());

    private TextView statusView;
    private TextView liveView;
    private TextView keysView;
    private TextView usbLogView;
    private TextView devicesView;
    private RadioGroup modeGroup;
    private Button startButton;
    private Button peekButton;

    // capture / overlay controls
    private final InputHub hub = InputHub.get();
    private PadView padPreview;
    private TextView captureStatus;
    private Button recordButton;
    private Button overlayButton;
    private Button usbCaptureButton;
    private Button bleButton;
    private TextView usbStatusView;
    private TextView bleStatusView;
    private TextView accessibilityStatus;
    private Spinner layoutSpinner;
    private String previewFamily;
    private String previewLayoutPref;
    private boolean statusUpdatePending;
    private final Runnable statusUpdater = () -> {
        statusUpdatePending = false;
        refreshCaptureUi();
    };
    private final Runnable periodic = new Runnable() {
        @Override
        public void run() {
            refreshCaptureUi();
            main.postDelayed(this, STATUS_INTERVAL_MS);
        }
    };

    // Main-thread state.
    private final StringBuilder usbLog = new StringBuilder();
    private final ArrayDeque<String> keyHistory = new ArrayDeque<>();

    private int liveDeviceId = Integer.MIN_VALUE;
    private String liveDeviceLabel = "";
    private int[] liveAxes = new int[0];
    private boolean[] liveAxisReported = new boolean[0];
    private float[] liveValues = new float[0];
    private final Set<String> heldKeys = new TreeSet<>();
    private long motionWindowStart;
    private int motionWindowCount;
    private float motionRate;
    private long lastMotionLog;
    private boolean liveUpdatePending;
    private final Runnable liveUpdater = () -> {
        liveUpdatePending = false;
        liveView.setText(buildLiveText());
    };

    private boolean started;
    private boolean logDirty;
    private boolean logUpdatePending;
    private final Runnable logUpdater = () -> {
        logUpdatePending = false;
        if (started) {
            usbLogView.setText(usbLog);
            logDirty = false;
        }
    };
    private final Set<Integer> gameDeviceIds = new HashSet<>();
    private final Runnable devicesRefresher = () -> {
        refreshDevices();
        refreshControllers();
    };

    private LinearLayout controllersList;
    private View calibPanel;
    private TextView calibTitle;
    private TextView calibPrompt;
    private TextView calibMessage;
    private TextView calibAnswers;
    private Button calibSkip;
    private Button calibRedo;
    private Button calibBack;
    private Button calibYes;
    private Button calibNo;
    private Button calibSave;
    private PadCalibration calibration;
    private int calibDeviceId = Integer.MIN_VALUE;
    private String calibName = "";
    private boolean calibLiveApplied;
    private final Runnable calibTicker = new Runnable() {
        @Override
        public void run() {
            if (calibration == null) {
                return;
            }
            calibration.tick(SystemClock.uptimeMillis());
            updateCalibUi();
            main.postDelayed(this, CALIB_TICK_MS);
        }
    };

    private final BroadcastReceiver usbReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            String action = intent.getAction();
            UsbDevice device = usbDeviceExtra(intent);
            String which = device != null ? Switch2Usb.describeDevice(device) : "(no device)";
            if (ACTION_USB_PERMISSION.equals(action)) {
                boolean granted = intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED,
                        false);
                int todo = pendingAction;
                pendingAction = ACTION_NONE;
                log("USB permission " + (granted ? "granted" : "DENIED") + " for " + which);
                if (granted && device != null && todo == ACTION_CAPTURE) {
                    startUsbCapture(device);
                } else if (granted && device != null && todo != ACTION_NONE) {
                    runUsbAction(device, todo);
                } else if (!granted) {
                    log("Without USB permission the app can't send the init. Tap Start again "
                            + "and allow access.");
                }
            } else if (UsbManager.ACTION_USB_DEVICE_ATTACHED.equals(action)) {
                log("USB attached: " + which);
            } else if (UsbManager.ACTION_USB_DEVICE_DETACHED.equals(action)) {
                log("USB detached: " + which);
            }
            refreshStatus(false);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        // Application context: the worker may still use it after this activity is destroyed.
        usb = getApplicationContext().getSystemService(UsbManager.class);
        inputManager = getSystemService(InputManager.class);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);

        statusView = findViewById(R.id.status);
        liveView = findViewById(R.id.live);
        keysView = findViewById(R.id.keys);
        usbLogView = findViewById(R.id.usb_log);
        devicesView = findViewById(R.id.devices);
        modeGroup = findViewById(R.id.mode_group);
        startButton = findViewById(R.id.btn_start);
        peekButton = findViewById(R.id.btn_peek);
        current = new WeakReference<>(this); // log lines go here from now on
        setUsbButtonsEnabled(!busy);
        applySystemBarInsets(findViewById(R.id.root));
        if (savedInstanceState != null) {
            restoreState(savedInstanceState);
        }

        modeGroup.check(selectedFormat() == Switch2Protocol.FORMAT_NINTENDO
                ? R.id.mode_nintendo : R.id.mode_standard);
        modeGroup.setOnCheckedChangeListener((group, checkedId) -> {
            int format = checkedId == R.id.mode_nintendo
                    ? Switch2Protocol.FORMAT_NINTENDO : Switch2Protocol.FORMAT_STANDARD;
            prefs.edit().putInt(KEY_FORMAT, format).apply();
            log("Report format for the next Start: " + Switch2Protocol.formatName(format));
        });
        startButton.setOnClickListener(v -> onStartClicked());
        peekButton.setOnClickListener(v -> onPeekClicked());
        findViewById(R.id.btn_refresh).setOnClickListener(v -> {
            refreshStatus(true);
            refreshDevices();
        });
        findViewById(R.id.btn_copy).setOnClickListener(v -> copyAll());
        findViewById(R.id.btn_clear).setOnClickListener(v -> {
            usbLog.setLength(0);
            usbLogView.setText("");
            keyHistory.clear();
            keysView.setText(R.string.keys_none);
        });
        findViewById(R.id.btn_exit).setOnClickListener(v -> finishAndRemoveTask());

        InputRouter.ensureSettings(this);
        setUpCaptureControls();
        setUpControllerControls();
        registerUsbReceiver();

        log("GC Bridge " + appVersion() + " on " + Build.MANUFACTURER + " " + Build.MODEL
                + ", Android " + Build.VERSION.RELEASE + " (API " + Build.VERSION.SDK_INT + ")");
        if (!getPackageManager().hasSystemFeature(PackageManager.FEATURE_USB_HOST)) {
            log("WARNING: this device doesn't report USB host support");
        }
        refreshStatus(true);
        if (savedInstanceState == null) {
            handleIntent(getIntent());
        }
    }

    @Override
    protected void onStart() {
        super.onStart();
        started = true;
        inputManager.registerInputDeviceListener(this, main);
        hub.addListener(this);
        InputRouter.hold(this, this);
        refreshDevices();
        refreshControllers();
        refreshCaptureUi();
        if (logDirty) {
            usbLogView.setText(usbLog);
            logDirty = false;
        }
    }

    @Override
    protected void onStop() {
        started = false;
        if (calibration != null) {
            cancelCalibration("left the screen");
        }
        inputManager.unregisterInputDeviceListener(this);
        hub.removeListener(this);
        InputRouter.release(this);
        main.removeCallbacks(devicesRefresher);
        main.removeCallbacks(statusUpdater);
        statusUpdatePending = false;
        main.removeCallbacks(liveUpdater);
        liveUpdatePending = false;
        main.removeCallbacks(logUpdater);
        logUpdatePending = false;
        main.removeCallbacks(calibTicker);
        super.onStop();
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        int from = Math.max(0, usbLog.length() - MAX_SAVED_LOG_CHARS);
        outState.putString(STATE_LOG, usbLog.substring(from));
        outState.putStringArrayList(STATE_KEYS, new ArrayList<>(keyHistory));
    }

    private void restoreState(Bundle state) {
        String savedLog = state.getString(STATE_LOG);
        if (savedLog != null) {
            usbLog.append(savedLog);
            usbLogView.setText(usbLog);
        }
        ArrayList<String> keys = state.getStringArrayList(STATE_KEYS);
        if (keys != null && !keys.isEmpty()) {
            keyHistory.addAll(keys);
            keysView.setText(String.join("\n", keyHistory));
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        handleIntent(intent);
    }

    @Override
    protected void onResume() {
        super.onResume();
        refreshCaptureUi();
        main.postDelayed(periodic, STATUS_INTERVAL_MS);
    }

    @Override
    protected void onPause() {
        main.removeCallbacks(periodic);
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        unregisterReceiver(usbReceiver);
        main.removeCallbacksAndMessages(null);
        // A new instance (e.g. launched while this one was finishing) may already be current.
        if (current.get() == this) {
            current = new WeakReference<>(null);
        }
        super.onDestroy();
    }

    private void handleIntent(Intent intent) {
        if (intent != null && getPackageName().endsWith(".dev")
                && intent.getBooleanExtra(EXTRA_HARNESS_DIAG, false)) {
            main.postDelayed(() -> exportDiagnostics(false), 500);
            return;
        }
        if (intent == null || !UsbManager.ACTION_USB_DEVICE_ATTACHED.equals(intent.getAction())) {
            return;
        }
        if ((intent.getFlags() & Intent.FLAG_ACTIVITY_LAUNCHED_FROM_HISTORY) != 0) {
            return; // reopened from Recents: the attach this task was started for is old news
        }
        UsbDevice device = usbDeviceExtra(intent);
        if (device == null) {
            return;
        }
        log("Opened by USB attach: " + Switch2Usb.describeDevice(device));
        if (!Switch2Protocol.isSupported(device.getVendorId(), device.getProductId())) {
            return;
        }
        if (!Switch2Usb.isAttached(usb, device)) {
            log("That controller is already unplugged.");
            return;
        }
        runUsbAction(device, ACTION_START);
    }

    // --- USB ---------------------------------------------------------------------------------

    private int selectedFormat() {
        return prefs.getInt(KEY_FORMAT, Switch2Protocol.FORMAT_STANDARD);
    }

    private void onStartClicked() {
        UsbDevice device = Switch2Usb.findController(usb);
        if (device == null) {
            log("No Switch 2 GameCube / Pro controller found on USB.");
            refreshStatus(true);
            return;
        }
        runUsbAction(device, ACTION_START);
    }

    private void onPeekClicked() {
        UsbDevice device = Switch2Usb.findController(usb);
        if (device == null) {
            log("Peek: no Switch 2 GameCube / Pro controller found on USB.");
            return;
        }
        new AlertDialog.Builder(this)
                .setTitle(R.string.btn_peek)
                .setMessage(R.string.peek_hint)
                .setPositiveButton(R.string.btn_peek, (d, w) -> runUsbAction(device, ACTION_PEEK))
                .setNegativeButton(android.R.string.cancel, null)
                .show();
    }

    /** Runs START or PEEK on the worker thread, asking for USB permission first if needed. */
    private void runUsbAction(UsbDevice device, int action) {
        if (busy) {
            if (action == ACTION_START) {
                // e.g. the controller was replugged while Peek was still running
                queuedStart = device;
                log("Busy; Start will run when the current operation finishes.");
            } else {
                log("Busy; wait for the current operation to finish.");
            }
            return;
        }
        if (!usb.hasPermission(device)) {
            requestUsbPermission(device, action);
            return;
        }
        busy = true;
        setUsbButtonsEnabled(false);
        final int format = selectedFormat();
        final UsbManager usbManager = usb;
        WORKER.execute(() -> {
            try {
                if (action == ACTION_PEEK) {
                    Switch2Usb.peekHid(usbManager, device, PEEK_MILLIS, MainActivity::log);
                } else {
                    boolean ok = Switch2Usb.startController(usbManager, device, format,
                            MainActivity::log);
                    log(ok ? "Done. Check \"Input devices\" and press buttons / move sticks."
                            : "Start failed; see the messages above.");
                }
            } catch (RuntimeException e) {
                Log.e(TAG, "USB operation failed", e);
                log("ERROR: " + e);
            } finally {
                MAIN.post(MainActivity::onUsbActionFinished);
            }
        });
    }

    /** Main thread, after every Start / Peek: re-enable the current screen, run a queued Start. */
    private static void onUsbActionFinished() {
        busy = false;
        UsbDevice next = queuedStart;
        queuedStart = null;
        MainActivity a = current.get();
        if (a == null) {
            return;
        }
        a.setUsbButtonsEnabled(true);
        a.refreshStatus(false);
        if (next != null) {
            if (Switch2Usb.isAttached(a.usb, next)) {
                a.runUsbAction(next, ACTION_START);
            } else {
                log("Queued Start skipped: " + Switch2Usb.describeDevice(next) + " is gone.");
            }
        }
    }

    private void requestUsbPermission(UsbDevice device, int action) {
        pendingAction = action;
        // Explicit (package-scoped) intent: required for a mutable PendingIntent on Android 14+.
        // Mutable because UsbManager fills in EXTRA_DEVICE / EXTRA_PERMISSION_GRANTED.
        Intent intent = new Intent(ACTION_USB_PERMISSION).setPackage(getPackageName());
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            flags |= PendingIntent.FLAG_MUTABLE;
        }
        PendingIntent pi = PendingIntent.getBroadcast(this, 0, intent, flags);
        log("Asking for USB permission for " + Switch2Usb.describeDevice(device) + "…");
        usb.requestPermission(device, pi);
    }

    /**
     * Only our own PendingIntent (sent with this app's identity) and system USB broadcasts need
     * to reach the receiver, so it is not exported. RECEIVER_NOT_EXPORTED is a plain int that
     * Android 13+ enforces and older releases ignore (they only look at the instant-app bit).
     */
    @SuppressLint("InlinedApi")
    private void registerUsbReceiver() {
        IntentFilter filter = new IntentFilter(ACTION_USB_PERMISSION);
        filter.addAction(UsbManager.ACTION_USB_DEVICE_ATTACHED);
        filter.addAction(UsbManager.ACTION_USB_DEVICE_DETACHED);
        registerReceiver(usbReceiver, filter, Context.RECEIVER_NOT_EXPORTED);
    }

    private void setUsbButtonsEnabled(boolean enabled) {
        startButton.setEnabled(enabled);
        peekButton.setEnabled(enabled);
    }

    private void refreshStatus(boolean logUsbDevices) {
        UsbDevice device = Switch2Usb.findController(usb);
        if (device != null) {
            statusView.setText(Switch2Protocol.modelName(device.getProductId()) + " on USB ("
                    + Switch2Usb.describeDevice(device) + "), permission: "
                    + (usb.hasPermission(device) ? "granted" : "not yet"));
        } else {
            statusView.setText("No Switch 2 GameCube / Pro controller on USB. Connect it with a "
                    + "USB-C data cable.");
        }
        if (logUsbDevices) {
            List<UsbDevice> all = new ArrayList<>(usb.getDeviceList().values());
            log("USB devices attached: " + all.size());
            for (UsbDevice d : all) {
                log("  " + Switch2Usb.describeDevice(d) + " \"" + d.getProductName() + "\"");
            }
        }
    }

    @SuppressWarnings("deprecation")
    private static UsbDevice usbDeviceExtra(Intent intent) {
        // The typed getter is buggy on Android 13, so use it from 14 on (as AndroidX does).
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            return intent.getParcelableExtra(UsbManager.EXTRA_DEVICE, UsbDevice.class);
        }
        return intent.getParcelableExtra(UsbManager.EXTRA_DEVICE);
    }

    // --- log -----------------------------------------------------------------------------------

    /** Logcat + the current screen's USB log. Safe to call from any thread. */
    static void log(String message) {
        Log.i(TAG, message);
        String line = LocalTime.now().format(TIME) + " " + message + "\n";
        if (Looper.myLooper() == Looper.getMainLooper()) {
            appendToCurrent(line);
        } else {
            MAIN.post(() -> appendToCurrent(line));
        }
    }

    private static void appendToCurrent(String line) {
        MainActivity a = current.get();
        if (a != null) {
            a.appendLog(line);
        }
    }

    private void appendLog(String line) {
        usbLog.append(line);
        if (usbLog.length() > MAX_LOG_CHARS) {
            int cut = usbLog.indexOf("\n", usbLog.length() - MAX_LOG_CHARS);
            usbLog.delete(0, cut < 0 ? usbLog.length() - MAX_LOG_CHARS : cut + 1);
        }
        logDirty = true;
        if (started && !logUpdatePending) {
            logUpdatePending = true;
            main.postDelayed(logUpdater, LOG_UI_INTERVAL_MS);
        }
    }

    private void copyAll() {
        String text = statusView.getText() + "\n\n== Live input ==\n" + liveView.getText()
                + "\n\n== Key events ==\n" + keysView.getText()
                + "\n\n== USB log ==\n" + usbLog
                + "\n== Input devices ==\n" + devicesView.getText() + "\n\n" + diagnosticsText();
        ClipboardManager cm = getSystemService(ClipboardManager.class);
        cm.setPrimaryClip(ClipData.newPlainText("GC Bridge log", text));
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
            Toast.makeText(this, R.string.copied, Toast.LENGTH_SHORT).show();
        }
    }

    private String appVersion() {
        return appVersion(this);
    }

    static String appVersion(Context ctx) {
        try {
            return ctx.getPackageManager().getPackageInfo(ctx.getPackageName(), 0).versionName;
        } catch (PackageManager.NameNotFoundException e) {
            return "?";
        }
    }

    // --- input devices -------------------------------------------------------------------------

    private void refreshDevices() {
        int[] ids = inputManager.getInputDeviceIds();
        StringBuilder sb = new StringBuilder();
        sb.append(ids.length).append(" device(s); * = gamepad / joystick / Nintendo\n");
        gameDeviceIds.clear();
        for (int id : ids) {
            InputDevice d = InputDevice.getDevice(id);
            if (d != null) {
                sb.append(InputDiagnostics.describe(d));
                if (InputRouter.isGameDevice(d)) {
                    gameDeviceIds.add(id);
                }
            }
        }
        devicesView.setText(sb.toString());
    }

    private void scheduleDevicesRefresh() {
        main.removeCallbacks(devicesRefresher);
        if (started) {
            main.postDelayed(devicesRefresher, DEVICE_REFRESH_MS);
        }
    }

    @Override
    public void onInputDeviceAdded(int deviceId) {
        InputDevice d = InputDevice.getDevice(deviceId);
        if (InputRouter.isGameDevice(d)) {
            log("InputDevice added: " + InputDiagnostics.shortSummary(d));
        }
        scheduleDevicesRefresh();
    }

    @Override
    public void onInputDeviceRemoved(int deviceId) {
        if (gameDeviceIds.contains(deviceId)) {
            log("InputDevice removed: #" + deviceId);
        }
        if (deviceId == liveDeviceId) {
            liveDeviceLabel += " (removed)";
            scheduleLiveUpdate();
        }
        if (calibration != null && deviceId == calibDeviceId) {
            cancelCalibration("the controller went away");
        }
        scheduleDevicesRefresh();
    }

    @Override
    public void onInputDeviceChanged(int deviceId) {
        InputDevice d = InputDevice.getDevice(deviceId);
        if (InputRouter.isGameDevice(d)) {
            log("InputDevice changed: " + InputDiagnostics.shortSummary(d));
        }
        if (deviceId == liveDeviceId) {
            liveDeviceId = Integer.MIN_VALUE; // re-read its axes on the next event
        }
        scheduleDevicesRefresh();
    }

    // --- live events ---------------------------------------------------------------------------

    private static boolean isGameSource(int source) {
        return (source & InputDevice.SOURCE_GAMEPAD) == InputDevice.SOURCE_GAMEPAD
                || (source & InputDevice.SOURCE_JOYSTICK) == InputDevice.SOURCE_JOYSTICK;
    }

    /**
     * Gamepad keys are handled here, before the view hierarchy, so they never click buttons,
     * move focus or close the app (B often maps to BACK). The phone's own back gesture / button
     * isn't from a gamepad and still works, as does the Exit button.
     */
    @Override
    public boolean dispatchKeyEvent(KeyEvent event) {
        if (AndroidInput.isSystemVolumeKey(event.getKeyCode())) {
            return super.dispatchKeyEvent(event);
        }
        InputDevice device = event.getDevice();
        boolean game = isGameSource(event.getSource()) || InputDiagnostics.isGameDevice(device);
        if (calibration != null && game && event.getDeviceId() == calibDeviceId && !calibration.isLive()) {
            int action = event.getAction();
            if ((action == KeyEvent.ACTION_DOWN || action == KeyEvent.ACTION_UP) && event.getRepeatCount() == 0) {
                calibration.onKey(event.getScanCode(), event.getKeyCode(), action == KeyEvent.ACTION_DOWN,
                        SystemClock.uptimeMillis());
                onDeviceKey(event, device);
                updateCalibUi();
            }
            return true;
        }
        boolean external = device != null && device.isExternal() && !device.isVirtual();
        if (game || external) {
            onDeviceKey(event, device);
            InputRouter.onKey(event);
        }
        return game || super.dispatchKeyEvent(event);
    }

    private void onDeviceKey(KeyEvent e, InputDevice device) {
        boolean down = e.getAction() == KeyEvent.ACTION_DOWN;
        if (!down && e.getAction() != KeyEvent.ACTION_UP) {
            return;
        }
        if (down && e.getRepeatCount() > 0) {
            return; // auto-repeat
        }
        noteLiveDevice(device, e.getDeviceId());
        String key = InputDiagnostics.keyName(e.getKeyCode());
        if (down) {
            heldKeys.add(key);
        } else {
            heldKeys.remove(key);
        }
        int scan = e.getScanCode();
        int hidButton = Switch2Protocol.hidButtonForScanCode(scan);
        String line = String.format(Locale.ROOT, "%s %s %-14s scan=0x%03x%s dev=#%d",
                LocalTime.now().format(TIME), down ? "DOWN" : "UP  ", key, scan,
                hidButton > 0 ? " (HID btn " + hidButton + "?)" : "", e.getDeviceId());
        keyHistory.addFirst(line);
        while (keyHistory.size() > MAX_KEY_HISTORY) {
            keyHistory.removeLast();
        }
        keysView.setText(String.join("\n", keyHistory));
        Log.i(TAG, "key " + (down ? "down " : "up ") + key + " keyCode=" + e.getKeyCode()
                + " scan=" + scan + " source=0x" + Integer.toHexString(e.getSource())
                + " device=" + (device != null ? InputDiagnostics.shortSummary(device)
                : "#" + e.getDeviceId()));
        scheduleLiveUpdate();
    }

    @Override
    public boolean dispatchGenericMotionEvent(MotionEvent event) {
        InputDevice device = event.getDevice();
        boolean joystick = event.isFromSource(InputDevice.SOURCE_CLASS_JOYSTICK)
                || (InputDiagnostics.isGameDevice(device)
                && !event.isFromSource(InputDevice.SOURCE_CLASS_POINTER));
        if (!joystick) {
            return super.dispatchGenericMotionEvent(event);
        }
        if (event.getActionMasked() == MotionEvent.ACTION_MOVE) {
            onDeviceMotion(event, device);
        }
        if (calibration != null && event.getDeviceId() == calibDeviceId && !calibration.isLive()) {
            if (event.getActionMasked() == MotionEvent.ACTION_MOVE && device != null) {
                long now = SystemClock.uptimeMillis();
                for (InputDevice.MotionRange r : device.getMotionRanges()) {
                    if ((r.getSource() & InputDevice.SOURCE_CLASS_JOYSTICK) != 0) {
                        calibration.onAxis(r.getAxis(), event.getAxisValue(r.getAxis()), now);
                    }
                }
                updateCalibUi();
            }
            return true;
        }
        InputRouter.onMotion(event);
        return true;
    }

    private void onDeviceMotion(MotionEvent e, InputDevice device) {
        noteLiveDevice(device, e.getDeviceId());
        for (int i = 0; i < liveAxes.length; i++) {
            liveValues[i] = e.getAxisValue(liveAxes[i]);
        }
        long now = SystemClock.uptimeMillis();
        motionWindowCount++;
        if (now - motionWindowStart >= 1000) {
            motionRate = motionWindowCount * 1000f / Math.max(1, now - motionWindowStart);
            motionWindowStart = now;
            motionWindowCount = 0;
        }
        if (now - lastMotionLog >= MOTION_LOG_INTERVAL_MS) {
            lastMotionLog = now;
            StringBuilder sb = new StringBuilder("motion dev=#").append(e.getDeviceId());
            for (int i = 0; i < liveAxes.length; i++) {
                if (liveAxisReported[i]) {
                    sb.append(String.format(Locale.ROOT, " %s=%.2f",
                            InputDiagnostics.axisName(liveAxes[i]), liveValues[i]));
                }
            }
            Log.i(TAG, sb.toString());
        }
        scheduleLiveUpdate();
    }

    /** Switches the live panel to {@code device} and works out which axes to show for it. */
    private void noteLiveDevice(InputDevice device, int deviceId) {
        if (deviceId == liveDeviceId) {
            return;
        }
        liveDeviceId = deviceId;
        liveDeviceLabel = device != null ? InputDiagnostics.shortSummary(device) : "#" + deviceId;
        LinkedHashSet<Integer> axes = new LinkedHashSet<>();
        for (int a : InputDiagnostics.STANDARD_AXES) {
            axes.add(a);
        }
        Set<Integer> reported = new LinkedHashSet<>();
        if (device != null) {
            for (InputDevice.MotionRange r : device.getMotionRanges()) {
                reported.add(r.getAxis());
            }
        }
        axes.addAll(reported);
        liveAxes = new int[axes.size()];
        liveAxisReported = new boolean[axes.size()];
        int i = 0;
        for (int a : axes) {
            liveAxes[i] = a;
            liveAxisReported[i] = reported.contains(a);
            i++;
        }
        liveValues = new float[liveAxes.length];
        heldKeys.clear();
        motionWindowStart = SystemClock.uptimeMillis();
        motionWindowCount = 0;
        motionRate = 0;
    }

    private void scheduleLiveUpdate() {
        if (!liveUpdatePending) {
            liveUpdatePending = true;
            main.postDelayed(liveUpdater, LIVE_UI_INTERVAL_MS);
        }
    }

    private String buildLiveText() {
        if (liveDeviceId == Integer.MIN_VALUE) {
            return getString(R.string.live_none);
        }
        StringBuilder sb = new StringBuilder(liveDeviceLabel);
        sb.append(String.format(Locale.ROOT, "\nmotion events: %.0f/s   (-- = axis not reported)\n",
                motionRate));
        for (int i = 0; i < liveAxes.length; i++) {
            String value = liveAxisReported[i]
                    ? String.format(Locale.ROOT, "%+6.2f", liveValues[i]) : "    --";
            sb.append(String.format(Locale.ROOT, "%-9s%s", InputDiagnostics.axisName(liveAxes[i]),
                    value));
            sb.append(i % 2 == 0 && i + 1 < liveAxes.length ? "    " : "\n");
        }
        sb.append("held: ").append(heldKeys.isEmpty() ? "-" : String.join(" ", heldKeys));
        return sb.toString();
    }


    // --- log & overlay controls ----------------------------------------------------------------

    private void setUpCaptureControls() {
        padPreview = findViewById(R.id.pad_preview);
        captureStatus = findViewById(R.id.capture_status);
        recordButton = findViewById(R.id.btn_record);
        overlayButton = findViewById(R.id.btn_overlay);
        usbCaptureButton = findViewById(R.id.btn_usb_capture);
        bleButton = findViewById(R.id.btn_ble);
        usbStatusView = findViewById(R.id.usb_status);
        bleStatusView = findViewById(R.id.ble_status);
        accessibilityStatus = findViewById(R.id.accessibility_status);
        layoutSpinner = findViewById(R.id.layout_spinner);

        recordButton.setOnClickListener(v -> {
            if (hub.isRecording()) {
                CaptureService.send(this, CaptureService.ACTION_RECORD_STOP);
            } else {
                askNotificationPermission();
                CaptureService.send(this, CaptureService.ACTION_RECORD_START);
            }
            scheduleStatusUpdate();
        });
        findViewById(R.id.btn_marker).setOnClickListener(v ->
                CaptureService.send(this, CaptureService.ACTION_MARKER));
        overlayButton.setOnClickListener(v -> {
            if (CaptureService.overlayShown()) {
                CaptureService.send(this, CaptureService.ACTION_OVERLAY_HIDE);
            } else if (!OverlayWindow.canDraw(this)) {
                log(getString(R.string.overlay_permission_hint));
                Toast.makeText(this, R.string.overlay_permission_hint, Toast.LENGTH_LONG).show();
                try {
                    startActivity(new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                            Uri.parse("package:" + getPackageName())));
                } catch (RuntimeException e) {
                    startActivity(new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION));
                }
            } else {
                askNotificationPermission();
                CaptureService.send(this, CaptureService.ACTION_OVERLAY_SHOW);
            }
            scheduleStatusUpdate();
        });
        findViewById(R.id.btn_recordings).setOnClickListener(v -> showRecordings());
        findViewById(R.id.btn_accessibility).setOnClickListener(v -> {
            try {
                startActivity(KeyCaptureService.settingsIntent());
            } catch (RuntimeException e) {
                log("Can't open the accessibility settings: " + e);
            }
        });
        usbCaptureButton.setOnClickListener(v -> onUsbCaptureClicked());
        bleButton.setOnClickListener(v -> onBleClicked());

        String[] names = LayoutStore.names(this);
        String[] entries = new String[names.length + 1];
        entries[0] = "auto";
        System.arraycopy(names, 0, entries, 1, names.length);
        ArrayAdapter<String> adapter = new ArrayAdapter<>(this,
                android.R.layout.simple_spinner_item, entries);
        adapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
        layoutSpinner.setAdapter(adapter);
        String current = prefs.getString(CaptureService.PREF_LAYOUT, "auto");
        for (int i = 0; i < entries.length; i++) {
            if (entries[i].equals(current)) {
                layoutSpinner.setSelection(i);
            }
        }
        layoutSpinner.setOnItemSelectedListener(new AdapterView.OnItemSelectedListener() {
            @Override
            public void onItemSelected(AdapterView<?> parent, View view, int position, long id) {
                String pick = entries[position];
                if (!pick.equals(prefs.getString(CaptureService.PREF_LAYOUT, "auto"))) {
                    prefs.edit().putString(CaptureService.PREF_LAYOUT, pick).apply();
                    if (CaptureService.isRunning()) {
                        CaptureService.send(MainActivity.this, CaptureService.ACTION_OVERLAY_REFRESH);
                    }
                }
                updatePreviewLayout();
            }

            @Override
            public void onNothingSelected(AdapterView<?> parent) {
            }
        });

        SeekBar size = findViewById(R.id.overlay_size);
        size.setProgress(Math.round((prefs.getFloat(OverlayWindow.PREF_SCALE, OverlayWindow.DEFAULT_SCALE) - 0.15f) * 100));
        SeekBar alpha = findViewById(R.id.overlay_alpha);
        alpha.setProgress(Math.round((prefs.getFloat(OverlayWindow.PREF_ALPHA, OverlayWindow.DEFAULT_ALPHA) - 0.2f) * 100));
        SeekBar.OnSeekBarChangeListener seek = new SeekBar.OnSeekBarChangeListener() {
            @Override
            public void onProgressChanged(SeekBar bar, int progress, boolean fromUser) {
                if (!fromUser) {
                    return;
                }
                if (bar == size) {
                    prefs.edit().putFloat(OverlayWindow.PREF_SCALE, 0.15f + progress / 100f).apply();
                } else {
                    prefs.edit().putFloat(OverlayWindow.PREF_ALPHA, 0.2f + progress / 100f).apply();
                }
            }

            @Override
            public void onStartTrackingTouch(SeekBar bar) {
            }

            @Override
            public void onStopTrackingTouch(SeekBar bar) {
                if (CaptureService.overlayShown()) {
                    CaptureService.send(MainActivity.this, CaptureService.ACTION_OVERLAY_REFRESH);
                }
            }
        };
        size.setOnSeekBarChangeListener(seek);
        alpha.setOnSeekBarChangeListener(seek);
        updatePreviewLayout();
    }

    private void askNotificationPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
                && checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{android.Manifest.permission.POST_NOTIFICATIONS},
                    REQ_NOTIFICATIONS);
        }
    }

    private void onUsbCaptureClicked() {
        if (CaptureService.usbCapturing()) {
            CaptureService.send(this, CaptureService.ACTION_USB_CAPTURE_STOP);
            scheduleStatusUpdate();
            return;
        }
        UsbDevice device = Switch2Usb.findController(usb);
        if (device == null) {
            log("USB capture: no Switch 2 GameCube / Pro controller found on USB.");
            return;
        }
        if (busy) {
            log("Busy; wait for the current USB operation to finish.");
            return;
        }
        if (!usb.hasPermission(device)) {
            requestUsbPermission(device, ACTION_CAPTURE);
            return;
        }
        startUsbCapture(device);
    }

    private void startUsbCapture(UsbDevice device) {
        askNotificationPermission();
        Intent i = CaptureService.intent(this, CaptureService.ACTION_USB_CAPTURE_START)
                .putExtra(CaptureService.EXTRA_USB_DEVICE, device);
        CaptureService.send(this, i);
        scheduleStatusUpdate();
    }

    private void onBleClicked() {
        if (CaptureService.bleActive()) {
            CaptureService.send(this, CaptureService.ACTION_BLE_STOP);
            scheduleStatusUpdate();
            return;
        }
        if (!Switch2Ble.hasBluetoothLe(this)) {
            log(getString(R.string.ble_unsupported));
            return;
        }
        List<String> missing = Switch2Ble.missingPermissions(this);
        if (!missing.isEmpty()) {
            requestPermissions(missing.toArray(new String[0]), REQ_BLE);
            return;
        }
        askNotificationPermission();
        CaptureService.send(this, CaptureService.ACTION_BLE_START);
        scheduleStatusUpdate();
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQ_BLE) {
            if (Switch2Ble.hasPermissions(this)) {
                CaptureService.send(this, CaptureService.ACTION_BLE_START);
            } else {
                log("Bluetooth permission denied; the Bluetooth reader can't scan.");
            }
        }
        scheduleStatusUpdate();
    }

    private void showRecordings() {
        List<File> files = RecordingStore.list(this);
        if (files.isEmpty()) {
            new AlertDialog.Builder(this).setTitle(R.string.recordings_title)
                    .setMessage(getString(R.string.recordings_none) + "\n\n" + getString(R.string.recordings_where, getPackageName()))
                    .setPositiveButton(android.R.string.ok, null).show();
            return;
        }
        String[] labels = new String[files.size()];
        for (int i = 0; i < labels.length; i++) {
            labels[i] = RecordingStore.describe(files.get(i));
        }
        new AlertDialog.Builder(this).setTitle(R.string.recordings_title)
                .setItems(labels, (d, which) -> recordingActions(files.get(which)))
                .setNegativeButton(android.R.string.cancel, null).show();
    }

    private void recordingActions(File f) {
        String[] actions = {getString(R.string.recordings_share), getString(R.string.recordings_export),
                getString(R.string.recordings_delete)};
        new AlertDialog.Builder(this).setTitle(f.getName())
                .setMessage(getString(R.string.recordings_where, getPackageName()))
                .setItems(actions, (d, which) -> {
                    if (which == 0) {
                        Intent send = new Intent(Intent.ACTION_SEND)
                                .setType("application/octet-stream")
                                .putExtra(Intent.EXTRA_STREAM, RecordingStore.shareUri(this, f))
                                .putExtra(Intent.EXTRA_SUBJECT, f.getName())
                                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                        startActivity(Intent.createChooser(send, f.getName()));
                    } else if (which == 1) {
                        try {
                            Uri uri = RecordingStore.exportToDownloads(this, f);
                            log("Copied to Downloads/" + RecordingStore.downloadsSubdir(this) + "/" + f.getName()
                                    + " (" + uri + ")");
                            Toast.makeText(this, "Downloads/" + RecordingStore.downloadsSubdir(this) + "/" + f.getName(),
                                    Toast.LENGTH_LONG).show();
                        } catch (IOException | RuntimeException e) {
                            log("ERROR copying to Downloads: " + e);
                        }
                    } else if (f.equals(hub.recordingFile())) {
                        log("Stop the recording before deleting it.");
                    } else if (f.delete()) {
                        log("Deleted " + f.getName());
                    }
                })
                .setNegativeButton(android.R.string.cancel, null).show();
    }

    /** Preview and overlay layout for the active controller (or the forced layout). */
    private void updatePreviewLayout() {
        InputHub.Device d = hub.activeDevice();
        String family = d != null ? d.family : Pad.FAMILY_GENERIC;
        String pref = prefs.getString(CaptureService.PREF_LAYOUT, "auto");
        padPreview.setUnavailable(CaptureService.unavailableOf(d));
        if (family.equals(previewFamily) && pref.equals(previewLayoutPref) && padPreview.getLayout() != null) {
            return;
        }
        previewFamily = family;
        previewLayoutPref = pref;
        padPreview.setLayout(LayoutStore.pick(this, pref, family));
        if (d != null) {
            padPreview.setState(d.state);
        }
    }

    private void scheduleStatusUpdate() {
        if (!statusUpdatePending) {
            statusUpdatePending = true;
            main.postDelayed(statusUpdater, 50);
        }
    }

    private void refreshCaptureUi() {
        if (captureStatus == null) {
            return;
        }
        updatePreviewLayout();
        StringBuilder sb = new StringBuilder();
        if (hub.isRecording()) {
            long s = hub.recordingDurationNs() / 1_000_000_000L;
            File f = hub.recordingFile();
            sb.append(String.format(Locale.ROOT, "RECORDING %d:%02d  %d rows  %s\n", s / 60, s % 60,
                    hub.recordingRows(), f != null ? f.getName() : ""));
        } else {
            sb.append("Not recording\n");
        }
        List<InputHub.Device> devices = hub.connectedDevices();
        if (devices.isEmpty()) {
            sb.append("No controller seen yet");
        } else {
            for (InputHub.Device d : devices) {
                sb.append(d == hub.activeDevice() ? "* " : "  ").append(d.shortName())
                        .append(" [").append(d.family).append(", ").append(d.backend).append("] ")
                        .append(d.inputEvents).append(" events\n  ").append(d.state).append('\n');
            }
        }
        String err = hub.lastError();
        if (err != null) {
            sb.append("ERROR ").append(err);
        }
        captureStatus.setText(sb.toString().trim());
        recordButton.setText(hub.isRecording() ? R.string.btn_stop_record : R.string.btn_record);
        overlayButton.setText(CaptureService.overlayShown() ? R.string.btn_overlay_hide : R.string.btn_overlay_show);
        usbCaptureButton.setText(CaptureService.usbCapturing() ? R.string.btn_usb_capture_stop
                : R.string.btn_usb_capture_start);
        usbStatusView.setText("USB capture: " + CaptureService.usbStatus());
        bleButton.setText(CaptureService.bleActive() ? R.string.btn_ble_stop : R.string.btn_ble_start);
        bleStatusView.setText("Bluetooth: " + CaptureService.bleStatus());
        if (KeyCaptureService.isConnected()) {
            accessibilityStatus.setText(R.string.accessibility_on);
        } else if (KeyCaptureService.isEnabledInSettings(this)) {
            accessibilityStatus.setText(R.string.accessibility_enabled_not_connected);
        } else {
            accessibilityStatus.setText(R.string.accessibility_off);
        }
    }

    private void setUpControllerControls() {
        controllersList = findViewById(R.id.controllers_list);
        calibPanel = findViewById(R.id.calib_panel);
        calibTitle = findViewById(R.id.calib_title);
        calibPrompt = findViewById(R.id.calib_prompt);
        calibMessage = findViewById(R.id.calib_message);
        calibAnswers = findViewById(R.id.calib_answers);
        calibSkip = findViewById(R.id.calib_skip);
        calibRedo = findViewById(R.id.calib_redo);
        calibBack = findViewById(R.id.calib_back);
        calibYes = findViewById(R.id.calib_yes);
        calibNo = findViewById(R.id.calib_no);
        calibSave = findViewById(R.id.calib_save);
        findViewById(R.id.btn_diagnostics).setOnClickListener(v -> exportDiagnostics(true));
        findViewById(R.id.btn_import_profile).setOnClickListener(v -> {
            Intent pick = new Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE)
                    .setType("*/*");
            try {
                startActivityForResult(pick, REQ_IMPORT_PROFILE);
            } catch (RuntimeException e) {
                log("Can't open the file picker: " + e);
            }
        });
        calibSkip.setOnClickListener(v -> calibAction(c -> c.skip(SystemClock.uptimeMillis())));
        calibRedo.setOnClickListener(v -> calibAction(c -> c.redo(SystemClock.uptimeMillis())));
        calibBack.setOnClickListener(v -> calibAction(c -> c.back(SystemClock.uptimeMillis())));
        calibYes.setOnClickListener(v -> calibAction(c -> c.answerBoth(true, SystemClock.uptimeMillis())));
        calibNo.setOnClickListener(v -> calibAction(c -> c.answerBoth(false, SystemClock.uptimeMillis())));
        calibSave.setOnClickListener(v -> saveCalibration());
        findViewById(R.id.calib_cancel).setOnClickListener(v -> cancelCalibration("cancelled"));
    }

    private interface CalibStep {
        void apply(PadCalibration c);
    }

    private void calibAction(CalibStep step) {
        if (calibration != null) {
            step.apply(calibration);
            updateCalibUi();
        }
    }

    private Button smallButton(String text, View.OnClickListener l) {
        Button b = new Button(this, null, android.R.attr.buttonBarButtonStyle);
        b.setText(text);
        b.setAllCaps(false);
        b.setOnClickListener(l);
        b.setLayoutParams(new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
        return b;
    }

    private void refreshControllers() {
        if (controllersList == null) {
            return;
        }
        controllersList.removeAllViews();
        PadSettings settings = InputRouter.settings();
        int shown = 0;
        for (int id : inputManager.getInputDeviceIds()) {
            InputDevice d = InputDevice.getDevice(id);
            if (!InputRouter.isGameDevice(d)) {
                continue;
            }
            shown++;
            PadIdentity identity = InputRouter.identityOf(d, id);
            InputRouter.Entry en = InputRouter.entryFor(id);
            PadClass cls = en != null ? en.cls : InputRouter.classOf(identity);
            String key = identity.deviceKey();
            String drawAs = settings.drawAs.getOrDefault(key, "auto");
            boolean hasProfile = settings.profiles.containsKey(key);
            TextView info = new TextView(this);
            info.setTypeface(android.graphics.Typeface.MONOSPACE);
            info.setTextSize(12);
            info.setTextIsSelectable(true);
            info.setText(String.format(Locale.ROOT, "#%d \"%s\" %s%n%s%nroutes: %s%s", id, identity.name,
                    identity.idsKey(), cls.summary(),
                    InputRouter.describeRoutes(en != null ? en.routes : AndroidInput.routes(cls, identity)),
                    hasProfile ? "\nprofile saved" : ""));
            info.setPadding(0, 12, 0, 0);
            controllersList.addView(info);
            LinearLayout row = new LinearLayout(this);
            row.setOrientation(LinearLayout.HORIZONTAL);
            row.addView(smallButton(getString(cls.ignored ? R.string.btn_unignore : R.string.btn_ignore), v -> {
                PadSettings.setIgnored(this, key, !cls.ignored);
                InputRouter.reloadSettings(this);
                log((cls.ignored ? "Stopped ignoring " : "Ignoring ") + identity.name);
                refreshControllers();
            }));
            row.addView(smallButton(getString(R.string.btn_draw_as, drawAs), v -> chooseDrawAs(key, identity.name)));
            row.addView(smallButton(getString(R.string.btn_setup), v -> startCalibration(id, identity, cls)));
            controllersList.addView(row);
            if (hasProfile) {
                LinearLayout row2 = new LinearLayout(this);
                row2.setOrientation(LinearLayout.HORIZONTAL);
                row2.addView(smallButton(getString(R.string.btn_export_profile), v -> exportProfile(key)));
                row2.addView(smallButton(getString(R.string.btn_delete_profile), v -> {
                    PadSettings.saveProfile(this, key, null);
                    InputRouter.reloadSettings(this);
                    log("Deleted the profile of " + identity.name);
                    refreshControllers();
                }));
                controllersList.addView(row2);
            }
        }
        if (shown == 0) {
            TextView none = new TextView(this);
            none.setText(R.string.controllers_none);
            controllersList.addView(none);
        }
    }

    private void chooseDrawAs(String key, String name) {
        String[] names = LayoutStore.names(this);
        String[] entries = new String[names.length + 1];
        entries[0] = "auto";
        System.arraycopy(names, 0, entries, 1, names.length);
        new AlertDialog.Builder(this).setTitle(name)
                .setItems(entries, (dlg, which) -> {
                    PadSettings.setDrawAs(this, key, entries[which]);
                    InputRouter.reloadSettings(this);
                    previewFamily = null;
                    updatePreviewLayout();
                    log("Draw " + name + " as " + entries[which]);
                    refreshControllers();
                })
                .setNegativeButton(android.R.string.cancel, null).show();
    }

    private void startCalibration(int deviceId, PadIdentity identity, PadClass cls) {
        if (calibration != null) {
            cancelCalibration(null);
        }
        String model = cls.model != null ? cls.model
                : PadClassifier.hasAny(identity, PadClassifier.GC_TOKENS) ? PadClass.MODEL_GAMECUBE : null;
        calibration = new PadCalibration(identity, model, SystemClock.uptimeMillis());
        calibDeviceId = deviceId;
        calibName = identity.name;
        calibLiveApplied = false;
        calibPanel.setVisibility(View.VISIBLE);
        log("Set up " + identity.name + " (" + (model != null ? model : "by position") + ")");
        main.removeCallbacks(calibTicker);
        main.post(calibTicker);
        updateCalibUi();
        View root = findViewById(R.id.root);
        if (root instanceof ScrollView) {
            root.post(() -> ((ScrollView) root).smoothScrollTo(0, calibPanel.getTop()));
        }
    }

    private String madeText() {
        return LocalDate.now() + " on " + Build.MANUFACTURER + " " + Build.MODEL;
    }

    private void updateCalibUi() {
        PadCalibration c = calibration;
        if (c == null || calibPanel == null) {
            return;
        }
        if (c.isLive() && !calibLiveApplied) {
            calibLiveApplied = true;
            PadSettings now = InputRouter.settings();
            Map<String, Map<String, Object>> profiles = new LinkedHashMap<>(now.profiles);
            profiles.put(c.deviceKey(), c.profile(madeText()));
            InputRouter.setSettings(new PadSettings(profiles, now.ignore, now.drawAs));
            previewFamily = null;
            updatePreviewLayout();
        }
        calibTitle.setText(getString(R.string.calib_title, calibName, c.stepIndex() + 1, c.stepCount()));
        long left = c.remainingMs(SystemClock.uptimeMillis());
        calibPrompt.setText(c.prompt() + (left >= 0 ? String.format(Locale.ROOT, "  (%d s)", (left + 999) / 1000) : ""));
        String msg = c.message();
        if (c.isLive()) {
            List<?> unavailable = Json.asArray(c.profile(null).get("unavailable"));
            if (unavailable != null && !unavailable.isEmpty()) {
                List<String> names = new ArrayList<>();
                for (Object o : unavailable) {
                    names.add(String.valueOf(o));
                }
                msg = getString(R.string.calib_finish_unavailable, String.join(", ", names));
            }
        }
        calibMessage.setText(msg);
        calibAnswers.setText(String.join("\n", c.summary()));
        boolean confirm = c.awaitingConfirm();
        calibYes.setVisibility(confirm ? View.VISIBLE : View.GONE);
        calibNo.setVisibility(confirm ? View.VISIBLE : View.GONE);
        calibSkip.setVisibility(confirm || c.isLive() ? View.GONE : View.VISIBLE);
        calibRedo.setVisibility(confirm ? View.GONE : View.VISIBLE);
        calibBack.setVisibility(confirm ? View.GONE : View.VISIBLE);
        calibSave.setVisibility(c.isLive() ? View.VISIBLE : View.GONE);
    }

    private void closeCalibration() {
        calibration = null;
        calibDeviceId = Integer.MIN_VALUE;
        main.removeCallbacks(calibTicker);
        if (calibPanel != null) {
            calibPanel.setVisibility(View.GONE);
        }
    }

    private void cancelCalibration(String why) {
        boolean restore = calibLiveApplied;
        closeCalibration();
        calibLiveApplied = false;
        if (restore) {
            InputRouter.reloadSettings(this);
        }
        if (why != null) {
            log("Set up stopped: " + why);
        }
        previewFamily = null;
        updatePreviewLayout();
    }

    private void saveCalibration() {
        PadCalibration c = calibration;
        if (c == null || !c.isLive()) {
            return;
        }
        Map<String, Object> profile = c.profile(madeText());
        PadSettings.saveProfile(this, c.deviceKey(), profile);
        closeCalibration();
        calibLiveApplied = false;
        InputRouter.reloadSettings(this);
        log("Saved a profile for " + calibName + " (" + c.deviceKey() + ")");
        refreshControllers();
    }

    private void exportProfile(String key) {
        Map<String, Object> profile = InputRouter.settings().profiles.get(key);
        if (profile == null) {
            return;
        }
        String name = PadProfile.fileName(profile, LocalDate.now().toString());
        try {
            Uri uri = RecordingStore.exportBytes(this, name, "application/json",
                    Json.write(profile).getBytes(StandardCharsets.UTF_8));
            log("Profile copied to Downloads/" + RecordingStore.downloadsSubdir(this) + "/" + name + " (" + uri + ")");
            Toast.makeText(this, "Downloads/" + RecordingStore.downloadsSubdir(this) + "/" + name,
                    Toast.LENGTH_LONG).show();
        } catch (IOException | RuntimeException e) {
            log("ERROR exporting the profile: " + e);
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQ_IMPORT_PROFILE || resultCode != RESULT_OK || data == null || data.getData() == null) {
            return;
        }
        try (InputStream in = getContentResolver().openInputStream(data.getData())) {
            if (in == null) {
                throw new IOException("can't open " + data.getData());
            }
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) {
                bos.write(buf, 0, n);
                if (bos.size() > MAX_PROFILE_BYTES) {
                    throw new IOException("the file is larger than 1 MB");
                }
            }
            Map<String, Object> profile = Json.asObject(Json.parse(new String(bos.toByteArray(),
                    StandardCharsets.UTF_8)));
            String key = profile != null ? PadProfile.matchKey(profile) : null;
            if (key == null || !key.startsWith("sig:") || Json.asObject(profile.get("buttons")) == null) {
                throw new IOException("not a controller profile (needs match.key and buttons)");
            }
            PadSettings.saveProfile(this, key, profile);
            InputRouter.reloadSettings(this);
            log("Imported a profile for " + key);
            refreshControllers();
        } catch (IOException | RuntimeException e) {
            log("ERROR importing the profile: " + e.getMessage());
        }
    }

    private String diagnosticsText() {
        InputHub.Device d = hub.activeDevice();
        return ControllerDiagnostics.build(this, captureStatus != null ? captureStatus.getText().toString() : "",
                d != null ? d.shortName() + " [" + d.family + "] " + d.key : "none",
                prefs.getString(CaptureService.PREF_LAYOUT, "auto"));
    }

    private void exportDiagnostics(boolean toClipboard) {
        String text = diagnosticsText();
        ControllerDiagnostics.logChunks(text);
        try {
            File f = ControllerDiagnostics.write(this, text);
            log("Diagnostics written to " + f);
        } catch (IOException | RuntimeException e) {
            log("ERROR writing diagnostics: " + e);
        }
        if (toClipboard) {
            ClipboardManager cm = getSystemService(ClipboardManager.class);
            cm.setPrimaryClip(ClipData.newPlainText("GC Bridge controller diagnostics", text));
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
                Toast.makeText(this, R.string.copied, Toast.LENGTH_SHORT).show();
            }
        }
    }

    // --- hub listener (any thread) --------------------------------------------------------------

    @Override
    public void onInput(InputHub.Device device) {
        if (padPreview != null && device == hub.activeDevice()) {
            padPreview.setState(device.state);
        }
        main.post(this::scheduleStatusUpdate);
    }

    @Override
    public void onStatus() {
        main.post(this::scheduleStatusUpdate);
    }

    // --- layout --------------------------------------------------------------------------------

    /** targetSdk 36 is always edge-to-edge on Android 15+: pad the content clear of the bars. */
    private static void applySystemBarInsets(View root) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) {
            return; // not edge-to-edge there
        }
        final int left = root.getPaddingLeft();
        final int top = root.getPaddingTop();
        final int right = root.getPaddingRight();
        final int bottom = root.getPaddingBottom();
        root.setOnApplyWindowInsetsListener((v, insets) -> {
            Insets bars = insets.getInsets(
                    WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
            v.setPadding(left + bars.left, top + bars.top, right + bars.right,
                    bottom + bars.bottom);
            return WindowInsets.CONSUMED;
        });
    }
}
