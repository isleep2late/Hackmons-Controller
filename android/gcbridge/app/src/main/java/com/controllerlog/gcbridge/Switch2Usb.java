package com.controllerlog.gcbridge;

import android.hardware.usb.UsbConstants;
import android.hardware.usb.UsbDevice;
import android.hardware.usb.UsbDeviceConnection;
import android.hardware.usb.UsbEndpoint;
import android.hardware.usb.UsbInterface;
import android.hardware.usb.UsbManager;
import android.os.SystemClock;

import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;
import java.util.TreeMap;

/**
 * Talks to the controller's vendor interface (interface 1, bulk) with the Android USB host API.
 * The HID interface (interface 0) is left to the kernel, except in {@link #peekHid}, which
 * the user has to trigger explicitly. All methods block; call them off the main thread.
 */
final class Switch2Usb {

    interface Log {
        void line(String message);
    }

    private static final int SEND_TIMEOUT_MS = 1000;
    private static final int REPLY_TIMEOUT_MS = 200;

    private Switch2Usb() {
    }

    /** First plugged-in Switch 2 GameCube / Pro controller, or null. */
    static UsbDevice findController(UsbManager usb) {
        for (UsbDevice d : usb.getDeviceList().values()) {
            if (Switch2Protocol.isSupported(d.getVendorId(), d.getProductId())) {
                return d;
            }
        }
        return null;
    }

    static boolean isAttached(UsbManager usb, UsbDevice device) {
        for (UsbDevice d : usb.getDeviceList().values()) {
            if (d.getDeviceName().equals(device.getDeviceName())) {
                return true;
            }
        }
        return false;
    }

    static String describeDevice(UsbDevice d) {
        return String.format(Locale.ROOT, "%04x:%04x %s", d.getVendorId(), d.getProductId(),
                d.getDeviceName());
    }

    /** Interface / endpoint layout as Android sees it. */
    static void logLayout(UsbDevice d, Log log) {
        log.line(String.format(Locale.ROOT, "%s, %d interface(s), manufacturer=%s product=%s",
                describeDevice(d), d.getInterfaceCount(), d.getManufacturerName(),
                d.getProductName()));
        for (int i = 0; i < d.getInterfaceCount(); i++) {
            UsbInterface intf = d.getInterface(i);
            StringBuilder sb = new StringBuilder(String.format(Locale.ROOT,
                    "  interface %d alt %d: class 0x%02x sub 0x%02x proto 0x%02x",
                    intf.getId(), intf.getAlternateSetting(), intf.getInterfaceClass(),
                    intf.getInterfaceSubclass(), intf.getInterfaceProtocol()));
            for (int e = 0; e < intf.getEndpointCount(); e++) {
                UsbEndpoint ep = intf.getEndpoint(e);
                sb.append(String.format(Locale.ROOT, "; ep 0x%02x %s %s %dB",
                        ep.getAddress(),
                        ep.getDirection() == UsbConstants.USB_DIR_IN ? "IN" : "OUT",
                        endpointType(ep.getType()), ep.getMaxPacketSize()));
            }
            log.line(sb.toString());
        }
    }

    private static String endpointType(int type) {
        switch (type) {
            case UsbConstants.USB_ENDPOINT_XFER_BULK:
                return "bulk";
            case UsbConstants.USB_ENDPOINT_XFER_INT:
                return "interrupt";
            case UsbConstants.USB_ENDPOINT_XFER_ISOC:
                return "isochronous";
            default:
                return "control";
        }
    }

