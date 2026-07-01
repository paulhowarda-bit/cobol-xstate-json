#!/usr/bin/env python3
"""Build the distributable artifacts under ``dist/`` from the current source.

Run this after changing anything in ``src/`` or ``runtime/`` - the ``dist/``
artifacts are gitignored build output and do NOT update themselves. Skipping
this step ships stale code (e.g. a ``.pyz`` that predates a bug fix), so it is a
required step of any release.

Produces:
  dist/cobol-xstate.pyz          - a self-contained zipapp (the executable)
  dist/pyz-stage/                - the staging tree the .pyz is zipped from
  dist/cobol-xstate-pyz.tar.gz   - the .pyz, tarred
  dist/cobol-xstate-src.tar.gz   - a source archive (src/runtime/examples/docs)
  dist/cobol-xstate-src.zip      - the same source archive, zipped

Usage:  python build.py
"""

from __future__ import annotations

import os
import shutil
import stat
import tarfile
import zipapp
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
STAGE = DIST / "pyz-stage"
PKG = "cobol_xstate"

# Files/dirs that make up the redistributable source archive.
SRC_ARCHIVE_MEMBERS = [
    "src", "runtime", "examples", "tests",
    "README.md", "pyproject.toml", "build.py",
]

_MAIN = "# -*- coding: utf-8 -*-\nimport cobol_xstate.cli\ncobol_xstate.cli.main()\n"


def _on_rm_error(func, path, _exc):
    """rmtree handler: clear the read-only bit (common on Windows/OneDrive) and retry."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _clean_stage() -> None:
    if STAGE.exists():
        shutil.rmtree(STAGE, onexc=_on_rm_error)
    STAGE.mkdir(parents=True)


def _copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(
        src, dst,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "node_modules"),
    )


def build_pyz() -> Path:
    """Stage the package + runtime and zip them into a runnable .pyz."""
    _clean_stage()
    _copy_tree(ROOT / "src" / PKG, STAGE / PKG)
    _copy_tree(ROOT / "runtime", STAGE / "runtime")
    (STAGE / "__main__.py").write_text(_MAIN, encoding="utf-8")

    pyz = DIST / "cobol-xstate.pyz"
    if pyz.exists():
        pyz.unlink()
    # Shebang so `./cobol-xstate.pyz` works on POSIX; harmless on Windows.
    zipapp.create_archive(STAGE, target=pyz, interpreter="/usr/bin/env python3")
    return pyz


def build_pyz_tarball(pyz: Path) -> Path:
    out = DIST / "cobol-xstate-pyz.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        tar.add(pyz, arcname=pyz.name)
    return out


def _iter_source_files():
    for member in SRC_ARCHIVE_MEMBERS:
        p = ROOT / member
        if not p.exists():
            continue
        if p.is_file():
            yield p
        else:
            for f in p.rglob("*"):
                if f.is_file() and "__pycache__" not in f.parts \
                        and "node_modules" not in f.parts and f.suffix != ".pyc":
                    yield f


def build_source_archives() -> tuple[Path, Path]:
    tgz = DIST / "cobol-xstate-src.tar.gz"
    zp = DIST / "cobol-xstate-src.zip"
    files = sorted(set(_iter_source_files()))
    with tarfile.open(tgz, "w:gz") as tar:
        for f in files:
            tar.add(f, arcname=str(f.relative_to(ROOT)))
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, arcname=str(f.relative_to(ROOT)))
    return tgz, zp


def main() -> None:
    DIST.mkdir(exist_ok=True)
    pyz = build_pyz()
    pyz_tar = build_pyz_tarball(pyz)
    tgz, zp = build_source_archives()
    for artifact in (pyz, pyz_tar, tgz, zp):
        print(f"built {artifact.relative_to(ROOT)} ({artifact.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
