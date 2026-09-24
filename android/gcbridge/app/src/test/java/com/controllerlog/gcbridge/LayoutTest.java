package com.controllerlog.gcbridge;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;
import static org.junit.Assume.assumeTrue;

import org.junit.Test;

import java.io.IOException;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

public class LayoutTest {

    private static List<Layout> repositoryLayouts() throws IOException {
        Path root = TestFiles.root();
        assumeTrue("repository root not available", root != null);
        Path dir = root.resolve("controllerlog/layouts");
        assumeTrue(Files.isDirectory(dir));
        List<Layout> out = new ArrayList<>();
        try (DirectoryStream<Path> ds = Files.newDirectoryStream(dir, "*.json")) {
            for (Path p : ds) {
                String stem = p.getFileName().toString().replace(".json", "");
                out.add(Layout.parse(TestFiles.read(p), stem));
            }
        }
        return out;
    }

    @Test
    public void everyRepositoryLayoutParses() throws IOException {
        List<Layout> layouts = repositoryLayouts();
        assertTrue(layouts.size() >= 7);
        for (Layout l : layouts) {
            assertTrue(l.name, l.width > 0 && l.height > 0);
            assertTrue(l.name, !l.elements.isEmpty());
            assertNotNull(l.themeColor("active"));
        }
        assertEquals("gamecube", Layout.forFamily(layouts, "gamecube").name);
        assertEquals("playstation", Layout.forFamily(layouts, "playstation").name);
        assertEquals("generic", Layout.forFamily(layouts, "no-such-family").name);
        assertNotNull(Layout.byName(layouts, "xbox"));
    }

    @Test
    public void validatesLikeLayoutsPy() {
        String ok = "{\"size\":[10,10],\"elements\":[{\"type\":\"button\",\"input\":\"south\","
                + "\"shape\":\"circle\",\"cx\":1,\"cy\":1,\"r\":1}]}";
        Layout l = Layout.parse(ok, "t");
        assertEquals("t", l.name);
        assertEquals("#ffd23f", l.themeColor("active"));
        assertEquals(14.0, l.themeNumber("font_size"), 0);
        for (String bad : new String[]{
                "{\"elements\":[]}",
                "{\"size\":[10,10],\"elements\":[]}",
                "{\"size\":[10,10],\"elements\":[{\"type\":\"button\",\"input\":\"nope\",\"shape\":\"circle\",\"cx\":1,\"cy\":1,\"r\":1}]}",
                "{\"size\":[10,10],\"elements\":[{\"type\":\"button\",\"input\":\"south\",\"shape\":\"rect\",\"x\":1}]}",
                "{\"size\":[10,10],\"elements\":[{\"type\":\"stick\",\"x_axis\":\"left_x\",\"y_axis\":\"bogus\",\"cx\":1,\"cy\":1,\"r\":1}]}",
                "{\"size\":[10,10],\"elements\":[{\"type\":\"blob\"}]}",
        }) {
            try {
                Layout.parse(bad, "bad");
                fail("accepted " + bad);
            } catch (IllegalArgumentException expected) {
                // ok
            }
        }
    }

    @Test
    public void parsesCssColours() {
        assertEquals(0xFFFFD23F, Layout.color("#ffd23f"));
        assertEquals(0xFFFFD23F, Layout.color("#FFD23F"));
        assertEquals(0xFF112233, Layout.color("#123"));
        assertEquals(0x80112233, Layout.color("#11223380"));
        assertEquals(0x88112233, Layout.color("#1238"));
        assertEquals(0, Layout.color("none"));
        assertEquals(0, Layout.color("transparent"));
        assertEquals(0, Layout.color("red"));
        assertEquals(0, Layout.color(null));
    }
}
