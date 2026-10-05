package com.controllerlog.gcbridge;

import android.content.Context;
import android.content.SharedPreferences;

import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.Map;
import java.util.Set;

final class PadSettings {

    static final String PREFS = "gcbridge";
    static final String KEY_PROFILES = "pad_profiles";
    static final String KEY_IGNORE = "pad_ignore";
    static final String KEY_DRAW_AS = "pad_draw_as";

    static final PadSettings EMPTY = new PadSettings(Collections.emptyMap(), Collections.emptySet(),
            Collections.emptyMap());

    final Map<String, Map<String, Object>> profiles;
    final Set<String> ignore;
    final Map<String, String> drawAs;

    PadSettings(Map<String, Map<String, Object>> profiles, Set<String> ignore, Map<String, String> drawAs) {
        this.profiles = Collections.unmodifiableMap(new LinkedHashMap<>(profiles));
        this.ignore = Collections.unmodifiableSet(new LinkedHashSet<>(ignore));
        this.drawAs = Collections.unmodifiableMap(new LinkedHashMap<>(drawAs));
    }

    private static SharedPreferences prefs(Context ctx) {
        return ctx.getApplicationContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    static PadSettings load(Context ctx) {
        SharedPreferences p = prefs(ctx);
        Map<String, Map<String, Object>> profiles = PadProfile.parseProfiles(p.getString(KEY_PROFILES, null));
        Set<String> ignore = new LinkedHashSet<>(p.getStringSet(KEY_IGNORE, Collections.emptySet()));
        Map<String, String> drawAs = new LinkedHashMap<>();
        String raw = p.getString(KEY_DRAW_AS, null);
        if (raw != null) {
            try {
                Map<String, Object> m = Json.asObject(Json.parse(raw));
                if (m != null) {
                    for (Map.Entry<String, Object> e : m.entrySet()) {
                        if (e.getValue() instanceof String) {
                            drawAs.put(e.getKey(), (String) e.getValue());
                        }
                    }
                }
            } catch (IllegalArgumentException e) {
                drawAs.clear();
            }
        }
        return new PadSettings(profiles, ignore, drawAs);
    }

    static void saveProfile(Context ctx, String deviceKey, Map<String, Object> profile) {
        Map<String, Object> all = new LinkedHashMap<>(load(ctx).profiles);
        if (profile == null) {
            all.remove(deviceKey);
        } else {
            all.put(deviceKey, profile);
        }
        prefs(ctx).edit().putString(KEY_PROFILES, Json.write(all)).apply();
    }

    static void setIgnored(Context ctx, String deviceKey, boolean ignored) {
        Set<String> s = new LinkedHashSet<>(load(ctx).ignore);
        if (ignored) {
            s.add(deviceKey);
        } else {
            s.remove(deviceKey);
        }
        prefs(ctx).edit().putStringSet(KEY_IGNORE, s).apply();
    }

    static void setDrawAs(Context ctx, String deviceKey, String family) {
        Map<String, Object> m = new LinkedHashMap<>(load(ctx).drawAs);
        if (family == null || "auto".equals(family)) {
            m.remove(deviceKey);
        } else {
            m.put(deviceKey, family);
        }
        prefs(ctx).edit().putString(KEY_DRAW_AS, Json.write(m)).apply();
    }
}
