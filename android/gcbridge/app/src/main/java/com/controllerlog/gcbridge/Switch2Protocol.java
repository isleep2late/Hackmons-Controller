package com.controllerlog.gcbridge;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * Switch 2 controller USB command bytes and report decoding. Plain Java (no Android classes)
 * so it can be unit-tested on the JVM.
 *
 * <p>The command bytes mirror {@code INIT_SEQUENCE}, {@code flash_read_command} and
 * {@code led_command} in {@code controllerlog/input/switch2_usb.py} (from SDL 3.4's
 * {@code SDL_hidapi_switch2.c}), which were verified on a real NSO GameCube controller over USB.
 */
public final class Switch2Protocol {

    private Switch2Protocol() {
    }

    public static final int NINTENDO_VID = 0x057E;
    public static final int PID_GAMECUBE = 0x2073;
    public static final int PID_PRO = 0x2069;

    /** Report format byte: standard HID gamepad report, id 0x0A (buttons + 4 x 12-bit axes). */
    public static final int FORMAT_STANDARD = 0x0A;
    /** Report format byte: Nintendo vendor report, id 0x05 (what SDL and ControllerLog parse). */
    public static final int FORMAT_NINTENDO = 0x05;

    /** Index of the "report format" command within the init sequence (the 9th command). */
    public static final int REPORT_FORMAT_COMMAND = 8;
    /** Offset of the format byte inside that command. */
    public static final int REPORT_FORMAT_OFFSET = 8;

    public static final int PACKET_SIZE = 64;
    public static final int SERIAL_FLASH_ADDRESS = 0x13000;
    public static final int FLASH_REPLY_LEN = 0x50;
    public static final int FLASH_DATA_OFFSET = 0x10;

    /** Same order and bytes as switch2_usb.py INIT_SEQUENCE (format byte = 0x05 there). */
    private static final int[][] INIT = {
            {0x07, 0x91, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00},
            {0x0C, 0x91, 0x00, 0x02, 0x00, 0x04, 0x00, 0x00, 0x27, 0x00, 0x00, 0x00},
            {0x11, 0x91, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00},
            {0x0A, 0x91, 0x00, 0x08, 0x00, 0x14, 0x00, 0x00, 0x01, 0xFF, 0xFF, 0xFF,
                    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x35, 0x00, 0x46, 0x00, 0x00, 0x00, 0x00,
                    0x00, 0x00, 0x00, 0x00},
            {0x0C, 0x91, 0x00, 0x04, 0x00, 0x04, 0x00, 0x00, 0x27, 0x00, 0x00, 0x00},
            {0x01, 0x91, 0x00, 0x0C, 0x00, 0x00, 0x00, 0x00},
            {0x01, 0x91, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00},
            {0x08, 0x91, 0x00, 0x02, 0x00, 0x04, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00},
            {0x03, 0x91, 0x00, 0x0A, 0x00, 0x04, 0x00, 0x00, 0x05, 0x00, 0x00, 0x00},
            {0x03, 0x91, 0x00, 0x0D, 0x00, 0x08, 0x00, 0x00, 0x01, 0x00, 0xFF, 0xFF,
                    0xFF, 0xFF, 0xFF, 0xFF},
    };

    private static final String[] INIT_LABELS = {
            "unknown 0x07",
            "feature mask",
            "unknown 0x11",
            "rumble data",
            "enable features",
            "unknown 0x01/0x0C",
            "enable rumble",
            "grip buttons",
            "report format",
            "start output",
    };

    private static final int[] PLAYER_LED = {0x01, 0x03, 0x07, 0x0F, 0x09, 0x05, 0x0D, 0x06};

    public static int initCommandCount() {
        return INIT.length;
    }

    public static String initLabel(int index) {
        return INIT_LABELS[index];
    }

    /** The init sequence with {@code format} (0x0A or 0x05) in the report-format command. */
    public static byte[][] initSequence(int format) {
        if (format < 0 || format > 0xFF) {
            throw new IllegalArgumentException("format must be a byte: " + format);
        }
        byte[][] out = new byte[INIT.length][];
        for (int i = 0; i < INIT.length; i++) {
            out[i] = toBytes(INIT[i]);
        }
        out[REPORT_FORMAT_COMMAND][REPORT_FORMAT_OFFSET] = (byte) format;
        return out;
    }

