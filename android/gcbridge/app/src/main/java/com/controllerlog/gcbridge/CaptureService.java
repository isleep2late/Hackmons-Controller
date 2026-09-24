package com.controllerlog.gcbridge;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.hardware.usb.UsbDevice;
import android.hardware.usb.UsbManager;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.util.Log;

import java.io.File;
import java.io.IOException;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;

/**
 * Foreground service that owns everything that must outlive the screen: the recording, the
 * floating overlay, the USB capture thread and the Bluetooth reader. Controlled with intents
 * (see the ACTION_ constants); it stops itself once nothing is left running.
 */
public final class CaptureService extends Service implements InputHub.Listener {

    static final String ACTION_START = "com.controllerlog.gcbridge.START";
    static final String ACTION_STOP_ALL = "com.controllerlog.gcbridge.STOP_ALL";
    static final String ACTION_RECORD_START = "com.controllerlog.gcbridge.RECORD_START";
    static final String ACTION_RECORD_STOP = "com.controllerlog.gcbridge.RECORD_STOP";
    static final String ACTION_MARKER = "com.controllerlog.gcbridge.MARKER";
    static final String ACTION_OVERLAY_SHOW = "com.controllerlog.gcbridge.OVERLAY_SHOW";
    static final String ACTION_OVERLAY_HIDE = "com.controllerlog.gcbridge.OVERLAY_HIDE";
    static final String ACTION_OVERLAY_REFRESH = "com.controllerlog.gcbridge.OVERLAY_REFRESH";
    static final String ACTION_USB_CAPTURE_START = "com.controllerlog.gcbridge.USB_CAPTURE_START";
    static final String ACTION_USB_CAPTURE_STOP = "com.controllerlog.gcbridge.USB_CAPTURE_STOP";
    static final String ACTION_BLE_START = "com.controllerlog.gcbridge.BLE_START";
    static final String ACTION_BLE_STOP = "com.controllerlog.gcbridge.BLE_STOP";
    static final String EXTRA_USB_DEVICE = "usb_device";
    static final String EXTRA_ADDRESS = "address";
    static final String EXTRA_LABEL = "label";

    static final String PREF_LAYOUT = "layout";

    private static final String CHANNEL = "gcbridge_capture";
    private static final int NOTIFICATION_ID = 1;
    private static final long TICK_MS = 1000;

    private static volatile CaptureService instance;

    private final Handler main = new Handler(Looper.getMainLooper());
    private final InputHub hub = InputHub.get();
    private SharedPreferences prefs;
    private OverlayWindow overlay;
    private Switch2Usb.Capture usbCapture;
    private Switch2Ble ble;
    private String overlayLayoutName;
    private String overlayFamily;
    private boolean overlayUpdatePending;
    private long lastNotificationMs;
    private String lastNotificationText = "";
    private final Runnable ticker = this::tick;

    // --- static helpers for the screen ------------------------------------------------------

    static boolean isRunning() {
        return instance != null;
    }

    static boolean overlayShown() {
        CaptureService s = instance;
        return s != null && s.overlay != null && s.overlay.isShown();
    }

    static boolean usbCapturing() {
        CaptureService s = instance;
        return s != null && s.usbCapture != null && s.usbCapture.isRunning();
    }

    static String usbStatus() {
        CaptureService s = instance;
        if (s == null || s.usbCapture == null) {
            return "off";
        }
        double hz = s.usbCapture.rateHz();
        return s.usbCapture.status() + (hz > 0 ? String.format(Locale.ROOT, ", %.0f reports/s", hz) : "");
    }

    static boolean bleActive() {
        CaptureService s = instance;
        return s != null && s.ble != null && s.ble.isActive();
    }

    static String bleStatus() {
        CaptureService s = instance;
        if (s == null || s.ble == null) {
            return "off";
        }
        double hz = s.ble.rateHz();
        return s.ble.status() + (hz > 0 ? String.format(Locale.ROOT, ", %.0f reports/s", hz) : "");
    }

