# Downloads: v0.3.1

The files are attached to the GitHub Release
[v0.3.1](https://github.com/isleep2late/Hackmons-Controller/releases/tag/v0.3.1); they are not
committed here, and their checksums are on the release page. From a terminal:

```bash
curl -L -O https://github.com/isleep2late/Hackmons-Controller/releases/download/v0.3.1/GCBridge-0.3.1.apk
```

| File | What it is | Use |
|---|---|---|
| `GCBridge-0.3.1.apk` | the Android app (debug-signed with the same key as 0.2.2, versionCode 6) | open it on the phone, or `adb install -r GCBridge-0.3.1.apk`. It is signed with the same key as 0.2.2 (built with `python scripts/build_apk_nosdk.py --test`), so it updates 0.2.2 and 0.3.0 in place and keeps their recordings and settings |
| `controllerlog-0.2.4-windows-x64.zip` | portable Windows program, unchanged since [v0.2.4](../v0.2.4/) | see [v0.2.4](../v0.2.4/) |
| `controllerlog-0.2.4-linux-x64.tar.gz` | portable Linux program, unchanged since [v0.2.4](../v0.2.4/) | see [v0.2.4](../v0.2.4/) |

This release changes only the Android app (see `CHANGELOG.md`): GC Bridge now sends the
controller only to the official [is2l Remote](https://github.com/isleep2late/is2l-remote/releases).
Before it connects it checks that the installed is2l Remote is signed with the certificate of
the is2l Remote releases (SHA-256
`84b56217735286469bdc3db3eab919c275bf7d5a85a41b4fa13170e2758d866d`; verified, it is what
`apksigner verify --print-certs` prints for the published `is2l-remote-0.1.0-android-arm64.apk`,
package `com.isleep2late.remote`). For any other app under
that name it does not connect, sends nothing, and says so in the **is2l Remote** section. An
is2l Remote you built yourself is signed with your own key and is refused as well; use a
released APK. See
[android/README.md](../../android/README.md#play-on-your-pc-with-is2l-remote) for the steps.

**Not tested on a device.** The feed to is2l Remote and the certificate check are covered by
the JVM unit tests only.
