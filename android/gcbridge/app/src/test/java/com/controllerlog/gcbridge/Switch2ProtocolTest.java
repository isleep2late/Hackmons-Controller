package com.controllerlog.gcbridge;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
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
import java.util.Locale;
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

    // --- report parsing, calibration and BLE builders vs. the Python implementation ----------

    private static java.util.Map<String, Object> vectors() throws IOException {
        Path root = TestFiles.root();
        assumeTrue("repository root not available", root != null);
        Path f = root.resolve("tests/fixtures/switch2/java_vectors.json");
        assumeTrue("java_vectors.json missing (run make_java_vectors.py)", Files.isRegularFile(f));
        return Json.asObject(Json.parse(TestFiles.read(f)));
    }

    private static Switch2Protocol.Calibration calibration(java.util.Map<String, Object> c) {
        Switch2Protocol.Calibration cal = new Switch2Protocol.Calibration();
        cal.left = stick(Json.asObject(c.get("left")));
        cal.right = stick(Json.asObject(c.get("right")));
        List<Object> tz = Json.asArray(c.get("trigger_zero"));
        cal.triggerZeroLeft = ((Number) tz.get(0)).intValue();
        cal.triggerZeroRight = ((Number) tz.get(1)).intValue();
        return cal;
    }

    private static Switch2Protocol.StickCal stick(java.util.Map<String, Object> s) {
        return new Switch2Protocol.StickCal(axis(Json.asObject(s.get("x"))), axis(Json.asObject(s.get("y"))));
    }

    private static Switch2Protocol.AxisCal axis(java.util.Map<String, Object> a) {
        return new Switch2Protocol.AxisCal((int) Json.num(a, "neutral", 0), (int) Json.num(a, "below", 0),
                (int) Json.num(a, "above", 0));
    }

    private static int[] ints(Object v) {
        List<Object> l = Json.asArray(v);
        int[] out = new int[l.size()];
        for (int i = 0; i < out.length; i++) {
            out[i] = ((Number) l.get(i)).intValue();
        }
        return out;
    }

    @Test
    public void usbReportsDecodeLikeSwitch2UsbPy() throws IOException {
        List<Object> cases = Json.asArray(vectors().get("usb_reports"));
        int[] buttons = new int[Pad.NUM_BUTTONS];
        int[] axes = new int[Pad.NUM_AXES];
        for (Object o : cases) {
            java.util.Map<String, Object> c = Json.asObject(o);
            byte[] report = TestFiles.hex((String) c.get("report"));
            String model = (String) c.get("model");
            assertTrue(Switch2Protocol.parseInputReport(model, report, 0, report.length,
                    calibration(Json.asObject(c.get("calibration"))), Switch2Protocol.DEFAULT_DEADZONE,
                    buttons, axes));
            assertArrayEquals(model + " buttons " + c.get("report"), ints(c.get("buttons")), buttons);
            assertArrayEquals(model + " axes " + c.get("report"), ints(c.get("axes")), axes);
        }
        assertTrue(cases.size() >= 10);
    }

    @Test
    public void bleReportIsTheUsbReportWithoutTheIdByte() throws IOException {
        List<Object> cases = Json.asArray(vectors().get("ble_reports"));
        int[] buttons = new int[Pad.NUM_BUTTONS];
        int[] axes = new int[Pad.NUM_AXES];
        for (Object o : cases) {
            java.util.Map<String, Object> c = Json.asObject(o);
            byte[] data = TestFiles.hex((String) c.get("data"));
            byte[] withId = new byte[data.length + 1];
            withId[0] = (byte) Switch2Protocol.FORMAT_NINTENDO;
            System.arraycopy(data, 0, withId, 1, data.length);
            String model = (String) c.get("model");
            assertTrue(Switch2Protocol.parseInputReport(model, withId, 0, withId.length,
                    Switch2Protocol.Calibration.defaults(model), Switch2Protocol.DEFAULT_DEADZONE,
                    buttons, axes));
            assertArrayEquals(model + " buttons", ints(c.get("buttons")), buttons);
            int[] want = ints(c.get("axes"));
            for (int i = 0; i < want.length; i++) {
                // the Python BLE reader rounds in floating point and negates instead of
                // complementing the Y axes; agree within 4 LSB of 32767
                assertTrue(model + " axis " + i + ": " + axes[i] + " vs " + want[i],
                        Math.abs(axes[i] - want[i]) <= 4);
            }
        }
    }

    @Test
    public void nonInputReportsAreRejected() {
        int[] buttons = new int[Pad.NUM_BUTTONS];
        int[] axes = new int[Pad.NUM_AXES];
        byte[] r = new byte[64];
        r[0] = 0x0A;
        assertFalse(Switch2Protocol.parseInputReport("gamecube", r, 0, 64,
                Switch2Protocol.Calibration.defaults("gamecube"), 0.03, buttons, axes));
        r[0] = 0x05;
        assertFalse(Switch2Protocol.parseInputReport("gamecube", r, 0, 63,
                Switch2Protocol.Calibration.defaults("gamecube"), 0.03, buttons, axes));
        assertTrue(Switch2Protocol.parseInputReport("gamecube", r, 0, 64,
                Switch2Protocol.Calibration.defaults("gamecube"), 0.03, buttons, axes));
    }

    @Test
    public void calibrationFromFlashMatchesPython() throws IOException {
        java.util.Map<String, Object> v = Json.asObject(vectors().get("usb_calibration"));
        java.util.Map<String, Object> flash = Json.asObject(v.get("flash"));
        Switch2Protocol.Calibration cal = Switch2Protocol.readCalibration(address -> {
            Object hex = flash.get(String.format(Locale.ROOT, "0x%X", address));
            return hex == null ? null : TestFiles.hex((String) hex);
        }, (String) v.get("model"));
        java.util.Map<String, Object> want = Json.asObject(v.get("expected"));
        assertEquals(want.get("serial"), cal.serial);
        assertEquals(want.get("source"), cal.source);
        assertEquals((int) Json.num(Json.asObject(Json.asObject(want.get("left")).get("x")), "neutral", 0), cal.left.x.neutral);
        assertEquals((int) Json.num(Json.asObject(Json.asObject(want.get("left")).get("y")), "below", 0), cal.left.y.below);
        assertEquals((int) Json.num(Json.asObject(Json.asObject(want.get("right")).get("x")), "above", 0), cal.right.x.above);
        assertEquals(((Number) Json.asArray(want.get("trigger_zero")).get(0)).intValue(), cal.triggerZeroLeft);
        assertEquals(((Number) Json.asArray(want.get("trigger_zero")).get(1)).intValue(), cal.triggerZeroRight);
        // defaults
        java.util.Map<String, Object> defaults = Json.asObject(v.get("defaults"));
        for (String model : new String[]{"gamecube", "pro"}) {
            java.util.Map<String, Object> d = Json.asObject(defaults.get(model));
            Switch2Protocol.Calibration c = Switch2Protocol.Calibration.defaults(model);
            assertEquals((int) Json.num(Json.asObject(Json.asObject(d.get("left")).get("x")), "below", 0), c.left.x.below);
            assertEquals((int) Json.num(Json.asObject(Json.asObject(d.get("right")).get("y")), "above", 0), c.right.y.above);
        }
        // erased and unreadable flash -> defaults
        Switch2Protocol.Calibration none = Switch2Protocol.readCalibration(address -> null, "gamecube");
        assertEquals("defaults", none.source);
        assertEquals(1225, none.left.x.below);
    }

    @Test
    public void usbCommandBytesMatchPython() throws IOException {
        java.util.Map<String, Object> cmds = Json.asObject(vectors().get("usb_commands"));
        java.util.Map<String, Object> flash = Json.asObject(cmds.get("flash_read"));
        for (java.util.Map.Entry<String, Object> e : flash.entrySet()) {
            int addr = Integer.parseInt(e.getKey().substring(2), 16);
            assertArrayEquals(e.getKey(), TestFiles.hex((String) e.getValue()), Switch2Protocol.flashReadCommand(addr));
        }
        java.util.Map<String, Object> led = Json.asObject(cmds.get("led"));
        for (java.util.Map.Entry<String, Object> e : led.entrySet()) {
            assertArrayEquals("led " + e.getKey(), TestFiles.hex((String) e.getValue()),
                    Switch2Protocol.ledCommand(Integer.parseInt(e.getKey())));
        }
    }

    @Test
    public void bleCommandBytesMatchPython() throws IOException {
        java.util.Map<String, Object> cmds = Json.asObject(vectors().get("ble_commands"));
        for (Object o : Json.asArray(cmds.get("memory_read"))) {
            java.util.Map<String, Object> c = Json.asObject(o);
            int addr = Integer.parseInt(((String) c.get("address")).substring(2), 16);
            assertArrayEquals((String) c.get("address"), TestFiles.hex((String) c.get("hex")),
                    Switch2Protocol.buildMemoryRead(addr, (int) Json.num(c, "length", 0)));
        }
        java.util.Map<String, Object> leds = Json.asObject(cmds.get("player_leds"));
        for (java.util.Map.Entry<String, Object> e : leds.entrySet()) {
            assertArrayEquals("leds " + e.getKey(), TestFiles.hex((String) e.getValue()),
                    Switch2Protocol.buildPlayerLeds(Integer.parseInt(e.getKey())));
        }
        assertArrayEquals(TestFiles.hex((String) cmds.get("feature_set_mask")),
                Switch2Protocol.buildFeatureCommand(Switch2Protocol.SUB_FEATURE_SET_MASK, Switch2Protocol.DEFAULT_FEATURES));
        assertArrayEquals(TestFiles.hex((String) cmds.get("feature_enable")),
                Switch2Protocol.buildFeatureCommand(Switch2Protocol.SUB_FEATURE_ENABLE, Switch2Protocol.DEFAULT_FEATURES));
        assertArrayEquals(TestFiles.hex((String) cmds.get("rate_descriptor")), Switch2Protocol.BLE_RATE_VALUE);
        java.util.Map<String, Object> uuids = Json.asObject(vectors().get("ble_uuids"));
        assertEquals(uuids.get("service"), Switch2Protocol.BLE_SERVICE);
        assertEquals(uuids.get("input_common"), Switch2Protocol.BLE_INPUT_COMMON);
        assertEquals(uuids.get("command"), Switch2Protocol.BLE_COMMAND);
        assertEquals(uuids.get("command_response"), Switch2Protocol.BLE_COMMAND_RESPONSE);
        assertEquals(uuids.get("rate_descriptor"), Switch2Protocol.BLE_RATE_DESCRIPTOR);
        java.util.Map<String, Object> models = Json.asObject(uuids.get("models"));
        for (String key : new String[]{"gamecube", "pro"}) {
            java.util.Map<String, Object> m = Json.asObject(models.get(key));
            int pid = (int) Json.num(m, "product_id", 0);
            assertEquals(key, Switch2Protocol.modelKey(pid));
            assertEquals(m.get("input"), Switch2Protocol.bleModelInputUuid(pid));
            assertEquals(m.get("ext_response"), Switch2Protocol.bleModelResponseUuid(pid));
        }
    }

    @Test
    public void bleAdvertisementsAndResponsesParseLikePython() throws IOException {
        for (Object o : Json.asArray(vectors().get("ble_manufacturer_data"))) {
            java.util.Map<String, Object> c = Json.asObject(o);
            Switch2Protocol.Advertisement adv = Switch2Protocol.parseManufacturerData(
                    TestFiles.hex((String) c.get("payload")));
            java.util.Map<String, Object> want = Json.asObject(c.get("expected"));
            if (want == null) {
                assertNull((String) c.get("payload"), adv);
                continue;
            }
            assertNotNull((String) c.get("payload"), adv);
            assertEquals((int) Json.num(want, "product_id", 0), adv.productId);
            assertEquals(want.get("model"), adv.model);
            assertEquals(want.get("wake"), adv.wake);
            assertEquals(want.get("host_address"), adv.hostAddress);
            assertEquals(want.get("pairing_mode"), adv.pairingMode);
        }
        for (Object o : Json.asArray(vectors().get("ble_command_responses"))) {
            java.util.Map<String, Object> c = Json.asObject(o);
            Switch2Protocol.CommandResponse r = Switch2Protocol.parseCommandResponse(
                    TestFiles.hex((String) c.get("data")));
            java.util.Map<String, Object> want = Json.asObject(c.get("expected"));
            if (want == null) {
                assertNull(r);
                continue;
            }
            assertNotNull(r);
            assertEquals((int) Json.num(want, "cmd", -1), r.cmd);
            assertEquals((int) Json.num(want, "sub", -1), r.sub);
            assertEquals((int) Json.num(want, "ack", -1), r.ack);
            assertArrayEquals(TestFiles.hex((String) want.get("payload")), r.payload);
            java.util.Map<String, Object> mem = Json.asObject(c.get("memory"));
            if (mem != null) {
                assertEquals(Integer.parseInt(((String) mem.get("address")).substring(2), 16),
                        Switch2Protocol.memoryReadAddress(r));
                assertArrayEquals(TestFiles.hex((String) mem.get("data")), Switch2Protocol.memoryReadData(r));
            }
        }
    }
}
