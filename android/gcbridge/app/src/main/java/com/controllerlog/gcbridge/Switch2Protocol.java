package com.controllerlog.gcbridge;

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

    private static byte[] toBytes(int[] values) {
        byte[] b = new byte[values.length];
        for (int i = 0; i < values.length; i++) {
            b[i] = (byte) values[i];
        }
        return b;
    }
}
