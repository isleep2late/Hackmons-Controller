package com.controllerlog.gcbridge;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public class CtlogWriterTest {

    @Test
    public void writesTheLogfilePyLayout() throws IOException {
        File f = File.createTempFile("gcbridge", ".ctlog");
        f.deleteOnExit();
        Map<String, Object> extra = new LinkedHashMap<>();
        extra.put("source", "gcbridge");
        Map<String, Object> dev = new LinkedHashMap<>();
        dev.put("id", 0);
        dev.put("name", "Test pad");
        dev.put("backend", "android");
        dev.put("family", "gamecube");
        try (CtlogWriter w = new CtlogWriter(f, extra, null)) {
            w.device(0, 0, dev);
            w.button(1_500_000L, 0, Pad.SOUTH, true);
            w.axis(2_000_000L, 0, Pad.LEFT_X, -412);
            w.marker(3_000_000L, "split:1");
            w.button(4_000_000L, 0, Pad.SOUTH, false);
            w.disconnect(5_000_000L, 0);
            assertEquals(6, w.count());
        }
        List<String> lines = Files.readAllLines(f.toPath(), StandardCharsets.UTF_8);
        assertEquals(7, lines.size());
        Map<String, Object> header = Json.asObject(Json.parse(lines.get(0)));
        assertEquals("controllerlog", header.get("format"));
        assertEquals(1L, header.get("version"));
        assertEquals("ns", header.get("time_unit"));
        assertEquals("gcbridge", header.get("source"));
        assertTrue(((String) header.get("created_utc")).endsWith("+00:00"));
        assertTrue(header.get("meta") instanceof Map);
        assertEquals("[0,0,\"+\",null,{\"id\":0,\"name\":\"Test pad\",\"backend\":\"android\",\"family\":\"gamecube\"}]",
                lines.get(1));
        assertEquals("[1500000,0,\"b\",0,1]", lines.get(2));
        assertEquals("[2000000,0,\"a\",0,-412]", lines.get(3));
        assertEquals("[3000000,null,\"m\",null,\"split:1\"]", lines.get(4));
        assertEquals("[4000000,0,\"b\",0,0]", lines.get(5));
        assertEquals("[5000000,0,\"-\"]", lines.get(6));
    }

    @Test
    public void hubRecordsOnlyChangesAndRebasesTime() throws IOException {
        File f = File.createTempFile("gcbridge", ".ctlog");
        f.deleteOnExit();
        InputHub hub = InputHub.get();
        InputHub.Device d = hub.connect("test:" + System.nanoTime(), "Hub pad", "test", "xbox",
                0x045E, 0x0B13, "wireless", null);
        hub.startRecording(f, null, null);
        long t = InputHub.now();
        hub.button(d, Pad.EAST, true, t + 1_000_000);
        hub.button(d, Pad.EAST, true, t + 2_000_000);   // no change: not written
        int[] buttons = new int[Pad.NUM_BUTTONS];
        int[] axes = new int[Pad.NUM_AXES];
        buttons[Pad.EAST] = 1;
        axes[Pad.RIGHT_TRIGGER] = 30000;
        hub.state(d, buttons, axes, t + 3_000_000);      // only the trigger is new
        hub.axis(d, Pad.RIGHT_TRIGGER, 30000, t + 4_000_000);
        hub.disconnect(d.key);                          // releases east + trigger, then "-"
        assertTrue(hub.recordingRows() >= 6);
        hub.stopRecording();
        List<String> lines = Files.readAllLines(f.toPath(), StandardCharsets.UTF_8);
        assertTrue(lines.get(1).startsWith("[0," + d.id + ",\"+\","));
        assertTrue(lines.get(2).endsWith("," + d.id + ",\"b\"," + Pad.EAST + ",1]"));
        assertTrue(lines.get(3).endsWith("," + d.id + ",\"a\"," + Pad.RIGHT_TRIGGER + ",30000]"));
        assertTrue(lines.get(4).endsWith("\"b\"," + Pad.EAST + ",0]"));
        assertTrue(lines.get(5).endsWith("\"a\"," + Pad.RIGHT_TRIGGER + ",0]"));
        assertTrue(lines.get(6).endsWith("\"-\"]"));
        assertEquals(d, hub.find(d.key));
        assertTrue(!d.connected);
    }
}
