package com.controllerlog.gcbridge;

import android.Manifest;
import android.annotation.SuppressLint;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothGatt;
import android.bluetooth.BluetoothGattCallback;
import android.bluetooth.BluetoothGattCharacteristic;
import android.bluetooth.BluetoothGattDescriptor;
import android.bluetooth.BluetoothGattService;
import android.bluetooth.BluetoothManager;
import android.bluetooth.BluetoothProfile;
import android.bluetooth.le.BluetoothLeScanner;
import android.bluetooth.le.ScanCallback;
import android.bluetooth.le.ScanFilter;
import android.bluetooth.le.ScanResult;
import android.bluetooth.le.ScanSettings;
import android.content.Context;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;

import java.util.ArrayDeque;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;

/**
 * EXPERIMENTAL: reads a Switch 2 GameCube / Pro controller over Bluetooth LE, no pairing, the
 * way {@code controllerlog/input/switch2_ble.py} does on the PC (that reader has never been
 * run against a controller either). Android never sees a gamepad this way: input goes to the
 * hub (recording + overlay) only.
 *
 * <p>Sequence: scan for manufacturer data 0x0553 -> connect (LE, no bonding) -> MTU 247 ->
 * high connection priority -> discover -> notifications on the report 0x05 characteristic and
 * the command responses -> player LED, feature commands, rate descriptor -> calibration reads.
 * Every GATT operation goes through a queue (Android allows one at a time).
 */
// Permissions are checked in hasPermissions() before scan()/connect, and every Bluetooth call
// also catches SecurityException; lint can't follow that across methods.
@SuppressLint("MissingPermission")
final class Switch2Ble {

    interface Log {
        void line(String message);
    }

    private static final long SCAN_TIMEOUT_MS = 30_000;
    private static final long OP_TIMEOUT_MS = 3_000;
    private static final long RECONNECT_DELAY_MS = 1_500;
    private static final int MTU = 247;
    private static final UUID CCCD = UUID.fromString(Switch2Protocol.BLE_CCCD);

    private final Context ctx;
    private final InputHub hub;
    private final Log log;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final ArrayDeque<Runnable> ops = new ArrayDeque<>();
    private boolean opInFlight;
    private Runnable opTimeout;

    private BluetoothLeScanner scanner;
    private ScanCallback scanCallback;
    private BluetoothGatt gatt;
    private String wantedAddress;
    private volatile boolean active;
    private volatile String status = "off";
    private String model;
    private int productId;
    private String address;
    private volatile Switch2Protocol.Calibration cal;
    private InputHub.Device device;
    private String deviceKey;
    private final byte[] report = new byte[Switch2Protocol.REPORT_LEN];
    private final int[] buttons = new int[Pad.NUM_BUTTONS];
    private final int[] axes = new int[Pad.NUM_AXES];
    private final Map<Integer, byte[]> flash = new LinkedHashMap<>();
    private BluetoothGattCharacteristic commandChar;
    private long reports;
    private long windowStartNs;
    private int windowCount;
    private volatile double rateHz;
    private boolean shortReportWarned;

    Switch2Ble(Context ctx, InputHub hub, Log log) {
        this.ctx = ctx.getApplicationContext();
        this.hub = hub;
        this.log = log;
        this.scanTimeout = this::onScanTimeout;
    }

