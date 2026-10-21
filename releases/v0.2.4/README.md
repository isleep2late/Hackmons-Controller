# Downloads: v0.2.4

The files are attached to the GitHub Release
[v0.2.4](https://github.com/isleep2late/Hackmons-Controller/releases/tag/v0.2.4); they are not
committed here (only their checksums are). From a terminal:

```bash
curl -L -O https://github.com/isleep2late/Hackmons-Controller/releases/download/v0.2.4/controllerlog-0.2.4-linux-x64.tar.gz
```

| File | What it is | Use |
|---|---|---|
| `controllerlog-0.2.4-windows-x64.zip` | portable Windows program: own Python 3.14, all dependencies, SDL3.dll | unzip anywhere, double-click `live.cmd`, `live-bridge.cmd`, `doctor.cmd` or `view.cmd`; `controllerlog.cmd <command>` for the rest. Replay and `--bridge` need the ViGEmBus driver |
| `controllerlog-0.2.4-linux-x64.tar.gz` | portable Linux program (x86-64, glibc 2.28+: Ubuntu 20.04, Debian 11, Fedora, Arch and later; SDL3 3.4.16 included) | `tar xzf` it, run `./install-udev-rules` once per computer (sudo; lets you open the controller over USB and create the virtual pad), replug the controller, then `./doctor`, `./live`, `./live-bridge`, `./view` or `./controllerlog <command>` inside the folder |
| `GCBridge-0.2.2.apk` | the Android app, unchanged since [v0.2.2](../v0.2.2/) (same file) | open it on the phone, or `adb install -r GCBridge-0.2.2.apk` |
| `SHA256SUMS.txt` | checksums | `sha256sum -c SHA256SUMS.txt` |

This release changes only the PC programs (see `CHANGELOG.md`): the Linux bundle runs on
distributions back to glibc 2.28 (its SDL3 is built in the manylinux_2_28 container by
`scripts/build_sdl3_linux.sh`), `./install-udev-rules` sets up device access on a computer
without Steam, and the launchers pass extra options through. Built with
`scripts/build_sdl3_linux.sh`, then
`python scripts/build_bundle.py --platform linux --sdl3-lib build/sdl3/libSDL3.so.0 --archive`
and `--platform windows --archive`.
