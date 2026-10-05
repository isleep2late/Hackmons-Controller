package com.controllerlog.gcbridge;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

import java.io.IOException;

public class VariantTest {

    @Test
    public void devExportsNeverLandInTheOwnersFolder() {
        assertEquals("GC Bridge", RecordingStore.downloadsSubdir("com.controllerlog.gcbridge"));
        assertEquals("GC Bridge DEV", RecordingStore.downloadsSubdir("com.controllerlog.gcbridge.dev"));
    }

    @Test
    public void manifestKeepsTheNamesTheDevBuildRewrites() throws IOException {
        String manifest = TestFiles.read(TestFiles.root().resolve("android/gcbridge/app/src/main/AndroidManifest.xml"));
        assertTrue(manifest.contains("android:authorities=\"com.controllerlog.gcbridge.recordings\""));
        assertTrue(manifest.contains("android.hardware.usb.action.USB_DEVICE_ATTACHED"));
    }
}
