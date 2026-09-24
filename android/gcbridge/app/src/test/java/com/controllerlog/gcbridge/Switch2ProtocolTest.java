package com.controllerlog.gcbridge;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;
import static org.junit.Assume.assumeTrue;

import org.junit.Test;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public class Switch2ProtocolTest {

    /**
     * controllerlog/input/switch2_usb.py INIT_SEQUENCE, printed with bytes.hex(' ') - the
     * sequence verified on a real NSO GameCube controller (report format byte 0x05).
     */
    private static final String[] PYTHON_INIT_HEX = {
            "07 91 00 01 00 00 00 00",
            "0c 91 00 02 00 04 00 00 27 00 00 00",
            "11 91 00 01 00 00 00 00",
            "0a 91 00 08 00 14 00 00 01 ff ff ff ff ff ff ff ff 35 00 46 00 00 00 00 00 00 00 00",
            "0c 91 00 04 00 04 00 00 27 00 00 00",
            "01 91 00 0c 00 00 00 00",
            "01 91 00 01 00 00 00 00",
            "08 91 00 02 00 04 00 00 01 00 00 00",
            "03 91 00 0a 00 04 00 00 05 00 00 00",
            "03 91 00 0d 00 08 00 00 01 00 ff ff ff ff ff ff",
    };

    private static byte[] bytes(String hex) {
        String[] parts = hex.trim().split("\\s+");
        byte[] b = new byte[parts.length];
        for (int i = 0; i < parts.length; i++) {
            b[i] = (byte) Integer.parseInt(parts[i], 16);
        }
        return b;
    }

    @Test
    public void nintendoFormatSequenceEqualsPythonInitSequence() {
        byte[][] seq = Switch2Protocol.initSequence(Switch2Protocol.FORMAT_NINTENDO);
        assertEquals(PYTHON_INIT_HEX.length, seq.length);
        assertEquals(PYTHON_INIT_HEX.length, Switch2Protocol.initCommandCount());
        for (int i = 0; i < seq.length; i++) {
            assertArrayEquals("command " + (i + 1), bytes(PYTHON_INIT_HEX[i]), seq[i]);
        }
    }

    @Test
    public void standardFormatOnlyChangesTheFormatByte() {
        byte[][] std = Switch2Protocol.initSequence(Switch2Protocol.FORMAT_STANDARD);
        for (int i = 0; i < std.length; i++) {
            byte[] expected = bytes(PYTHON_INIT_HEX[i]);
            if (i == Switch2Protocol.REPORT_FORMAT_COMMAND) {
                expected[Switch2Protocol.REPORT_FORMAT_OFFSET] = 0x0A;
            }
            assertArrayEquals("command " + (i + 1), expected, std[i]);
        }
        assertArrayEquals(bytes("03 91 00 0a 00 04 00 00 0a 00 00 00"),
                std[Switch2Protocol.REPORT_FORMAT_COMMAND]);
        assertEquals("the 9th command is the report format command", 8,
                Switch2Protocol.REPORT_FORMAT_COMMAND);
    }

    @Test
    public void sequencesAreFreshCopies() {
        byte[][] a = Switch2Protocol.initSequence(Switch2Protocol.FORMAT_STANDARD);
        a[0][0] = 0x55;
        a[Switch2Protocol.REPORT_FORMAT_COMMAND][Switch2Protocol.REPORT_FORMAT_OFFSET] = 0x77;
        byte[][] b = Switch2Protocol.initSequence(Switch2Protocol.FORMAT_NINTENDO);
        assertArrayEquals(bytes(PYTHON_INIT_HEX[0]), b[0]);
        assertEquals(0x05, b[Switch2Protocol.REPORT_FORMAT_COMMAND][Switch2Protocol.REPORT_FORMAT_OFFSET]);
    }

    @Test(expected = IllegalArgumentException.class)
    public void rejectsNonByteFormat() {
        Switch2Protocol.initSequence(0x100);
    }

    @Test
    public void flashReadAndLedCommandsMatchPython() {
        assertArrayEquals(bytes("02 91 00 01 00 08 00 00 40 00 00 00 00 30 01 00"),
                Switch2Protocol.flashReadCommand(0x13000));
        assertArrayEquals(bytes("02 91 00 01 00 08 00 00 40 00 00 00 80 c0 1f 00"),
                Switch2Protocol.flashReadCommand(0x1FC080));
        assertArrayEquals(bytes("09 91 00 07 00 08 00 00 01 00 00 00 00 00 00 00"),
                Switch2Protocol.ledCommand(1));
        assertArrayEquals(bytes("09 91 00 07 00 08 00 00 00 00 00 00 00 00 00 00"),
                Switch2Protocol.ledCommand(0));
        assertArrayEquals(bytes("09 91 00 07 00 08 00 00 06 00 00 00 00 00 00 00"),
                Switch2Protocol.ledCommand(8));
        assertArrayEquals(bytes("09 91 00 07 00 08 00 00 01 00 00 00 00 00 00 00"),
                Switch2Protocol.ledCommand(9));
    }

    @Test
    public void parsesSerialFromFlashReply() {
        byte[] reply = new byte[0x50];
        byte[] serial = "HEH50012345678\0junk".getBytes(StandardCharsets.US_ASCII);
        System.arraycopy(serial, 0, reply, 0x12, 16);
        assertEquals("HEH50012345678", Switch2Protocol.parseSerial(reply, reply.length));
        assertNull(Switch2Protocol.parseSerial(reply, 0x4F));
        assertNull(Switch2Protocol.parseSerial(null, 0));
    }

    @Test
    public void decodesStandardReport() {
        byte[] r = new byte[64];
        r[0] = 0x0A;
        // buttons 1 and 21
        r[3] = 0x01;
        r[5] = 0x10;
        // X=0x123, Y=0xABC, Rx=0x800, Rz=0xFFF
        r[6] = 0x23;
        r[7] = (byte) 0xC1;
        r[8] = (byte) 0xAB;
        r[9] = 0x00;
        r[10] = (byte) 0xF8;
        r[11] = (byte) 0xFF;
        Switch2Protocol.StandardReport s = Switch2Protocol.parseStandardReport(r, r.length);
        assertNotNull(s);
        assertEquals((1 << 20) | 1, s.buttons);
        assertEquals(0x123, s.x);
        assertEquals(0xABC, s.y);
        assertEquals(0x800, s.rx);
        assertEquals(0xFFF, s.rz);
        assertTrue(Switch2Protocol.describeInputReport(r, r.length).endsWith("buttons=1,21"));
        r[0] = 0x05;
        assertNull(Switch2Protocol.parseStandardReport(r, r.length));
    }

    @Test
    public void hidButtonFromScanCode() {
        assertEquals(1, Switch2Protocol.hidButtonForScanCode(0x130)); // BTN_SOUTH
        assertEquals(16, Switch2Protocol.hidButtonForScanCode(0x13F));
        assertEquals(1, Switch2Protocol.hidButtonForScanCode(0x120)); // BTN_TRIGGER
        assertEquals(17, Switch2Protocol.hidButtonForScanCode(0x2C0)); // BTN_TRIGGER_HAPPY1
        assertEquals(21, Switch2Protocol.hidButtonForScanCode(0x2C4));
        assertEquals(1, Switch2Protocol.hidButtonForScanCode(0x100)); // BTN_0 (BTN_MISC)
        assertEquals(10, Switch2Protocol.hidButtonForScanCode(0x109)); // BTN_9
        assertEquals(-1, Switch2Protocol.hidButtonForScanCode(0x1E)); // KEY_A
        assertEquals(-1, Switch2Protocol.hidButtonForScanCode(0x140)); // BTN_DIGI
    }

    /**
     * Reads INIT_SEQUENCE straight out of switch2_usb.py (path passed by the Gradle build) so a
     * change on the Python side can't silently diverge. Skipped when the file isn't there.
     */
    @Test
    public void matchesInitSequenceInPythonSource() throws IOException {
        String prop = System.getProperty("switch2.py");
        assumeTrue("switch2.py path not set", prop != null);
        Path py = Paths.get(prop);
        assumeTrue("not found: " + py, Files.isRegularFile(py));
        List<byte[]> fromPython = parsePythonInitSequence(
                new String(Files.readAllBytes(py), StandardCharsets.UTF_8));
        byte[][] ours = Switch2Protocol.initSequence(Switch2Protocol.FORMAT_NINTENDO);
        assertEquals(fromPython.size(), ours.length);
        for (int i = 0; i < ours.length; i++) {
            assertArrayEquals("command " + (i + 1), fromPython.get(i), ours[i]);
        }
    }

    private static List<byte[]> parsePythonInitSequence(String source) {
        int start = source.indexOf("INIT_SEQUENCE:");
        assertTrue("INIT_SEQUENCE not found", start >= 0);
        int end = source.indexOf("\n))", start);
        assertTrue("end of INIT_SEQUENCE not found", end > start);
        StringBuilder code = new StringBuilder();
        String[] lines = source.substring(source.indexOf('\n', start) + 1, end).split("\n");
        for (String line : lines) {
            int hash = line.indexOf('#');
            code.append(hash >= 0 ? line.substring(0, hash) : line).append('\n');
        }
        List<byte[]> out = new ArrayList<>();
        Matcher list = Pattern.compile("\\[([^\\]]*)\\]").matcher(code);
        Pattern number = Pattern.compile("0[xX][0-9a-fA-F]+|\\d+");
        while (list.find()) {
            Matcher n = number.matcher(list.group(1));
            List<Integer> values = new ArrayList<>();
            while (n.find()) {
                String t = n.group();
                values.add(t.startsWith("0x") || t.startsWith("0X")
                        ? Integer.parseInt(t.substring(2), 16) : Integer.parseInt(t));
            }
            byte[] b = new byte[values.size()];
            for (int i = 0; i < b.length; i++) {
                b[i] = (byte) (int) values.get(i);
            }
            out.add(b);
        }
        return out;
    }
}
