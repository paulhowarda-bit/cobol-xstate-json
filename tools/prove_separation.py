#!/usr/bin/env python3
"""Prove, in real virtualenvs, that the distributions install and stand apart.

tests/test_package_boundaries.py blocks imports inside one interpreter, which is fast and
runs in CI. It cannot prove the thing that actually matters to an operator: that a machine
which never installed the COBOL package does not HAVE the COBOL package. Only an install
shows that, so this builds throwaway venvs and checks:

    artifacts + parser + cobol + jcl   both console scripts work
    artifacts + jcl                    `import cobol_xstate` raises (and cobol_parser is not
                                  even findable); the JCL CLI still works
    artifacts + parser                 parse_program works with NO modelling engine installed
    artifacts + parser + cobol         a full COBOL run works; --bind-jcl fails with the
                                  exact pip line

Slow (four venvs, four installs), so it is a tool rather than a test. Run it before
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
# The mainframe-artifacts and cobol-parser distributions live in the mainframe-common repository now; this
# proof installs them from a sibling checkout (override with MAINFRAME_COMMON_REPO).
# Unlike the JCL checkout below, that one is NOT optional: every venv here needs mainframe-artifacts,
# so with no checkout the proof refuses to run rather than proving nothing quietly.
import os
COMMON_REPO = Path(os.environ.get("MAINFRAME_COMMON_REPO",
                                  REPO.parent / "mainframe-common"))
# The JCL front-end lives in its OWN repository; this proof installs it from a
# sibling checkout (override with JCL_DEPENDENCIES_REPO). When the checkout is absent
# the jcl-involving venvs are SKIPPED with a message - never silently passed.
JCL_REPO = Path(os.environ.get("JCL_DEPENDENCIES_REPO",
                               REPO.parent / "jcl-dependencies"))
# Which checkout each installable name comes from.
DISTS = {"core": COMMON_REPO / "mainframe-artifacts", "parser": COMMON_REPO / "cobol-parser",
         "cobol": REPO, "jcl": JCL_REPO}
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
        args += ["-e", str(DISTS[d])]
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

    if not (COMMON_REPO / "mainframe-artifacts" / "pyproject.toml").is_file():
        print(f"mainframe-common checkout not found at {COMMON_REPO} - every venv "
              f"here installs mainframe-artifacts (and most install cobol-parser) "
              f"from it, so there is nothing this proof can prove without one. Clone "
              f"it beside this repo "
              f"or set MAINFRAME_COMMON_REPO to point at a checkout.",
              file=sys.stderr)
        return 2

    root = Path(tempfile.mkdtemp(prefix="cobol-xstate-separation-"))
    out = root / "out"
    have_jcl = (JCL_REPO / "pyproject.toml").is_file()
    if not have_jcl:
        print(f"NOTE: jcl-dependencies checkout not found at {JCL_REPO} - the venvs "
              f"that install it are SKIPPED (set JCL_DEPENDENCIES_REPO to point at a "
              f"checkout). The cobol-only checks still run and still gate.")
    try:
        print("everything (artifacts + parser + cobol + jcl)" if have_jcl
              else "artifacts + parser + cobol (jcl checkout absent)")
        v = make_venv(root, "all", ("core", "parser", "cobol", "jcl") if have_jcl
                      else ("core", "parser", "cobol"))
        scripts = (("cobol-xstate", "cobol-parser", "jcl-dependencies") if have_jcl
                   else ("cobol-xstate", "cobol-parser"))
        for script in scripts:
            check(f"{script} console script exists",
                  (v / BIN / f"{script}{EXE}").exists())
        r = run(v, "-m", "cobol_xstate", "examples/accum.cbl",
                "--outdir", str(out / "a"), "-q")
        check("a COBOL run writes its eight files", r.returncode == 0
              and len(list((out / "a").glob("*.json"))) == 8)
        # The two-step run: parse upfront, then model from the parse bundle.
        r = run(v, "-m", "cobol_parser", "examples/accum.cbl",
                "-o", str(out / "accum.parse.json"), "-q")
        check("cobol-parser writes a parse bundle", r.returncode == 0
              and (out / "accum.parse.json").is_file())
        r = run(v, "-m", "cobol_xstate", "examples/accum.cbl",
                "--from-parse", str(out / "accum.parse.json"),
                "--outdir", str(out / "a2"), "-q")
        ok = r.returncode == 0 and len(list((out / "a2").glob("*.json"))) == 8
        same = ok and all((out / "a" / p.name).read_bytes() == p.read_bytes()
                          for p in (out / "a2").glob("*.json"))
        check("--from-parse writes the same eight files byte-for-byte", same)
        if have_jcl:
            r = run(v, "-m", "jcl_dependencies",
                    str(JCL_REPO / "examples" / "acctunld.jcl"),
                    "--outdir", str(out / "b"), "-q")
            check("a JCL run writes its four files", r.returncode == 0
                  and len(list((out / "b").glob("*.json"))) == 4,
                  "" if r.returncode == 0 else r.stderr.strip().splitlines()[-1][:100])

            print("\nJCL box (artifacts + jcl ONLY - no COBOL modelling engine)")
            v = make_venv(root, "jcl", ("core", "jcl"))
            r = run(v, "-c", "import cobol_xstate")
            check("import cobol_xstate raises ModuleNotFoundError",
                  r.returncode != 0 and "ModuleNotFoundError" in r.stderr)
            r = run(v, "-c", "import importlib.util as u; "
                             "print(u.find_spec('cobol_xstate') is None)")
            check("the COBOL package is not even findable", r.stdout.strip() == "True")
            r = run(v, "-c", "import importlib.util as u; "
                             "print(u.find_spec('cobol_parser') is None)")
            check("the COBOL parse front-end is not even findable",
                  r.stdout.strip() == "True")
            r = run(v, "-m", "jcl_dependencies",
                    str(JCL_REPO / "examples" / "dailypost.jcl"),
                    "--outdir", str(out / "c"), "-q")
            check("the JCL CLI still works with no COBOL package at all",
                  r.returncode == 0 and len(list((out / "c").glob("*.json"))) == 4,
                  "" if r.returncode == 0 else r.stderr.strip().splitlines()[-1][:100])

        print("\nparse box (artifacts + parser ONLY - no modelling engine at all)")
        v = make_venv(root, "parse", ("core", "parser"))
        r = run(v, "-c", "import cobol_xstate")
        check("import cobol_xstate raises ModuleNotFoundError",
              r.returncode != 0 and "ModuleNotFoundError" in r.stderr)
        r = run(v, "-c",
                "from pathlib import Path\n"
                "from cobol_parser import parse_program\n"
                "src = Path('examples/accum.cbl').read_text(encoding='utf-8')\n"
                "prog = parse_program(src)\n"
                "print('PARAS', len(prog.paragraphs), 'ID', prog.program_id)")
        check("parse_program recovers a real example with no modelling engine",
              r.returncode == 0 and "ID" in r.stdout and "PARAS 0" not in r.stdout,
              "" if r.returncode == 0 else r.stderr.strip().splitlines()[-1][:100])
        r = run(v, "-m", "cobol_parser", "examples/accum.cbl",
                "-o", str(out / "parse-box.parse.json"), "-q")
        check("the cobol-parser CLI works with no modelling engine",
              r.returncode == 0 and (out / "parse-box.parse.json").is_file(),
              "" if r.returncode == 0 else r.stderr.strip().splitlines()[-1][:100])

        print("\nCOBOL box (artifacts + parser + cobol - the JCL extra not installed)")
        v = make_venv(root, "cobol", ("core", "parser", "cobol"))
        r = run(v, "-c", "import jcl_dependencies")
        check("import jcl_dependencies raises ModuleNotFoundError",
              r.returncode != 0 and "ModuleNotFoundError" in r.stderr)
        r = run(v, "-m", "cobol_xstate", "examples/accum.cbl",
                "--outdir", str(out / "d"), "-q")
        check("a full COBOL run is unaffected", r.returncode == 0
              and len(list((out / "d").glob("*.json"))) == 8)
        bind_job = (str(JCL_REPO / "examples" / "acctunld.jcl") if have_jcl
                    else str(out / "missing.jcl"))
        if not have_jcl:
            (out / "missing.jcl").parent.mkdir(parents=True, exist_ok=True)
            (out / "missing.jcl").write_text("//J JOB\n//S EXEC PGM=IEFBR14\n")
        r = run(v, "-m", "cobol_xstate", "examples/sqlunld.cbl",
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
    print("\nSEPARATION PROVEN - the distributions install and stand apart")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
