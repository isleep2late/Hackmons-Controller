# Downloads: v0.2.2

Whole files, committed here so they can be fetched straight from GitHub. Open a file below
on GitHub and use the **Download raw file** button (the arrow icon next to "Raw"), or fetch it
from a terminal:

```bash
curl -L -o GCBridge-0.2.2.apk \
  https://github.com/isleep2late/Hackmons-Controller/raw/main/releases/v0.2.2/GCBridge-0.2.2.apk
```

| File | What it is | Use |
|---|---|---|
| `GCBridge-0.2.2.apk` | the Android app (debug-signed with the same key as 0.2.1, versionCode 4) | open it on the phone, or `adb install -r GCBridge-0.2.2.apk`; it updates 0.2.1 in place and keeps its recordings and settings |
| `SHA256SUMS.txt` | checksums | `sha256sum -c SHA256SUMS.txt` |

This release changes only the Android app (see `CHANGELOG.md`). The PC programs were not
rebuilt: use `controllerlog-0.2.1-windows-x64.zip` and `controllerlog-0.2.1-linux-x64.tar.gz`
from [`releases/v0.2.1/`](../v0.2.1/). The APK was built with
`python scripts/build_apk_nosdk.py --test`.