    /** Read 0x40 bytes of SPI flash at {@code address}; the reply is 0x50 bytes, data at 0x10. */
    public static byte[] flashReadCommand(int address) {
        return new byte[]{0x02, (byte) 0x91, 0x00, 0x01, 0x00, 0x08, 0x00, 0x00, 0x40, 0x00, 0x00,
                0x00, (byte) address, (byte) (address >>> 8), (byte) (address >>> 16),
                (byte) (address >>> 24)};
    }

    /** Player LED pattern for players 1-8 (0 = all off), as in switch2_usb.led_command. */
    public static byte[] ledCommand(int player) {
        int pattern = player > 0 ? PLAYER_LED[(player - 1) % 8] : 0;
        return new byte[]{0x09, (byte) 0x91, 0x00, 0x07, 0x00, 0x08, 0x00, 0x00, (byte) pattern,
                0, 0, 0, 0, 0, 0, 0};
    }

    /**
     * Serial number from a flash-read reply of address 0x13000 (string at data offset 2, up to
     * 16 bytes, NUL-terminated), or null if the reply is too short.
     */
    public static String parseSerial(byte[] reply, int len) {
        if (reply == null || len < FLASH_REPLY_LEN) {
            return null;
        }
        StringBuilder sb = new StringBuilder();
        int start = FLASH_DATA_OFFSET + 2;
        for (int i = start; i < start + 16; i++) {
            int c = reply[i] & 0xFF;
            if (c == 0) {
                break;
            }
            sb.append(c < 0x80 ? (char) c : '�');
        }
        return sb.toString().trim();
    }

    public static boolean isSupported(int vendorId, int productId) {
        return vendorId == NINTENDO_VID && (productId == PID_GAMECUBE || productId == PID_PRO);
    }

    public static String modelName(int productId) {
        switch (productId) {
            case PID_GAMECUBE:
                return "NSO GameCube controller (Switch 2)";
            case PID_PRO:
                return "Switch 2 Pro Controller";
            default:
                return String.format(Locale.ROOT, "unknown Nintendo device 0x%04X", productId);
        }
    }

    public static String formatName(int format) {
        switch (format) {
            case FORMAT_STANDARD:
                return "standard gamepad (0x0A)";
            case FORMAT_NINTENDO:
                return "Nintendo/SDL (0x05)";
            default:
                return String.format(Locale.ROOT, "0x%02X", format);
        }
    }

    public static String hex(byte[] b, int off, int len) {
        StringBuilder sb = new StringBuilder(len * 3);
        for (int i = off; i < off + len; i++) {
            if (i > off) {
                sb.append(' ');
            }
            sb.append(Character.forDigit((b[i] >> 4) & 0xF, 16));
            sb.append(Character.forDigit(b[i] & 0xF, 16));
        }
        return sb.toString();
    }

    public static String hex(byte[] b) {
        return hex(b, 0, b.length);
    }

    // --- input reports (only visible to the app via the optional "peek") --------------------

    /** Decoded standard gamepad report 0x0A: 21 buttons and 4 raw 12-bit axes (0..4095). */
    public static final class StandardReport {
        /** Bit n set = HID button n+1 pressed. */
        public final int buttons;
        public final int x;
        public final int y;
        public final int rx;
        public final int rz;

        StandardReport(int buttons, int x, int y, int rx, int rz) {
            this.buttons = buttons;
            this.x = x;
            this.y = y;
            this.rx = rx;
            this.rz = rz;
        }
    }

    /**
     * Report 0x0A per the controller's HID report descriptor: id, 2 vendor bytes, 21 buttons +
     * 3 padding bits (bytes 3..5), 4 x 12-bit axes X/Y/Rx/Rz packed little-endian (bytes 6..11),
     * then 52 vendor bytes.
     */
    public static StandardReport parseStandardReport(byte[] r, int len) {
        if (r == null || len < 12 || (r[0] & 0xFF) != FORMAT_STANDARD) {
            return null;
        }
        int buttons = ((r[3] & 0xFF) | (r[4] & 0xFF) << 8 | (r[5] & 0xFF) << 16) & 0x1FFFFF;
        return new StandardReport(buttons,
                u12(r[6], r[7], false), u12(r[7], r[8], true),
                u12(r[9], r[10], false), u12(r[10], r[11], true));
    }

