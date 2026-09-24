package com.controllerlog.gcbridge;

import android.content.ContentResolver;
import android.content.ContentValues;
import android.content.Context;
import android.net.Uri;
import android.os.Environment;
import android.provider.MediaStore;

import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/**
 * Where recordings live: {@code Android/data/com.controllerlog.gcbridge/files/recordings/}
 * (visible from a PC over USB file transfer). Files can also be shared through
 * {@link RecordingProvider} or copied to Downloads/GC Bridge.
 */
final class RecordingStore {

    static final String AUTHORITY = "com.controllerlog.gcbridge.recordings";
    static final String DOWNLOADS_SUBDIR = "GC Bridge";

    private RecordingStore() {
    }

    static File dir(Context ctx) {
        File d = ctx.getExternalFilesDir("recordings");
        if (d == null) {
            d = new File(ctx.getFilesDir(), "recordings");
        }
        if (!d.isDirectory() && !d.mkdirs()) {
            throw new IllegalStateException("can't create " + d);
        }
        return d;
    }

    static File newFile(Context ctx) {
        String stamp = new SimpleDateFormat("yyyyMMdd_HHmmss", Locale.ROOT).format(new Date());
        File f = new File(dir(ctx), "gcbridge_" + stamp + ".ctlog");
        int n = 2;
        while (f.exists()) {
            f = new File(dir(ctx), "gcbridge_" + stamp + "_" + n++ + ".ctlog");
        }
        return f;
    }

    /** Newest first. */
    static List<File> list(Context ctx) {
        File[] files = dir(ctx).listFiles((d, name) -> name.endsWith(".ctlog"));
        List<File> out = new ArrayList<>();
        if (files != null) {
            out.addAll(Arrays.asList(files));
        }
        out.sort((a, b) -> Long.compare(b.lastModified(), a.lastModified()));
        return out;
    }

    static Uri shareUri(File f) {
        return new Uri.Builder().scheme(ContentResolver.SCHEME_CONTENT).authority(AUTHORITY)
                .appendPath(f.getName()).build();
    }

    static String describe(File f) {
        long size = f.length();
        String s = size < 1024 ? size + " B" : size < 1024 * 1024
                ? String.format(Locale.ROOT, "%.1f KB", size / 1024.0)
                : String.format(Locale.ROOT, "%.1f MB", size / 1048576.0);
        return f.getName() + "  (" + s + ", "
                + new SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.ROOT).format(new Date(f.lastModified())) + ")";
    }

    /** Copies a recording to Downloads/GC Bridge through MediaStore (no permission needed). */
    static Uri exportToDownloads(Context ctx, File f) throws IOException {
        ContentResolver cr = ctx.getContentResolver();
        ContentValues v = new ContentValues();
        v.put(MediaStore.Downloads.DISPLAY_NAME, f.getName());
        v.put(MediaStore.Downloads.MIME_TYPE, "application/octet-stream");
        v.put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/" + DOWNLOADS_SUBDIR);
        v.put(MediaStore.Downloads.IS_PENDING, 1);
        Uri uri = cr.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, v);
        if (uri == null) {
            throw new IOException("MediaStore refused the file");
        }
        try (InputStream in = new FileInputStream(f); OutputStream out = cr.openOutputStream(uri)) {
            if (out == null) {
                throw new IOException("can't open " + uri);
            }
            byte[] buf = new byte[1 << 16];
            int n;
            while ((n = in.read(buf)) > 0) {
                out.write(buf, 0, n);
            }
        } catch (IOException e) {
            cr.delete(uri, null, null);
            throw e;
        }
        v.clear();
        v.put(MediaStore.Downloads.IS_PENDING, 0);
        cr.update(uri, v, null, null);
        return uri;
    }
}
