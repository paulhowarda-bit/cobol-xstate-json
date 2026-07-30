#!/usr/bin/env python3
"""Byte-stability ratchet for the two RETRIEVAL REPORTS (.prefetch.json / .fetch.json).

tools/byteproof.py covers the views, which are estate-free. These two files are not:
their contents depend on what an artifact service answered. So this driver runs both
stages against the recorded fake client in tests/fakes/estate.py, which answers from a
fixed table and covers every outcome the reports distinguish - local, fetched,
not-found, error, a probe chain, alternatives, and a detected-type disagreement.

This is the half of the ratchet that guards the riskiest part of the module split:
prefetch.py is being cut three ways (engine to core, the COBOL closure and the JCL
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
JCL_EXAMPLES = EXAMPLES / "jcl"

sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))

from fakes.estate import fetch_artifact                              # noqa: E402

from cobol_xstate_core.fetch import fetch_dependencies               # noqa: E402

from cobol_xstate.artifacts import build_artifacts                   # noqa: E402
from cobol_xstate.dynamic_calls import (annotate_artifacts,          # noqa: E402
                                        build_dynamic_calls)
from cobol_xstate_jcl.parser import parse_jcl                               # noqa: E402
from cobol_xstate_jcl.views import build_jcl_artifacts               # noqa: E402
from cobol_xstate.normalizer import detect_source_format             # noqa: E402
from cobol_xstate.parser import parse_program                        # noqa: E402
from cobol_xstate_jcl.prefetch import prefetch_jcl                   # noqa: E402
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
    """Replace this run's directory with a stable token, in both path spellings."""
    for form in (str(run_dir), str(run_dir).replace("\\", "/"),
                 str(run_dir).replace("\\", "\\\\")):
        text = text.replace(form, "<RUNDIR>")
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
    machine = build_machine(program, source_name=path.name)

    art = build_artifacts(machine)
    art = attribute_resolution(art, program, pre.store)
    dyn = build_dynamic_calls(machine, art)
    art = annotate_artifacts(art, dyn)

    report = fetch_dependencies(art, fetch_artifact, dest=deps, prefetched=pre.store,
                                dynamic=dyn, jobs=jobs)
    return {"prefetch": json_text(pre.report()), "fetch": json_text(report)}


def jcl_reports(path: Path, run_dir: Path, jobs: int = JOBS) -> Dict[str, str]:
    source = path.read_text(encoding="utf-8", errors="replace")
    deps = str(run_dir / "deps")
    pre = prefetch_jcl(source, fetch_artifact, paths=[str(JCL_EXAMPLES)], dest=deps,
                       source_name=path.name, jobs=jobs)
    job = parse_jcl(source, resolver=pre.resolver(), source_name=path.name)
    art = build_jcl_artifacts(job)
    report = fetch_dependencies(art, fetch_artifact, dest=deps, prefetched=pre.store,
                                jobs=jobs)
    return {"jcl.prefetch": json_text(pre.report()), "jcl.fetch": json_text(report)}


def build_manifest(jobs: int = JOBS) -> Dict[str, str]:
    out: Dict[str, str] = {}
    tmp = Path(tempfile.mkdtemp(prefix="byteproof-reports-"))
    try:
        for src in sorted(EXAMPLES.glob("*.cbl")):
            run_dir = tmp / src.stem
            for view, text in cobol_reports(src, run_dir, jobs).items():
                out[f"{src.name}::{view}"] = digest(normalize(text, run_dir))
        if JCL_EXAMPLES.is_dir():
            for src in sorted(JCL_EXAMPLES.iterdir()):
                if src.suffix.lower() not in (".jcl", ".prc", ".proc"):
                    continue
                run_dir = tmp / f"jcl-{src.stem}"
                for view, text in jcl_reports(src, run_dir, jobs).items():
                    out[f"jcl/{src.name}::{view}"] = digest(normalize(text, run_dir))
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
