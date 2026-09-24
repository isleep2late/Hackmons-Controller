package com.controllerlog.gcbridge;

import java.util.Arrays;

/**
 * The canonical controller model, identical to {@code controllerlog/model.py}: button and axis
 * indices follow SDL3's SDL_GamepadButton / SDL_GamepadAxis, so a .ctlog written here reads
 * exactly like one recorded on the PC.
 */
public final class Pad {

    private Pad() {
    }

    public static final String[] BUTTONS = {
            "south", "east", "west", "north", "back", "guide", "start", "left_stick",
            "right_stick", "left_shoulder", "right_shoulder", "dpad_up", "dpad_down", "dpad_left",
            "dpad_right", "misc1", "right_paddle1", "left_paddle1", "right_paddle2", "left_paddle2",
            "touchpad", "misc2", "misc3", "misc4", "misc5", "misc6",
    };
    public static final String[] AXES = {
            "left_x", "left_y", "right_x", "right_y", "left_trigger", "right_trigger",
    };
    public static final int NUM_BUTTONS = BUTTONS.length;
    public static final int NUM_AXES = AXES.length;

    public static final int SOUTH = 0, EAST = 1, WEST = 2, NORTH = 3, BACK = 4, GUIDE = 5,
            START = 6, LEFT_STICK = 7, RIGHT_STICK = 8, LEFT_SHOULDER = 9, RIGHT_SHOULDER = 10,
            DPAD_UP = 11, DPAD_DOWN = 12, DPAD_LEFT = 13, DPAD_RIGHT = 14, MISC1 = 15,
            RIGHT_PADDLE1 = 16, LEFT_PADDLE1 = 17, RIGHT_PADDLE2 = 18, LEFT_PADDLE2 = 19,
            TOUCHPAD = 20, MISC2 = 21, MISC3 = 22, MISC4 = 23, MISC5 = 24, MISC6 = 25;
    public static final int LEFT_X = 0, LEFT_Y = 1, RIGHT_X = 2, RIGHT_Y = 3, LEFT_TRIGGER = 4,
            RIGHT_TRIGGER = 5;

    public static final int AXIS_MIN = -32768;
    public static final int AXIS_MAX = 32767;
    /** A trigger past this value counts as pressed for the overlay (model.py). */
    public static final int TRIGGER_PRESS_THRESHOLD = 16384;

    public static final String FAMILY_XBOX = "xbox";
    public static final String FAMILY_PLAYSTATION = "playstation";
    public static final String FAMILY_SWITCH = "switch";
    public static final String FAMILY_GAMECUBE = "gamecube";
    public static final String FAMILY_GENERIC = "generic";

    public static int buttonIndex(String name) {
        for (int i = 0; i < BUTTONS.length; i++) {
            if (BUTTONS[i].equals(name)) {
                return i;
            }
        }
        return -1;
    }

    public static int axisIndex(String name) {
        for (int i = 0; i < AXES.length; i++) {
            if (AXES[i].equals(name)) {
                return i;
            }
        }
        return -1;
    }

    /** True for names an overlay element may light: every button plus the two triggers. */
    public static boolean isDigitalInput(String name) {
        return buttonIndex(name) >= 0 || "left_trigger".equals(name) || "right_trigger".equals(name);
    }

    public static int clampAxis(long v) {
        return (int) Math.max(AXIS_MIN, Math.min(AXIS_MAX, v));
    }

    /** Full instantaneous state of one controller (model.PadState). */
    public static final class State {
        public final int[] buttons = new int[NUM_BUTTONS];
        public final int[] axes = new int[NUM_AXES];

        public State() {
        }

        public State(State other) {
            set(other);
        }

        public void set(State other) {
            System.arraycopy(other.buttons, 0, buttons, 0, NUM_BUTTONS);
            System.arraycopy(other.axes, 0, axes, 0, NUM_AXES);
        }

        public void clear() {
            Arrays.fill(buttons, 0);
            Arrays.fill(axes, 0);
        }

        /** @return true if the button changed */
        public boolean setButton(int index, boolean down) {
            int v = down ? 1 : 0;
            if (index < 0 || index >= NUM_BUTTONS || buttons[index] == v) {
                return false;
            }
            buttons[index] = v;
            return true;
        }

        /** @return true if the axis changed */
        public boolean setAxis(int index, int value) {
            if (index < 0 || index >= NUM_AXES) {
                return false;
            }
            int v = clampAxis(value);
            if (axes[index] == v) {
                return false;
            }
            axes[index] = v;
            return true;
        }

        /** Digital view of a button or trigger by canonical name. */
        public boolean pressed(String name) {
            int b = buttonIndex(name);
            if (b >= 0) {
                return buttons[b] != 0;
            }
            if ("left_trigger".equals(name)) {
                return axes[LEFT_TRIGGER] >= TRIGGER_PRESS_THRESHOLD;
            }
            if ("right_trigger".equals(name)) {
                return axes[RIGHT_TRIGGER] >= TRIGGER_PRESS_THRESHOLD;
            }
            return false;
        }

        public boolean anyPressed() {
            for (int b : buttons) {
                if (b != 0) {
                    return true;
                }
            }
            return axes[LEFT_TRIGGER] >= TRIGGER_PRESS_THRESHOLD
                    || axes[RIGHT_TRIGGER] >= TRIGGER_PRESS_THRESHOLD;
        }

        @Override
        public boolean equals(Object o) {
            return o instanceof State && Arrays.equals(buttons, ((State) o).buttons)
                    && Arrays.equals(axes, ((State) o).axes);
        }

        @Override
        public int hashCode() {
            return 31 * Arrays.hashCode(buttons) + Arrays.hashCode(axes);
        }

        @Override
        public String toString() {
            StringBuilder sb = new StringBuilder();
            for (int i = 0; i < NUM_BUTTONS; i++) {
                if (buttons[i] != 0) {
                    sb.append(sb.length() > 0 ? " " : "").append(BUTTONS[i]);
                }
            }
            for (int i = 0; i < NUM_AXES; i++) {
                if (axes[i] != 0) {
                    sb.append(sb.length() > 0 ? " " : "").append(AXES[i]).append('=').append(axes[i]);
                }
            }
            return sb.length() == 0 ? "-" : sb.toString();
        }
    }
}
