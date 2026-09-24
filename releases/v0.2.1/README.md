# Downloads: v0.2.1

Whole files, committed here so they can be fetched straight from GitHub. Open a file below
on GitHub and use the **Download raw file** button (the arrow icon next to "Raw"), or fetch it
from a terminal:

```bash
curl -L -o controllerlog-0.2.1-linux-x64.tar.gz \
  https://github.com/isleep2late/Hackmons-Controller/raw/claude/awesome-curie-q04679/releases/v0.2.1/controllerlog-0.2.1-linux-x64.tar.gz
```

(Replace the branch name in the URL with `main` once this branch is merged.)

| File | What it is | Use |
|---|---|---|
| `GCBridge-0.2.1.apk` | the Android app (debug-signed) | open it on the phone, or `adb install -r GCBridge-0.2.1.apk` |
| `controllerlog-0.2.1-windows-x64.zip` | portable Windows program: own Python 3.14, all dependencies, SDL3.dll | unzip anywhere, double-click `live.cmd`, `doctor.cmd` or `view.cmd`; `controllerlog.cmd <command>` for the rest. Replay and `--bridge` need the ViGEmBus driver |
| `controllerlog-0.2.1-linux-x64.tar.gz` | portable Linux program (x86-64, glibc 2.39+ for the bundled SDL3) | `tar xzf` it, then `./doctor`, `./live`, `./view` or `./controllerlog <command>` inside the folder |
| `SHA256SUMS.txt` | checksums | `sha256sum -c SHA256SUMS.txt` |

There is no single `.exe`: the Windows program is the folder inside the zip, with `.cmd`
launchers. The same files are what the Release workflow attaches to a GitHub Release
(`.github/workflows/release.yml`); this folder exists because that workflow can't run until
Actions is enabled for the repository. Rebuild them with `scripts/build_bundle.py` and
`scripts/build_apk_nosdk.py` (or Gradle).