    /**
     * First interface (alternate setting 0) of {@code interfaceClass} that has an endpoint of
     * {@code type} in each of {@code directions}, preferring interface number {@code preferredId}
     * (where the controller has it: 1 for the vendor command interface, 0 for HID).
     */
    static UsbInterface findInterface(UsbDevice d, int interfaceClass, int preferredId,
                                              int type, int... directions) {
        UsbInterface fallback = null;
        for (int i = 0; i < d.getInterfaceCount(); i++) {
            UsbInterface intf = d.getInterface(i);
            if (intf.getInterfaceClass() != interfaceClass || intf.getAlternateSetting() != 0) {
                continue;
            }
            boolean hasAll = true;
            for (int dir : directions) {
                hasAll &= findEndpoint(intf, type, dir) != null;
            }
            if (!hasAll) {
                continue;
            }
            if (intf.getId() == preferredId) {
                return intf;
            }
            if (fallback == null) {
                fallback = intf;
            }
        }
        return fallback;
    }

    static UsbEndpoint findEndpoint(UsbInterface intf, int type, int direction) {
        for (int e = 0; e < intf.getEndpointCount(); e++) {
            UsbEndpoint ep = intf.getEndpoint(e);
            if (ep.getType() == type && ep.getDirection() == direction) {
                return ep;
            }
        }
        return null;
    }

    /**
     * Sends the init sequence (with {@code format} as the report format) and player LED 1 over
     * the vendor interface, then releases it and closes the connection.
     *
     * @return true if every command was sent
     */
    static boolean startController(UsbManager usb, UsbDevice device, int format, Log log) {
        log.line("Start: " + Switch2Protocol.modelName(device.getProductId()) + ", report format "
                + Switch2Protocol.formatName(format));
        logLayout(device, log);
        UsbInterface cmdIntf = findInterface(device, UsbConstants.USB_CLASS_VENDOR_SPEC, 1,
                UsbConstants.USB_ENDPOINT_XFER_BULK, UsbConstants.USB_DIR_OUT,
                UsbConstants.USB_DIR_IN);
        if (cmdIntf == null) {
            log.line("ERROR: no vendor-class (0xFF) interface with bulk IN and OUT endpoints; is "
                    + "this really a Switch 2 controller?");
            return false;
        }
        UsbEndpoint out = findEndpoint(cmdIntf, UsbConstants.USB_ENDPOINT_XFER_BULK,
                UsbConstants.USB_DIR_OUT);
        UsbEndpoint in = findEndpoint(cmdIntf, UsbConstants.USB_ENDPOINT_XFER_BULK,
                UsbConstants.USB_DIR_IN);
        if (cmdIntf.getId() != 1) {
            log.line("WARNING: using interface " + cmdIntf.getId() + " as the command interface "
                    + "(expected interface 1)");
        }
        if (!usb.hasPermission(device)) {
            log.line("ERROR: no USB permission for this device (tap Start and allow access)");
            return false;
        }
        UsbDeviceConnection conn = usb.openDevice(device);
        if (conn == null) {
            log.line(isAttached(usb, device)
                    ? "ERROR: openDevice failed (permission revoked or device busy)"
                    : "ERROR: device is gone (unplugged)");
            return false;
        }
        try {
            // force=true only detaches a kernel driver from THIS interface; interface 0 (HID)
            // stays with the kernel.
            if (!conn.claimInterface(cmdIntf, true)) {
                log.line("ERROR: claimInterface(" + cmdIntf.getId() + ") failed; another app may "
                        + "be using the controller's command interface");
                return false;
            }
            log.line(String.format(Locale.ROOT, "Claimed interface %d (bulk OUT 0x%02x, IN 0x%02x)",
                    cmdIntf.getId(), out.getAddress(), in.getAddress()));
            try {
                return sendInit(usb, device, conn, out, in, format, log);
            } finally {
                boolean released = conn.releaseInterface(cmdIntf);
                log.line("Released interface " + cmdIntf.getId() + (released ? "" : " (failed)"));
            }
        } finally {
            conn.close();
            log.line("Connection closed");
        }
    }

