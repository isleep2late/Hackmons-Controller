package com.controllerlog.gcbridge;

import android.content.Context;
import android.content.res.AssetManager;
import android.util.Log;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/** The layout files shipped as assets (copied from controllerlog/layouts at build time). */
final class LayoutStore {

    private static List<Layout> cached;

    private LayoutStore() {
    }

    static synchronized List<Layout> all(Context ctx) {
        if (cached != null) {
            return cached;
        }
        List<Layout> out = new ArrayList<>();
        AssetManager assets = ctx.getAssets();
        try {
            String[] names = assets.list("layouts");
            if (names != null) {
                for (String n : names) {
                    if (!n.endsWith(".json")) {
                        continue;
                    }
                    try (InputStream in = assets.open("layouts/" + n)) {
                        byte[] buf = readAll(in);
                        out.add(Layout.parse(new String(buf, StandardCharsets.UTF_8),
                                n.substring(0, n.length() - 5)));
                    } catch (IOException | RuntimeException e) {
                        Log.e(MainActivity.TAG, "layout " + n + " unusable: " + e);
                    }
                }
            }
        } catch (IOException e) {
            Log.e(MainActivity.TAG, "no layouts in assets: " + e);
        }
        Collections.sort(out, (a, b) -> a.name.compareTo(b.name));
        cached = Collections.unmodifiableList(out);
        return cached;
    }

    private static byte[] readAll(InputStream in) throws IOException {
        java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) > 0) {
            bos.write(buf, 0, n);
        }
        return bos.toByteArray();
    }

    static String[] names(Context ctx) {
        List<Layout> all = all(ctx);
        String[] out = new String[all.size()];
        for (int i = 0; i < out.length; i++) {
            out[i] = all.get(i).name;
        }
        return out;
    }

    /** "auto" (or unknown names) picks the layout for the controller family. */
    static Layout pick(Context ctx, String preference, String family) {
        List<Layout> all = all(ctx);
        if (all.isEmpty()) {
            return null;
        }
        if (preference != null && !"auto".equals(preference)) {
            Layout l = Layout.byName(all, preference);
            if (l != null) {
                return l;
            }
        }
        return Layout.forFamily(all, family == null ? Pad.FAMILY_GENERIC : family);
    }
}
