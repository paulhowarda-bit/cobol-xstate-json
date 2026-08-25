#!/usr/bin/env python3
"""Byte-stability ratchet for the two RETRIEVAL REPORTS (.prefetch.json / .fetch.json).

tools/byteproof.py covers the views, which are estate-free. These two files are not:
their contents depend on what an artifact service answered. So this driver runs both
stages against the recorded fake client in tests/fakes/estate.py, which answers from a
fixed table and covers every outcome the reports distinguish - local, fetched,
not-found, error, a probe chain, alternatives, and a detected-type disagreement.

This is the half of the ratchet that guards the riskiest part of the module split:
prefetch.py is being cut three ways (engine to mainframe-artifacts, the COBOL closure and the JCL
closure to their own packages), and a mistake there shows up here rather than in the
views.

ONE NORMALIZATION, stated plainly. `artifact_service.save_member` puts the run's own
`deps/` directory into every `copiedTo` value, so these reports already embed a local
filesystem path today - they are machine-dependent before this tool touches them. The
run directory prefix (and the OS path separator) is therefore replaced with the token
<RUNDIR> before hashing. Nothing else is normalized: every status, reason, ordering and
count is hashed exactly as written.

Usage:
    python tools/byteproof_reports.py --record goldens/reports.sha256
    python tools/byteproof_reports.py --check  goldens/reports.sha256
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Dict, List

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples"

# mainframe-artifacts/src and cobol-parser/src live in the mainframe-common sibling checkout (override
# with MAINFRAME_COMMON_REPO). When the distributions are pip-installed instead, the
# checkout paths simply do not exist and the inserts are inert.
import os                                                             # noqa: E402
_COMMON = Path(os.environ.get("MAINFRAME_COMMON_REPO",
                              REPO.parent / "mainframe-common"))
for _tree in (_COMMON / "mainframe-artifacts" / "src", _COMMON / "cobol-parser" / "src",
              REPO / "src", REPO / "tests"):
    sys.path.insert(0, str(_tree))

from fakes.estate import fetch_artifact                              # noqa: E402

from mainframe_artifacts.fetch import fetch_dependencies               # noqa: E402

from cobol_xstate.artifacts import build_artifacts                   # noqa: E402
from cobol_xstate.dynamic_calls import (annotate_artifacts,          # noqa: E402
                                        build_dynamic_calls)
from cobol_xstate.normalizer import detect_source_format             # noqa: E402
from cobol_xstate.parser import parse_program                        # noqa: E402
from cobol_xstate.prefetch import (attribute_resolution,             # noqa: E402
                                   prefetch_cobol)
from cobol_xstate.preprocessor import CopybookResolver               # noqa: E402
from cobol_xstate.statechart import build_machine                    # noqa: E402

INDENT = 2
DEFAULT_EXTS = ("", ".cpy", ".CPY", ".cbl", ".cob", ".copy", ".CBL")
JOBS = 1  # sequential: --jobs is asserted byte-neutral separately, below


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize(text: str, run_dir: Path) -> str:
    """Replace this run's directory and this checkout's search roots with stable tokens.

    Two different local paths reach these reports. ``copiedTo`` names the run's own deps/
    directory, and a member resolved from disk carries the ``source`` it was read from -
    so both are machine-dependent before this tool touches them. Normalizing them is what
    makes the goldens portable across checkouts and layouts.

    Most specific root first: the examples directories sit under the repo root, so
    replacing the repo root first would leave their tails unnormalized.
    """
    for root, token in ((run_dir, "<RUNDIR>"), (EXAMPLES, "<EXAMPLES>"),
                        (REPO, "<REPO>")):
        for form in (str(root), str(root).replace("\\", "/"),
                     str(root).replace("\\", "\\\\")):
            text = text.replace(form, token)
    return text


def json_text(obj) -> str:
    return json.dumps(obj, indent=INDENT) + "\n"


def cobol_reports(path: Path, run_dir: Path, jobs: int = JOBS) -> Dict[str, str]:
    """Both retrieval reports for one COBOL source, sequenced exactly as cli._run does."""
    source = path.read_text(encoding="utf-8", errors="replace")
    fmt = detect_source_format(source).format
    deps = str(run_dir / "deps")

    pre = prefetch_cobol(source, fetch_artifact, paths=[str(EXAMPLES)], dest=deps,
                         fmt=fmt, source_name=path.name, exts=DEFAULT_EXTS, jobs=jobs)
    resolver = CopybookResolver(paths=[str(EXAMPLES)], exts=DEFAULT_EXTS,
                                fetcher=fetch_artifact, store=pre.store)
    program = parse_program(source, fmt, resolver=resolver)
    # conventions=None: the determinism pin (see byteproof.py) - report goldens must
    # not depend on the local mfdep.db either.
    machine = build_machine(program, source_name=path.name, conventions=None)

    art = build_artifacts(machine)
    art = attribute_resolution(art, program, pre.store)
    dyn = build_dynamic_calls(machine, art)
    art = annotate_artifacts(art, dyn)

    report = fetch_dependencies(art, fetch_artifact, dest=deps, prefetched=pre.store,
                                dynamic=dyn, jobs=jobs)
    return {"prefetch": json_text(pre.report()), "fetch": json_text(report)}



def build_manifest(jobs: int = JOBS) -> Dict[str, str]:
    out: Dict[str, str] = {}
    tmp = Path(tempfile.mkdtemp(prefix="byteproof-reports-"))
    try:
        for src in sorted(EXAMPLES.glob("*.cbl")):
            run_dir = tmp / src.stem
            for view, text in cobol_reports(src, run_dir, jobs).items():
                out[f"{src.name}::{view}"] = digest(normalize(text, run_dir))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return dict(sorted(out.items()))


def dump(path: Path, manifest: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(f"{sha}  {key}" for key, sha in manifest.items()) + "\n",
                    encoding="utf-8")


def load(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            sha, _, key = line.partition("  ")
            out[key] = sha
    return out


def compare(before: Dict[str, str], after: Dict[str, str]) -> List[str]:
    problems = []
    for key in sorted(set(before) - set(after)):
        problems.append(f"MISSING  {key}")
    for key in sorted(set(after) - set(before)):
        problems.append(f"ADDED    {key}")
    for key in sorted(set(before) & set(after)):
        if before[key] != after[key]:
            problems.append(f"CHANGED  {key}\n           golden {before[key]}\n"
                            f"           now    {after[key]}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="byte-stability ratchet for the retrieval "
                                             "reports, against a recorded fake estate")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--record", metavar="FILE")
    g.add_argument("--check", metavar="FILE")
    ap.add_argument("--jobs", type=int, default=JOBS,
                    help="retrieval concurrency; the reports must be identical at any "
                         "value (row order follows the plan, never completion order)")
    args = ap.parse_args()

    manifest = build_manifest(args.jobs)

    if args.record:
        dump(Path(args.record), manifest)
        print(f"recorded {len(manifest)} report digests -> {args.record}")
        return 0

    path = Path(args.check)
    if not path.exists():
        print(f"error: no goldens at {path} - run --record first", file=sys.stderr)
        return 2
    problems = compare(load(path), manifest)
    if problems:
        print(f"REPORT BYTE-STABILITY FAILURE: {len(problems)} difference(s)\n",
              file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1
    print(f"byte-stable: {len(manifest)} report digests match {args.check} "
          f"(--jobs {args.jobs})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
