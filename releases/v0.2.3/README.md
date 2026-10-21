# Downloads: v0.2.3

The files are attached to the GitHub Release
[v0.2.3](https://github.com/isleep2late/Hackmons-Controller/releases/tag/v0.2.3); they are not
committed here (only their checksums are). From a terminal:

```bash
curl -L -O https://github.com/isleep2late/Hackmons-Controller/releases/download/v0.2.3/controllerlog-0.2.3-linux-x64.tar.gz
```

| File | What it is | Use |
|---|---|---|
| `controllerlog-0.2.3-windows-x64.zip` | portable Windows program: own Python 3.14, all dependencies, SDL3.dll | unzip anywhere, double-click `live.cmd`, `live-bridge.cmd`, `doctor.cmd` or `view.cmd`; `controllerlog.cmd <command>` for the rest. Replay and `--bridge` need the ViGEmBus driver |
| `controllerlog-0.2.3-linux-x64.tar.gz` | portable Linux program (x86-64, glibc 2.39+ for the bundled SDL3 3.4.16) | `tar xzf` it, then `./doctor`, `./live`, `./live-bridge`, `./view` or `./controllerlog <command>` inside the folder. Replay and `--bridge` need write access to `/dev/uinput` (`./doctor` checks) |
| `GCBridge-0.2.2.apk` | the Android app, unchanged since [v0.2.2](../v0.2.2/) (same file) | open it on the phone, or `adb install -r GCBridge-0.2.2.apk` |
| `SHA256SUMS.txt` | checksums | `sha256sum -c SHA256SUMS.txt` |

This release changes only the PC programs (see `CHANGELOG.md`): `--bridge` and `replay` work on
Linux, and the new `live-bridge` launcher mirrors the controller onto a virtual Xbox 360 pad so
emulators that cannot start the Switch 2 GameCube pad see a normal gamepad. Built with
`python scripts/build_bundle.py --platform linux --sdl3-lib <SDL 3.4.16 libSDL3.so.0> --archive`
and `--platform windows --archive`.
