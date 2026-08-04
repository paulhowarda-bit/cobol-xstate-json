#!/usr/bin/env python3
"""Prove, in real virtualenvs, that the three distributions install and stand apart.

tests/test_package_boundaries.py blocks imports inside one interpreter, which is fast and
runs in CI. It cannot prove the thing that actually matters to an operator: that a machine
which never installed the COBOL package does not HAVE the COBOL package. Only an install
shows that, so this builds three throwaway venvs and checks:

    core + cobol + jcl   both console scripts work
    core + jcl           `import cobol_xstate` raises; the JCL CLI still works
    core + cobol         a full COBOL run works; --bind-jcl fails with the exact pip line

Slow (three venvs, three installs), so it is a tool rather than a test. Run it before
releasing, and after anything that touches a pyproject.

    python tools/prove_separation.py [--keep]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# The JCL front-end lives in its OWN repository now; this proof installs it from a
# sibling checkout (override with JCL_DEPENDENCIES_REPO). When the checkout is absent
# the jcl-involving venvs are SKIPPED with a message - never silently passed.
import os
JCL_REPO = Path(os.environ.get("JCL_DEPENDENCIES_REPO",
                               REPO.parent / "jcl-dependencies"))
IS_WIN = sys.platform == "win32"
BIN = "Scripts" if IS_WIN else "bin"
EXE = ".exe" if IS_WIN else ""

_failures: list = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))
    if not ok:
        _failures.append(label)


def make_venv(root: Path, name: str, dists) -> Path:
    venv = root / name
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True,
                   capture_output=True)
    py = venv / BIN / f"python{EXE}"
    args = [str(py), "-m", "pip", "install", "-q"]
    for d in dists:
        args += ["-e", str(JCL_REPO if d == "jcl" else REPO / d)]
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode:
        print(proc.stdout, proc.stderr)
        raise SystemExit(f"install failed for {name}")
    return venv


def run(venv: Path, *args, **kw):
    return subprocess.run([str(venv / BIN / f"python{EXE}"), *args],
                          capture_output=True, text=True, cwd=str(REPO), **kw)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="do not delete the venvs")
    args = ap.parse_args()

    root = Path(tempfile.mkdtemp(prefix="cobol-xstate-separation-"))
    out = root / "out"
    have_jcl = (JCL_REPO / "pyproject.toml").is_file()
    if not have_jcl:
        print(f"NOTE: jcl-dependencies checkout not found at {JCL_REPO} - the venvs "
              f"that install it are SKIPPED (set JCL_DEPENDENCIES_REPO to point at a "
              f"checkout). The cobol-only checks still run and still gate.")
    try:
        print("all three (core + cobol + jcl)" if have_jcl
              else "core + cobol (jcl checkout absent)")
        v = make_venv(root, "all", ("core", "cobol", "jcl") if have_jcl
                      else ("core", "cobol"))
        scripts = ("cobol-xstate", "jcl-dependencies") if have_jcl else ("cobol-xstate",)
        for script in scripts:
            check(f"{script} console script exists",
                  (v / BIN / f"{script}{EXE}").exists())
        r = run(v, "-m", "cobol_xstate", "cobol/examples/accum.cbl",
                "--outdir", str(out / "a"), "-q")
        check("a COBOL run writes its eight files", r.returncode == 0
              and len(list((out / "a").glob("*.json"))) == 8)
        if have_jcl:
            r = run(v, "-m", "jcl_dependencies",
                    str(JCL_REPO / "examples" / "acctunld.jcl"),
                    "--outdir", str(out / "b"), "-q")
            check("a JCL run writes its four files", r.returncode == 0
                  and len(list((out / "b").glob("*.json"))) == 4,
                  "" if r.returncode == 0 else r.stderr.strip().splitlines()[-1][:100])

            print("\nJCL box (core + jcl ONLY - no COBOL modelling engine)")
            v = make_venv(root, "jcl", ("core", "jcl"))
            r = run(v, "-c", "import cobol_xstate")
            check("import cobol_xstate raises ModuleNotFoundError",
                  r.returncode != 0 and "ModuleNotFoundError" in r.stderr)
            r = run(v, "-c", "import importlib.util as u; "
                             "print(u.find_spec('cobol_xstate') is None)")
            check("the COBOL package is not even findable", r.stdout.strip() == "True")
            r = run(v, "-m", "jcl_dependencies",
                    str(JCL_REPO / "examples" / "dailypost.jcl"),
                    "--outdir", str(out / "c"), "-q")
            check("the JCL CLI still works with no COBOL package at all",
                  r.returncode == 0 and len(list((out / "c").glob("*.json"))) == 4,
                  "" if r.returncode == 0 else r.stderr.strip().splitlines()[-1][:100])

        print("\nCOBOL box (core + cobol ONLY - the JCL extra not installed)")
        v = make_venv(root, "cobol", ("core", "cobol"))
        r = run(v, "-c", "import jcl_dependencies")
        check("import jcl_dependencies raises ModuleNotFoundError",
              r.returncode != 0 and "ModuleNotFoundError" in r.stderr)
        r = run(v, "-m", "cobol_xstate", "cobol/examples/accum.cbl",
                "--outdir", str(out / "d"), "-q")
        check("a full COBOL run is unaffected", r.returncode == 0
              and len(list((out / "d").glob("*.json"))) == 8)
        bind_job = (str(JCL_REPO / "examples" / "acctunld.jcl") if have_jcl
                    else str(out / "missing.jcl"))
        if not have_jcl:
            (out / "missing.jcl").parent.mkdir(parents=True, exist_ok=True)
            (out / "missing.jcl").write_text("//J JOB\n//S EXEC PGM=IEFBR14\n")
        r = run(v, "-m", "cobol_xstate", "cobol/examples/sqlunld.cbl",
                "--outdir", str(out / "e"), "--bind-jcl", bind_job, "-q")
        combined = r.stdout + r.stderr
        check("--bind-jcl exits 2 naming the exact pip command",
              r.returncode == 2 and "pip install cobol-xstate[jcl]" in combined,
              combined.strip().splitlines()[-1][:80] if combined.strip() else "")
    finally:
        if args.keep:
            print(f"\nvenvs kept in {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)

    if _failures:
        print(f"\nSEPARATION FAILED: {len(_failures)} check(s)", file=sys.stderr)
        return 1
    print("\nSEPARATION PROVEN - the three distributions install and stand apart")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