    static boolean sendInit(UsbManager usb, UsbDevice device, UsbDeviceConnection conn,
                                    UsbEndpoint out, UsbEndpoint in, int format, Log log) {
        // Optional: the serial number from flash, as a sanity check of the command channel.
        byte[] reply = command(conn, out, in,
                Switch2Protocol.flashReadCommand(Switch2Protocol.SERIAL_FLASH_ADDRESS),
                Switch2Protocol.FLASH_REPLY_LEN, "flash read 0x13000", log);
        if (reply != null && reply != SEND_FAILED) {
            String serial = Switch2Protocol.parseSerial(reply, reply.length);
            log.line(serial != null ? "Serial: \"" + serial + "\""
                    : "Serial: reply too short (" + reply.length + " bytes)");
        } else if (!isAttached(usb, device)) {
            log.line("ERROR: device is gone (unplugged)");
            return false;
        } else {
            log.line("Serial: not read (continuing with the init anyway)");
        }

        byte[][] seq = Switch2Protocol.initSequence(format);
        int sent = 0;
        for (int i = 0; i < seq.length; i++) {
            String label = String.format(Locale.ROOT, "init %d/%d %s", i + 1, seq.length,
                    Switch2Protocol.initLabel(i));
            if (i == Switch2Protocol.REPORT_FORMAT_COMMAND) {
                label += " = " + Switch2Protocol.formatName(format);
            }
            if (commandSent(conn, out, in, seq[i], label, log)) {
                sent++;
            } else if (!isAttached(usb, device)) {
                log.line("ERROR: device is gone (unplugged) after " + sent + " commands");
                return false;
            }
        }
        commandSent(conn, out, in, Switch2Protocol.ledCommand(1), "player LED 1", log);
        if (sent == seq.length) {
            log.line("Init sent (" + sent + "/" + seq.length + " commands). The controller should "
                    + "now stream " + Switch2Protocol.formatName(format) + " reports on its HID "
                    + "interface at 250 Hz.");
            return true;
        }
        log.line("WARNING: only " + sent + "/" + seq.length + " init commands were sent");
        return false;
    }

    private static boolean commandSent(UsbDeviceConnection conn, UsbEndpoint out, UsbEndpoint in,
                                       byte[] cmd, String label, Log log) {
        return command(conn, out, in, cmd, Switch2Protocol.PACKET_SIZE, label, log) != SEND_FAILED;
    }

    static final byte[] SEND_FAILED = new byte[0];

    /**
     * Bulk OUT the command, then read the reply in 64-byte packets until a short packet,
     * {@code replyLen} bytes, or a timeout (like switch2_usb.UsbTransport.command).
     *
     * @return the reply, null if there was no reply, or {@link #SEND_FAILED}
     */
    static byte[] command(UsbDeviceConnection conn, UsbEndpoint out, UsbEndpoint in,
                                  byte[] cmd, int replyLen, String label, Log log) {
        int n = conn.bulkTransfer(out, cmd, cmd.length, SEND_TIMEOUT_MS);
        if (n != cmd.length) {
            log.line("-> " + label + ": " + Switch2Protocol.hex(cmd));
            log.line("   SEND FAILED (bulkTransfer returned " + n + ")");
            return SEND_FAILED;
        }
        byte[] reply = new byte[0];
        byte[] buf = new byte[Switch2Protocol.PACKET_SIZE];
        while (reply.length < replyLen) {
            int got = conn.bulkTransfer(in, buf, buf.length, REPLY_TIMEOUT_MS);
            if (got < 0) {
                break;
            }
            byte[] joined = new byte[reply.length + got];
            System.arraycopy(reply, 0, joined, 0, reply.length);
            System.arraycopy(buf, 0, joined, reply.length, got);
            reply = joined;
            if (got < buf.length) {
                break;
            }
        }
        log.line("-> " + label + ": " + Switch2Protocol.hex(cmd));
        if (reply.length == 0) {
            log.line("<- (no reply within " + REPLY_TIMEOUT_MS + " ms)");
            return null;
        }
        log.line("<- " + reply.length + "B: " + Switch2Protocol.hex(reply));
        return reply;
    }

