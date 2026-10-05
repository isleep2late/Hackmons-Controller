package com.controllerlog.gcbridge;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import org.junit.Test;

import java.io.IOException;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public class JsonTest {

    private static final int LIMIT = 64;

    @Test
    public void parsesNestedStructures() {
        Object v = Json.parse(" {\"a\": [1, 2.5, -3e2, \"x\\ny\\u0041\", true, false, null], \"b\": {}} ");
        Map<String, Object> o = Json.asObject(v);
        List<Object> a = Json.asArray(o.get("a"));
        assertEquals(Long.valueOf(1), a.get(0));
        assertEquals(Double.valueOf(2.5), a.get(1));
        assertEquals(Double.valueOf(-300), a.get(2));
        assertEquals("x\nyA", a.get(3));
        assertEquals(Boolean.TRUE, a.get(4));
        assertEquals(Boolean.FALSE, a.get(5));
        assertNull(a.get(6));
        assertTrue(Json.asObject(o.get("b")).isEmpty());
    }

    @Test
    public void rejectsGarbage() {
        for (String bad : new String[]{"", "{", "[1,]", "{\"a\" 1}", "tru", "\"unterminated", "1 2"}) {
            try {
                Json.parse(bad);
                fail("accepted " + bad);
            } catch (IllegalArgumentException expected) {
                // ok
            }
        }
    }

    @Test
    public void deepNestingIsRefusedInsteadOfOverflowingTheStack() {
        StringBuilder open = new StringBuilder();
        for (int i = 0; i < 100_000; i++) {
            open.append('[');
        }
        String[] deep = {open.toString(), open.toString().replace('[', '{').replace("{", "{\"a\":")};
        for (String bad : deep) {
            try {
                Json.parse(bad);
                fail("accepted " + bad.length() + " nested levels");
            } catch (IllegalArgumentException expected) {
                assertTrue(expected.getMessage(), expected.getMessage().contains("nested deeper than"));
            }
        }
        String ok = repeat("[", LIMIT) + repeat("]", LIMIT);
        assertEquals(1, Json.asArray(Json.parse(ok)).size());
        try {
            Json.parse("[" + ok + "]");
            fail("accepted " + (LIMIT + 1) + " levels");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("nested deeper than"));
        }
        assertEquals(1001, Json.asArray(Json.parse("[" + repeat("[],", 1000) + "[]]")).size());
    }

    @Test
    public void profileImportCatchesWhatTheParserThrows() throws IOException {
        String src = TestFiles.javaSource("MainActivity.java");
        String body = TestFiles.body(src, "protected void onActivityResult(").replaceAll("\\s+", "");
        assertTrue(body, body.contains("Json.asObject(Json.parse("));
        assertTrue(body, body.contains("catch(IOException|RuntimeExceptione){log(\"ERRORimportingtheprofile:"));
    }

    private static String repeat(String s, int n) {
        StringBuilder b = new StringBuilder();
        for (int i = 0; i < n; i++) {
            b.append(s);
        }
        return b.toString();
    }

    @Test
    public void writesCompactRowsThePythonReaderAccepts() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("id", 0);
        m.put("name", "Pad \"1\"\\");
        m.put("axes", new int[]{1, -2});
        m.put("list", new ArrayList<>(Arrays.asList("a", null, 2.0, 2.5)));
        assertEquals("{\"id\":0,\"name\":\"Pad \\\"1\\\"\\\\\",\"axes\":[1,-2],\"list\":[\"a\",null,2,2.5]}",
                Json.write(m));
        assertEquals("[0,null,\"m\",null,\"split\"]",
                Json.write(Arrays.asList(0L, null, "m", null, "split")));
        // round trip
        assertEquals(m.get("name"), Json.asObject(Json.parse(Json.write(m))).get("name"));
    }
}
