package com.controllerlog.gcbridge;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;
import static org.junit.Assume.assumeTrue;

import org.junit.Test;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public class PadTest {

    @Test
    public void indicesMatchTheSdlOrder() {
        assertEquals(26, Pad.NUM_BUTTONS);
        assertEquals(6, Pad.NUM_AXES);
        assertEquals(0, Pad.buttonIndex("south"));
        assertEquals(11, Pad.buttonIndex("dpad_up"));
        assertEquals(22, Pad.MISC3);
        assertEquals(4, Pad.axisIndex("left_trigger"));
        assertEquals(-1, Pad.buttonIndex("left_trigger"));
        assertTrue(Pad.isDigitalInput("left_trigger"));
        assertFalse(Pad.isDigitalInput("left_x"));
    }

    /** The names must be exactly model.py's BUTTONS / AXES tuples, in order. */
    @Test
    public void namesMatchModelPy() throws Exception {
        Path root = TestFiles.root();
        assumeTrue("repository root not available", root != null);
        Path model = root.resolve("controllerlog/model.py");
        assumeTrue(Files.isRegularFile(model));
        String text = TestFiles.read(model);
        assertArrayMatches(text, "BUTTONS", Pad.BUTTONS);
        assertArrayMatches(text, "AXES", Pad.AXES);
    }

    private static void assertArrayMatches(String text, String tuple, String[] names) {
        Matcher m = Pattern.compile(tuple + ": tuple\\[str, \\.\\.\\.\\] = \\((.*?)\\n\\)", Pattern.DOTALL)
                .matcher(text);
        assertTrue(tuple + " not found in model.py", m.find());
        Matcher item = Pattern.compile("\"([a-z0-9_]+)\"").matcher(m.group(1));
        int i = 0;
        while (item.find()) {
            assertTrue(tuple + " has more entries than Pad", i < names.length);
            assertEquals(tuple + "[" + i + "]", item.group(1), names[i]);
            i++;
        }
        assertEquals(tuple + " length", names.length, i);
    }

    @Test
    public void stateTracksChangesAndDigitalTriggers() {
        Pad.State s = new Pad.State();
        assertTrue(s.setButton(Pad.SOUTH, true));
        assertFalse(s.setButton(Pad.SOUTH, true));
        assertTrue(s.setAxis(Pad.LEFT_TRIGGER, 40000));
        assertEquals(32767, s.axes[Pad.LEFT_TRIGGER]);
        assertTrue(s.pressed("left_trigger"));
        assertTrue(s.setAxis(Pad.LEFT_TRIGGER, 16383));
        assertFalse(s.pressed("left_trigger"));
        assertTrue(s.pressed("south"));
        assertFalse(s.setAxis(99, 1));
        assertEquals("south left_trigger=16383", s.toString());
        Pad.State copy = new Pad.State(s);
        assertEquals(s, copy);
        copy.clear();
        assertFalse(copy.anyPressed());
    }
}
