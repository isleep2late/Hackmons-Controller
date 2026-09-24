package com.controllerlog.gcbridge;

import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.PixelFormat;
import android.graphics.drawable.GradientDrawable;
import android.provider.Settings;
import android.util.DisplayMetrics;
import android.util.Log;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewConfiguration;
import android.view.WindowManager;
import android.widget.FrameLayout;
import android.widget.TextView;

/**
 * The floating controller ("display over other apps"): a {@link PadView} in a small window
 * that games underneath keep receiving input from. Drag to move; tap to collapse it into a
 * badge and tap the badge to expand. Position, size and opacity live in the preferences.
 */
final class OverlayWindow {

    static final String PREF_X = "overlay_x";
    static final String PREF_Y = "overlay_y";
    static final String PREF_SCALE = "overlay_scale";      // fraction of the screen width
    static final String PREF_ALPHA = "overlay_alpha";
    static final String PREF_COLLAPSED = "overlay_collapsed";
    static final float DEFAULT_SCALE = 0.45f;
    static final float DEFAULT_ALPHA = 0.9f;

    private final Context ctx;
    private final SharedPreferences prefs;
    private final WindowManager wm;
    private FrameLayout container;
    private PadView padView;
    private TextView badge;
    private WindowManager.LayoutParams params;
    private Layout layout;
    private boolean shown;

    OverlayWindow(Context ctx, SharedPreferences prefs) {
        this.ctx = ctx;
        this.prefs = prefs;
        this.wm = ctx.getSystemService(WindowManager.class);
    }

    static boolean canDraw(Context ctx) {
        return Settings.canDrawOverlays(ctx);
    }

    boolean isShown() {
        return shown;
    }

    /** Adds the window; false without the "display over other apps" permission. */
    boolean show() {
        if (shown) {
            return true;
        }
        if (!canDraw(ctx)) {
            return false;
        }
        container = new FrameLayout(ctx);
        padView = new PadView(ctx);
        padView.setLayout(layout);
        badge = new TextView(ctx);
        badge.setText("GC");
        badge.setTextColor(Color.WHITE);
        badge.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        badge.setGravity(Gravity.CENTER);
        GradientDrawable bg = new GradientDrawable();
        bg.setShape(GradientDrawable.OVAL);
        bg.setColor(0xCC5B4B8A);
        badge.setBackground(bg);
        int badgeSize = dp(44);
        container.addView(padView, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));
        container.addView(badge, new FrameLayout.LayoutParams(badgeSize, badgeSize, Gravity.TOP | Gravity.START));
        container.setOnTouchListener(new DragListener());

        params = new WindowManager.LayoutParams(1, 1, WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                        | WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL
                        | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
                PixelFormat.TRANSLUCENT);
        params.gravity = Gravity.TOP | Gravity.START;
        params.x = prefs.getInt(PREF_X, dp(16));
        params.y = prefs.getInt(PREF_Y, dp(96));
        applySize();
        try {
            wm.addView(container, params);
            shown = true;
        } catch (RuntimeException e) {  // BadTokenException, SecurityException
            Log.e(MainActivity.TAG, "overlay: addView failed: " + e);
            container = null;
            padView = null;
            badge = null;
            return false;
        }
        return true;
    }

    void hide() {
        if (!shown) {
            return;
        }
        try {
            wm.removeViewImmediate(container);
        } catch (RuntimeException e) {
            Log.w(MainActivity.TAG, "overlay: removeView: " + e);
        }
        shown = false;
        container = null;
        padView = null;
        badge = null;
    }

    void setLayout(Layout l) {
        layout = l;
        if (padView != null) {
            padView.setLayout(l);
            applySize();
            update();
        }
    }

    void setState(Pad.State s) {
        if (padView != null) {
            padView.setState(s);
        }
    }

    void clearState() {
        if (padView != null) {
            padView.clearState();
        }
    }

    /** Re-reads size, opacity and the collapsed flag from the preferences. */
    void applyPrefs() {
        if (shown) {
            applySize();
            update();
        }
    }

    private void applySize() {
        if (params == null) {
            return;
        }
        DisplayMetrics dm = ctx.getResources().getDisplayMetrics();
        boolean collapsed = prefs.getBoolean(PREF_COLLAPSED, false);
        float scale = Math.max(0.15f, Math.min(1f, prefs.getFloat(PREF_SCALE, DEFAULT_SCALE)));
        int w = Math.max(dp(80), Math.round(dm.widthPixels * scale));
        double aspect = layout == null ? 1.5 : layout.width / Math.max(1.0, layout.height);
        int h = Math.max(dp(48), (int) Math.round(w / aspect));
        if (collapsed) {
            w = dp(44);
            h = dp(44);
        }
        params.width = w;
        params.height = h;
        params.alpha = Math.max(0.2f, Math.min(1f, prefs.getFloat(PREF_ALPHA, DEFAULT_ALPHA)));
        params.x = Math.max(0, Math.min(dm.widthPixels - w, params.x));
        params.y = Math.max(0, Math.min(dm.heightPixels - h, params.y));
        if (padView != null) {
            padView.setVisibility(collapsed ? View.GONE : View.VISIBLE);
            badge.setVisibility(collapsed ? View.VISIBLE : View.GONE);
        }
    }

    private void update() {
        if (shown) {
            try {
                wm.updateViewLayout(container, params);
            } catch (RuntimeException e) {
                Log.w(MainActivity.TAG, "overlay: updateViewLayout: " + e);
            }
        }
    }

    private int dp(int v) {
        return Math.round(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v,
                ctx.getResources().getDisplayMetrics()));
    }

    private final class DragListener implements View.OnTouchListener {
        private float downX, downY;
        private int startX, startY;
        private long downTime;
        private boolean moved;
        private final int slop = ViewConfiguration.get(ctx).getScaledTouchSlop();

        @Override
        public boolean onTouch(View v, MotionEvent e) {
            switch (e.getActionMasked()) {
                case MotionEvent.ACTION_DOWN:
                    downX = e.getRawX();
                    downY = e.getRawY();
                    startX = params.x;
                    startY = params.y;
                    downTime = e.getEventTime();
                    moved = false;
                    return true;
                case MotionEvent.ACTION_MOVE: {
                    float dx = e.getRawX() - downX, dy = e.getRawY() - downY;
                    if (!moved && Math.hypot(dx, dy) < slop) {
                        return true;
                    }
                    moved = true;
                    params.x = startX + Math.round(dx);
                    params.y = startY + Math.round(dy);
                    update();
                    return true;
                }
                case MotionEvent.ACTION_UP:
                case MotionEvent.ACTION_CANCEL:
                    if (!moved && e.getEventTime() - downTime < ViewConfiguration.getLongPressTimeout()) {
                        prefs.edit().putBoolean(PREF_COLLAPSED, !prefs.getBoolean(PREF_COLLAPSED, false))
                                .apply();
                        applySize();
                        update();
                    }
                    prefs.edit().putInt(PREF_X, params.x).putInt(PREF_Y, params.y).apply();
                    v.performClick();
                    return true;
                default:
                    return false;
            }
        }
    }
}
