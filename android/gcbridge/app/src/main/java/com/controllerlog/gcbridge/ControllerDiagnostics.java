package com.controllerlog.gcbridge;

import android.content.Context;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.hardware.display.DisplayManager;
import android.hardware.input.InputManager;
import android.hardware.usb.UsbDevice;
import android.hardware.usb.UsbManager;
import android.os.Build;
import android.util.Log;
import android.view.Display;
import android.view.InputDevice;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.TimeZone;

final class ControllerDiagnostics {

    static final int LOG_CHUNK = 3000;

    private ControllerDiagnostics() {
    }

    static String variant(Context ctx) {
        return ctx.getPackageName().endsWith(".dev") ? "dev" : "release";
    }

    static String build(Context ctx, String captureBlock, String activeDevice, String layoutChoice) {
        StringBuilder sb = new StringBuilder();
        sb.append("== GC Bridge controller diagnostics ==\n");
        String version = "?";
        long code = -1;
        try {
            PackageInfo pi = ctx.getPackageManager().getPackageInfo(ctx.getPackageName(), 0);
            version = pi.versionName;
            code = pi.getLongVersionCode();
        } catch (PackageManager.NameNotFoundException e) {
            version = "?";
        }
        boolean debuggable = (ctx.getApplicationInfo().flags & ApplicationInfo.FLAG_DEBUGGABLE) != 0;
        sb.append(String.format(Locale.ROOT, "app %s %s (%d), variant %s, debuggable %b\n", ctx.getPackageName(),
                version, code, variant(ctx), debuggable));
        sb.append(String.format(Locale.ROOT, "device %s %s, %s\nAndroid %s (API %d), kernel %s\n", Build.MANUFACTURER,
                Build.MODEL, Build.FINGERPRINT, Build.VERSION.RELEASE, Build.VERSION.SDK_INT,
                System.getProperty("os.version")));
        DisplayManager dm = ctx.getSystemService(DisplayManager.class);
        if (dm != null) {
            for (Display d : dm.getDisplays()) {
                sb.append(String.format(Locale.ROOT, "display %d \"%s\" %dx%d flags 0x%x\n", d.getDisplayId(),
                        d.getName(), d.getMode().getPhysicalWidth(), d.getMode().getPhysicalHeight(), d.getFlags()));
            }
        }
        sb.append("\n== Input devices ==\n");
        InputManager im = ctx.getSystemService(InputManager.class);
        int[] ids = im != null ? im.getInputDeviceIds() : new int[0];
        PadSettings settings = InputRouter.settings();
        for (int id : ids) {
            InputDevice d = InputDevice.getDevice(id);
            if (d == null) {
                continue;
            }
            sb.append(InputDiagnostics.describe(d));
            if (!InputRouter.isGameDevice(d)) {
                continue;
            }
            PadIdentity identity = InputRouter.identityOf(d, id);
            InputRouter.Entry entry = InputRouter.entryFor(id);
            PadClass cls = entry != null ? entry.cls : InputRouter.classOf(identity);
            sb.append("    key ").append(identity.deviceKey()).append('\n');
            sb.append("    rule ").append(cls.summary()).append('\n');
            sb.append("    family ").append(cls.family).append(", table ").append(cls.table)
                    .append(", ignored ").append(cls.ignored)
                    .append(", draw as ").append(settings.drawAs.getOrDefault(identity.deviceKey(), "auto"))
                    .append(", profile ").append(settings.profiles.containsKey(identity.deviceKey()) ? "yes" : "no")
                    .append('\n');
            sb.append("    routes ").append(InputRouter.describeRoutes(entry != null ? entry.routes
                    : AndroidInput.routes(cls, identity))).append('\n');
            if (!cls.unavailable.isEmpty()) {
                sb.append("    not delivered by this connection: ").append(String.join(", ", cls.unavailable))
                        .append('\n');
            }
            if (entry != null) {
                sb.append("    learned scan codes ").append(InputRouter.describeLearned(entry)).append('\n');
                sb.append("    last motion ").append(InputRouter.lastMotion(entry)).append('\n');
                for (String dem : entry.demotions) {
                    sb.append("    demoted: ").append(dem).append('\n');
                }
            } else {
                sb.append("    no events seen yet\n");
            }
        }
        sb.append("\n== USB ==\n");
        UsbManager usb = ctx.getSystemService(UsbManager.class);
        if (usb != null) {
            for (UsbDevice d : usb.getDeviceList().values()) {
                Switch2Usb.logLayout(d, line -> sb.append(line).append('\n'));
            }
        }
        sb.append("USB capture: ").append(CaptureService.usbStatus()).append('\n');
        sb.append("\n== Last pad key events (oldest first) ==\n");
        for (String line : InputRouter.keyLog()) {
            sb.append(line).append('\n');
        }
        sb.append("\n== Capture ==\n").append(captureBlock == null ? "" : captureBlock).append('\n');
        sb.append("active device: ").append(activeDevice).append('\n');
        sb.append("layout choice: ").append(layoutChoice).append('\n');
        return sb.toString();
    }

    static String stamp() {
        SimpleDateFormat f = new SimpleDateFormat("yyyyMMdd'T'HHmmss'Z'", Locale.ROOT);
        f.setTimeZone(TimeZone.getTimeZone("UTC"));
        return f.format(new Date());
    }

    static File write(Context ctx, String text) throws IOException {
        File dir = ctx.getExternalFilesDir("diagnostics");
        if (dir == null) {
            dir = new File(ctx.getFilesDir(), "diagnostics");
        }
        if (!dir.isDirectory() && !dir.mkdirs()) {
            throw new IOException("can't create " + dir);
        }
        File f = new File(dir, "controllers-" + stamp() + ".txt");
        try (OutputStream out = new FileOutputStream(f)) {
            out.write(text.getBytes(StandardCharsets.UTF_8));
        }
        return f;
    }

    static void logChunks(String text) {
        int n = (text.length() + LOG_CHUNK - 1) / LOG_CHUNK;
        for (int i = 0; i < n; i++) {
            String part = text.substring(i * LOG_CHUNK, Math.min(text.length(), (i + 1) * LOG_CHUNK));
            Log.i(MainActivity.TAG, "DIAG " + (i + 1) + "/" + n + "\n" + part);
        }
    }
}
