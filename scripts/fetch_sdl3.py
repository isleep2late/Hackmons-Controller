"""Download the official SDL3 runtime DLL from libsdl-org's GitHub releases into ./vendor.

Usage:  python scripts/fetch_sdl3.py [VERSION]      (default: 3.4.16)

Only Windows x64 is handled here; on Linux/macOS install SDL3 from your
package manager (the loader also searches the system library path).
"""

from __future__ import annotations

import hashlib
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

DEFAULT_VERSION = "3.4.16"
ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    version = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_VERSION
    url = (f"https://github.com/libsdl-org/SDL/releases/download/release-{version}/"
           f"SDL3-{version}-win32-x64.zip")
    print(f"Downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "controllerlog-fetch-sdl3"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    print(f"  {len(data):,} bytes, sha256 {hashlib.sha256(data).hexdigest()}")
    out_dir = ROOT / "vendor"
    out_dir.mkdir(exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith("sdl3.dll")]
        if not names:
            print("SDL3.dll not found in archive", file=sys.stderr)
            return 1
        dll = zf.read(names[0])
    target = out_dir / "SDL3.dll"
    target.write_bytes(dll)
    (out_dir / "SDL3.version").write_text(version + "\n", encoding="utf-8")
    print(f"Wrote {target} ({len(dll):,} bytes, sha256 {hashlib.sha256(dll).hexdigest()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
