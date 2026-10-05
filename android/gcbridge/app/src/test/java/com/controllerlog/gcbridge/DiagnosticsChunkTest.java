package com.controllerlog.gcbridge;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

import java.util.List;

public class DiagnosticsChunkTest {

    @Test
    public void chunksRejoinAndNeverEndOnANewline() {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < 400; i++) {
            sb.append(i % 7 == 0 ? "" : "x".repeat(i % 50)).append('\n');
        }
        String text = sb.toString();
        for (int cut = 0; cut < 60; cut++) {
            String t = text.substring(cut);
            List<String> parts = ControllerDiagnostics.chunks(t);
            assertEquals(t, String.join("", parts));
            for (int i = 0; i < parts.size(); i++) {
                assertTrue(parts.get(i).length() <= ControllerDiagnostics.LOG_CHUNK);
                if (i < parts.size() - 1) {
                    assertFalse(parts.get(i).endsWith("\n"));
                }
            }
        }
    }
}
