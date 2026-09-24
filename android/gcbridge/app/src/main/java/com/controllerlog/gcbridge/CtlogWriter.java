package com.controllerlog.gcbridge;

import java.io.BufferedWriter;
import java.io.Closeable;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStreamWriter;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;
import java.util.TimeZone;

/**
 * Writes the {@code .ctlog} format of {@code controllerlog/logfile.py}: a JSON header line,
 * then one compact row per event, {@code [t_ns, device, kind, code, value]}. Timestamps are
 * nanoseconds since the recording started. Flushed at least every 250 ms while events arrive
 * (call {@link #flushIfDue} from a timer for the idle case); the PC reader tolerates a
 * truncated last line, so a crash loses at most that much.
 */
public final class CtlogWriter implements Closeable {

    public static final String FORMAT = "controllerlog";
    public static final int VERSION = 1;
    private static final long FLUSH_INTERVAL_NS = 250_000_000L;

    private final BufferedWriter out;
    private final StringBuilder sb = new StringBuilder(128);
    private long lastFlushNs = System.nanoTime();
    private boolean dirty;
    private boolean closed;
    private int count;

    public CtlogWriter(File file, Map<String, Object> headerExtra, Map<String, Object> meta)
            throws IOException {
        File dir = file.getParentFile();
        if (dir != null && !dir.isDirectory() && !dir.mkdirs()) {
            throw new IOException("can't create " + dir);
        }
        out = new BufferedWriter(new OutputStreamWriter(new FileOutputStream(file),
                StandardCharsets.UTF_8), 1 << 16);
        Map<String, Object> header = new LinkedHashMap<>();
        header.put("format", FORMAT);
        header.put("version", VERSION);
        header.put("created_utc", utcNow());
        header.put("time_unit", "ns");
        if (headerExtra != null) {
            header.putAll(headerExtra);
        }
        header.put("meta", meta != null ? meta : new LinkedHashMap<>());
        out.write(Json.write(header));
        out.write('\n');
        out.flush();
    }

    static String utcNow() {
        SimpleDateFormat f = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS", Locale.ROOT);
        f.setTimeZone(TimeZone.getTimeZone("UTC"));
        return f.format(new Date()) + "+00:00";
    }

    public synchronized int count() {
        return count;
    }

    /** {@code "+"} row: a controller with this id is (now) part of the recording. */
    public synchronized void device(long tNs, int device, Map<String, Object> info) throws IOException {
        row(tNs, device, "+", null, info);
    }

    public synchronized void disconnect(long tNs, int device) throws IOException {
        sb.setLength(0);
        sb.append('[').append(Math.max(0, tNs)).append(',').append(device).append(",\"-\"]\n");
        write();
    }

    public synchronized void button(long tNs, int device, int code, boolean down) throws IOException {
        sb.setLength(0);
        sb.append('[').append(Math.max(0, tNs)).append(',').append(device).append(",\"b\",")
                .append(code).append(',').append(down ? 1 : 0).append("]\n");
        write();
    }

    public synchronized void axis(long tNs, int device, int code, int value) throws IOException {
        sb.setLength(0);
        sb.append('[').append(Math.max(0, tNs)).append(',').append(device).append(",\"a\",")
                .append(code).append(',').append(value).append("]\n");
        write();
    }

    public synchronized void marker(long tNs, String label) throws IOException {
        row(tNs, null, "m", null, label);
    }

    private void row(long tNs, Integer device, String kind, Object code, Object value)
            throws IOException {
        sb.setLength(0);
        sb.append('[').append(Math.max(0, tNs)).append(',');
        sb.append(device == null ? "null" : device.toString()).append(",\"").append(kind)
                .append("\",");
        Json.write(sb, code);
        sb.append(',');
        Json.write(sb, value);
        sb.append("]\n");
        write();
    }

    private void write() throws IOException {
        if (closed) {
            return;
        }
        out.write(sb.toString());
        count++;
        dirty = true;
        long now = System.nanoTime();
        if (now - lastFlushNs >= FLUSH_INTERVAL_NS) {
            out.flush();
            lastFlushNs = now;
            dirty = false;
        }
    }

    /** Flushes a tail written after the last interval flush (call from a periodic timer). */
    public synchronized void flushIfDue() throws IOException {
        if (!closed && dirty && System.nanoTime() - lastFlushNs >= FLUSH_INTERVAL_NS) {
            out.flush();
            lastFlushNs = System.nanoTime();
            dirty = false;
        }
    }

    public synchronized void flush() throws IOException {
        if (!closed) {
            out.flush();
            lastFlushNs = System.nanoTime();
            dirty = false;
        }
    }

    @Override
    public synchronized void close() throws IOException {
        if (!closed) {
            closed = true;
            out.flush();
            out.close();
        }
    }
}
