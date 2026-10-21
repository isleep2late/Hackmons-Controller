"""Tests for the Linux udev rules and device-access checks (fake sysfs, no root)."""

from __future__ import annotations

import logging
import os
import sys
import types

import pytest

from controllerlog import udev
from controllerlog.cli import main
from controllerlog.hub import Hub
from controllerlog.input import switch2_usb


def fake_sysfs(tmp_path, devices):
    root = tmp_path / "usb"
    root.mkdir()
    for name, (vid, pid, bus, dev) in devices.items():
        d = root / name
        d.mkdir()
        (d / "idVendor").write_text(f"{vid:04x}\n")
        (d / "idProduct").write_text(f"{pid:04x}\n")
        (d / "busnum").write_text(f"{bus}\n")
        (d / "devnum").write_text(f"{dev}\n")
    (root / "7-2:1.0").mkdir()          # interfaces are skipped
    return str(root)


def test_rules_cover_uinput_and_every_switch2_pad():
    assert 'KERNEL=="uinput"' in udev.RULES and 'TAG+="uaccess"' in udev.RULES
    assert 'OPTIONS+="static_node=uinput"' in udev.RULES
    for pid in ("2066", "2067", "2069", "2073"):
        assert pid in udev.RULES
    usb_rule = next(l for l in udev.RULES.splitlines() if l.startswith('SUBSYSTEM=="usb"'))
    assert 'ENV{DEVTYPE}=="usb_device"' in usb_rule and 'ATTRS{idVendor}=="057e"' in usb_rule
    assert "{{" not in udev.RULES      # f-string braces rendered


def test_usb_node_and_hid_path(tmp_path):
    sys_usb = fake_sysfs(tmp_path, {"7-2": (0x057E, 0x2073, 7, 11)})
    assert udev.usb_node("7-2", sys_usb) == "/dev/bus/usb/007/011"
    assert udev.node_for_hid_path(b"7-2:1.0", sys_usb) == "/dev/bus/usb/007/011"
    assert udev.node_for_hid_path(b"/dev/hidraw3", sys_usb) is None
    assert udev.usb_node("9-9", sys_usb) is None


def test_plugged_switch2(tmp_path):
    sys_usb = fake_sysfs(tmp_path, {"7-2": (0x057E, 0x2073, 7, 11),
                                    "1-4": (0x046D, 0xC52B, 1, 3),      # a mouse receiver
                                    "3-1": (0x057E, 0x2009, 3, 2)})     # Switch 1 Pro: not ours
    assert udev.plugged_switch2(sys_usb) == [("7-2", 0x2073, "/dev/bus/usb/007/011")]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_access_problem(tmp_path):
    node = tmp_path / "node"
    node.write_text("")
    assert udev.access_problem(str(node), access=lambda p, m: True) is None
    assert "no permission" in udev.access_problem(str(node), access=lambda p, m: False)
    assert udev.access_problem(str(tmp_path / "missing"), access=lambda p, m: False) is None
    assert udev.access_problem(None) is None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_install_runs_sudo_steps(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(udev.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(udev.shutil, "which", lambda name: "/usr/bin/sudo")

    def run(cmd, check=False):
        calls.append(cmd)
        if cmd[1] == "install":
            assert open(cmd[-2]).read() == udev.RULES
        return types.SimpleNamespace(returncode=0)

    dest = tmp_path / "60-controllerlog.rules"
    assert udev.install(run=run, rules_path=str(dest)) == 0
    assert [c[1] for c in calls] == ["install", "udevadm", "udevadm", "modprobe"]
    assert all(c[0] == "sudo" for c in calls)
    assert calls[0][-1] == str(dest)
    assert not os.path.exists(calls[0][-2])          # temp file removed
    assert "replug" in capsys.readouterr().out


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_install_skips_when_up_to_date(tmp_path, capsys):
    dest = tmp_path / "60-controllerlog.rules"
    dest.write_text(udev.RULES)
    assert udev.install(run=lambda *a, **k: pytest.fail("ran a command"), rules_path=str(dest)) == 0
    assert "already installed" in capsys.readouterr().out


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_install_stops_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(udev.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(udev.shutil, "which", lambda name: "/usr/bin/sudo")
    calls = []

    def run(cmd, check=False):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=1)
    assert udev.install(run=run, rules_path=str(tmp_path / "r.rules")) == 1
    assert len(calls) == 1


def test_cli_prints_rules(capsys):
    assert main(["udev-rules"]) == 0
    assert capsys.readouterr().out == udev.RULES


def test_open_failure_warns_once_with_permission_hint(monkeypatch, caplog):
    found = switch2_usb.FoundController(0x2073, "gamecube", b"7-2:1.0")
    monkeypatch.setattr(switch2_usb, "node_for_hid_path", lambda p: "/dev/bus/usb/007/011")
    monkeypatch.setattr(switch2_usb, "access_problem",
                        lambda node: "no permission to open /dev/bus/usb/007/011")

    def factory(f):
        raise OSError("open failed")
    be = switch2_usb.Switch2UsbBackend(Hub(), rescan_s=0, transport_factory=factory,
                                       finder=lambda: [found])
    with caplog.at_level(logging.WARNING, logger="controllerlog.input.switch2_usb"):
        for _ in range(3):
            be._session(found)
    msgs = [r.getMessage() for r in caplog.records]
    assert len(msgs) == 1
    assert "no permission" in msgs[0] and "install-udev-rules" in msgs[0]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_open_failure_in_use_hint(monkeypatch, caplog):
    found = switch2_usb.FoundController(0x2073, "gamecube", b"7-2:1.0")
    monkeypatch.setattr(switch2_usb, "access_problem", lambda node: None)

    def factory(f):
        raise OSError("open failed")
    be = switch2_usb.Switch2UsbBackend(Hub(), rescan_s=0, transport_factory=factory,
                                       finder=lambda: [found])
    with caplog.at_level(logging.WARNING, logger="controllerlog.input.switch2_usb"):
        be._session(found)
    assert "only one 'live'" in caplog.records[0].getMessage()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_doctor_flags_missing_node(monkeypatch):
    from controllerlog import doctor
    monkeypatch.setattr(udev, "plugged_switch2", lambda: [("7-2", 0x2073, "/nonexistent/007/011")])
    c = doctor.check_switch2_usb_access()
    assert c.status == "missing" and "device node" in c.detail


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_doctor_usb_access_ok_and_denied(monkeypatch, tmp_path):
    from controllerlog import doctor
    node = tmp_path / "011"
    node.write_text("")
    monkeypatch.setattr(udev, "plugged_switch2", lambda: [("7-2", 0x2073, str(node))])
    assert doctor.check_switch2_usb_access().status == "ok"
    monkeypatch.setattr(udev.os, "access", lambda p, m: False)
    c = doctor.check_switch2_usb_access()
    assert c.status == "missing" and "install-udev-rules" in c.hints[0]
