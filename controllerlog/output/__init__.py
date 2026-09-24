"""Controller outputs: virtual Xbox 360 / DualShock 4 pads (ViGEmBus via vgamepad)."""

from __future__ import annotations

from .virtual_pad import (NINTENDO_LABEL_REMAP, TARGETS, VIGEMBUS_URL, TargetInfo,
                          VirtualPad, VirtualPadError, VirtualPadUnavailable,
                          create_virtual_pad, normalize_target, remap_state,
                          validate_remap, virtual_pad_unavailable_reason)

__all__ = ["VirtualPad", "VirtualPadError", "VirtualPadUnavailable", "TargetInfo", "TARGETS",
           "NINTENDO_LABEL_REMAP", "VIGEMBUS_URL", "create_virtual_pad", "normalize_target",
           "remap_state", "validate_remap", "virtual_pad_unavailable_reason"]
