# Downloads: v0.3.0

The files are attached to the GitHub Release
[v0.3.0](https://github.com/isleep2late/Hackmons-Controller/releases/tag/v0.3.0); they are not
committed here, and their checksums are on the release page. From a terminal:

```bash
curl -L -O https://github.com/isleep2late/Hackmons-Controller/releases/download/v0.3.0/GCBridge-0.3.0.apk
```

| File | What it is | Use |
|---|---|---|
| `GCBridge-0.3.0.apk` | the Android app (debug-signed with the same key as 0.2.2, versionCode 5) | open it on the phone, or `adb install -r GCBridge-0.3.0.apk`. It is signed with the same key as 0.2.2 (built with `python scripts/build_apk_nosdk.py --test`), so it updates 0.2.2 in place and keeps its recordings and settings |
| `controllerlog-0.2.4-windows-x64.zip` | portable Windows program, unchanged since [v0.2.4](../v0.2.4/) | see [v0.2.4](../v0.2.4/) |
| `controllerlog-0.2.4-linux-x64.tar.gz` | portable Linux program, unchanged since [v0.2.4](../v0.2.4/) | see [v0.2.4](../v0.2.4/) |

This release changes only the Android app (see `CHANGELOG.md`): GC Bridge can hand the
controller it reads to [is2l Remote](https://github.com/isleep2late/is2l-remote/releases), a
separate program that lets you use a controller to play games remotely on your desktop. Set
up, check and calibrate the controller in GC Bridge, switch on *Send this controller to is2l
Remote*, and is2l Remote carries it to the PC as a virtual Xbox-style controller. You need the
is2l Remote release published alongside this one (or a newer one); see
[android/README.md](../../android/README.md#play-on-your-pc-with-is2l-remote) for the steps and
for what each input source delivers in the background.

**Not tested on a device.** The feed to is2l Remote is covered by the JVM unit tests only.
