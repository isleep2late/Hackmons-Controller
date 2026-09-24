package com.controllerlog.gcbridge;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.AccessibilityServiceInfo;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.provider.Settings;
import android.text.TextUtils;
import android.util.Log;
import android.view.KeyEvent;
import android.view.accessibility.AccessibilityEvent;

/**
 * "Button capture everywhere": an accessibility service that sees controller key events
 * system-wide (with flagRequestFilterKeyEvents) and passes them to the hub without consuming
 * them, so the overlay and the recording keep working while a game is in front. Android gives
 * stick and trigger axes only to the focused app, and turns some pads' D-pad into key events
 * inside that app, so those are not seen here.
 */
public final class KeyCaptureService extends AccessibilityService {

    private static volatile KeyCaptureService instance;

    static boolean isConnected() {
        return instance != null;
    }

    /** Whether the user switched the service on in Settings > Accessibility. */
    static boolean isEnabledInSettings(Context ctx) {
        String enabled = Settings.Secure.getString(ctx.getContentResolver(),
                Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES);
        if (TextUtils.isEmpty(enabled)) {
            return false;
        }
        String me = new ComponentName(ctx, KeyCaptureService.class).flattenToString();
        String meShort = new ComponentName(ctx, KeyCaptureService.class).flattenToShortString();
        for (String s : enabled.split(":")) {
            if (s.equalsIgnoreCase(me) || s.equalsIgnoreCase(meShort)) {
                return true;
            }
        }
        return false;
    }

    static Intent settingsIntent() {
        return new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
    }

    @Override
    protected void onServiceConnected() {
        super.onServiceConnected();
        AccessibilityServiceInfo info = getServiceInfo();
        if (info != null) {
            info.flags |= AccessibilityServiceInfo.FLAG_REQUEST_FILTER_KEY_EVENTS;
            setServiceInfo(info);
        }
        instance = this;
        Log.i(MainActivity.TAG, "KeyCaptureService connected");
        MainActivity.log("Button capture (accessibility service) is on");
    }

    @Override
    protected boolean onKeyEvent(KeyEvent event) {
        try {
            InputRouter.onKey(event);
        } catch (RuntimeException e) {
            Log.e(MainActivity.TAG, "onKeyEvent", e);
        }
        return false;   // never consume: the game in front must still get the button
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        // not interested in UI events
    }

    @Override
    public void onInterrupt() {
    }

    @Override
    public boolean onUnbind(Intent intent) {
        instance = null;
        MainActivity.log("Button capture (accessibility service) is off");
        return super.onUnbind(intent);
    }

    @Override
    public void onDestroy() {
        instance = null;
        super.onDestroy();
    }
}
