"""Build a portable ControllerLog bundle for Windows or Linux: no Python install needed.

The bundle is a folder with its own Python 3.14 (python-build-standalone), the
controllerlog package and every dependency, SDL3, and launchers::

    controllerlog-<version>-windows-x64/     controllerlog-<version>-linux-x64/
        controllerlog.cmd  live.cmd  doctor.cmd    controllerlog  live  doctor
        python/  (python.exe, Lib/site-packages, vendor/SDL3.dll)
        README.txt

Both bundles can be built on Linux (the Windows one is assembled from Windows wheels).

    python scripts/build_bundle.py --platform windows
    python scripts/build_bundle.py --platform linux --sdl3-lib /path/to/libSDL3.so.0
    python scripts/build_bundle.py --platform windows --archive     # also zip it into dist/

Windows gets the official SDL3.dll from libsdl-org's GitHub release; Linux needs a
libSDL3.so.0 built on a glibc no newer than the target's (see .github/workflows/release.yml).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename

ROOT = Path(__file__).resolve().parent.parent
DOWNLOADS = ROOT / "vendor" / "downloads"
PBS_RELEASES = "https://github.com/astral-sh/python-build-standalone/releases/download"
PBS_LATEST = ("https://raw.githubusercontent.com/astral-sh/python-build-standalone/"
              "latest-release/latest-release.json")
SDL3_VERSION = "3.4.16"

TARGETS = {
    "windows": {
        "pbs": "x86_64-pc-windows-msvc-install_only_stripped.tar.gz",
        "platforms": ["win_amd64"],
        "env": {"platform_system": "Windows", "sys_platform": "win32", "os_name": "nt",
                "platform_machine": "AMD64"},
        "site": ("Lib", "site-packages"),
        "sdl_name": "SDL3.dll",
        "extras": ["video", "ble", "usb", "windows"],
    },
    "linux": {
        "pbs": "x86_64-unknown-linux-gnu-install_only_stripped.tar.gz",
        "platforms": ["manylinux_2_17_x86_64", "manylinux2014_x86_64", "manylinux_2_28_x86_64",
                      "manylinux_2_34_x86_64", "linux_x86_64"],
        "env": {"platform_system": "Linux", "sys_platform": "linux", "os_name": "posix",
                "platform_machine": "x86_64"},
        "site": ("lib", "python3.14", "site-packages"),
        "sdl_name": "libSDL3.so.0",
        "extras": ["video", "ble", "usb"],
    },
}


def log(msg: str) -> None:
    print(f"[bundle] {msg}", flush=True)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    log(" ".join(str(c) for c in cmd[:8]) + (" ..." if len(cmd) > 8 else ""))
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def download(url: str, dst: Path) -> Path:
    if dst.exists() and dst.stat().st_size > 0:
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    log(f"downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "controllerlog-bundle"})
    with urllib.request.urlopen(req, timeout=300) as r, open(dst, "wb") as f:
        shutil.copyfileobj(r, f)
    return dst


def project_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    return re.search(r'^version = "([^"]+)"', text, re.M).group(1)


def project_dependencies(extras: list[str]) -> list[str]:
    import tomllib
    proj = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    deps = list(proj["dependencies"])
    for e in extras:
        deps += proj.get("optional-dependencies", {}).get(e, [])
    return deps


def pbs_tag() -> str:
    data = json.loads(urllib.request.urlopen(PBS_LATEST, timeout=60).read())
    return data["tag"]


def find_python_version(tag: str, target: dict) -> str:
    """Newest 3.14.x asset in the release (the SHA256SUMS list names every asset)."""
    sums = download(f"{PBS_RELEASES}/{tag}/SHA256SUMS", DOWNLOADS / tag / "SHA256SUMS")
    versions = set()
    for line in sums.read_text().splitlines():
        m = re.search(r"cpython-(3\.14\.\d+)\+" + re.escape(tag) + "-" + re.escape(target["pbs"]), line)
        if m:
            versions.add(m.group(1))
    if not versions:
        raise SystemExit(f"no 3.14 build for {target['pbs']} in python-build-standalone {tag}")
    return max(versions, key=lambda v: tuple(int(x) for x in v.split(".")))


def verify_sha256(path: Path, tag: str) -> None:
    sums = (DOWNLOADS / tag / "SHA256SUMS").read_text()
    m = re.search(r"^([0-9a-f]{64})\s+" + re.escape(path.name) + r"$", sums, re.M)
    if not m:
        raise SystemExit(f"{path.name} not in SHA256SUMS")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != m.group(1):
        raise SystemExit(f"checksum mismatch for {path.name}")


# --- wheels ---------------------------------------------------------------------------------

def wheel_requires(wheel: Path) -> list[str]:
    with zipfile.ZipFile(wheel) as zf:
        meta = next(n for n in zf.namelist() if n.endswith(".dist-info/METADATA"))
        text = zf.read(meta).decode("utf-8", "replace")
    return [line.split(":", 1)[1].strip() for line in text.splitlines()
            if line.startswith("Requires-Dist:")]


def marker_env(target: dict, pyver: str) -> dict[str, str]:
    major_minor = ".".join(pyver.split(".")[:2])
    return {**target["env"], "python_version": major_minor, "python_full_version": pyver,
            "implementation_name": "cpython", "platform_python_implementation": "CPython",
            "implementation_version": pyver, "platform_release": "", "platform_version": "",
            "extra": ""}


def dist_name(path: Path) -> str:
    """Canonical distribution name of a wheel or sdist file name."""
    if path.suffix == ".whl":
        return canonicalize_name(parse_wheel_filename(path.name)[0])
    stem = path.name
    for suffix in (".tar.gz", ".zip", ".tar.bz2"):
        if stem.endswith(suffix):
            stem = stem[:-len(suffix)]
    return canonicalize_name(stem.rsplit("-", 1)[0])


def pip_download(req: Requirement, target: dict, pyver: str, dest: Path, source: bool = False) -> Path:
    """Fetch one distribution for the target platform; returns the wheel (or sdist) path.

    Markers are evaluated by resolve_wheels for the *target*, so only name and version go to
    pip (which would evaluate them for the host and skip Windows-only packages on Linux).
    """
    spec = req.name + str(req.specifier)
    cmd = [sys.executable, "-m", "pip", "download", "-q", "--no-deps", "-d", dest, spec]
    if source:
        cmd += ["--no-binary", ":all:"]
    else:
        cmd += ["--only-binary", ":all:", "--python-version", pyver, "--implementation", "cp"]
        for p in target["platforms"]:
            cmd += ["--platform", p]
    run(cmd)
    want = canonicalize_name(req.name)
    kind = (lambda p: p.suffix != ".whl") if source else (lambda p: p.suffix == ".whl")
    cands = [p for p in dest.glob("*") if kind(p) and dist_name(p) == want]
    if not cands:
        raise SystemExit(f"pip download produced nothing for {spec}")
    return max(cands, key=lambda p: p.stat().st_mtime)


def build_pure_wheel(sdist: Path, dest: Path) -> Path:
    before = set(dest.glob("*.whl"))
    run([sys.executable, "-m", "pip", "wheel", "-q", "--no-deps", "-w", dest, sdist])
    new = set(dest.glob("*.whl")) - before
    if not new:
        raise SystemExit(f"no wheel built from {sdist}")
    return new.pop()


def resolve_wheels(specs: list[str], target: dict, pyver: str, dest: Path) -> list[Path]:
    """Download wheels for ``specs`` and, transitively, their requirements that apply to the
    target platform (pip evaluates markers for the *host*, so that part is done here)."""
    env = marker_env(target, pyver)
    wheels: dict[str, Path] = {}
    queue = list(specs)
    while queue:
        spec = queue.pop(0)
        req = Requirement(spec)
        key = req.name.lower().replace("_", "-")
        if key in wheels:
            continue
        if req.marker is not None and not req.marker.evaluate(env):
            continue
        try:
            wheel = pip_download(req, target, pyver, dest)
        except subprocess.CalledProcessError:
            log(f"no binary wheel for {req.name}; building from source (pure Python only)")
            sdist = pip_download(req, target, pyver, dest, source=True)
            wheel = build_pure_wheel(sdist, dest)
        wheels[key] = wheel
        for line in wheel_requires(wheel):
            r = Requirement(line)
            if r.marker is not None and not r.marker.evaluate(env):
                continue
            queue.append(str(r))
    return list(wheels.values())


def install_wheels(wheels: list[Path], target: dict, pyver: str, site: Path) -> None:
    cmd = [sys.executable, "-m", "pip", "install", "-q", "--no-deps", "--no-compile", "--upgrade",
           "--only-binary", ":all:", "--python-version", pyver, "--implementation", "cp",
           "--target", site]
    for p in target["platforms"]:
        cmd += ["--platform", p]
    run(cmd + [str(w) for w in wheels])
    # pip --target writes console scripts with the host's shebang: the launchers use -m instead
    shutil.rmtree(site / "bin", ignore_errors=True)
    shutil.rmtree(site / "Scripts", ignore_errors=True)


# --- bundle ---------------------------------------------------------------------------------

def write_launchers(bundle: Path, platform: str, version: str) -> None:
    if platform == "windows":
        (bundle / "controllerlog.cmd").write_text(
            "@echo off\r\n\"%~dp0python\\python.exe\" -m controllerlog %*\r\n", encoding="ascii")
        (bundle / "live.cmd").write_text(
            "@echo off\r\necho Starting the live overlay (recording on). Close this window or press q to stop.\r\n"
            "\"%~dp0python\\python.exe\" -m controllerlog live --record --open\r\npause\r\n",
            encoding="ascii")
        (bundle / "doctor.cmd").write_text(
            "@echo off\r\n\"%~dp0python\\python.exe\" -m controllerlog doctor\r\npause\r\n",
            encoding="ascii")
        (bundle / "view.cmd").write_text(
            "@echo off\r\n\"%~dp0python\\python.exe\" -m controllerlog view %*\r\npause\r\n",
            encoding="ascii")
    else:
        sh = "#!/bin/sh\nHERE=$(cd \"$(dirname \"$0\")\" && pwd)\n"
        for name, args in (("controllerlog", '"$@"'), ("live", "live --record --open"),
                           ("doctor", "doctor"), ("view", 'view "$@"')):
            p = bundle / name
            p.write_text(sh + f'exec "$HERE/python/bin/python3" -m controllerlog {args}\n')
            p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (bundle / "README.txt").write_text(
        f"ControllerLog {version} portable ({platform}, x86-64)\n"
        "=================================================\n\n"
        "Nothing to install: this folder has its own Python and every dependency.\n\n"
        + ("Double-click live.cmd to start the live overlay with recording, doctor.cmd for the\n"
           "setup check, view.cmd to open the recording viewer. Any other command:\n\n"
           "    controllerlog.cmd <command> ...      e.g.  controllerlog.cmd stats recordings\\run.ctlog\n\n"
           "Replay and --bridge need the ViGEmBus driver: https://github.com/nefarius/ViGEmBus/releases\n"
           if platform == "windows" else
           "Run ./live for the live overlay with recording, ./doctor for the setup check, ./view for\n"
           "the recording viewer, or ./controllerlog <command> for anything else. Reading\n"
           "controllers needs access to /dev/input (be in the 'input' group or use udev rules).\n"
           "Replay through a virtual controller is Windows-only (ViGEmBus).\n")
        + "\nRecordings go to ./recordings. Full documentation: README.md in the repository\n"
        "(https://github.com/isleep2late/Hackmons-Controller).\n", encoding="utf-8")


def make_archive(bundle: Path, platform: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    if platform == "windows":
        archive = out_dir / (bundle.name + ".zip")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for p in sorted(bundle.rglob("*")):
                zf.write(p, f"{bundle.name}/{p.relative_to(bundle)}")
    else:
        archive = out_dir / (bundle.name + ".tar.gz")
        with tarfile.open(archive, "w:gz", compresslevel=6) as tf:
            tf.add(bundle, arcname=bundle.name)
    log(f"archive: {archive} ({archive.stat().st_size / 1e6:.1f} MB)")
    return archive


def build(platform: str, out_dir: Path, sdl3_lib: Path | None, archive: bool,
          pyver_override: str | None, tag_override: str | None, smoke: bool) -> Path:
    target = TARGETS[platform]
    version = project_version()
    tag = tag_override or pbs_tag()
    pyver = pyver_override or find_python_version(tag, target)
    log(f"controllerlog {version} for {platform}: Python {pyver} (python-build-standalone {tag})")

    # 1. Python
    asset = f"cpython-{pyver}+{tag}-{target['pbs']}"
    tarball = download(f"{PBS_RELEASES}/{tag}/{asset}", DOWNLOADS / tag / asset)
    verify_sha256(tarball, tag)
    work = ROOT / "build" / "bundle" / platform
    if work.exists():
        shutil.rmtree(work)
    bundle = work / f"controllerlog-{version}-{platform}-x64"
    bundle.mkdir(parents=True)
    with tarfile.open(tarball) as tf:
        tf.extractall(bundle, filter="data")   # -> bundle/python
    python_dir = bundle / "python"
    site = python_dir.joinpath(*target["site"])
    site.mkdir(parents=True, exist_ok=True)

    # 2. packages: controllerlog itself + dependencies resolved for the target
    wheel_dir = ROOT / "build" / "wheels" / platform
    wheel_dir.mkdir(parents=True, exist_ok=True)
    for old in wheel_dir.glob("controllerlog-*"):
        old.unlink()
    run([sys.executable, "-m", "pip", "wheel", "-q", "--no-deps", "-w", wheel_dir, ROOT])
    own = next(wheel_dir.glob("controllerlog-*.whl"))
    deps = project_dependencies(target["extras"])
    wheels = resolve_wheels(deps, target, pyver, wheel_dir)
    install_wheels([own] + wheels, target, pyver, site)
    log(f"installed {len(wheels) + 1} packages into {site.relative_to(bundle)}")

    # 3. SDL3 next to the interpreter (sys.prefix/vendor is on the loader's search path)
    vendor = python_dir / "vendor"
    vendor.mkdir(exist_ok=True)
    if platform == "windows":
        url = (f"https://github.com/libsdl-org/SDL/releases/download/release-{SDL3_VERSION}/"
               f"SDL3-{SDL3_VERSION}-win32-x64.zip")
        z = download(url, DOWNLOADS / f"SDL3-{SDL3_VERSION}-win32-x64.zip")
        with zipfile.ZipFile(z) as zf:
            name = next(n for n in zf.namelist() if n.lower().endswith("sdl3.dll"))
            (vendor / "SDL3.dll").write_bytes(zf.read(name))
    elif sdl3_lib is not None:
        shutil.copy(sdl3_lib, vendor / target["sdl_name"])
    else:
        log("WARNING: no --sdl3-lib given; the Linux bundle will use the system SDL3 if present")

    # 4. launchers, readme, docs, archive
    write_launchers(bundle, platform, version)
    (bundle / "recordings").mkdir()
    shutil.copy(ROOT / "LICENSE", bundle / "LICENSE.txt")
    shutil.copy(ROOT / "README.md", bundle / "README.md")
    docs = bundle / "docs"
    docs.mkdir()
    for p in (ROOT / "docs").glob("*.md"):
        shutil.copy(p, docs / p.name)
    size = sum(p.stat().st_size for p in bundle.rglob("*") if p.is_file())
    log(f"bundle: {bundle} ({size / 1e6:.1f} MB)")
    if smoke and platform == "linux" and sys.platform == "linux":
        smoke_test(bundle)
    if archive:
        make_archive(bundle, platform, out_dir)
    return bundle


def smoke_test(bundle: Path) -> None:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    run([bundle / "controllerlog", "--help"], env=env, stdout=subprocess.DEVNULL)
    out = subprocess.run([bundle / "controllerlog", "layouts"], env=env, check=True,
                         capture_output=True, text=True).stdout
    assert "gamecube" in out, out
    sample = ROOT / "tests" / "fixtures" / "android" / "gcbridge_sample.ctlog"
    if sample.exists():
        out = subprocess.run([bundle / "controllerlog", "stats", sample], env=env, check=True,
                             capture_output=True, text=True).stdout
        assert "south" in out, out
    log("smoke test passed")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--platform", choices=sorted(TARGETS), required=True)
    ap.add_argument("--out", type=Path, default=ROOT / "dist")
    ap.add_argument("--sdl3-lib", type=Path, help="prebuilt libSDL3.so.0 for the Linux bundle")
    ap.add_argument("--archive", action="store_true", help="also write the zip / tar.gz into --out")
    ap.add_argument("--python-version", help="exact CPython 3.14.x (default: newest in the release)")
    ap.add_argument("--pbs-tag", help="python-build-standalone release tag (default: latest)")
    ap.add_argument("--no-smoke", action="store_true", help="skip running the Linux bundle")
    args = ap.parse_args()
    build(args.platform, args.out, args.sdl3_lib, args.archive, args.python_version, args.pbs_tag,
          not args.no_smoke)
    return 0


if __name__ == "__main__":
    sys.exit(main())