    static int u12(byte lo, byte hi, boolean highNibble) {
        int l = lo & 0xFF;
        int h = hi & 0xFF;
        return highNibble ? (l >> 4) | (h << 4) : l | ((h & 0x0F) << 8);
    }

    /** One-line human description of a raw HID input report. */
    public static String describeInputReport(byte[] r, int len) {
        if (r == null || len <= 0) {
            return "(empty)";
        }
        int id = r[0] & 0xFF;
        StandardReport s = parseStandardReport(r, len);
        if (s != null) {
            StringBuilder sb = new StringBuilder();
            sb.append(String.format(Locale.ROOT, "id=0x0A X=%4d Y=%4d Rx=%4d Rz=%4d buttons=",
                    s.x, s.y, s.rx, s.rz));
            if (s.buttons == 0) {
                sb.append('-');
            } else {
                boolean first = true;
                for (int i = 0; i < 21; i++) {
                    if ((s.buttons & (1 << i)) != 0) {
                        if (!first) {
                            sb.append(',');
                        }
                        sb.append(i + 1);
                        first = false;
                    }
                }
            }
            return sb.toString();
        }
        if (id == FORMAT_NINTENDO && len >= 63) {
            return String.format(Locale.ROOT,
                    "id=0x05 btn=%02x %02x %02x L=(%4d,%4d) R=(%4d,%4d) trig=(%3d,%3d)",
                    r[5] & 0xFF, r[6] & 0xFF, r[7] & 0xFF,
                    u12(r[11], r[12], false), u12(r[12], r[13], true),
                    u12(r[14], r[15], false), u12(r[15], r[16], true),
                    r[61] & 0xFF, r[62] & 0xFF);
        }
        return String.format(Locale.ROOT, "id=0x%02X len=%d %s", id, len,
                hex(r, 0, Math.min(len, 16)));
    }

    /**
     * Guess the HID button number (1-based) from a Linux key code, assuming the kernel's
     * hid-generic/hid-input mapping for the Button usage page: buttons 1-16 go to BTN_GAMEPAD
     * (0x130..) in a Gamepad collection or BTN_JOYSTICK (0x120..) in a Joystick collection, and
     * buttons 17+ go to BTN_TRIGGER_HAPPY1 (0x2C0..). In any other collection every button goes
     * to BTN_MISC + n - 1 (0x100..). Returns -1 for other codes.
     */
    public static int hidButtonForScanCode(int scanCode) {
        if (scanCode >= 0x100 && scanCode <= 0x11F) {
            return scanCode - 0x100 + 1;
        }
        if (scanCode >= 0x120 && scanCode <= 0x13F) {
            return (scanCode & 0x0F) + 1;
        }
        if (scanCode >= 0x2C0 && scanCode <= 0x2E7) {
            return scanCode - 0x2C0 + 17;
        }
        return -1;
    }


    // --- models --------------------------------------------------------------------------

    public static final String MODEL_GAMECUBE = "gamecube";
    public static final String MODEL_PRO = "pro";

    /** "gamecube" / "pro" for a supported product id, else null. */
    public static String modelKey(int productId) {
        switch (productId) {
            case PID_GAMECUBE:
                return MODEL_GAMECUBE;
            case PID_PRO:
                return MODEL_PRO;
            default:
                return null;
        }
    }

    // --- calibration (switch2_usb.py) -----------------------------------------------------

    /** One stick axis: raw neutral and the raw span below / above it to full deflection. */
    public static final class AxisCal {
        public final int neutral;
        public final int below;
        public final int above;

        public AxisCal(int neutral, int below, int above) {
            this.neutral = neutral;
            this.below = below;
            this.above = above;
        }
    }

    public static final class StickCal {
        public final AxisCal x;
        public final AxisCal y;

        public StickCal(AxisCal x, AxisCal y) {
            this.x = x;
            this.y = y;
        }