    /**
     * Debug only: claims the HID interface (detaching the kernel's HID driver while it runs),
     * reads raw input reports for about {@code millis} ms and logs what arrives. Android's
     * releaseInterface asks the kernel to reattach its driver afterwards; if that doesn't
     * happen, the gamepad only comes back after a replug.
     */
    static void peekHid(UsbManager usb, UsbDevice device, int millis, Log log) {
        UsbInterface hid = findInterface(device, UsbConstants.USB_CLASS_HID, 0,
                UsbConstants.USB_ENDPOINT_XFER_INT, UsbConstants.USB_DIR_IN);
        if (hid == null) {
            log.line("Peek: ERROR: no HID interface with an interrupt IN endpoint");
            return;
        }
        UsbEndpoint in = findEndpoint(hid, UsbConstants.USB_ENDPOINT_XFER_INT,
                UsbConstants.USB_DIR_IN);
        if (!usb.hasPermission(device)) {
            log.line("Peek: ERROR: no USB permission for this device");
            return;
        }
        UsbDeviceConnection conn = usb.openDevice(device);
        if (conn == null) {
            log.line("Peek: ERROR: openDevice failed");
            return;
        }
        try {
            if (!conn.claimInterface(hid, true)) {
                log.line("Peek: ERROR: claimInterface(" + hid.getId() + ") failed");
                return;
            }
            log.line("Peek: claimed HID interface " + hid.getId() + " (the kernel HID driver is "
                    + "detached while Peek runs)");
            try {
                byte[] buf = new byte[Math.max(64, in.getMaxPacketSize())];
                Map<Integer, Integer> perId = new TreeMap<>();
                int total = 0;
                int shown = 0;
                long t0 = SystemClock.elapsedRealtime();
                long end = t0 + millis;
                while (SystemClock.elapsedRealtime() < end) {
                    int n = conn.bulkTransfer(in, buf, buf.length, 100);
                    if (n <= 0) {
                        if (!isAttached(usb, device)) {
                            log.line("Peek: device is gone");
                            break;
                        }
                        continue;
                    }
                    total++;
                    perId.merge(buf[0] & 0xFF, 1, Integer::sum);
                    if (shown < 12) {
                        log.line("Peek: " + Switch2Protocol.describeInputReport(buf, n));
                        if (shown == 0) {
                            log.line("Peek: raw " + n + "B: " + Switch2Protocol.hex(buf, 0, n));
                        }
                        shown++;
                    }
                }
                double secs = (SystemClock.elapsedRealtime() - t0) / 1000.0;
                StringBuilder ids = new StringBuilder();
                for (Map.Entry<Integer, Integer> e : perId.entrySet()) {
                    ids.append(String.format(Locale.ROOT, " id 0x%02X x%d", e.getKey(),
                            e.getValue()));
                }
                log.line(String.format(Locale.ROOT, "Peek: %d reports in %.1f s (%.0f/s):%s",
                        total, secs, total / secs, total == 0 ? " none (controller not streaming; "
                                + "run Start first)" : ids.toString()));
            } finally {
                boolean released = conn.releaseInterface(hid);
                log.line("Peek: released HID interface " + hid.getId() + (released
                        ? "; Android should now reattach the kernel HID driver (watch Input "
                        + "devices; replug the controller if the gamepad doesn't come back)"
                        : " (failed; replug the controller)"));
            }
        } finally {
            conn.close();
        }
    }

    // --- capture: GC Bridge reads the controller itself -----------------------------------------

