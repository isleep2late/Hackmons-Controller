"""Linux device access: the udev rules ControllerLog needs, and checks that say when they're missing.

Two things need permissions a normal user doesn't have on a stock Linux install:

* the Switch 2 controllers' USB device node (``/dev/bus/usb/BBB/DDD``): ControllerLog sends
  the controller its start-up commands there and reads its reports (hidapi's libusb backend);
* ``/dev/uinput``: the virtual Xbox 360 / DS4 pads of ``--bridge`` and ``replay``.

Steam's udev rules (package ``steam-devices`` / ``steam-launcher``) happen to grant both; on a
computer without Steam ``controllerlog udev-rules --install`` (the bundle's
``./install-udev-rules``) installs :data:`RULES` once. ``TAG+="uaccess"`` gives the user of
the local desktop session access, the same way Steam and the distributions' own rules do.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

RULES_PATH = "/etc/udev/rules.d/60-controllerlog.rules"
NINTENDO_VID = 0x057E
# Switch 2 Joy-Con (R) 2066, Joy-Con (L) 2067, Pro Controller 2069, GameCube controller 2073.
SWITCH2_PIDS = (0x2066, 0x2067, 0x2069, 0x2073)
INSTALL_HINT = ("run ./install-udev-rules once (or: controllerlog udev-rules --install), "
                "then unplug and replug the controller")

_PIDS = "|".join(f"{p:04x}" for p in SWITCH2_PIDS)
RULES = f"""\
# ControllerLog / GC Bridge (https://github.com/isleep2late/Hackmons-Controller)
# Lets the user of the local desktop session use Switch 2 controllers over USB and create
# virtual controllers (--bridge, replay). Installed by: controllerlog udev-rules --install

# Virtual controllers (uinput)
KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"

# Switch 2 Joy-Con, Pro Controller and GameCube controller over USB
SUBSYSTEM=="usb", ENV{{DEVTYPE}}=="usb_device", ATTRS{{idVendor}}=="057e", ATTRS{{idProduct}}=="{_PIDS}", MODE="0660", TAG+="uaccess"
KERNEL=="hidraw*", ATTRS{{idVendor}}=="057e", ATTRS{{idProduct}}=="{_PIDS}", MODE="0660", TAG+="uaccess"
"""

SYS_USB = "/sys/bus/usb/devices"


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="ascii") as f:
            return f.read().strip()
    except OSError:
        return None


def usb_node(sysfs_name: str, sys_usb: str = SYS_USB) -> str | None:
    """``"7-2"`` (a USB device's sysfs name) -> ``"/dev/bus/usb/007/011"``, or None if gone."""
    bus = _read(f"{sys_usb}/{sysfs_name}/busnum")
    dev = _read(f"{sys_usb}/{sysfs_name}/devnum")
    if not (bus and dev and bus.isdigit() and dev.isdigit()):
        return None
    return f"/dev/bus/usb/{int(bus):03d}/{int(dev):03d}"


def node_for_hid_path(hid_path: bytes | str, sys_usb: str = SYS_USB) -> str | None:
    """hidapi libusb path (``b"7-2:1.0"``) -> the USB device node; None for other path kinds."""
    path = hid_path.decode(errors="replace") if isinstance(hid_path, bytes) else hid_path
    if path.startswith("/") or ":" not in path:
        return None
    return usb_node(path.split(":", 1)[0], sys_usb)


def access_problem(node: str | None, access: Callable[[str, int], bool] | None = None) -> str | None:
    """Why ``node`` can't be opened read/write by this user, or None if it can (or is unknown)."""
    if not node or not sys.platform.startswith("linux"):
        return None
    if not os.path.exists(node):
        return None
    if (access or os.access)(node, os.R_OK | os.W_OK):
        return None
    return f"no permission to open {node}"


def plugged_switch2(sys_usb: str = SYS_USB) -> list[tuple[str, int, str | None]]:
    """``(sysfs name, product id, device node)`` of every Switch 2 controller on USB (Linux)."""
    out = []
    try:
        names = sorted(os.listdir(sys_usb))
    except OSError:
        return out
    for name in names:
        if ":" in name:
            continue
        vid, pid = _read(f"{sys_usb}/{name}/idVendor"), _read(f"{sys_usb}/{name}/idProduct")
        try:
            if vid and pid and int(vid, 16) == NINTENDO_VID and int(pid, 16) in SWITCH2_PIDS:
                out.append((name, int(pid, 16), usb_node(name, sys_usb)))
        except ValueError:
            continue
    return out


def install(run: Callable[..., object] = subprocess.run, rules_path: str = RULES_PATH) -> int:
    """Install :data:`RULES` with sudo and reload udev. Returns a process exit code."""
    if not sys.platform.startswith("linux"):
        print("udev rules are only needed on Linux.", file=sys.stderr)
        return 2
    if Path(rules_path).is_file() and Path(rules_path).read_text(encoding="utf-8") == RULES:
        print(f"{rules_path} is already installed and up to date.")
    else:
        sudo = [] if os.geteuid() == 0 else ["sudo"]
        if sudo and shutil.which("sudo") is None:
            print(f"sudo not found: as root, save the output of 'controllerlog udev-rules' to "
                  f"{rules_path}, then run 'udevadm control --reload-rules && udevadm trigger'.",
                  file=sys.stderr)
            return 2
        print(f"Installing {rules_path} (sudo may ask for your password)...")
        with tempfile.NamedTemporaryFile("w", suffix=".rules", delete=False, encoding="utf-8") as f:
            f.write(RULES)
            tmp = f.name
        try:
            steps = [
                [*sudo, "install", "-m", "0644", tmp, rules_path],
                [*sudo, "udevadm", "control", "--reload-rules"],
                [*sudo, "udevadm", "trigger", "--subsystem-match=usb", "--subsystem-match=misc",
                 "--subsystem-match=hidraw"],
            ]
            for cmd in steps:
                rc = getattr(run(cmd, check=False), "returncode", 1)
                if rc != 0:
                    print(f"error: {' '.join(cmd)} failed (exit {rc})", file=sys.stderr)
                    return 1
            # uinput is usually loaded on first open; load it now so /dev/uinput exists. Best effort.
            run([*sudo, "modprobe", "uinput"], check=False)
        finally:
            os.unlink(tmp)
    print("Done. Unplug and replug the controller. If it still can't be opened, log out and back "
          "in (access goes to the user of the local desktop session).")
    return 0


__all__ = ["RULES", "RULES_PATH", "INSTALL_HINT", "SWITCH2_PIDS", "usb_node", "node_for_hid_path",
           "access_problem", "plugged_switch2", "install"]
