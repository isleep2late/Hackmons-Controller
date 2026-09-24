package com.controllerlog.gcbridge;

import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * One controller drawing from {@code controllerlog/layouts/*.json} (see docs/LAYOUTS.md). The
 * same files drive the PC overlay and video renderer; the app ships copies as assets.
 */
public final class Layout {

    public final String name;
    public final String title;
    public final List<String> families;
    public final double width;
    public final double height;
    public final Map<String, Object> theme;
    public final List<Map<String, Object>> body;
    public final List<Map<String, Object>> elements;

    private static final Map<String, Object> DEFAULT_THEME = new LinkedHashMap<>();

    static {
        DEFAULT_THEME.put("body", "#2b2f36");
        DEFAULT_THEME.put("body_stroke", "#15171b");
        DEFAULT_THEME.put("idle", "#3d434d");
        DEFAULT_THEME.put("idle_stroke", "#15171b");
        DEFAULT_THEME.put("active", "#ffd23f");
        DEFAULT_THEME.put("label", "#e8e8e8");
        DEFAULT_THEME.put("label_active", "#111111");
        DEFAULT_THEME.put("font_size", 14L);
        DEFAULT_THEME.put("stroke_width", 2L);
    }

    private Layout(String name, String title, List<String> families, double width, double height,
                   Map<String, Object> theme, List<Map<String, Object>> body,
                   List<Map<String, Object>> elements) {
        this.name = name;
        this.title = title;
        this.families = families;
        this.width = width;
        this.height = height;
        this.theme = theme;
        this.body = body;
        this.elements = elements;
    }

    /** Parses and validates a layout file; the fallback name is used when the file has none. */
    public static Layout parse(String json, String fallbackName) {
        Map<String, Object> root = Json.asObject(Json.parse(json));
        if (root == null) {
            throw new IllegalArgumentException("layout is not a JSON object");
        }
        String name = Json.str(root, "name", fallbackName);
        List<Object> size = Json.asArray(root.get("size"));
        if (size == null || size.size() != 2 || !(size.get(0) instanceof Number)
                || !(size.get(1) instanceof Number)) {
            throw new IllegalArgumentException(name + ": size must be [width, height]");
        }
        Map<String, Object> theme = new LinkedHashMap<>(DEFAULT_THEME);
        Map<String, Object> t = Json.asObject(root.get("theme"));
        if (t != null) {
            theme.putAll(t);
        }
        List<String> families = new ArrayList<>();
        List<Object> f = Json.asArray(root.get("families"));
        if (f != null) {
            for (Object o : f) {
                if (o instanceof String) {
                    families.add((String) o);
                }
            }
        }
        List<Map<String, Object>> body = objects(root.get("body"), name + ".body");
        List<Map<String, Object>> elements = objects(root.get("elements"), name + ".elements");
        if (elements.isEmpty()) {
            throw new IllegalArgumentException(name + ": elements must be a non-empty list");
        }
        for (Map<String, Object> b : body) {
            checkShape(b, name + ".body");
        }
        for (Map<String, Object> el : elements) {
            String type = Json.str(el, "type", "");
            if ("button".equals(type) || "trigger".equals(type)) {
                if (!Pad.isDigitalInput(Json.str(el, "input", ""))) {
                    throw new IllegalArgumentException(name + ": unknown input " + el.get("input"));
                }
                checkShape(el, name + "." + type);
            } else if ("stick".equals(type)) {
                if (Pad.axisIndex(Json.str(el, "x_axis", "")) < 0
                        || Pad.axisIndex(Json.str(el, "y_axis", "")) < 0) {
                    throw new IllegalArgumentException(name + ": stick needs x_axis and y_axis");
                }
                if (Json.has(el, "button") && Pad.buttonIndex(Json.str(el, "button", "")) < 0) {
                    throw new IllegalArgumentException(name + ": unknown stick button");
                }
                for (String k : new String[]{"cx", "cy", "r"}) {
                    if (!(el.get(k) instanceof Number)) {
                        throw new IllegalArgumentException(name + ": stick needs " + k);
                    }
                }
            } else {
                throw new IllegalArgumentException(name + ": unknown element type " + type);
            }
        }
        return new Layout(name, Json.str(root, "title", name), Collections.unmodifiableList(families),
                ((Number) size.get(0)).doubleValue(), ((Number) size.get(1)).doubleValue(), theme,
                body, elements);
    }

    private static List<Map<String, Object>> objects(Object v, String where) {
        List<Map<String, Object>> out = new ArrayList<>();
        List<Object> list = Json.asArray(v);
        if (list == null) {
            return out;
        }
        for (Object o : list) {
            Map<String, Object> m = Json.asObject(o);
            if (m == null) {
                throw new IllegalArgumentException(where + ": entries must be objects");
            }
            out.add(m);
        }
        return out;
    }

    private static void checkShape(Map<String, Object> obj, String where) {
        String shape = Json.str(obj, "shape", "");
        String[] need;
        switch (shape) {
            case "rect":
                need = new String[]{"x", "y", "w", "h"};
                break;
            case "circle":
                need = new String[]{"cx", "cy", "r"};
                break;
            case "ellipse":
                need = new String[]{"cx", "cy", "rx", "ry"};
                break;
            case "polygon":
                List<Object> pts = Json.asArray(obj.get("points"));
                if (pts == null || pts.size() < 3) {
                    throw new IllegalArgumentException(where + ": polygon needs at least 3 points");
                }
                return;
            case "text":
                if (!(obj.get("x") instanceof Number) || !(obj.get("y") instanceof Number)
                        || !(obj.get("text") instanceof String)) {
                    throw new IllegalArgumentException(where + ": text needs x, y, text");
                }
                return;
            default:
                throw new IllegalArgumentException(where + ": unknown shape " + shape);
        }
        for (String k : need) {
            if (!(obj.get(k) instanceof Number)) {
                throw new IllegalArgumentException(where + ": " + shape + " needs " + k);
            }
        }
    }

    /** The layout whose {@code families} lists {@code family}, else "generic", else the first. */
    public static Layout forFamily(Collection<Layout> layouts, String family) {
        Layout generic = null;
        Layout first = null;
        for (Layout l : layouts) {
            if (first == null) {
                first = l;
            }
            if (l.families.contains(family)) {
                return l;
            }
            if (Pad.FAMILY_GENERIC.equals(l.name)) {
                generic = l;
            }
        }
        return generic != null ? generic : first;
    }

    public static Layout byName(Collection<Layout> layouts, String name) {
        for (Layout l : layouts) {
            if (l.name.equals(name)) {
                return l;
            }
        }
        return null;
    }

    public String themeColor(String key) {
        return Json.str(theme, key, (String) DEFAULT_THEME.get(key));
    }

    public double themeNumber(String key) {
        return Json.num(theme, key, ((Number) DEFAULT_THEME.get(key)).doubleValue());
    }

    /**
     * CSS hex colour ({@code #rgb}, {@code #rgba}, {@code #rrggbb}, {@code #rrggbbaa}) to ARGB;
     * {@code none} / {@code transparent} / unknown text give 0 (fully transparent).
     */
    public static int color(String css) {
        if (css == null) {
            return 0;
        }
        String s = css.trim().toLowerCase(Locale.ROOT);
        if (!s.startsWith("#")) {
            return 0;
        }
        s = s.substring(1);
        try {
            switch (s.length()) {
                case 3:
                case 4: {
                    int r = Integer.parseInt(s.substring(0, 1), 16) * 17;
                    int g = Integer.parseInt(s.substring(1, 2), 16) * 17;
                    int b = Integer.parseInt(s.substring(2, 3), 16) * 17;
                    int a = s.length() == 4 ? Integer.parseInt(s.substring(3, 4), 16) * 17 : 255;
                    return (a << 24) | (r << 16) | (g << 8) | b;
                }
                case 6:
                    return (int) (0xFF000000L | Long.parseLong(s, 16));
                case 8: {
                    long v = Long.parseLong(s, 16);
                    return (int) (((v & 0xFF) << 24) | (v >>> 8));
                }
                default:
                    return 0;
            }
        } catch (NumberFormatException e) {
            return 0;
        }
    }
}
