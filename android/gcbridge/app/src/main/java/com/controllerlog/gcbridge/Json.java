package com.controllerlog.gcbridge;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Tiny JSON reader/writer, so the layout files and the .ctlog rows need neither org.json (not
 * available to JVM unit tests) nor a library. Objects become {@code LinkedHashMap<String,
 * Object>}, arrays {@code ArrayList<Object>}, numbers {@code Long} or {@code Double}, and
 * {@code null} stays {@code null}.
 */
public final class Json {

    private Json() {
    }

    /** Parses one JSON value; throws IllegalArgumentException on malformed input. */
    public static Object parse(String text) {
        Parser p = new Parser(text);
        Object v = p.value();
        p.skipSpace();
        if (p.pos != text.length()) {
            throw p.error("trailing characters");
        }
        return v;
    }

    /** Encodes strings, numbers, booleans, null, Maps (String keys), Lists and arrays. */
    public static String write(Object value) {
        StringBuilder sb = new StringBuilder();
        write(sb, value);
        return sb.toString();
    }

    public static void write(StringBuilder sb, Object v) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof String) {
            quote(sb, (String) v);
        } else if (v instanceof Boolean || v instanceof Integer || v instanceof Long
                || v instanceof Short || v instanceof Byte) {
            sb.append(v);
        } else if (v instanceof Float || v instanceof Double) {
            double d = ((Number) v).doubleValue();
            if (Double.isNaN(d) || Double.isInfinite(d)) {
                sb.append("null");
            } else if (d == Math.rint(d) && Math.abs(d) < 1e15) {
                sb.append((long) d);
            } else {
                sb.append(d);
            }
        } else if (v instanceof Map) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : ((Map<?, ?>) v).entrySet()) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                quote(sb, String.valueOf(e.getKey()));
                sb.append(':');
                write(sb, e.getValue());
            }
            sb.append('}');
        } else if (v instanceof Iterable) {
            sb.append('[');
            boolean first = true;
            for (Object o : (Iterable<?>) v) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                write(sb, o);
            }
            sb.append(']');
        } else if (v instanceof int[]) {
            sb.append('[');
            int[] a = (int[]) v;
            for (int i = 0; i < a.length; i++) {
                if (i > 0) {
                    sb.append(',');
                }
                sb.append(a[i]);
            }
            sb.append(']');
        } else if (v instanceof Object[]) {
            sb.append('[');
            Object[] a = (Object[]) v;
            for (int i = 0; i < a.length; i++) {
                if (i > 0) {
                    sb.append(',');
                }
                write(sb, a[i]);
            }
            sb.append(']');
        } else {
            quote(sb, String.valueOf(v));
        }
    }

    public static void quote(StringBuilder sb, String s) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"':
                    sb.append("\\\"");
                    break;
                case '\\':
                    sb.append("\\\\");
                    break;
                case '\n':
                    sb.append("\\n");
                    break;
                case '\r':
                    sb.append("\\r");
                    break;
                case '\t':
                    sb.append("\\t");
                    break;
                default:
                    if (c < 0x20) {
                        sb.append(String.format(java.util.Locale.ROOT, "\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
            }
        }
        sb.append('"');
    }

    // --- typed accessors for parsed values -----------------------------------------------

    @SuppressWarnings("unchecked")
    public static Map<String, Object> asObject(Object v) {
        return v instanceof Map ? (Map<String, Object>) v : null;
    }

    @SuppressWarnings("unchecked")
    public static List<Object> asArray(Object v) {
        return v instanceof List ? (List<Object>) v : null;
    }

    public static String str(Map<String, Object> o, String key, String dflt) {
        Object v = o == null ? null : o.get(key);
        return v instanceof String ? (String) v : dflt;
    }

    public static double num(Map<String, Object> o, String key, double dflt) {
        Object v = o == null ? null : o.get(key);
        return v instanceof Number ? ((Number) v).doubleValue() : dflt;
    }

    public static boolean has(Map<String, Object> o, String key) {
        return o != null && o.get(key) != null;
    }

    private static final class Parser {
        private final String s;
        private int pos;

        Parser(String s) {
            this.s = s;
        }

        IllegalArgumentException error(String what) {
            return new IllegalArgumentException("JSON: " + what + " at " + pos);
        }

        void skipSpace() {
            while (pos < s.length()) {
                char c = s.charAt(pos);
                if (c == ' ' || c == '\n' || c == '\r' || c == '\t') {
                    pos++;
                } else {
                    break;
                }
            }
        }

        Object value() {
            skipSpace();
            if (pos >= s.length()) {
                throw error("unexpected end");
            }
            char c = s.charAt(pos);
            switch (c) {
                case '{':
                    return object();
                case '[':
                    return array();
                case '"':
                    return string();
                case 't':
                    literal("true");
                    return Boolean.TRUE;
                case 'f':
                    literal("false");
                    return Boolean.FALSE;
                case 'n':
                    literal("null");
                    return null;
                default:
                    if (c == '-' || (c >= '0' && c <= '9')) {
                        return number();
                    }
                    throw error("unexpected '" + c + "'");
            }
        }

        private void literal(String word) {
            if (!s.startsWith(word, pos)) {
                throw error("expected " + word);
            }
            pos += word.length();
        }

        private Map<String, Object> object() {
            Map<String, Object> out = new LinkedHashMap<>();
            pos++; // {
            skipSpace();
            if (peek() == '}') {
                pos++;
                return out;
            }
            while (true) {
                skipSpace();
                if (peek() != '"') {
                    throw error("expected key");
                }
                String key = string();
                skipSpace();
                if (peek() != ':') {
                    throw error("expected ':'");
                }
                pos++;
                out.put(key, value());
                skipSpace();
                char c = peek();
                pos++;
                if (c == '}') {
                    return out;
                }
                if (c != ',') {
                    throw error("expected ',' or '}'");
                }
            }
        }

        private List<Object> array() {
            List<Object> out = new ArrayList<>();
            pos++; // [
            skipSpace();
            if (peek() == ']') {
                pos++;
                return out;
            }
            while (true) {
                out.add(value());
                skipSpace();
                char c = peek();
                pos++;
                if (c == ']') {
                    return out;
                }
                if (c != ',') {
                    throw error("expected ',' or ']'");
                }
            }
        }

        private char peek() {
            if (pos >= s.length()) {
                throw error("unexpected end");
            }
            return s.charAt(pos);
        }

        private String string() {
            pos++; // opening quote
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (pos >= s.length()) {
                    throw error("unterminated string");
                }
                char c = s.charAt(pos++);
                if (c == '"') {
                    return sb.toString();
                }
                if (c != '\\') {
                    sb.append(c);
                    continue;
                }
                if (pos >= s.length()) {
                    throw error("bad escape");
                }
                char e = s.charAt(pos++);
                switch (e) {
                    case '"':
                    case '\\':
                    case '/':
                        sb.append(e);
                        break;
                    case 'b':
                        sb.append('\b');
                        break;
                    case 'f':
                        sb.append('\f');
                        break;
                    case 'n':
                        sb.append('\n');
                        break;
                    case 'r':
                        sb.append('\r');
                        break;
                    case 't':
                        sb.append('\t');
                        break;
                    case 'u':
                        if (pos + 4 > s.length()) {
                            throw error("bad \\u escape");
                        }
                        sb.append((char) Integer.parseInt(s.substring(pos, pos + 4), 16));
                        pos += 4;
                        break;
                    default:
                        throw error("bad escape \\" + e);
                }
            }
        }

        private Number number() {
            int start = pos;
            boolean floating = false;
            if (peek() == '-') {
                pos++;
            }
            while (pos < s.length()) {
                char c = s.charAt(pos);
                if (c >= '0' && c <= '9') {
                    pos++;
                } else if (c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') {
                    floating = true;
                    pos++;
                } else {
                    break;
                }
            }
            String t = s.substring(start, pos);
            try {
                return floating ? (Number) Double.valueOf(t) : (Number) Long.valueOf(t);
            } catch (NumberFormatException ex) {
                throw error("bad number " + t);
            }
        }
    }
}
