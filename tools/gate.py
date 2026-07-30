#!/usr/bin/env python3
"""GATE B in one command: prove this working tree emits the same bytes as the goldens.

The module split (see the plan) moves large amounts of code between packages. Almost
every step of it is supposed to change NOTHING about the output, so the only way to know
a step was clean is to hash every view and every retrieval report and compare. Running
that by hand, in four variants, after each of a dozen substeps, is how it stops getting
run - so it is one command.

Four variants, because each pins a different way the output could stop being stable:

    views   @ PYTHONHASHSEED=0        the baseline
    views   @ PYTHONHASHSEED=1234     no set/dict iteration order leaked into output
    reports @ --jobs 1                the sequential retrieval path (no threads at all)
    reports @ --jobs 8                the concurrent one - row order must still follow
                                      the PLAN, not the order answers arrived

Exit code is 0 only if all four agree with the goldens.

    python tools/gate.py                 # check
    python tools/gate.py --record        # re-record all goldens (only when a change
                                         # to the output is INTENDED and reviewed)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GOLDENS = REPO / "goldens"
VIEWS = GOLDENS / "views.sha256"
REPORTS = GOLDENS / "reports.sha256"

CHECKS = [
    ("views   @ PYTHONHASHSEED=0",
     [sys.executable, "tools/byteproof.py", "--check", str(VIEWS)], {"PYTHONHASHSEED": "0"}),
    ("views   @ PYTHONHASHSEED=1234",
     [sys.executable, "tools/byteproof.py", "--check", str(VIEWS)], {"PYTHONHASHSEED": "1234"}),
    ("reports @ --jobs 1",
     [sys.executable, "tools/byteproof_reports.py", "--check", str(REPORTS), "--jobs", "1"], {}),
    ("reports @ --jobs 8",
     [sys.executable, "tools/byteproof_reports.py", "--check", str(REPORTS), "--jobs", "8"], {}),
]

RECORDS = [
    ("views", [sys.executable, "tools/byteproof.py", "--record", str(VIEWS)]),
    ("reports", [sys.executable, "tools/byteproof_reports.py", "--record", str(REPORTS)]),
]


def run(label, cmd, extra_env) -> bool:
    env = dict(os.environ)
    env.update(extra_env)
    proc = subprocess.run(cmd, cwd=REPO, env=env, capture_output=True, text=True)
    ok = proc.returncode == 0
    print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="Gate B: byte-stability across views and "
                                             "retrieval reports")
    ap.add_argument("--record", action="store_true",
                    help="re-record the goldens instead of checking them. Only when an "
                         "output change is intended AND has been reviewed - re-recording "
                         "to make a red gate green destroys the guarantee.")
    args = ap.parse_args()

    if args.record:
        for label, cmd in RECORDS:
            if not run(f"record {label}", cmd, {}):
                return 1
        print("\ngoldens re-recorded - review the diff before committing")
        return 0

    results = [run(label, cmd, env) for label, cmd, env in CHECKS]
    if all(results):
        print("\nGATE B PASS - output is byte-identical to the goldens")
        return 0
    print(f"\nGATE B FAIL - {results.count(False)} of {len(results)} check(s) differ",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