    static Intent intent(Context ctx, String action) {
        return new Intent(ctx, CaptureService.class).setAction(action);
    }

    static void send(Context ctx, String action) {
        send(ctx, intent(ctx, action));
    }

    static void send(Context ctx, Intent intent) {
        try {
            ctx.startForegroundService(intent);
        } catch (RuntimeException e) {   // ForegroundServiceStartNotAllowedException & co.
            Log.e(MainActivity.TAG, "startForegroundService: " + e);
            MainActivity.log("Can't start the capture service: " + e.getMessage());
        }
    }

    // --- lifecycle ---------------------------------------------------------------------------

    @Override
    public void onCreate() {
        super.onCreate();
        instance = this;
        prefs = getSharedPreferences("gcbridge", MODE_PRIVATE);
        createChannel();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(NOTIFICATION_ID, buildNotification("starting"),
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        } else {
            startForeground(NOTIFICATION_ID, buildNotification("starting"));
        }
        hub.addListener(this);
        main.postDelayed(ticker, TICK_MS);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_START : intent.getAction();
        if (action == null) {
            action = ACTION_START;
        }
        try {
            handle(action, intent);
        } catch (RuntimeException e) {
            Log.e(MainActivity.TAG, "CaptureService " + action, e);
            MainActivity.log("ERROR " + action + ": " + e);
        }
        refreshNotification(true);
        if (maybeStop()) {
            return START_NOT_STICKY;
        }
        return START_STICKY;
    }

    private void handle(String action, Intent intent) {
        switch (action) {
            case ACTION_START:
                break;
            case ACTION_STOP_ALL:
                stopRecording();
                hideOverlay();
                stopUsbCapture();
                stopBle();
                break;
            case ACTION_RECORD_START:
                startRecording();
                break;
            case ACTION_RECORD_STOP:
                stopRecording();
                break;
            case ACTION_MARKER: {
                String label = intent != null ? intent.getStringExtra(EXTRA_LABEL) : null;
                hub.marker(label == null || label.isEmpty() ? "marker" : label);
                MainActivity.log("Marker" + (hub.isRecording() ? " written" : " (not recording)"));
                break;
            }
            case ACTION_OVERLAY_SHOW:
                showOverlay();
                break;
            case ACTION_OVERLAY_HIDE:
                hideOverlay();
                break;
            case ACTION_OVERLAY_REFRESH:
                if (overlay != null) {
                    overlayLayoutName = null;   // re-pick the layout
                    overlay.applyPrefs();
                    updateOverlayLayout();
                }
                break;
            case ACTION_USB_CAPTURE_START:
                startUsbCapture(intent);
                break;
            case ACTION_USB_CAPTURE_STOP:
                stopUsbCapture();
                break;
            case ACTION_BLE_START:
                startBle(intent != null ? intent.getStringExtra(EXTRA_ADDRESS) : null);
                break;
            case ACTION_BLE_STOP:
                stopBle();
                break;
            default:
                Log.w(MainActivity.TAG, "unknown action " + action);
        }
    }

    private boolean maybeStop() {
        boolean idle = !hub.isRecording() && (overlay == null || !overlay.isShown())
                && (usbCapture == null || !usbCapture.isRunning()) && (ble == null || !ble.isActive());
        if (idle) {
            stopForeground(STOP_FOREGROUND_REMOVE);
            stopSelf();
        }
        return idle;
    }