    /**
     * Reads a 0x40-byte flash block over the command interface (like switch2_usb.read_flash):
     * the reply to a flash read is 0x50 bytes with the data at offset 0x10.
     */
    static byte[] readFlash(UsbDeviceConnection conn, UsbEndpoint out, UsbEndpoint in, int address,
                            Log log) {
        byte[] reply = command(conn, out, in, Switch2Protocol.flashReadCommand(address),
                Switch2Protocol.FLASH_REPLY_LEN,
                String.format(Locale.ROOT, "flash read 0x%X", address), log);
        if (reply == null || reply == SEND_FAILED || reply.length < Switch2Protocol.FLASH_REPLY_LEN) {
            return null;
        }
        byte[] data = new byte[Switch2Protocol.FLASH_REPLY_LEN - Switch2Protocol.FLASH_DATA_OFFSET];
        System.arraycopy(reply, Switch2Protocol.FLASH_DATA_OFFSET, data, 0, data.length);
        return data;
    }

    /**
     * Capture mode: initialises the controller in Nintendo report format, reads its calibration,
     * then claims the HID interface and streams every 250 Hz report into the hub, in the
     * background, until {@link #stop()} or an unplug. While it runs, Android's own gamepad for
     * this controller is detached (games won't see it); releasing the interface gives it back.
     */
    static final class Capture implements Runnable {
        private final UsbManager usb;
        private final UsbDevice device;
        private final InputHub hub;
        private final Log log;
        private final Thread thread = new Thread(this, "gcbridge-usb-capture");
        private volatile boolean stopRequested;
        private volatile boolean running = true;
        private volatile String status = "starting";
        private volatile double rateHz;
        private volatile String deviceKey;

        Capture(UsbManager usb, UsbDevice device, InputHub hub, Log log) {
            this.usb = usb;
            this.device = device;
            this.hub = hub;
            this.log = log;
        }

        void start() {
            thread.start();
        }

