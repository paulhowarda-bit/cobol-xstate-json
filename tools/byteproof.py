#!/usr/bin/env python3
"""Byte-stability ratchet: hash every view of every example, and refuse to let a
refactor change one byte of it.

CLAUDE.md states the invariant this enforces:

    Output is byte-stable and deterministic. Views are compared byte-for-byte across
    all examples/*.cbl and across PYTHONHASHSEED. A refactor that should not change
    output must produce an identical bundle - verify by diffing every view over every
    example (build a Machine per example, serialize each view, hash), not just by a
    green test run.

Until now that was a procedure a human was trusted to run. This is the automated form,
built BEFORE the module split so every step of the split can be checked against it.

What is hashed is the EXACT TEXT the CLI would write to disk - `json.dumps(obj,
indent=2) + "\\n"` for the JSON views, the module source for the JS ones - not a
normalized or re-parsed form. A view that reorders its keys, changes its indent, or
gains a trailing newline is a changed view, and this must say so.

A refusal is a result, not an absence: `--target reactive` legitimately refuses some
programs (CICS handler regions, recursive PERFORM). The refusal REASON is hashed, so a
lowering that starts refusing a program it used to accept - or changes its explanation -
is caught exactly like a changed byte.

Deliberately estate-free: every example resolves its copybooks from examples/ alone,
with no fetcher. That keeps the ratchet runnable on a laptop with no mainframe
connection, and keeps the hashes from depending on what an estate happened to answer.
The retrieval REPORTS (.prefetch.json / .fetch.json), which do depend on that, are
covered separately by tools/byteproof_reports.py against a recorded fake client.

Usage:
    python tools/byteproof.py --record goldens/views.sha256
    python tools/byteproof.py --check  goldens/views.sha256
    python tools/byteproof.py --check  goldens/views.sha256 --verbose
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import traceback
from pathlib import Path
from typing import Callable, Dict, List, Tuple

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "cobol" / "examples"
JCL_EXAMPLES = REPO / "jcl" / "examples"

for _tree in ("core/src", "cobol/src", "jcl/src"):
    sys.path.insert(0, str(REPO / _tree))

from cobol_xstate.artifacts import build_artifacts                    # noqa: E402
from cobol_xstate.business import build_business_view                 # noqa: E402
from cobol_xstate.dynamic_calls import (annotate_artifacts,           # noqa: E402
                                        build_dynamic_calls)
from cobol_xstate.emitter import emit_setup_module                    # noqa: E402
from jcl_dependencies.parser import parse_jcl                       # noqa: E402
from jcl_dependencies.views import (build_jcl_artifacts,              # noqa: E402
                                    build_jcl_lineage)
from cobol_xstate.lineage import build_lineage                        # noqa: E402
from cobol_xstate.normalizer import detect_source_format              # noqa: E402
from cobol_xstate.parser import parse_program                         # noqa: E402
from cobol_xstate.prefetch import attribute_resolution                # noqa: E402
from cobol_xstate.preprocessor import CopybookResolver                # noqa: E402
from cobol_xstate.reactive import (build_reactive_view,               # noqa: E402
                                   emit_reactive_module)
from cobol_xstate.statechart import build_machine                     # noqa: E402

INDENT = 2  # the CLI default; the hashes are of what a default run would write


# --------------------------------------------------------------------------- digests

def normalize(text: str) -> str:
    """Replace this checkout's search roots with stable tokens before hashing.

    One view genuinely embeds a local filesystem path, and it is not this tool's doing:
    a copybook row in the artifact manifest carries the ``source`` it resolved from, so
    ``<program>::artifacts`` is machine-dependent for any program that COPYs a member
    found on disk. That was already true before these goldens existed - the path simply
    never moved, so it never showed. Normalizing the search roots is what makes the
    goldens portable across checkouts, and what let the three-distribution restructure
    be VERIFIED as path-only rather than assumed to be.

    Most specific root first: the examples directories sit under the repo root, so
    replacing the repo root first would leave their tails unnormalized.
    """
    for root, token in ((EXAMPLES, "<EXAMPLES>"), (JCL_EXAMPLES, "<JCL_EXAMPLES>"),
                        (REPO, "<REPO>")):
        for form in (str(root), str(root).replace("\\", "/"),
                     str(root).replace("\\", "\\\\")):
            text = text.replace(form, token)
    return text


def digest(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def json_text(obj) -> str:
    """Exactly what cli._companion writes: dumps(indent) + a trailing newline."""
    return json.dumps(obj, indent=INDENT) + "\n"


def guarded(fn: Callable[[], str]) -> str:
    """Run a view, turning a principled refusal into a hashable result.

    NotImplementedError (which ReactiveLoweringError also is) means "this lowering
    refuses this program" - a fact about the program, and part of the output contract.
    Any OTHER exception is a genuine breakage: record it as such so --check fails
    loudly rather than the view silently vanishing from the manifest.
    """
    try:
        return fn()
    except NotImplementedError as exc:
        return f"REFUSED: {exc}\n"
    except Exception:                                    # noqa: BLE001 - deliberate
        return "ERROR:\n" + traceback.format_exc(limit=0)


# ----------------------------------------------------------------------- the views

def cobol_views(path: Path) -> Dict[str, str]:
    """Every artifact a default run would write for one COBOL source, as text.

    Mirrors cli._run's sequencing exactly, including the order in which the artifact
    manifest is built (build -> attribute -> annotate). attribute_resolution with an
    empty store is a no-op, but it is called anyway so this stays the same call
    sequence the CLI makes - if that ever stops being a no-op, the ratchet sees it.
    """
    source = path.read_text(encoding="utf-8", errors="replace")
    fmt = detect_source_format(source).format
    resolver = CopybookResolver(paths=[str(EXAMPLES)], fetcher=None, store={})
    program = parse_program(source, fmt, resolver=resolver)
    machine = build_machine(program, source_name=path.name)

    art = build_artifacts(machine)
    art = attribute_resolution(art, program, {})
    dyn = build_dynamic_calls(machine, art)
    art = annotate_artifacts(art, dyn)

    return {
        "bundle": guarded(lambda: machine.to_json(machine_only=False,
                                                  indent=INDENT) + "\n"),
        "bundle.machine-only": guarded(lambda: machine.to_json(machine_only=True,
                                                               indent=INDENT) + "\n"),
        "business": guarded(lambda: json_text(build_business_view(machine))),
        "lineage": guarded(lambda: json_text(build_lineage(machine))),
        "artifacts": guarded(lambda: json_text(art)),
        "dynamic-calls": guarded(lambda: json_text(dyn)),
        "reactive": guarded(lambda: json_text(build_reactive_view(machine))),
        "js": guarded(lambda: emit_setup_module(machine)),
        "reactive.mjs": guarded(lambda: emit_reactive_module(machine)),
    }


def jcl_views(path: Path) -> Dict[str, str]:
    """The JCL views, parsed with NO resolver - so an unresolved PROC stays unresolved
    and is hashed as such. Supplying one would make the hashes depend on the estate."""
    source = path.read_text(encoding="utf-8", errors="replace")
    job = parse_jcl(source, resolver=None, source_name=path.name)
    return {
        "jcl.lineage": guarded(lambda: json_text(build_jcl_lineage(job))),
        "jcl.artifacts": guarded(lambda: json_text(build_jcl_artifacts(job))),
    }


def build_manifest() -> Dict[str, str]:
    """key -> sha256, over every example. Sorted so the file is diff-friendly and the
    ordering cannot depend on the filesystem."""
    out: Dict[str, str] = {}
    for src in sorted(EXAMPLES.glob("*.cbl")):
        for view, text in cobol_views(src).items():
            out[f"{src.name}::{view}"] = digest(text)
    for src in sorted(JCL_EXAMPLES.iterdir()) if JCL_EXAMPLES.is_dir() else []:
        if src.suffix.lower() not in (".jcl", ".prc", ".proc"):
            continue
        for view, text in jcl_views(src).items():
            out[f"jcl/{src.name}::{view}"] = digest(text)
    return dict(sorted(out.items()))


# ------------------------------------------------------------------------ file I/O

def dump(path: Path, manifest: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{sha}  {key}" for key, sha in manifest.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        sha, _, key = line.partition("  ")
        out[key] = sha
    return out


def compare(before: Dict[str, str], after: Dict[str, str]) -> List[str]:
    """Every difference, named. Missing and added keys are differences too - a view
    that stops being produced is exactly the kind of silent loss this exists to catch."""
    problems = []
    for key in sorted(set(before) - set(after)):
        problems.append(f"MISSING  {key} (was in the goldens, not produced now)")
    for key in sorted(set(after) - set(before)):
        problems.append(f"ADDED    {key} (produced now, not in the goldens)")
    for key in sorted(set(before) & set(after)):
        if before[key] != after[key]:
            problems.append(f"CHANGED  {key}\n"
                            f"           golden {before[key]}\n"
                            f"           now    {after[key]}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="byte-stability ratchet over every view "
                                             "of every example")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--record", metavar="FILE", help="write the goldens")
    g.add_argument("--check", metavar="FILE", help="compare against the goldens")
    ap.add_argument("--verbose", action="store_true",
                    help="list every key checked, not just the differences")
    args = ap.parse_args()

    manifest = build_manifest()

    if args.record:
        dump(Path(args.record), manifest)
        print(f"recorded {len(manifest)} view digests -> {args.record}")
        return 0

    path = Path(args.check)
    if not path.exists():
        print(f"error: no goldens at {path} - run --record first", file=sys.stderr)
        return 2
    problems = compare(load(path), manifest)
    if args.verbose:
        for key in manifest:
            print(f"  {key}")
    if problems:
        print(f"BYTE-STABILITY FAILURE: {len(problems)} difference(s)\n",
              file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1
    print(f"byte-stable: {len(manifest)} view digests match {args.check}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