    @Override
    public void onDestroy() {
        main.removeCallbacks(ticker);
        hub.removeListener(this);
        stopRecording();
        hideOverlay();
        stopUsbCapture();
        stopBle();
        instance = null;
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    // --- recording ----------------------------------------------------------------------------

    private void startRecording() {
        if (hub.isRecording()) {
            return;
        }
        File f = RecordingStore.newFile(this);
        Map<String, Object> extra = new LinkedHashMap<>();
        extra.put("source", "gcbridge");
        extra.put("app_version", MainActivity.appVersion(this));
        extra.put("device", Build.MANUFACTURER + " " + Build.MODEL);
        extra.put("android", Build.VERSION.RELEASE + " (API " + Build.VERSION.SDK_INT + ")");
        Map<String, Object> meta = new LinkedHashMap<>();
        meta.put("button_capture", KeyCaptureService.isConnected());
        try {
            hub.startRecording(f, extra, meta);
            MainActivity.log("Recording to " + f.getName());
        } catch (IOException e) {
            MainActivity.log("ERROR can't record: " + e);
        }
    }

    private void stopRecording() {
        if (!hub.isRecording()) {
            return;
        }
        int rows = hub.recordingRows();
        File f = hub.stopRecording();
        if (f != null) {
            MainActivity.log("Recording saved: " + f.getName() + " (" + rows + " rows, "
                    + f.length() + " bytes)");
        }
    }

    // --- overlay ------------------------------------------------------------------------------

    private void showOverlay() {
        if (overlay == null) {
            overlay = new OverlayWindow(this, prefs);
        }
        if (overlay.isShown()) {
            return;
        }
        overlayLayoutName = null;
        updateOverlayLayout();
        if (!overlay.show()) {
            MainActivity.log(OverlayWindow.canDraw(this)
                    ? "ERROR the overlay window could not be created"
                    : "Overlay needs the \"Display over other apps\" permission");
        } else {
            InputHub.Device d = hub.activeDevice();
            if (d != null) {
                overlay.setState(d.state);
            }
        }
    }

    private void hideOverlay() {
        if (overlay != null) {
            overlay.hide();
        }
    }

    /** Picks the layout for the preference / the active controller's family. */
    private void updateOverlayLayout() {
        if (overlay == null) {
            return;
        }
        InputHub.Device d = hub.activeDevice();
        String family = d != null ? d.family : Pad.FAMILY_GENERIC;
        String pref = prefs.getString(PREF_LAYOUT, "auto");
        if (family.equals(overlayFamily) && pref.equals(overlayLayoutName)) {
            return;
        }
        Layout l = LayoutStore.pick(this, pref, family);
        overlayFamily = family;
        overlayLayoutName = pref;
        overlay.setLayout(l);
    }

    // --- USB capture --------------------------------------------------------------------------

    @SuppressWarnings("deprecation")
    private void startUsbCapture(Intent intent) {
        if (usbCapture != null && usbCapture.isRunning()) {
            MainActivity.log("USB capture is already running");
            return;
        }
        UsbManager usb = getSystemService(UsbManager.class);
        UsbDevice device = null;
        if (intent != null) {
            device = Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
                    ? intent.getParcelableExtra(EXTRA_USB_DEVICE, UsbDevice.class)
                    : intent.getParcelableExtra(EXTRA_USB_DEVICE);
        }
        if (device == null) {
            device = Switch2Usb.findController(usb);
        }
        if (device == null) {
            MainActivity.log("USB capture: no Switch 2 GameCube / Pro controller on USB");
            return;
        }
        if (!usb.hasPermission(device)) {
            MainActivity.log("USB capture: no USB permission (tap Start controller first and allow access)");
            return;
        }
        usbCapture = new Switch2Usb.Capture(usb, device, hub, MainActivity::log);
        usbCapture.start();
    }

    private void stopUsbCapture() {
        if (usbCapture != null) {
            usbCapture.stop();
            usbCapture = null;
        }
    }

    // --- Bluetooth ----------------------------------------------------------------------------

    private void startBle(String address) {
        if (ble == null) {
            ble = new Switch2Ble(this, hub, MainActivity::log);
        }
        if (!Switch2Ble.hasPermissions(this)) {
            MainActivity.log("Bluetooth: permission not granted");
            return;
        }
        ble.start(address);
    }

    private void stopBle() {
        if (ble != null) {
            ble.stop();
            ble = null;
        }
    }

    // --- hub listener (any thread) -------------------------------------------------------------

    @Override
    public void onInput(InputHub.Device device) {
        OverlayWindow o = overlay;
        if (o == null || !o.isShown()) {
            return;
        }
        // PadView copies the state and coalesces redraws itself; the layout switch (a
        // different controller became active) goes through the main thread.
        o.setState(device.state);
        if (!device.family.equals(overlayFamily) && !overlayUpdatePending) {
            overlayUpdatePending = true;
            main.post(() -> {
                overlayUpdatePending = false;
                updateOverlayLayout();
            });
        }
    }

    @Override
    public void onStatus() {
        main.post(() -> {
            updateOverlayLayout();
            refreshNotification(false);
        });
    }

    private void tick() {
        hub.tick();
        refreshNotification(false);
        if (usbCapture != null && !usbCapture.isRunning()) {
            usbCapture = null;
        }
        if (ble != null && !ble.isActive() && !hub.isRecording()) {
            // a scan that found nothing: keep the object for its status text, but don't
            // keep the service alive for it
            String st = ble.status();
            if (st.startsWith("no controller") || st.startsWith("Bluetooth is off")
                    || st.startsWith("missing")) {
                ble = null;
                MainActivity.log("Bluetooth: " + st);
            }
        }
        if (!maybeStop()) {
            main.postDelayed(ticker, TICK_MS);
        }
    }

    // --- notification --------------------------------------------------------------------------

    private void createChannel() {
        NotificationManager nm = getSystemService(NotificationManager.class);
        NotificationChannel ch = new NotificationChannel(CHANNEL, getString(R.string.channel_capture),
                NotificationManager.IMPORTANCE_LOW);
        ch.setDescription(getString(R.string.channel_capture_desc));
        nm.createNotificationChannel(ch);
    }

    private String statusText() {
        StringBuilder sb = new StringBuilder();
        if (hub.isRecording()) {
            long s = hub.recordingDurationNs() / 1_000_000_000L;
            sb.append(String.format(Locale.ROOT, "Recording %d:%02d, %d rows", s / 60, s % 60,
                    hub.recordingRows()));
        } else {
            sb.append("Not recording");
        }
        if (overlay != null && overlay.isShown()) {
            sb.append(" · overlay");
        }
        if (usbCapture != null && usbCapture.isRunning()) {
            sb.append(" · USB ").append(usbCapture.status());
        }
        if (ble != null && ble.isActive()) {
            sb.append(" · BLE ").append(ble.status());
        }
        InputHub.Device d = hub.activeDevice();
        if (d != null) {
            sb.append(" · ").append(d.shortName());
        }
        return sb.toString();
    }

    private void refreshNotification(boolean force) {
        long now = System.currentTimeMillis();
        String text = statusText();
        if (!force && now - lastNotificationMs < TICK_MS && text.equals(lastNotificationText)) {
            return;
        }
        lastNotificationMs = now;
        lastNotificationText = text;
        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.notify(NOTIFICATION_ID, buildNotification(text));
    }

    private Notification buildNotification(String text) {
        Intent open = new Intent(this, MainActivity.class)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent openPi = PendingIntent.getActivity(this, 0, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder b = new Notification.Builder(this, CHANNEL)
                .setSmallIcon(R.drawable.ic_stat_pad)
                .setContentTitle(getString(R.string.app_name))
                .setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setContentIntent(openPi)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setVisibility(Notification.VISIBILITY_PUBLIC);
        if (hub.isRecording()) {
            b.addAction(action(R.string.action_marker, ACTION_MARKER, 1));
            b.addAction(action(R.string.action_stop_recording, ACTION_RECORD_STOP, 2));
        } else {
            b.addAction(action(R.string.action_record, ACTION_RECORD_START, 3));
        }
        b.addAction(action(R.string.action_stop_all, ACTION_STOP_ALL, 4));
        return b.build();
    }

    private Notification.Action action(int label, String action, int code) {
        PendingIntent pi = PendingIntent.getService(this, code, intent(this, action),
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Action.Builder(null, getString(label), pi).build();
    }

}
