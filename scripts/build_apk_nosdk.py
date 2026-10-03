"""Build and unit-test the GC Bridge APK without the Android SDK or Gradle.

Meant for machines that can't download the SDK from dl.google.com (some cloud dev
boxes). It uses the same tools the SDK ships, fetched from mirrors that are usually
reachable: aapt2 (from the Apktool jar), R8/D8 (Google's r8-releases bucket), the
Robolectric android-all jar (Maven Central) as android.jar, apksig via uber-apk-signer
(GitHub), plus JUnit 4 for the JVM tests. A JDK 17+ must be on PATH.

    python scripts/build_apk_nosdk.py --fetch      # download the tools into vendor/android-nosdk
    python scripts/build_apk_nosdk.py              # build android/gcbridge -> app-debug.apk
    python scripts/build_apk_nosdk.py --test       # also run the JVM unit tests

The APK is debug-signed (same as Gradle's assembleDebug: install it with adb or by
opening it on the phone). Version numbers come from app/build.gradle.kts.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "android" / "gcbridge" / "app"
LAYOUTS = ROOT / "controllerlog" / "layouts"
TOOLS_DEFAULT = ROOT / "vendor" / "android-nosdk"
DEV_SUFFIX = ".dev"
DEV_LABEL = "GC Bridge DEV"

# name -> (url, sha256 or None). android-all "17" = Android 17 (API 37).
TOOLS = {
    "apktool.jar": ("https://github.com/iBotPeaches/Apktool/releases/download/v2.12.1/"
                    "apktool_2.12.1.jar", None),
    "r8lib.jar": ("https://storage.googleapis.com/r8-releases/raw/8.7.18/r8lib.jar", None),
    "uber-apk-signer.jar": ("https://github.com/patrickfav/uber-apk-signer/releases/download/"
                            "v1.3.0/uber-apk-signer-1.3.0.jar", None),
    "android.jar": ("https://repo1.maven.org/maven2/org/robolectric/android-all/"
                    "17-robolectric-15733970/android-all-17-robolectric-15733970.jar", None),
    "junit.jar": ("https://repo1.maven.org/maven2/junit/junit/4.13.2/junit-4.13.2.jar", None),
    "hamcrest.jar": ("https://repo1.maven.org/maven2/org/hamcrest/hamcrest-core/1.3/"
                     "hamcrest-core-1.3.jar", None),
}


def log(msg: str) -> None:
    print(f"[nosdk] {msg}", flush=True)


def run(cmd: list[str], **kw) -> None:
    log(" ".join(str(c) for c in cmd[:6]) + (" ..." if len(cmd) > 6 else ""))
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def fetch(tools: Path) -> None:
    tools.mkdir(parents=True, exist_ok=True)
    for name, (url, sha) in TOOLS.items():
        dst = tools / name
        if dst.exists() and dst.stat().st_size > 1000:
            continue
        log(f"downloading {name}")
        req = urllib.request.Request(url, headers={"User-Agent": "controllerlog-nosdk"})
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    data = r.read()
                break
            except Exception as e:  # rate limits (HTTP 429) are common on Maven Central
                if attempt == 4:
                    raise
                log(f"  retry after error: {e}")
                import time
                time.sleep(5 * (attempt + 1))
        if sha and hashlib.sha256(data).hexdigest() != sha:
            raise SystemExit(f"{name}: checksum mismatch")
        dst.write_bytes(data)
    aapt2 = tools / ("aapt2.exe" if os.name == "nt" else "aapt2")
    if not aapt2.exists():
        member = {"nt": "prebuilt/windows/aapt2.exe", "posix": "prebuilt/linux/aapt2_64"}[os.name]
        if sys.platform == "darwin":
            member = "prebuilt/macosx/aapt2_64"
        with zipfile.ZipFile(tools / "apktool.jar") as zf:
            aapt2.write_bytes(zf.read(member))
        aapt2.chmod(0o755)
    log(f"tools ready in {tools}")


def gradle_values() -> dict[str, str]:
    text = (APP / "build.gradle.kts").read_text(encoding="utf-8")
    out = {}
    for key in ("namespace", "applicationId", "versionName"):
        m = re.search(rf'{key}\s*=\s*"([^"]+)"', text)
        out[key] = m.group(1) if m else ""
    for key in ("minSdk", "targetSdk", "versionCode"):
        m = re.search(rf"{key}\s*=\s*(\d+)", text)
        out[key] = m.group(1) if m else ""
    return out


def dev_manifest(src: str, namespace: str) -> str:
    authority = f'android:authorities="{namespace}.recordings"'
    if authority not in src:
        raise SystemExit(f"the manifest has no {authority}; the DEV build cannot rename the provider")
    src = src.replace(authority, f'android:authorities="{namespace}{DEV_SUFFIX}.recordings"')
    attach = re.compile(r'\s*<intent-filter>\s*<action android:name="android\.hardware\.usb\.action\.'
                        r'USB_DEVICE_ATTACHED"\s*/>\s*</intent-filter>\s*<meta-data\s+android:name="android\.'
                        r'hardware\.usb\.action\.USB_DEVICE_ATTACHED"\s+android:resource="@xml/device_filter"\s*/>')
    src, n = attach.subn("", src)
    if n != 1 or "USB_DEVICE_ATTACHED" in src:
        raise SystemExit("could not remove the USB_DEVICE_ATTACHED filter for the DEV build")
    return src


def prepared_manifest(build: Path, g: dict[str, str], variant: str = "release") -> Path:
    """Gradle injects package/version attributes and strips tools: attributes; do the same."""
    src = (APP / "src" / "main" / "AndroidManifest.xml").read_text(encoding="utf-8")
    src = re.sub(r'\s+tools:[A-Za-z]+="[^"]*"', "", src)
    src = re.sub(r'\s+xmlns:tools="[^"]*"', "", src)
    if variant == "dev":
        src = dev_manifest(src, g["namespace"])
    package = g["namespace"] or g["applicationId"]
    attrs = (f' package="{package}" android:versionCode="{g["versionCode"]}"'
             f' android:versionName="{g["versionName"]}"')
    src = src.replace("<manifest ", "<manifest" + attrs + " ", 1)
    dst = build / "AndroidManifest.xml"
    dst.write_text(src, encoding="utf-8")
    return dst


def java_files(*dirs: Path) -> list[Path]:
    return sorted(p for d in dirs if d.exists() for p in d.rglob("*.java"))


def check_link_options(aapt2: Path, options: list[str]) -> None:
    r = subprocess.run([str(aapt2), "link", "--help"], capture_output=True, text=True)
    text = r.stdout + r.stderr
    missing = [o for o in options if o not in text]
    if missing:
        raise SystemExit(f"this aapt2 ({aapt2}) has no {', '.join(missing)}, so the DEV build can't rename "
                         "the package; use an aapt2 that has them")


def dev_resources(build: Path, aapt2: Path) -> Path:
    values = build / "dev-res" / "values"
    values.mkdir(parents=True)
    (values / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
        f'    <string name="app_name">{DEV_LABEL}</string>\n'
        f'    <string name="accessibility_label">{DEV_LABEL} button capture</string>\n</resources>\n',
        encoding="utf-8")
    out = build / "dev-res.zip"
    run([aapt2, "compile", "--dir", build / "dev-res", "-o", out])
    return out


def build(tools: Path, test: bool, variant: str = "release") -> Path:
    g = gradle_values()
    aapt2 = tools / ("aapt2.exe" if os.name == "nt" else "aapt2")
    android_jar = tools / "android.jar"
    if variant == "dev" and g["applicationId"] != g["namespace"]:
        raise SystemExit("the DEV build expects applicationId == namespace in app/build.gradle.kts")
    build = APP / "build" / ("nosdk-dev" if variant == "dev" else "nosdk")
    if build.exists():
        shutil.rmtree(build)
    (build / "gen").mkdir(parents=True)
    (build / "classes").mkdir()
    (build / "dex").mkdir()
    (build / "assets" / "layouts").mkdir(parents=True)
    for p in LAYOUTS.glob("*.json"):
        shutil.copy(p, build / "assets" / "layouts" / p.name)

    # 1. resources
    run([aapt2, "compile", "--dir", APP / "src" / "main" / "res", "-o", build / "res.zip"])
    manifest = prepared_manifest(build, g, variant)
    unsigned = build / "app-unsigned.apk"
    extra: list = []
    if variant == "dev":
        check_link_options(aapt2, ["--rename-manifest-package", "--custom-package"])
        extra = ["-R", dev_resources(build, aapt2),
                 "--rename-manifest-package", g["namespace"] + DEV_SUFFIX,
                 "--custom-package", g["namespace"]]
    run([aapt2, "link", "-o", unsigned, "-I", android_jar, "--manifest", manifest,
         "-R", build / "res.zip", *extra, "--java", build / "gen", "-A", build / "assets",
         "--min-sdk-version", g["minSdk"], "--target-sdk-version", g["targetSdk"],
         "--auto-add-overlay", "--no-version-vectors"])

    # 2. java -> class -> dex
    srcs = java_files(APP / "src" / "main" / "java", build / "gen")
    # --release + classpath: javac refuses -bootclasspath for targets newer than 8. The
    # android.jar java.* classes are shadowed by the JDK's, which is fine for compiling.
    run(["javac", "--release", "17", "-encoding", "UTF-8", "-Xlint:all", "-Xlint:-options",
         "-Xlint:-classfile", "-Xlint:-processing", "-cp", android_jar, "-d", build / "classes",
         *srcs])
    classes = sorted((build / "classes").rglob("*.class"))
    run(["java", "-cp", tools / "r8lib.jar", "com.android.tools.r8.D8", "--release",
         "--min-api", g["minSdk"], "--lib", android_jar, "--output", build / "dex", *classes])

    # 3. package: dex in, resources.arsc stored uncompressed (required from targetSdk 30)
    packed = build / "app-packed.apk"
    with zipfile.ZipFile(unsigned) as src, zipfile.ZipFile(packed, "w") as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            method = zipfile.ZIP_STORED if info.filename == "resources.arsc" else info.compress_type
            dst.writestr(info.filename, data, compress_type=method)
        dst.write(build / "dex" / "classes.dex", "classes.dex", compress_type=zipfile.ZIP_DEFLATED)

    # 4. zipalign + debug-sign (v1 + v2 + v3)
    out_dir = APP / "build" / "outputs" / "apk" / "nosdk"
    out_dir.mkdir(parents=True, exist_ok=True)
    (build / "signed").mkdir(exist_ok=True)
    run(["java", "-jar", tools / "uber-apk-signer.jar", "--apks", packed, "--allowResign",
         "-o", build / "signed"])
    signed = next((build / "signed").glob("*.apk"))
    apk = out_dir / ("app-dev.apk" if variant == "dev" else "app-debug.apk")
    shutil.copy(signed, apk)
    log(f"APK: {apk} ({apk.stat().st_size:,} bytes)")

    if test:
        (build / "test-classes").mkdir()
        cp = os.pathsep.join(str(p) for p in (build / "classes", tools / "junit.jar",
                                              tools / "hamcrest.jar", android_jar))
        tests = java_files(APP / "src" / "test" / "java")
        run(["javac", "--release", "17", "-encoding", "UTF-8", "-nowarn", "-cp", cp, "-d",
             build / "test-classes", *tests])
        names = sorted(str(p.relative_to(APP / "src" / "test" / "java"))[:-5].replace("/", ".")
                       .replace("\\", ".") for p in tests if p.name.endswith("Test.java"))
        run(["java", "-cp", os.pathsep.join([str(build / "test-classes"), cp]),
             f"-Dswitch2.py={ROOT / 'controllerlog' / 'input' / 'switch2_usb.py'}",
             f"-Dcontrollerlog.root={ROOT}",
             "org.junit.runner.JUnitCore", *names])
    return apk


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tools", type=Path, default=Path(os.environ.get("NOSDK_TOOLS", TOOLS_DEFAULT)))
    ap.add_argument("--fetch", action="store_true", help="download the tools and exit")
    ap.add_argument("--test", action="store_true", help="run the JVM unit tests after building")
    ap.add_argument("--variant", choices=("release", "dev"), default="release",
                    help="dev: package com.controllerlog.gcbridge.dev, label GC Bridge DEV, its own provider "
                         "authority and no USB attach filter, so it installs beside the release")
    args = ap.parse_args()
    if args.fetch:
        fetch(args.tools)
        return 0
    missing = [n for n in TOOLS if not (args.tools / n).exists()]
    if missing:
        raise SystemExit(f"missing {missing} in {args.tools}; run with --fetch first")
    build(args.tools, args.test, args.variant)
    return 0


if __name__ == "__main__":
    sys.exit(main())