    /** Runtime permissions needed on this Android version. */
    static String[] permissions() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            return new String[]{Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT};
        }
        return new String[]{Manifest.permission.ACCESS_FINE_LOCATION};
    }

    static boolean hasPermissions(Context ctx) {
        for (String p : permissions()) {
            if (ctx.checkSelfPermission(p) != PackageManager.PERMISSION_GRANTED) {
                return false;
            }
        }
        return true;
    }

    boolean isActive() {
        return active;
    }

    String status() {
        return status;
    }

    double rateHz() {
        return rateHz;
    }

    /** Scans for a controller in pairing mode (or {@code address}) and keeps it connected. */
    synchronized void start(String address) {
        if (active) {
            return;
        }
        active = true;
        wantedAddress = address == null || address.isEmpty() ? null : address.toUpperCase(Locale.ROOT);
        handler.post(this::scan);
    }

    synchronized void stop() {
        active = false;
        handler.post(() -> {
            stopScan();
            closeGatt("stopped");
            status = "off";
        });
    }

    // --- scanning ---------------------------------------------------------------------------

    private void scan() {
        if (!active) {
            return;
        }
        BluetoothManager bm = ctx.getSystemService(BluetoothManager.class);
        BluetoothAdapter adapter = bm == null ? null : bm.getAdapter();
        if (adapter == null || !adapter.isEnabled()) {
            status = "Bluetooth is off";
            log.line("BLE: Bluetooth is off");
            active = false;
            return;
        }
        if (!hasPermissions(ctx)) {
            status = "missing Bluetooth permission";
            log.line("BLE: missing permission");
            active = false;
            return;
        }
        scanner = adapter.getBluetoothLeScanner();
        if (scanner == null) {
            status = "no LE scanner";
            active = false;
            return;
        }
        scanCallback = new ScanCallback() {
            @Override
            public void onScanResult(int callbackType, ScanResult result) {
                handler.post(() -> onAdvertisement(result));
            }

            @Override
            public void onScanFailed(int errorCode) {
                handler.post(() -> {
                    status = "scan failed (" + errorCode + ")";
                    log.line("BLE: scan failed, error " + errorCode);
                    scanner = null;
                    scanCallback = null;
                    scheduleRetry();
                });
            }
        };
        ScanFilter filter = new ScanFilter.Builder()
                .setManufacturerData(Switch2Protocol.BLE_COMPANY_ID, new byte[0]).build();
        ScanSettings settings = new ScanSettings.Builder()
                .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build();
        try {
            scanner.startScan(Collections.singletonList(filter), settings, scanCallback);
        } catch (SecurityException | IllegalStateException e) {
            status = "scan failed: " + e.getMessage();
            log.line("BLE: " + status);
            active = false;
            return;
        }
        status = "scanning (hold the controller's SYNC button)";
        log.line("BLE: scanning for a Switch 2 controller advertising company 0x0553"
                + (wantedAddress != null ? " at " + wantedAddress : " in pairing mode"));
        handler.postDelayed(scanTimeout, SCAN_TIMEOUT_MS);
    }

    private final Runnable scanTimeout;

    private void onScanTimeout() {
        if (scanner != null && gatt == null) {
            stopScan();
            status = "no controller found; hold SYNC until the LEDs run, then try again";
            log.line("BLE: " + status);
            active = false;
        }
    }

    private void stopScan() {
        handler.removeCallbacks(scanTimeout);
        if (scanner != null && scanCallback != null) {
            try {
                scanner.stopScan(scanCallback);
            } catch (SecurityException | IllegalStateException e) {
                android.util.Log.w(MainActivity.TAG, "BLE stopScan: " + e);
            }
        }
        scanner = null;
        scanCallback = null;
    }

    @SuppressWarnings("deprecation")   // connectGatt(Context, boolean, callback, transport)
    private void onAdvertisement(ScanResult result) {
        if (!active || gatt != null || result.getScanRecord() == null) {
            return;
        }
        byte[] md = result.getScanRecord().getManufacturerSpecificData(Switch2Protocol.BLE_COMPANY_ID);
        Switch2Protocol.Advertisement adv = Switch2Protocol.parseManufacturerData(md);
        if (adv == null) {
            return;
        }
        BluetoothDevice dev = result.getDevice();
        String addr = dev.getAddress().toUpperCase(Locale.ROOT);
        String what = String.format(Locale.ROOT, "%s PID %04X (%s) %d dBm %s", addr, adv.productId,
                adv.model == null ? "unknown model" : adv.model, result.getRssi(),
                adv.pairingMode ? "pairing mode" : adv.wake ? "wake" : "reconnecting to " + adv.hostAddress);
        boolean wanted = wantedAddress != null ? addr.equals(wantedAddress) : adv.pairingMode;
        if (!wanted || Switch2Protocol.modelKey(adv.productId) == null) {
            log.line("BLE: seen " + what + (wanted ? " (unsupported model)" : ""));
            return;
        }
        log.line("BLE: connecting to " + what);
        stopScan();
        model = Switch2Protocol.modelKey(adv.productId);
        productId = adv.productId;
        address = addr;
        cal = Switch2Protocol.Calibration.defaults(model);
        flash.clear();
        status = "connecting to " + addr;
        try {
            // No bonding: Switch 2 controllers drop the link on an SMP pairing attempt.
            gatt = dev.connectGatt(ctx, false, gattCallback, BluetoothDevice.TRANSPORT_LE);
        } catch (SecurityException e) {
            status = "connect failed: " + e.getMessage();
            log.line("BLE: " + status);
            active = false;
        }
    }

    private void scheduleRetry() {
        if (active) {
            handler.postDelayed(this::scan, RECONNECT_DELAY_MS);
        }
    }

    // --- GATT -------------------------------------------------------------------------------

    private final BluetoothGattCallback gattCallback = new BluetoothGattCallback() {
        @Override
        public void onConnectionStateChange(BluetoothGatt g, int statusCode, int newState) {
            handler.post(() -> {
                if (g != gatt) {
                    return;
                }
                if (newState == BluetoothProfile.STATE_CONNECTED && statusCode == BluetoothGatt.GATT_SUCCESS) {
                    status = "connected; negotiating";
                    log.line("BLE: connected to " + address);
                    boolean ok = false;
                    try {
                        ok = g.requestMtu(MTU);
                    } catch (SecurityException e) {
                        log.line("BLE: requestMtu: " + e);
                    }
                    if (!ok) {
                        discover();
                    }
                } else {
                    log.line("BLE: disconnected (status " + statusCode + ")");
                    closeGatt("disconnected (status " + statusCode + ")");
                    scheduleRetry();
                }
            });
        }

        @Override
        public void onMtuChanged(BluetoothGatt g, int mtu, int statusCode) {
            handler.post(() -> {
                if (g != gatt) {
                    return;
                }
                log.line("BLE: MTU " + mtu + (statusCode == BluetoothGatt.GATT_SUCCESS ? "" : " (status " + statusCode + ")")
                        + (mtu < Switch2Protocol.REPORT_LEN + 2 ? " - too small for 63-byte reports" : ""));
                try {
                    g.requestConnectionPriority(BluetoothGatt.CONNECTION_PRIORITY_HIGH);
                } catch (SecurityException e) {
                    log.line("BLE: requestConnectionPriority: " + e);
                }
                discover();
            });
        }

        @Override
        public void onServicesDiscovered(BluetoothGatt g, int statusCode) {
            handler.post(() -> {
                if (g != gatt) {
                    return;
                }
                if (statusCode != BluetoothGatt.GATT_SUCCESS) {
                    log.line("BLE: service discovery failed (" + statusCode + ")");
                    closeGatt("discovery failed");
                    scheduleRetry();
                    return;
                }
                setUp(g);
            });
        }

        @Override
        public void onDescriptorWrite(BluetoothGatt g, BluetoothGattDescriptor d, int statusCode) {
            handler.post(() -> {
                if (statusCode != BluetoothGatt.GATT_SUCCESS) {
                    log.line("BLE: descriptor write " + d.getUuid() + " failed (" + statusCode + ")");
                }
                opDone();
            });
        }

        @Override
        public void onCharacteristicWrite(BluetoothGatt g, BluetoothGattCharacteristic c, int statusCode) {
            handler.post(() -> {
                if (statusCode != BluetoothGatt.GATT_SUCCESS) {
                    log.line("BLE: write failed (" + statusCode + ")");
                }
                opDone();
            });
        }

        @Override
        @SuppressWarnings("deprecation")
        public void onCharacteristicChanged(BluetoothGatt g, BluetoothGattCharacteristic c) {
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
                onNotification(c.getUuid(), c.getValue(), System.nanoTime());
            }
        }

        @Override
        public void onCharacteristicChanged(BluetoothGatt g, BluetoothGattCharacteristic c, byte[] value) {
            onNotification(c.getUuid(), value, System.nanoTime());
        }
    };

    private void discover() {
        status = "discovering services";
        try {
            if (gatt != null && !gatt.discoverServices()) {
                log.line("BLE: discoverServices refused");
            }
        } catch (SecurityException e) {
            log.line("BLE: discoverServices: " + e);
        }
    }

    private void setUp(BluetoothGatt g) {
        BluetoothGattService svc = g.getService(UUID.fromString(Switch2Protocol.BLE_SERVICE));
        if (svc == null) {
            log.line("BLE: service " + Switch2Protocol.BLE_SERVICE + " not found; services: " + g.getServices().size());
            for (BluetoothGattService s : g.getServices()) {
                log.line("BLE:   " + s.getUuid());
            }
            closeGatt("no Switch 2 service");
            return;
        }
        BluetoothGattCharacteristic input = svc.getCharacteristic(UUID.fromString(Switch2Protocol.BLE_INPUT_COMMON));
        BluetoothGattCharacteristic response = svc.getCharacteristic(UUID.fromString(Switch2Protocol.BLE_COMMAND_RESPONSE));
        String extUuid = Switch2Protocol.bleModelResponseUuid(productId);
        BluetoothGattCharacteristic response2 = extUuid == null ? null : svc.getCharacteristic(UUID.fromString(extUuid));
        String modelInputUuid = Switch2Protocol.bleModelInputUuid(productId);
        BluetoothGattCharacteristic modelInput = modelInputUuid == null ? null : svc.getCharacteristic(UUID.fromString(modelInputUuid));
        commandChar = svc.getCharacteristic(UUID.fromString(Switch2Protocol.BLE_COMMAND));
        if (input == null) {
            log.line("BLE: input characteristic missing");
            closeGatt("no input characteristic");
            return;
        }
        log.line("BLE: service found; enabling notifications");
        enableNotifications(g, input);
        if (response != null) {
            enableNotifications(g, response);
        }
        if (response2 != null) {
            enableNotifications(g, response2);
        }
        if (commandChar != null) {
            queue(() -> writeCommand(g, Switch2Protocol.buildPlayerLeds(1)));
            queue(() -> writeCommand(g, Switch2Protocol.buildFeatureCommand(
                    Switch2Protocol.SUB_FEATURE_SET_MASK, Switch2Protocol.DEFAULT_FEATURES)));
            queue(() -> writeCommand(g, Switch2Protocol.buildFeatureCommand(
                    Switch2Protocol.SUB_FEATURE_ENABLE, Switch2Protocol.DEFAULT_FEATURES)));
        }
        if (modelInput != null) {
            BluetoothGattDescriptor rate = modelInput.getDescriptor(UUID.fromString(Switch2Protocol.BLE_RATE_DESCRIPTOR));
            if (rate != null) {
                queue(() -> writeDescriptor(g, rate, Switch2Protocol.BLE_RATE_VALUE));
            }
        }
        if (commandChar != null) {
            for (int addr : new int[]{Switch2Protocol.ADDR_DEVICE_INFO, Switch2Protocol.ADDR_FACTORY_LEFT,
                    Switch2Protocol.ADDR_FACTORY_RIGHT, Switch2Protocol.ADDR_USER_LEFT,
                    Switch2Protocol.ADDR_USER_RIGHT, Switch2Protocol.ADDR_TRIGGER_ZERO}) {
                int len = addr == Switch2Protocol.ADDR_TRIGGER_ZERO ? 2 : 0x40;
                queue(() -> writeCommand(g, Switch2Protocol.buildMemoryRead(addr, len)));
            }
        }
        queue(() -> {
            applyCalibration();
            opDone();
        });
        registerDevice();
        status = "connected to " + address + " (" + model + ")";
    }

    private void registerDevice() {
        Map<String, Object> extra = new LinkedHashMap<>();
        extra.put("transport", "ble");
        extra.put("report", "0x05");
        extra.put("reader", "gcbridge");
        deviceKey = "ble:" + address;
        device = hub.connect(deviceKey, Switch2Protocol.modelName(productId), "gcbridge-ble",
                Switch2Protocol.MODEL_GAMECUBE.equals(model) ? Pad.FAMILY_GAMECUBE : Pad.FAMILY_SWITCH,
                Switch2Protocol.NINTENDO_VID, productId, "wireless", extra);
    }

    private void applyCalibration() {
        Switch2Protocol.Calibration c = Switch2Protocol.readCalibration(flash::get, model);
        cal = c;
        log.line("BLE: calibration " + c.source + (c.serial.isEmpty() ? "" : ", serial " + c.serial));
    }

    private void onNotification(UUID uuid, byte[] value, long tNs) {
        if (value == null) {
            return;
        }
        if (uuid.toString().equalsIgnoreCase(Switch2Protocol.BLE_INPUT_COMMON)) {
            if (value.length < Switch2Protocol.REPORT_LEN - 1) {
                if (!shortReportWarned) {
                    shortReportWarned = true;
                    log.line("BLE: input notification is only " + value.length + " bytes (need 63): "
                            + "the ATT MTU is too small; sticks/triggers can't be decoded");
                }
                return;
            }
            InputHub.Device d = device;
            Switch2Protocol.Calibration c = cal;
            if (d == null || c == null) {
                return;
            }
            synchronized (report) {
                report[0] = (byte) Switch2Protocol.FORMAT_NINTENDO;
                System.arraycopy(value, 0, report, 1, Switch2Protocol.REPORT_LEN - 1);
                if (Switch2Protocol.parseInputReport(model, report, 0, Switch2Protocol.REPORT_LEN, c,
                        Switch2Protocol.DEFAULT_DEADZONE, buttons, axes)) {
                    hub.state(d, buttons, axes, tNs);
                }
                reports++;
                windowCount++;
                if (windowStartNs == 0) {
                    windowStartNs = tNs;
                } else if (tNs - windowStartNs >= 1_000_000_000L) {
                    rateHz = windowCount * 1e9 / (tNs - windowStartNs);
                    windowStartNs = tNs;
                    windowCount = 0;
                }
            }
            return;
        }
        Switch2Protocol.CommandResponse r = Switch2Protocol.parseCommandResponse(value);
        if (r == null) {
            handler.post(() -> log.line("BLE: notification " + uuid + " " + Switch2Protocol.hex(value, 0, Math.min(value.length, 24))));
            return;
        }
        if (r.cmd == Switch2Protocol.CMD_FLASH) {
            int addr = Switch2Protocol.memoryReadAddress(r);
            byte[] data = Switch2Protocol.memoryReadData(r);
            if (addr >= 0 && data != null) {
                synchronized (flash) {
                    flash.put(addr, data);
                }
            }
        }
        handler.post(() -> log.line(String.format(Locale.ROOT, "BLE: response cmd %02X/%02X ack %02X, %d bytes",
                r.cmd, r.sub, r.ack, r.payload.length)));
    }

    // --- one-at-a-time GATT operations ---------------------------------------------------------

    private void queue(Runnable op) {
        ops.add(op);
        next();
    }

    private void next() {
        if (opInFlight || ops.isEmpty() || gatt == null) {
            return;
        }
        opInFlight = true;
        Runnable op = ops.poll();
        opTimeout = () -> {
            log.line("BLE: operation timed out");
            opDone();
        };
        handler.postDelayed(opTimeout, OP_TIMEOUT_MS);
        op.run();
    }

    private void opDone() {
        if (opTimeout != null) {
            handler.removeCallbacks(opTimeout);
            opTimeout = null;
        }
        opInFlight = false;
        next();
    }

    private void enableNotifications(BluetoothGatt g, BluetoothGattCharacteristic c) {
        queue(() -> {
            try {
                if (!g.setCharacteristicNotification(c, true)) {
                    log.line("BLE: setCharacteristicNotification failed for " + c.getUuid());
                }
            } catch (SecurityException e) {
                log.line("BLE: " + e);
            }
            BluetoothGattDescriptor cccd = c.getDescriptor(CCCD);
            if (cccd == null) {
                log.line("BLE: no CCCD on " + c.getUuid());
                opDone();
                return;
            }
            writeDescriptor(g, cccd, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE);
        });
    }

    @SuppressWarnings("deprecation")
    private void writeDescriptor(BluetoothGatt g, BluetoothGattDescriptor d, byte[] value) {
        boolean ok;
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                ok = g.writeDescriptor(d, value) == BluetoothGatt.GATT_SUCCESS;
            } else {
                d.setValue(value);
                ok = g.writeDescriptor(d);
            }
        } catch (SecurityException e) {
            log.line("BLE: writeDescriptor: " + e);
            ok = false;
        }
        if (!ok) {
            log.line("BLE: writeDescriptor refused for " + d.getUuid());
            opDone();
        }
    }

    @SuppressWarnings("deprecation")
    private void writeCommand(BluetoothGatt g, byte[] packet) {
        BluetoothGattCharacteristic c = commandChar;
        if (c == null) {
            opDone();
            return;
        }
        boolean ok;
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                ok = g.writeCharacteristic(c, packet, BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE)
                        == BluetoothGatt.GATT_SUCCESS;
            } else {
                c.setWriteType(BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE);
                c.setValue(packet);
                ok = g.writeCharacteristic(c);
            }
        } catch (SecurityException e) {
            log.line("BLE: writeCharacteristic: " + e);
            ok = false;
        }
        if (!ok) {
            log.line("BLE: command write refused: " + Switch2Protocol.hex(packet));
            opDone();
        }
    }

    private void closeGatt(String why) {
        ops.clear();
        opInFlight = false;
        if (opTimeout != null) {
            handler.removeCallbacks(opTimeout);
            opTimeout = null;
        }
        if (gatt != null) {
            try {
                gatt.disconnect();
                gatt.close();
            } catch (SecurityException e) {
                android.util.Log.w(MainActivity.TAG, "BLE close: " + e);
            }
            gatt = null;
        }
        if (deviceKey != null) {
            hub.disconnect(deviceKey);
            deviceKey = null;
            device = null;
        }
        commandChar = null;
        status = why;
    }

    long reports() {
        return reports;
    }

    static boolean hasBluetoothLe(Context ctx) {
        return ctx.getPackageManager().hasSystemFeature(PackageManager.FEATURE_BLUETOOTH_LE);
    }

    static List<String> missingPermissions(Context ctx) {
        List<String> out = new java.util.ArrayList<>();
        for (String p : permissions()) {
            if (ctx.checkSelfPermission(p) != PackageManager.PERMISSION_GRANTED) {
                out.add(p);
            }
        }
        return out;
    }
}
