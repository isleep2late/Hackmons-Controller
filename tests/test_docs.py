"""Docs stay consistent with the code (commands, flags, claims about edited files)."""
from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path

from packaging.specifiers import SpecifierSet

import controllerlog
from controllerlog.cli import build_parser, main
from controllerlog.formats import gm2
from controllerlog.formats.gm2 import GB_FRAME_SAMPLES, Gm2Header, Gm2Movie

ROOT = Path(controllerlog.__file__).resolve().parent.parent
DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _subcommands() -> set[str]:
    sub = next(a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    return set(sub.choices)


def test_docs_only_name_real_subcommands():
    cmds = _subcommands()
    bad = []
    for f in DOCS:
        for m in re.finditer(r"(?:^|`)controllerlog ([a-z][a-z0-9]*)", _text(f), re.M):
            if m.group(1) not in cmds:
                bad.append(f"{f.name}: controllerlog {m.group(1)}")
    assert not bad, bad
    assert "controllerlog align" in _text(ROOT / "docs" / "TAS.md")


def test_docs_do_not_mention_a_record_subcommand():
    for name in ("TAS.md", "FORMATS.md"):
        assert "controllerlog record" not in _text(ROOT / "docs" / name), name
    # absolute latency can't be measured from a GSE log (whole-second start time)
    assert "is your **input latency**" not in _text(ROOT / "docs" / "TAS.md")


def test_readme_command_table_lists_every_subcommand():
    table = _text(ROOT / "README.md").split("## Commands", 1)[1].split("\n## ", 1)[0]
    missing = sorted(c for c in _subcommands() if f"`{c}" not in table)
    assert not missing, missing


def test_docs_do_not_claim_gm2_edits_are_flagged(tmp_path):
    src, out = tmp_path / "a.gm2", tmp_path / "b.gm2"
    gm2.write_gm2(src, Gm2Movie(Gm2Header(rom_name="T", emu_version="0.6"), b"", [(GB_FRAME_SAMPLES, 0)] * 10))
    assert main(["edit", str(src), str(out), "--do", "hold 3 east"]) == 0
    assert src.read_bytes()[:1024] == out.read_bytes()[:1024]   # an edited .gm2 carries no marker
    readme = " ".join(_text(ROOT / "README.md").split())
    tas_md = " ".join(_text(ROOT / "docs" / "TAS.md").split())
    assert "Every edit is flagged `tas_edited` in the metadata" not in readme
    assert "Every edit sets `tas_edited` / adds a comment" not in tas_md


def test_docs_describe_gm2_insert_delete_correctly():
    tas_md = " ".join(_text(ROOT / "docs" / "TAS.md").split())
    formats = " ".join(_text(ROOT / "docs" / "FORMATS.md").split())
    assert "inserting appends frames with the standard 35112-sample budget" not in tas_md
    assert "Edits change only the button fields" not in formats


def test_ffmpeg_composite_example_forces_libvpx_for_alpha():
    line = next(l for l in _text(ROOT / "docs" / "TAS.md").splitlines()
                if l.startswith("ffmpeg ") and "inputs.webm" in l)
    # ffmpeg's native VP9 decoder ignores the yuva420p alpha plane; libvpx must be forced for that input
    assert re.search(r"-c:v libvpx-vp9\s+-i inputs\.webm", line), line


def test_requires_python_matches_zstd_dependency():
    proj = tomllib.loads(_text(ROOT / "pyproject.toml"))["project"]
    spec = SpecifierSet(proj["requires-python"])
    has_fallback = any("zstd" in d.lower() for d in proj.get("dependencies", []))
    assert not spec.contains("3.13") or has_fallback, (
        "reading .gm2 needs compression.zstd (Python 3.14+) but pyproject allows " + str(spec))
    assert "3.14" in _text(ROOT / "README.md").split("## Quick start", 1)[1].split("\n## ", 1)[0]


def test_quick_start_activates_venv_and_mentions_video_extra():
    qs = _text(ROOT / "README.md").split("## Quick start", 1)[1].split("\n## ", 1)[0]
    assert "activate" in qs or r"Scripts\controllerlog" in qs
    assert "[all" in qs or "[video" in qs or "ffmpeg" in qs


def test_overlay_query_parameters_documented():
    text = _text(ROOT / "README.md") + _text(ROOT / "docs" / "LAYOUTS.md")
    missing = [k for k in ("device=", "history=", "fps=", "bg=", "scale=", "seconds=",
                           "labels=", "counts=", "map=") if k not in text]
    assert not missing, missing


def test_switch2_live_options_documented_as_live_options():
    text = _text(ROOT / "docs" / "BLUETOOTH.md")
    assert "--switch2-orientation" in text and "--switch2-deadzone" in text
    args = build_parser().parse_args(["live", "--switch2", "--switch2-orientation", "vertical",
                                      "--switch2-deadzone", "0"])
    assert args.switch2_orientation == "vertical" and args.switch2_deadzone == 0