        static StickCal uniform(int neutral, int span) {
            return new StickCal(new AxisCal(neutral, span, span), new AxisCal(neutral, span, span));
        }
    }

    public static final int DEFAULT_TRIGGER_ZERO = 30;
    public static final int TRIGGER_FULL = 232;
    public static final double DEFAULT_DEADZONE = 0.03;
    private static final byte[] USER_CAL_MAGIC = {(byte) 0xB2, (byte) 0xA1};

    /** Flash addresses (ndeadly memory_layout.md, SDL). */
    public static final int ADDR_DEVICE_INFO = 0x13000;
    public static final int ADDR_FACTORY_LEFT = 0x13080;
    public static final int ADDR_FACTORY_RIGHT = 0x130C0;
    public static final int ADDR_TRIGGER_ZERO = 0x13140;
    public static final int ADDR_USER_LEFT = 0x1FC040;
    public static final int ADDR_USER_RIGHT = 0x1FC060;
    public static final int ADDR_USER_RIGHT_SDL = 0x1FC080;
    public static final int FACTORY_STICK_OFFSET = 0x28;

    public static final class Calibration {
        public StickCal left;
        public StickCal right;
        public int triggerZeroLeft = DEFAULT_TRIGGER_ZERO;
        public int triggerZeroRight = DEFAULT_TRIGGER_ZERO;
        public String serial = "";
        public String source = "defaults";

        public static Calibration defaults(String model) {
            Calibration c = new Calibration();
            if (MODEL_GAMECUBE.equals(model)) {
                c.left = StickCal.uniform(2048, 1225);
                c.right = StickCal.uniform(2048, 1120);
            } else {
                c.left = StickCal.uniform(2048, 1610);
                c.right = StickCal.uniform(2048, 1610);
            }
            return c;
        }
    }

    /** Reads one 0x40-byte flash block; null when the read failed. */
    public interface FlashReader {
        byte[] read(int address);
    }

    /**
     * 9 bytes: neutral x/y, max x/y (span above), min x/y (span below) as packed 12-bit pairs.
     * Null for erased flash or zero fields.
     */
    public static StickCal parseStickCalibration(byte[] b, int off) {
        if (b == null || b.length < off + 9) {
            return null;
        }
        boolean erased = true;
        for (int i = 0; i < 9; i++) {
            if ((b[off + i] & 0xFF) != 0xFF) {
                erased = false;
                break;
            }
        }
        if (erased) {
            return null;
        }
        int nx = u12(b[off], b[off + 1], false), ny = u12(b[off + 1], b[off + 2], true);
        int ax = u12(b[off + 3], b[off + 4], false), ay = u12(b[off + 4], b[off + 5], true);
        int bx = u12(b[off + 6], b[off + 7], false), by = u12(b[off + 7], b[off + 8], true);
        if (nx == 0 || ny == 0 || ax == 0 || ay == 0 || bx == 0 || by == 0) {
            return null;
        }
        return new StickCal(new AxisCal(nx, bx, ax), new AxisCal(ny, by, ay));
    }

    /** Serial number string from the device info block at 0x13000. */
    public static String serialFromBlock(byte[] blk) {
        if (blk == null || blk.length < 18) {
            return "";
        }
        StringBuilder sb = new StringBuilder();
        for (int i = 2; i < 18; i++) {
            int c = blk[i] & 0xFF;
            if (c == 0) {
                break;
            }
            sb.append(c < 0x80 ? (char) c : '�');
        }
        return sb.toString().trim();
    }