        void stop() {
            stopRequested = true;
            try {
                thread.join(2000);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }

        boolean isRunning() {
            return running;
        }

        String status() {
            return status;
        }

        double rateHz() {
            return rateHz;
        }

        String deviceKey() {
            return deviceKey;
        }

        @Override
        public void run() {
            try {
                capture();
            } catch (RuntimeException e) {
                android.util.Log.e(MainActivity.TAG, "USB capture failed", e);
                log.line("Capture: ERROR " + e);
                status = "error: " + e;
            } finally {
                if (deviceKey != null) {
                    hub.disconnect(deviceKey);
                }
                running = false;
            }
        }

        private void capture() {
            String model = Switch2Protocol.modelKey(device.getProductId());
            if (model == null) {
                status = "not a Switch 2 controller";
                log.line("Capture: " + status);
                return;
            }
            if (!usb.hasPermission(device)) {
                status = "no USB permission";
                log.line("Capture: ERROR no USB permission");
                return;
            }
            UsbInterface cmdIntf = findInterface(device, UsbConstants.USB_CLASS_VENDOR_SPEC, 1,
                    UsbConstants.USB_ENDPOINT_XFER_BULK, UsbConstants.USB_DIR_OUT,
                    UsbConstants.USB_DIR_IN);
            UsbInterface hid = findInterface(device, UsbConstants.USB_CLASS_HID, 0,
                    UsbConstants.USB_ENDPOINT_XFER_INT, UsbConstants.USB_DIR_IN);
            if (cmdIntf == null || hid == null) {
                status = "interfaces not found";
                log.line("Capture: ERROR need a vendor interface (bulk) and a HID interface (interrupt IN)");
                return;
            }
            UsbDeviceConnection conn = usb.openDevice(device);
            if (conn == null) {
                status = "openDevice failed";
                log.line("Capture: ERROR openDevice failed");
                return;
            }
            try {
                // 1. init in Nintendo format + calibration, over the command interface
                Switch2Protocol.Calibration cal = Switch2Protocol.Calibration.defaults(model);
                UsbEndpoint out = findEndpoint(cmdIntf, UsbConstants.USB_ENDPOINT_XFER_BULK,
                        UsbConstants.USB_DIR_OUT);
                UsbEndpoint cmdIn = findEndpoint(cmdIntf, UsbConstants.USB_ENDPOINT_XFER_BULK,
                        UsbConstants.USB_DIR_IN);
                if (conn.claimInterface(cmdIntf, true)) {
                    try {
                        cal = Switch2Protocol.readCalibration(
                                address -> readFlash(conn, out, cmdIn, address, log), model);
                        log.line("Capture: calibration " + cal.source
                                + (cal.serial.isEmpty() ? "" : ", serial " + cal.serial));
                        if (!sendInit(usb, device, conn, out, cmdIn, Switch2Protocol.FORMAT_NINTENDO, log)) {
                            log.line("Capture: init incomplete; trying to read anyway");
                        }
                    } finally {
                        conn.releaseInterface(cmdIntf);
                    }
                } else {
                    log.line("Capture: WARNING command interface busy; default calibration, no init");
                }
                // 2. the HID interface: detaches Android's gamepad for this controller
                if (!conn.claimInterface(hid, true)) {
                    status = "claimInterface(HID) failed";
                    log.line("Capture: ERROR " + status);
                    return;
                }
                UsbEndpoint in = findEndpoint(hid, UsbConstants.USB_ENDPOINT_XFER_INT, UsbConstants.USB_DIR_IN);
                try {
                    String key = "usb:" + (cal.serial.isEmpty() ? device.getDeviceName() : cal.serial);
                    Map<String, Object> extra = new LinkedHashMap<>();
                    extra.put("transport", "usb");
                    extra.put("report", "0x05");
                    extra.put("calibration", cal.source);
                    extra.put("reader", "gcbridge");
                    InputHub.Device dev = hub.connect(key,
                            Switch2Protocol.modelName(device.getProductId()), "gcbridge-usb",
                            familyFor(model), Switch2Protocol.NINTENDO_VID, device.getProductId(),
                            "wired", extra);
                    deviceKey = key;
                    status = "reading";
                    log.line("Capture: reading HID reports (games can't see the controller until "
                            + "capture stops)");
                    byte[] buf = new byte[Math.max(64, in.getMaxPacketSize())];
                    int[] buttons = new int[Pad.NUM_BUTTONS];
                    int[] axes = new int[Pad.NUM_AXES];
                    long windowStart = SystemClock.elapsedRealtimeNanos();
                    int windowCount = 0;
                    int silent = 0;
                    while (!stopRequested) {
                        int n = conn.bulkTransfer(in, buf, buf.length, 100);
                        long now = System.nanoTime();
                        if (n <= 0) {
                            if (++silent % 10 == 0 && !isAttached(usb, device)) {
                                status = "unplugged";
                                log.line("Capture: controller unplugged");
                                break;
                            }
                            continue;
                        }
                        silent = 0;
                        if (Switch2Protocol.parseInputReport(model, buf, 0, n, cal,
                                Switch2Protocol.DEFAULT_DEADZONE, buttons, axes)) {
                            hub.state(dev, buttons, axes, now);
                        }
                        windowCount++;
                        long elapsed = SystemClock.elapsedRealtimeNanos() - windowStart;
                        if (elapsed >= 1_000_000_000L) {
                            rateHz = windowCount * 1e9 / elapsed;
                            windowStart = SystemClock.elapsedRealtimeNanos();
                            windowCount = 0;
                        }
                    }
                    if (stopRequested) {
                        status = "stopped";
                    }
                } finally {
                    boolean released = conn.releaseInterface(hid);
                    log.line("Capture: released HID interface" + (released ? "" : " (failed)")
                            + "; if the gamepad doesn't come back in Input devices, replug the "
                            + "controller");
                }
            } finally {
                conn.close();
            }
        }

        private static String familyFor(String model) {
            return Switch2Protocol.MODEL_GAMECUBE.equals(model) ? Pad.FAMILY_GAMECUBE : Pad.FAMILY_SWITCH;
        }
    }
}
