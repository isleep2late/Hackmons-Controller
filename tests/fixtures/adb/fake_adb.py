"""Fake ``adb`` executable for tests: replays the getevent transcripts in this folder.

Run as ``[sys.executable, fake_adb.py, ...adb args]``. Behaviour comes from the
JSON file named by ``$FAKE_ADB_SCENARIO``:

* ``devices``: file printed by ``adb devices -l``
* ``info``: file printed by ``adb shell getevent -i``
* ``probe``: extra files searched by ``getevent -i <path>`` (besides ``info``)
* ``streams``: files; the n-th ``getevent -t`` replays ``streams[min(n, len - 1)]``
* ``state``: directory for the stream counter and ``calls.txt`` (one line per call)
* ``speed``: pacing factor for stream timestamps (1.0 = real time, 0 = no pacing)
* ``fail_after_streams``: once this many streams ran, ``-s`` commands fail like
  an unplugged device
* ``device_error``: stderr text of that failure (default ``error: device '{serial}'
  not found``; current platform-tools print e.g. ``adb: device unauthorized.``)

Stream directives (never printed): ``#hang`` (stay alive until killed, max 60 s),
``#exit N``, ``#sleep S``, ``#stderr TEXT``.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TS = re.compile(r"^\[\s*(\d+)\.(\d+)\]")


def _read(name: str) -> str:
    return (HERE / name).read_text(encoding="utf-8")


def _blocks(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("add device"):
            out.append(line)
        elif out:
            out[-1] += line
    return out


def _counter(state: Path | None, name: str, bump: bool) -> int:
    if state is None:
        return 0
    f = state / f"{name}.count"
    n = int(f.read_text()) if f.exists() else 0
    if bump:
        f.write_text(str(n + 1))
    return n


def _replay(path: Path, speed: float) -> int:
    prev: float | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            cmd, _, arg = line[1:].partition(" ")
            if cmd == "hang":
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    time.sleep(0.05)
                return 0
            if cmd == "exit":
                return int(arg or 0)
            if cmd == "sleep":
                time.sleep(float(arg))
            elif cmd == "stderr":
                sys.stderr.write(arg + "\n")
                sys.stderr.flush()
            continue
        m = TS.match(line)
        if m and speed > 0:
            t = int(m.group(1)) + int(m.group(2)) / 10 ** len(m.group(2))
            if prev is not None and t > prev:
                time.sleep((t - prev) / speed)
            prev = t
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    return 0


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(newline="\n")
    sys.stderr.reconfigure(newline="\n")
    cfg_file = os.environ.get("FAKE_ADB_SCENARIO")
    cfg = json.loads(Path(cfg_file).read_text(encoding="utf-8")) if cfg_file else {}
    state = Path(cfg["state"]) if cfg.get("state") else None
    if state is not None:
        with open(state / "calls.txt", "a", encoding="utf-8") as f:
            f.write(" ".join(argv) + "\n")
    args = list(argv)
    serial = None
    if args[:1] == ["-s"] and len(args) > 1:
        serial, args = args[1], args[2:]
    if args == ["start-server"]:
        return 0
    if args[:1] == ["devices"]:
        sys.stdout.write(_read(cfg.get("devices", "devices_one.txt")))
        return 0
    if args[:1] == ["connect"]:
        print(f"connected to {args[1]}")
        return 0
    if args[:1] == ["pair"]:
        print(f"Successfully paired to {args[1]} [guid=adb-R5CT1234ABC-AbCdEf]")
        return 0
    limit = cfg.get("fail_after_streams")
    if serial and limit is not None and _counter(state, "stream", False) >= limit:
        msg = cfg.get("device_error", "error: device '{serial}' not found")
        sys.stderr.write(msg.format(serial=serial) + "\n")
        return 1
    pty = False
    if args[:1] == ["shell"]:
        while len(args) > 1 and args[1] in ("-t", "-tt", "-T", "-x"):  # adb shell options
            pty = pty or args[1] in ("-t", "-tt")
            args = [args[0], *args[2:]]
        if pty:  # a PTY turns "\n" into "\r\n" (onlcr)
            sys.stdout.reconfigure(newline="\r\n")
    if args[:2] == ["shell", "getevent"]:
        rest = args[2:]
        if rest[:1] == ["-i"]:
            if len(rest) == 1:
                sys.stdout.write(_read(cfg.get("info", "dualsense_phone_info.txt")))
                return 0
            texts = [cfg.get("info", "dualsense_phone_info.txt"), *cfg.get("probe", [])]
            for name in texts:
                for block in _blocks(_read(name)):
                    if block.split("\n", 1)[0].endswith(f": {rest[1]}"):
                        sys.stdout.write(re.sub(r"^add device \d+:", "add device 1:", block))
                        return 0
            sys.stderr.write(f"could not open {rest[1]}, No such file or directory\n")
            return 1
        if rest == ["-t"]:
            n = _counter(state, "stream", True)
            streams = cfg.get("streams", ["dualsense_stream.txt"])
            return _replay(HERE / streams[min(n, len(streams) - 1)], float(cfg.get("speed", 1.0)))
    sys.stderr.write(f"fake adb: unsupported command {argv}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
