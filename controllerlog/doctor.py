"""Read-only environment check: SDL3, virtual-pad driver, Bluetooth adapter, adb, ffmpeg.

Nothing here changes system state; on Windows it only queries PnP devices
through PowerShell.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

# USB Bluetooth radios Google Bumble has a firmware driver for (bumble/drivers/intel.py).
BUMBLE_INTEL_IDS = {(0x8087, 0x0032): "Intel AX210", (0x8087, 0x0033): "Intel AX211",
                    (0x8087, 0x0036): "Intel BE200"}
VIGEM_LATEST = (1, 22, 0)


@dataclass
class Check:
    name: str
    status: str            # "ok" | "warn" | "info" | "missing"
    detail: str
    hints: list[str] = field(default_factory=list)


def _powershell_json(script: str, timeout: float = 20.0) -> list[dict]:
    if sys.platform != "win32":
        return []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             script + " | ConvertTo-Json -Depth 3 -Compress"],
            capture_output=True, text=True, timeout=timeout, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return []
    if not out:
        return []
    data = json.loads(out)
    return data if isinstance(data, list) else [data]


def check_sdl() -> Check:
    try:
        from .input.sdl3 import SDL3
        sdl = SDL3()
        return Check("SDL3", "ok", f"SDL {sdl.version} loaded")
    except Exception as e:
        return Check("SDL3", "missing", str(e).splitlines()[0],
                     ["Run: python scripts/fetch_sdl3.py"])


def _version_tuple(s: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", s)[:3])


def check_vigem() -> Check:
    rows = _powershell_json(
        "Get-CimInstance Win32_PnPSignedDriver | Where-Object { $_.DeviceName -like "
        "'*Virtual Gamepad Emulation Bus*' } | Select-Object DeviceName, DriverVersion")
    if not rows:
        if sys.platform != "win32":
            return Check("ViGEmBus", "info", "Windows only (virtual Xbox/DS4 controllers)")
        return Check("ViGEmBus", "missing", "not installed - replay and bridge need it",
                     ["Install from https://github.com/nefarius/ViGEmBus/releases (v1.22.0)"])
    ver = rows[0].get("DriverVersion") or "?"
    hints = []
    status = "ok"
    try:
        if _version_tuple(ver) < VIGEM_LATEST:
            status = "warn"
            hints.append("Update to ViGEmBus 1.22.0 (the final release); older builds' updaters "
                         "contact the abandoned vigem.org domain (Nefarius's 'Legacinator' checks this)")
    except ValueError:
        pass
    return Check("ViGEmBus", status, f"installed, driver {ver}", hints)


def check_hidhide() -> Check:
    rows = _powershell_json(
        "Get-CimInstance Win32_PnPSignedDriver | Where-Object { $_.DeviceName -like '*HidHide*' } "
        "| Select-Object DeviceName, DriverVersion")
    if rows:
        return Check("HidHide", "ok", f"installed, driver {rows[0].get('DriverVersion', '?')}")
    return Check("HidHide", "info", "not installed (optional)",
                 ["For --bridge/replay into games: HidHide hides the real controller so the "
                  "game only sees the virtual one (https://github.com/nefarius/HidHide)"])


def check_bluetooth() -> list[Check]:
    if sys.platform != "win32":
        return [Check("Bluetooth", "info", "adapter check is Windows-only")]
    rows = _powershell_json(
        "Get-PnpDevice -PresentOnly | Where-Object { $_.Class -eq 'Bluetooth' -or "
        "$_.InstanceId -like 'IBTPCIBUS*' } | Select-Object Status, Class, FriendlyName, InstanceId")
    radios, paired = [], []
    for r in rows:
        iid = (r.get("InstanceId") or "").upper()
        name = r.get("FriendlyName") or ""
        if iid.startswith("USB\\VID_") and ("BLUETOOTH" in name.upper() or r.get("Class") == "Bluetooth"):
            radios.append(r)
        elif iid.startswith("IBTPCIBUS"):
            radios.append(r)
        elif (iid.startswith("BTHENUM\\DEV_") or iid.startswith("BTHLE\\DEV_")) and name:
            paired.append(name)
    checks = []
    if not radios:
        return [Check("Bluetooth adapter", "missing", "no Bluetooth radio found")]
    for r in radios:
        iid = r["InstanceId"].upper()
        m = re.match(r"USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", iid)
        if m:
            vid, pid = int(m.group(1), 16), int(m.group(2), 16)
            model = BUMBLE_INTEL_IDS.get((vid, pid))
            detail = f"{r.get('FriendlyName')} (USB {vid:04x}:{pid:04x}{', ' + model if model else ''})"
            if model:
                takeover = ("expert 'full stack' mode is possible: a user-space host stack "
                            "(Google Bumble) can drive this radio after binding WinUSB with Zadig "
                            f"(Bumble lists it; tested by Bumble on AX210/BE200{'' if pid != 0x33 else ', AX211 untested'})")
            else:
                takeover = ("USB-attached, so a WinUSB takeover is physically possible, but Bumble has "
                            "no known firmware driver for this radio")
            checks.append(Check("Bluetooth adapter", "ok", detail, [takeover]))
        else:
            checks.append(Check("Bluetooth adapter", "ok", f"{r.get('FriendlyName')} (PCIe: {iid})",
                                ["PCIe-attached radio: an adapter takeover is not possible; "
                                 "everything else works through the normal Windows stack"]))
    if paired:
        uniq = sorted(set(paired))
        checks.append(Check("Paired Bluetooth devices", "info", f"{len(uniq)}: " + ", ".join(uniq[:12])
                            + (" ..." if len(uniq) > 12 else ""),
                            ["An adapter takeover would disconnect ALL of these (keyboards, mice, "
                             "headphones) until Windows' driver is restored - see docs/BLUETOOTH.md"]))
    return checks


def check_ble() -> Check:
    """bleak (for the experimental Switch 2 reader) and the Windows 11 LE connection-parameter API."""
    try:
        import bleak  # noqa: F401
        from importlib.metadata import version
        have = f"bleak {version('bleak')}"
    except Exception:
        return Check("Bluetooth LE (Switch 2)", "info", "bleak not installed (only needed for "
                     "`controllerlog switch2`)", ["pip install bleak"])
    fast = False
    if sys.platform == "win32":
        try:
            fast = sys.getwindowsversion().build >= 22000  # BluetoothLEPreferredConnectionParameters
        except AttributeError:
            pass
    if fast:
        return Check("Bluetooth LE (Switch 2)", "ok", f"{have}; Windows 11 can request fast "
                     "connection parameters (~66 Hz reports)")
    return Check("Bluetooth LE (Switch 2)", "warn", f"{have}; no fast LE connection API on this OS "
                 "(Switch 2 reports ~16-33 Hz)")


def check_tool(name: str, env: str, why: str, extra_paths: list[str] = ()) -> Check:
    path = os.environ.get(env) or shutil.which(name)
    if not path:
        for p in extra_paths:
            if os.path.exists(p):
                path = p
                break
    if not path and name == "ffmpeg":
        try:
            import imageio_ffmpeg
            path = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            path = None
    if path:
        return Check(name, "ok", path)
    return Check(name, "info", f"not found ({why})")


def run_checks() -> list[Check]:
    local = os.environ.get("LOCALAPPDATA", "")
    checks = [check_sdl(), check_vigem(), check_hidhide(), *check_bluetooth(), check_ble(),
              check_tool("adb", "CONTROLLERLOG_ADB", "only needed for Android capture: install "
                         "Android SDK platform-tools",
                         [os.path.join(local, "Android", "Sdk", "platform-tools", "adb.exe")]),
              check_tool("ffmpeg", "CONTROLLERLOG_FFMPEG", "only needed for .mp4/.webm/.mov "
                         "renders: pip install imageio-ffmpeg")]
    return checks


def format_checks(checks: list[Check]) -> str:
    icon = {"ok": "[ok]  ", "warn": "[warn]", "info": "[info]", "missing": "[miss]"}
    lines = []
    for c in checks:
        lines.append(f"{icon.get(c.status, '[?]   ')} {c.name}: {c.detail}")
        for h in c.hints:
            lines.append(f"         - {h}")
    return "\n".join(lines)
