package com.controllerlog.gcbridge;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;

/** Finds files of the Python side of the repository (system property controllerlog.root). */
final class TestFiles {

    private TestFiles() {
    }

    /** Repository root: -Dcontrollerlog.root, else derived from the switch2.py property. */
    static Path root() {
        String r = System.getProperty("controllerlog.root");
        if (r != null && Files.isDirectory(Paths.get(r))) {
            return Paths.get(r);
        }
        String py = System.getProperty("switch2.py");
        if (py != null) {
            Path p = Paths.get(py).toAbsolutePath();
            // controllerlog/input/switch2_usb.py -> repository root
            if (p.getNameCount() >= 3) {
                return p.getParent().getParent().getParent();
            }
        }
        return null;
    }

    static String read(Path p) throws IOException {
        return new String(Files.readAllBytes(p), StandardCharsets.UTF_8);
    }

    static byte[] hex(String s) {
        String h = s.replace(" ", "");
        byte[] out = new byte[h.length() / 2];
        for (int i = 0; i < out.length; i++) {
            out[i] = (byte) Integer.parseInt(h.substring(2 * i, 2 * i + 2), 16);
        }
        return out;
    }
}