    /** Factory stick calibration, user calibration if present, GameCube trigger rest values. */
    public static Calibration readCalibration(FlashReader flash, String model) {
        Calibration cal = Calibration.defaults(model);
        List<String> got = new ArrayList<>();
        byte[] blk = flash.read(ADDR_DEVICE_INFO);
        if (blk != null) {
            cal.serial = serialFromBlock(blk);
        }
        int[] factory = {ADDR_FACTORY_LEFT, ADDR_FACTORY_RIGHT};
        for (int side = 0; side < 2; side++) {
            blk = flash.read(factory[side]);
            StickCal sc = blk != null ? parseStickCalibration(blk, FACTORY_STICK_OFFSET) : null;
            if (sc != null) {
                if (side == 0) {
                    cal.left = sc;
                } else {
                    cal.right = sc;
                }
                got.add("factory " + (side == 0 ? "left" : "right"));
            }
        }
        // User recalibration: SDL reads the right stick at 0x1FC080, ndeadly/BlueRetro at
        // 0x1FC060 - take whichever carries the magic.
        int[][] user = {{ADDR_USER_LEFT}, {ADDR_USER_RIGHT, ADDR_USER_RIGHT_SDL}};
        for (int side = 0; side < 2; side++) {
            for (int addr : user[side]) {
                blk = flash.read(addr);
                if (blk != null && blk.length >= 11 && blk[0] == USER_CAL_MAGIC[0]
                        && blk[1] == USER_CAL_MAGIC[1]) {
                    StickCal sc = parseStickCalibration(blk, 2);
                    if (sc != null) {
                        if (side == 0) {
                            cal.left = sc;
                        } else {
                            cal.right = sc;
                        }
                        got.add("user " + (side == 0 ? "left" : "right"));
                        break;
                    }
                }
            }
        }
        if (MODEL_GAMECUBE.equals(model)) {
            blk = flash.read(ADDR_TRIGGER_ZERO);
            if (blk != null && blk.length >= 2 && (blk[0] & 0xFF) != 0xFF && (blk[1] & 0xFF) != 0xFF) {
                cal.triggerZeroLeft = blk[0] & 0xFF;
                cal.triggerZeroRight = blk[1] & 0xFF;
                got.add("triggers");
            }
        }
        if (!got.isEmpty()) {
            cal.source = String.join(", ", got);
        }
        return cal;
    }

    // --- input report 0x05 (switch2_usb.py parse_report) ------------------------------------

    /** SDL MapJoystickAxis: scale by the span on each side of neutral; Y is inverted. */
    public static int mapAxis(int raw, AxisCal cal, boolean invert) {
        int v = raw - cal.neutral;
        int span = v < 0 ? cal.below : cal.above;
        if (span <= 0) {
            span = 2048;
        }
        double f = (double) v / span;
        int out = (int) Math.max(-32768.0, Math.min(32767.0, f * 32767));
        return invert ? ~out : out;
    }

    /** GameCube analog L/R: rest value -> 0, 232 -> full, as 0..32767 (SDL MapTriggerAxis). */
    public static int mapTrigger(int raw, int zero) {
        double f = (double) (raw - zero) / Math.max(1, TRIGGER_FULL - zero);
        return (int) Math.rint(Math.max(0.0, Math.min(1.0, f)) * 32767);
    }

    /** Zero a stick inside {@code dz} (fraction of full scale) and rescale the rest. */
    public static void radialDeadzone(int[] xy, double dz) {
        if (dz <= 0) {
            return;
        }
        double x = xy[0], y = xy[1];
        double mag = Math.sqrt(x * x + y * y) / 32767;
        if (mag <= dz) {
            xy[0] = 0;
            xy[1] = 0;
            return;
        }
        double k = (mag - dz) / (1 - dz) / mag;
        xy[0] = (int) Math.max(-32768.0, Math.min(32767.0, x * k));
        xy[1] = (int) Math.max(-32768.0, Math.min(32767.0, y * k));
    }

