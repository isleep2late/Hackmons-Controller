package com.controllerlog.gcbridge;

import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import java.io.IOException;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

final class PadFixtures {

    private PadFixtures() {
    }

    static final class Fixture {
        String id;
        Path path;
        Map<String, Object> json;
        PadIdentity identity;
        Map<String, Map<String, Object>> profiles = new LinkedHashMap<>();
        Set<String> ignore = new LinkedHashSet<>();

        Map<String, Object> expect() {
            return Json.asObject(json.get("expect"));
        }

        List<Map<String, Object>> list(String key) {
            List<Map<String, Object>> out = new ArrayList<>();
            List<Object> a = Json.asArray(json.get(key));
            if (a != null) {
                for (Object o : a) {
                    out.add(Json.asObject(o));
                }
            }
            return out;
        }

        PadClass classify() {
            return PadClassifier.classify(identity, profiles, ignore);
        }
    }

    static Path dir() {
        Path root = TestFiles.root();
        assertNotNull("controllerlog.root is not set", root);
        return root.resolve("tests").resolve("fixtures").resolve("pads");
    }

    static List<Fixture> all() throws IOException {
        List<Path> paths = new ArrayList<>();
        try (DirectoryStream<Path> ds = Files.newDirectoryStream(dir(), "*.json")) {
            for (Path p : ds) {
                paths.add(p);
            }
        }
        paths.sort(null);
        assertTrue("no pad fixtures in " + dir(), paths.size() >= 7);
        List<Fixture> out = new ArrayList<>();
        for (Path p : paths) {
            out.add(load(p));
        }
        return out;
    }

    static Fixture byId(String id) throws IOException {
        return load(dir().resolve(id + ".json"));
    }

    static Fixture load(Path p) throws IOException {
        Fixture f = new Fixture();
        f.path = p;
        f.json = Json.asObject(Json.parse(TestFiles.read(p)));
        f.id = Json.str(f.json, "id", p.getFileName().toString());
        Map<String, Object> d = Json.asObject(f.json.get("device"));
        int sources = 0;
        for (Object s : Json.asArray(d.get("sources"))) {
            sources |= PadIdentity.sourceFromName((String) s);
        }
        List<PadIdentity.Range> ranges = new ArrayList<>();
        for (Object o : Json.asArray(d.get("axes"))) {
            Map<String, Object> a = Json.asObject(o);
            int axis = PadIdentity.axisFromName(Json.str(a, "axis", null));
            assertTrue(f.id + ": unknown axis " + a, axis >= 0);
            ranges.add(new PadIdentity.Range(axis, (float) Json.num(a, "min", -1), (float) Json.num(a, "max", 1),
                    (float) Json.num(a, "flat", 0), true));
        }
        Set<Integer> keys = new LinkedHashSet<>();
        for (Object o : Json.asArray(d.get("hasKeys"))) {
            int code = PadIdentity.keyFromName((String) o);
            assertTrue(f.id + ": unknown key " + o, code >= 0);
            keys.add(code);
        }
        f.identity = new PadIdentity(Json.str(d, "name", ""), (int) Json.num(d, "vendor", 0),
                (int) Json.num(d, "product", 0), Json.str(d, "descriptor", ""), sources,
                Boolean.TRUE.equals(d.get("external")), (int) Json.num(d, "controllerNumber", 0), ranges, keys,
                null);
        Map<String, Object> ctx = Json.asObject(f.json.get("context"));
        if (ctx != null) {
            Map<String, Object> profiles = Json.asObject(ctx.get("profiles"));
            if (profiles != null) {
                for (Map.Entry<String, Object> e : profiles.entrySet()) {
                    f.profiles.put(e.getKey(), Json.asObject(e.getValue()));
                }
            }
            List<Object> ignore = Json.asArray(ctx.get("ignore"));
            if (ignore != null) {
                for (Object o : ignore) {
                    f.ignore.add((String) o);
                }
            }
        }
        return f;
    }

    static int keyCode(Map<String, Object> press) {
        int code = PadIdentity.keyFromName(Json.str(press, "keyCode", null));
        assertTrue("unknown key code in " + press, code >= 0);
        return code;
    }

    static int scan(Map<String, Object> press) {
        return (int) Json.num(press, "scan", 0);
    }
}