    // (report byte, bit mask, canonical button), USB offsets (report id at 0).
    private static final int[][] GC_BITS = {
            {5, 0x01, Pad.WEST}, {5, 0x02, Pad.NORTH}, {5, 0x04, Pad.SOUTH}, {5, 0x08, Pad.EAST},
            {5, 0x40, Pad.MISC4}, {5, 0x80, Pad.RIGHT_SHOULDER},
            {6, 0x02, Pad.START}, {6, 0x10, Pad.GUIDE}, {6, 0x20, Pad.MISC1}, {6, 0x40, Pad.MISC2},
            {7, 0x01, Pad.DPAD_DOWN}, {7, 0x02, Pad.DPAD_UP}, {7, 0x04, Pad.DPAD_RIGHT},
            {7, 0x08, Pad.DPAD_LEFT}, {7, 0x40, Pad.MISC3}, {7, 0x80, Pad.LEFT_SHOULDER},
    };
    private static final int[][] PRO_BITS = {
            {5, 0x01, Pad.WEST}, {5, 0x02, Pad.NORTH}, {5, 0x04, Pad.SOUTH}, {5, 0x08, Pad.EAST},
            {5, 0x40, Pad.RIGHT_SHOULDER},
            {6, 0x01, Pad.BACK}, {6, 0x02, Pad.START}, {6, 0x04, Pad.RIGHT_STICK},
            {6, 0x08, Pad.LEFT_STICK}, {6, 0x10, Pad.GUIDE}, {6, 0x20, Pad.MISC1},
            {6, 0x40, Pad.MISC2},
            {7, 0x01, Pad.DPAD_DOWN}, {7, 0x02, Pad.DPAD_UP}, {7, 0x04, Pad.DPAD_RIGHT},
            {7, 0x08, Pad.DPAD_LEFT}, {7, 0x40, Pad.LEFT_SHOULDER},
            {8, 0x01, Pad.RIGHT_PADDLE1}, {8, 0x02, Pad.LEFT_PADDLE1},
    };

    /**
     * Decodes a 64-byte Nintendo input report (id 0x05 at {@code data[off]}) into canonical
     * {@code buttons} (26) and {@code axes} (6). Over Bluetooth the same report arrives without
     * the id byte: pass a buffer with 0x05 prepended.
     *
     * @return false if this is not a complete 0x05 report
     */
    public static boolean parseInputReport(String model, byte[] data, int off, int len,
                                           Calibration cal, double deadzone, int[] buttons,
                                           int[] axes) {
        if (data == null || len < REPORT_LEN || off + len > data.length
                || (data[off] & 0xFF) != FORMAT_NINTENDO) {
            return false;
        }
        java.util.Arrays.fill(buttons, 0);
        java.util.Arrays.fill(axes, 0);
        int[][] bits = MODEL_GAMECUBE.equals(model) ? GC_BITS : PRO_BITS;
        for (int[] b : bits) {
            if ((data[off + b[0]] & b[1]) != 0) {
                buttons[b[2]] = 1;
            }
        }
        int[] l = {mapAxis(u12(data[off + 11], data[off + 12], false), cal.left.x, false),
                mapAxis(u12(data[off + 12], data[off + 13], true), cal.left.y, true)};
        int[] r = {mapAxis(u12(data[off + 14], data[off + 15], false), cal.right.x, false),
                mapAxis(u12(data[off + 15], data[off + 16], true), cal.right.y, true)};
        radialDeadzone(l, deadzone);
        radialDeadzone(r, deadzone);
        axes[Pad.LEFT_X] = l[0];
        axes[Pad.LEFT_Y] = l[1];
        axes[Pad.RIGHT_X] = r[0];
        axes[Pad.RIGHT_Y] = r[1];
        if (MODEL_GAMECUBE.equals(model)) {
            int lt = mapTrigger(data[off + 61] & 0xFF, cal.triggerZeroLeft);
            int rt = mapTrigger(data[off + 62] & 0xFF, cal.triggerZeroRight);
            axes[Pad.LEFT_TRIGGER] = lt <= deadzone * 32767 ? 0 : lt;
            axes[Pad.RIGHT_TRIGGER] = rt <= deadzone * 32767 ? 0 : rt;
        } else {
            axes[Pad.LEFT_TRIGGER] = (data[off + 7] & 0x80) != 0 ? Pad.AXIS_MAX : 0;
            axes[Pad.RIGHT_TRIGGER] = (data[off + 5] & 0x80) != 0 ? Pad.AXIS_MAX : 0;
        }
        return true;
    }

    public static final int REPORT_LEN = 64;

    // --- Bluetooth LE (switch2_ble.py) -----------------------------------------------------

    public static final int BLE_COMPANY_ID = 0x0553;
    public static final String BLE_SERVICE = "ab7de9be-89fe-49ad-828f-118f09df7fd0";
    public static final String BLE_INPUT_COMMON = "ab7de9be-89fe-49ad-828f-118f09df7fd2";
    public static final String BLE_COMMAND = "649d4ac9-8eb7-4e6c-af44-1ea54fe5f005";
    public static final String BLE_COMMAND_RESPONSE = "c765a961-d9d8-4d36-a20a-5315b111836a";
    public static final String BLE_RATE_DESCRIPTOR = "679d5510-5a24-4dee-9557-95df80486ecb";
    public static final String BLE_CCCD = "00002902-0000-1000-8000-00805f9b34fb";
    public static final byte[] BLE_RATE_VALUE = {(byte) 0x85, 0x00};

    /** Per-model input characteristic (its own report id) and "command response #2". */
    public static String bleModelInputUuid(int productId) {
        switch (productId) {
            case PID_PRO:
                return "7492866c-ec3e-4619-8258-32755ffcc0f8";
            case PID_GAMECUBE:
                return "8261cba1-9435-420c-84d6-f0c75a2c8e4d";
            case 0x2067:
                return "cc1bbbb5-7354-4d32-a716-a81cb241a32a";
            case 0x2066:
                return "d5a9e01e-2ffc-4cca-b20c-8b67142bf442";
            default:
                return null;
        }
    }

    public static String bleModelResponseUuid(int productId) {
        switch (productId) {
            case PID_PRO:
                return "506d9f7d-4278-4e95-a549-326ba77657e0";
            case PID_GAMECUBE:
                return "46f6ad29-cdaf-4569-a2fe-339020b94604";
            case 0x2067:
                return "63a3810f-aec7-474b-9010-3d52403cb996";
            case 0x2066:
                return "640ca58e-0e88-410c-a7f3-426faf2b690b";
            default:
                return null;
        }
    }

    public static final int DIR_REQUEST = 0x91;
    public static final int DIR_RESPONSE = 0x01;
    public static final int TRANSPORT_USB = 0x00;
    public static final int TRANSPORT_BLE = 0x01;
    public static final int CMD_FLASH = 0x02;
    public static final int SUB_MEMORY_READ = 0x04;
    public static final int CMD_LEDS = 0x09;
    public static final int SUB_LED_PATTERN = 0x07;
    public static final int CMD_FEATURES = 0x0C;
    public static final int SUB_FEATURE_SET_MASK = 0x02;
    public static final int SUB_FEATURE_ENABLE = 0x04;
    public static final int DEFAULT_FEATURES = 0x27;
    public static final int MAX_BLE_READ = 0x4F;

    /** 8-byte header {@code cmd 91 transport sub 00 len 00 00} followed by {@code data}. */
    public static byte[] buildCommand(int cmd, int sub, byte[] data, int transport) {
        byte[] out = new byte[8 + data.length];
        out[0] = (byte) cmd;
        out[1] = (byte) DIR_REQUEST;
        out[2] = (byte) transport;
        out[3] = (byte) sub;
        out[5] = (byte) data.length;
        System.arraycopy(data, 0, out, 8, data.length);
        return out;
    }

    public static byte[] buildMemoryRead(int address, int length) {
        if (length <= 0 || length > MAX_BLE_READ) {
            throw new IllegalArgumentException("BLE flash reads are limited to 1..0x4F bytes");
        }
        byte[] d = {(byte) length, 0x7E, 0, 0, (byte) address, (byte) (address >>> 8),
                (byte) (address >>> 16), (byte) (address >>> 24)};
        return buildCommand(CMD_FLASH, SUB_MEMORY_READ, d, TRANSPORT_BLE);
    }

    public static byte[] buildPlayerLeds(int player) {
        int pattern = player > 0 ? PLAYER_LED[(player - 1) % 8] : 0;
        byte[] d = new byte[8];
        d[0] = (byte) (pattern & 0x0F);
        return buildCommand(CMD_LEDS, SUB_LED_PATTERN, d, TRANSPORT_BLE);
    }

    public static byte[] buildFeatureCommand(int sub, int flags) {
        return buildCommand(CMD_FEATURES, sub, new byte[]{(byte) flags, 0, 0, 0}, TRANSPORT_BLE);
    }

    /** What a Switch 2 controller advertises (manufacturer data after company id 0x0553). */
    public static final class Advertisement {
        public final int vendorId;
        public final int productId;
        public final String model;          // "gamecube" / "pro" / null (Joy-Con 2 etc.)
        public final boolean wake;
        public final String hostAddress;    // console it wants, or null in pairing mode
        public final boolean pairingMode;

        Advertisement(int vendorId, int productId, boolean wake, String hostAddress) {
            this.vendorId = vendorId;
            this.productId = productId;
            this.model = bleModelKey(productId);
            this.wake = wake;
            this.hostAddress = hostAddress;
            this.pairingMode = hostAddress == null;
        }
    }

    /** Model key of any Switch 2 controller seen advertising (only gamecube/pro are readable). */
    public static String bleModelKey(int productId) {
        switch (productId) {
            case 0x2066:
                return "joycon_r";
            case 0x2067:
                return "joycon_l";
            default:
                return modelKey(productId);
        }
    }

    public static Advertisement parseManufacturerData(byte[] p) {
        if (p == null || p.length < 9) {
            return null;
        }
        int vid = (p[3] & 0xFF) | (p[4] & 0xFF) << 8;
        int pid = (p[5] & 0xFF) | (p[6] & 0xFF) << 8;
        if (vid != NINTENDO_VID || pid < 0x2060 || pid > 0x20FF) {
            return null;
        }
        String host = null;
        if (p.length >= 16) {
            boolean any = false;
            for (int i = 10; i < 16; i++) {
                any |= p[i] != 0;
            }
            if (any) {
                StringBuilder sb = new StringBuilder();
                for (int i = 15; i >= 10; i--) {
                    if (sb.length() > 0) {
                        sb.append(':');
                    }
                    sb.append(String.format(Locale.ROOT, "%02X", p[i] & 0xFF));
                }
                host = sb.toString();
            }
        }
        boolean wake = p.length > 9 && (p[9] & 0xFF) == 0x81;
        return new Advertisement(vid, pid, wake, host);
    }

    public static final class CommandResponse {
        public final int cmd;
        public final int sub;
        public final int transport;
        public final int ack;
        public final byte[] payload;

        CommandResponse(int cmd, int sub, int transport, int ack, byte[] payload) {
            this.cmd = cmd;
            this.sub = sub;
            this.transport = transport;
            this.ack = ack;
            this.payload = payload;
        }
    }

    /** Responses start with the 8-byte header at offset 0, or after 14 zero bytes (0x001E). */
    public static CommandResponse parseCommandResponse(byte[] data) {
        if (data == null) {
            return null;
        }
        for (int off : new int[]{0, 14}) {
            if (data.length < off + 8 || data[off + 1] != DIR_RESPONSE
                    || (data[off + 2] != TRANSPORT_USB && data[off + 2] != TRANSPORT_BLE)
                    || data[off] == 0) {
                continue;
            }
            boolean prefixClean = true;
            for (int i = 0; i < off; i++) {
                prefixClean &= data[i] == 0;
            }
            if (!prefixClean) {
                continue;
            }
            byte[] payload = new byte[data.length - off - 8];
            System.arraycopy(data, off + 8, payload, 0, payload.length);
            return new CommandResponse(data[off] & 0xFF, data[off + 3] & 0xFF, data[off + 2] & 0xFF,
                    data[off + 5] & 0xFF, payload);
        }
        return null;
    }

    /** {@code [address, data...]}: the flash address (as int) and bytes of a read response. */
    public static int memoryReadAddress(CommandResponse r) {
        byte[] p = r.payload;
        if (r.cmd != CMD_FLASH || p.length < 8) {
            return -1;
        }
        return (p[4] & 0xFF) | (p[5] & 0xFF) << 8 | (p[6] & 0xFF) << 16 | (p[7] & 0xFF) << 24;
    }

    public static byte[] memoryReadData(CommandResponse r) {
        byte[] p = r.payload;
        if (r.cmd != CMD_FLASH || p.length < 8) {
            return null;
        }
        int len = Math.min(p[0] & 0xFF, p.length - 8);
        byte[] out = new byte[len];
        System.arraycopy(p, 8, out, 0, len);
        return out;
    }

    private static byte[] toBytes(int[] values) {
        byte[] b = new byte[values.length];
        for (int i = 0; i < values.length; i++) {
            b[i] = (byte) values[i];
        }
        return b;
    }
}
