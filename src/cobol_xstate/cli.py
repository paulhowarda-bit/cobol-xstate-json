"""Command-line entry point: COBOL file -> XState v5 JSON statechart."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import List, Optional

from .artifacts import build_artifacts
from .business import build_business_view
from .emitter import emit_setup_module
from .jcl import parse_jcl
from .jcl_views import build_jcl_artifacts, build_jcl_lineage
from .lineage import build_lineage
from .normalizer import SourceFormat, detect_source_format
from .reactive import build_reactive_view, emit_reactive_module
from .parser import parse_program
from .preprocessor import CopybookResolver
from .runtime_assets import read_runtime_asset
from .statechart import build_machine


def _format(name: Optional[str]) -> Optional[SourceFormat]:
    if name is None:
        return None
    return {"fixed": SourceFormat.FIXED, "free": SourceFormat.FREE}[name]


# Suffix per target. Companions are built from the same base, so every artifact of one
# run has a distinct name and none can land on another's path.
_TARGET_EXT = {"js": ".mjs", "reactive": ".reactive.mjs",
               "lineage": ".lineage.json", "business": ".business.json",
               "artifacts": ".artifacts.json"}
_COMPANION_EXT = (".business.json", ".lineage.json", ".artifacts.json")


def _artifact_base(args, default_stem: Optional[str], program_id: str) -> str:
    """The shared base name every artifact of this run is built from.

    Derived from the SOURCE stem (or an explicit ``-o`` path), never by chopping a
    written filename at its first dot - a source called ``MY.PROG.cbl`` would otherwise
    yield companions named ``MY.*``, and one called ``X.business.cbl`` would have its
    bundle silently overwritten by the business view landing on the same path.
    """
    if args.output and args.output != "-":
        name = Path(args.output).name
        for suf in _COMPANION_EXT:          # -o out/prog.business.json -> base "prog"
            if name.endswith(suf):
                return name[: -len(suf)]
        return Path(args.output).stem       # strips only the final extension
    return default_stem or program_id or "machine"


def _resolve_out_path(args, base: str) -> Optional[Path]:
    """Where to write this run's primary artifact, or ``None`` for stdout.

    ``-o -`` -> stdout; ``-o PATH`` -> that exact path; otherwise ``<base><ext>`` in
    ``--outdir``. ``Path`` handles relative-vs-absolute; ``.`` is the current directory.
    """
    if args.output == "-":
        return None
    if args.output:
        return Path(args.output)
    return Path(args.outdir) / f"{base}{_TARGET_EXT.get(args.target, '.json')}"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cobol-xstate",
        description="Parse IBM Enterprise COBOL and emit its control flow as an "
                    "XState v5 JSON Harel statechart (a modernization rewrite contract).",
    )
    p.add_argument("source", help="path to a COBOL source file ('-' for stdin)")
    p.add_argument("-o", "--output",
                   help="exact output path, overriding --outdir and the default name; "
                        "'-' writes to stdout")
    p.add_argument("--outdir", default=".", metavar="DIR",
                   help="directory for the auto-named output file (default: current "
                        "directory). Relative paths resolve against the current "
                        "directory; created (with parents) if it does not exist. The "
                        "file is named after the source (or the PROGRAM-ID for stdin).")
    p.add_argument("--target",
                   choices=["json", "js", "reactive", "business", "lineage",
                            "artifacts"],
                   default="json",
                   help="json = the XState config bundle (default); js = a runnable "
                        "XState v5 setup() ES module backed by the decimal runtime; "
                        "reactive = an event-driven module whose boundary I/O is push / "
                        "fire-and-forget (see docs/reactive-target.md); business = a "
                        "read-only distillation that collapses technical scaffolding and "
                        "keeps only boundary/decision states (names left as fill-in); "
                        "lineage = one row per (external event, field) with the events "
                        "whose data reaches it (see docs/lineage-target.md); artifacts = "
                        "one row per related artifact this program touches - Db2 tables, "
                        "files/datasets, called programs, queues - with the resolution "
                        "chain each program-local name still needs "
                        "(see docs/artifacts-target.md)")
    p.add_argument("--jcl", action="store_true",
                   help="treat the input as JCL / a PROC (auto-detected for .jcl/.prc/"
                        ".proc or a source beginning with a // JOB/PROC statement). Emits "
                        "<name>.jcl.artifacts.json + <name>.jcl.lineage.json - the job's "
                        "dataset dataflow, control-card field lineage, and the related-"
                        "artifact manifest (see docs/jcl-target.md). External PROCs / "
                        "INCLUDE / control-card members are flagged unresolved from the "
                        "CLI; pass a retrieval function to the Python API to resolve them.")
    p.add_argument("--format", choices=["fixed", "free"],
                   help="source format (default: auto-detect)")
    p.add_argument("-I", "--copybook-path", action="append", default=[],
                   metavar="DIR", help="copybook search directory (repeatable)")
    p.add_argument("--copybook-ext", action="append", default=[], metavar="EXT",
                   help="extra copybook extension to try, e.g. .cpy (repeatable)")
    p.add_argument("--machine-only", action="store_true",
                   help="emit only the bare XState config (omit provenance/flags/notes)")
    p.add_argument("--no-lineage", action="store_true",
                   help="skip the companion <name>.lineage.json that the default run "
                        "writes alongside the bundle")
    p.add_argument("--no-business", action="store_true",
                   help="skip the companion <name>.business.json that the default run "
                        "writes alongside the bundle")
    p.add_argument("--no-reactive", action="store_true",
                   help="skip the companion <name>.reactive.json that the default run "
                        "writes alongside the bundle")
    p.add_argument("--no-artifacts", action="store_true",
                   help="skip the companion <name>.artifacts.json that the default run "
                        "writes alongside the bundle")
    p.add_argument("--indent", type=int, default=2, help="JSON indent (default: 2)")
    p.add_argument("--summary", action="store_true",
                   help="print a human-readable summary to stderr")
    return p


def _looks_like_jcl(source_name: str, source: str) -> bool:
    """JCL by extension, or by a leading ``//NAME JOB/PROC`` statement (a COBOL source
    never begins that way, so this does not misfire on COBOL)."""
    if source_name.lower().rsplit(".", 1)[-1] in ("jcl", "prc", "proc"):
        return True
    for line in source.splitlines():
        s = line.strip()
        if not s or s.startswith("//*"):
            continue
        return bool(re.match(r"^//\S*\s+(JOB|PROC)\b", s, re.I))
    return False


def _run_jcl(args, source: str, source_name: str, default_stem: Optional[str]) -> int:
    """Parse a JCL job / PROC and emit its lineage + artifact manifest. The CLI supplies no
    member resolver, so external PROCs / INCLUDE / control-card datasets are flagged; the
    Python API (parse_jcl(text, resolver=...)) is where a retrieval function is wired in."""
    import json as _json

    job = parse_jcl(source, resolver=None, source_name=source_name)
    lineage = build_jcl_lineage(job)
    artifacts = build_jcl_artifacts(job)
    base = _artifact_base(args, default_stem, job.name or "job")

    if args.output == "-":
        # one stream, one document: the two views as a single bundle
        print(_json.dumps({"format": "cobol-xstate-jcl", "job": job.name,
                           "source": source_name, "artifacts": artifacts,
                           "lineage": lineage}, indent=args.indent))
        return 0

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    art_path = Path(args.output) if args.output else outdir / f"{base}.jcl.artifacts.json"
    lin_path = (art_path.with_name(base + ".jcl.lineage.json") if args.output
                else outdir / f"{base}.jcl.lineage.json")
    art_path.parent.mkdir(parents=True, exist_ok=True)
    art_path.write_text(_json.dumps(artifacts, indent=args.indent) + "\n", encoding="utf-8")
    lin_path.write_text(_json.dumps(lineage, indent=args.indent) + "\n", encoding="utf-8")
    print(f"[{source_name}] wrote {art_path}", file=sys.stderr)
    print(f"[{source_name}] wrote {lin_path}", file=sys.stderr)

    if args.summary:
        print(f"[{job.name or 'JOB'}] {len(job.steps)} step(s), "
              f"{len(lineage['datasets'])} dataset(s), "
              f"{len(lineage['dataflow'])} dataflow edge(s), "
              f"{len(lineage['fieldLineage'])} field-lineage step(s), "
              f"{len(job.flags)} flag(s)", file=sys.stderr)
        for f in job.flags:
            print(f"  FLAG {f}", file=sys.stderr)
    return 0


def run(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    search_paths = list(args.copybook_path)
    if args.source == "-":
        source = sys.stdin.read()
        source_name = "<stdin>"
        default_stem = None  # no filename; fall back to PROGRAM-ID after parsing
    else:
        path = Path(args.source)
        if not path.exists():
            print(f"error: no such file: {path}", file=sys.stderr)
            return 2
        source = path.read_text(errors="replace")
        source_name = path.name
        default_stem = path.stem  # <stem>.cbl -> <stem>.json by default
        search_paths.append(str(path.parent))  # look beside the source by default

    if args.jcl or _looks_like_jcl(source_name, source):
        return _run_jcl(args, source, source_name, default_stem)

    default_exts = ("", ".cpy", ".CPY", ".cbl", ".cob", ".copy", ".CBL")
    resolver = CopybookResolver(
        paths=search_paths,
        exts=tuple(args.copybook_ext) + default_exts,
    )
    fmt = _format(args.format)
    if fmt is None:
        det = detect_source_format(source)
        fmt = det.format
        # A silent wrong guess corrupts every downstream stage, so surface it: state
        # what was picked, and warn (recommending --format) when confidence is low.
        level = "detected" if det.is_confident else "WARNING: low-confidence"
        print(f"[{source_name}] {level} source format = {fmt.value} "
              f"({det.confidence:.0%}: {det.reason})", file=sys.stderr)
        if not det.is_confident:
            print("  -> if the output looks corrupted, re-run with "
                  "--format fixed|free to override.", file=sys.stderr)

    program = parse_program(source, fmt, resolver=resolver)
    machine = build_machine(program, source_name=source_name)

    base = _artifact_base(args, default_stem, machine.program_id)
    out_path = _resolve_out_path(args, base)
    if out_path is not None:
        # Create the destination directory (and parents) if it does not exist.
        out_path.parent.mkdir(parents=True, exist_ok=True)

    # Always write UTF-8 explicitly: the platform default (cp1252 on Windows) cannot
    # encode the runtime's non-ASCII text, and JSON/JS artifacts must be portable.
    def _write(path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8")

    import json as _json

    def _companion(beside: Path, suffix: str, obj) -> None:
        path = beside.with_name(base + suffix)
        if path == beside:
            # Refuse to write a companion over the artifact we just wrote. Reachable
            # only for a source whose own name ends in a companion suffix; losing the
            # bundle silently is far worse than an odd filename.
            path = beside.with_name(base + ".view" + suffix)
            print(f"[{source_name}] note: companion would collide with {beside.name}; "
                  f"writing {path.name} instead", file=sys.stderr)
        _write(path, _json.dumps(obj, indent=args.indent) + "\n")
        print(f"[{source_name}] wrote {path}", file=sys.stderr)

    def _write_lineage_companion(beside: Path) -> None:
        """The field-lineage table travels with any machine view: the rows reference the
        machine's events and fields, so the two are read together."""
        if args.machine_only or args.no_lineage:
            return
        _companion(beside, ".lineage.json", build_lineage(machine))

    def _write_business_companion(beside: Path) -> None:
        """The business distillation: the same machine with scaffolding collapsed. It is
        the view a human reads, so a default run produces it beside the faithful one."""
        if args.machine_only or args.no_business:
            return
        _companion(beside, ".business.json", build_business_view(machine))

    def _write_artifacts_companion(beside: Path) -> None:
        """The related-artifact manifest: the Db2 tables, files, called programs and
        queues this program touches, each with the resolution chain its program-local
        name still needs. A logistics view of the same boundary the interface recovers."""
        if args.machine_only or args.no_artifacts:
            return
        _companion(beside, ".artifacts.json", build_artifacts(machine))

    def _write_reactive_companion(beside: Path) -> None:
        """The event-driven view: the machine the modernized system is built from.

        The reactive lowering REFUSES some programs (CICS handler regions, recursive
        PERFORM). On a default run that must not take the other views down with it - the
        refusal is a fact about this program, not a failure of the run. Say so and carry
        on; `--target reactive` is where a hard error belongs.
        """
        if args.machine_only or args.no_reactive:
            return
        try:
            view = build_reactive_view(machine)
        except NotImplementedError as exc:
            print(f"[{source_name}] note: no reactive view - {exc}", file=sys.stderr)
            return
        _companion(beside, ".reactive.json", view)

    if args.target in ("business", "lineage", "artifacts"):
        obj = (build_lineage(machine) if args.target == "lineage"
               else build_artifacts(machine) if args.target == "artifacts"
               else build_business_view(machine))
        text = _json.dumps(obj, indent=args.indent)
        if out_path is None:
            print(text)
        else:
            _write(out_path, text + "\n")
            print(f"[{source_name}] wrote {out_path}", file=sys.stderr)
            if args.target == "business":
                _write_lineage_companion(out_path)
    elif args.target in ("js", "reactive"):
        try:
            text = (emit_reactive_module(machine) if args.target == "reactive"
                    else emit_setup_module(machine))
        except NotImplementedError as exc:
            # An explicit --target reactive on a program the lowering refuses: report the
            # reason, not a traceback. The refusal is a fact about the program.
            print(f"error: {exc}", file=sys.stderr)
            return 3
        if out_path is None:
            print(text)
        else:
            _write(out_path, text)
            # The emitted module imports ./cobolRuntime.mjs, so the runtime must land
            # beside it. It ships as package data; a missing asset means a broken
            # install and raises rather than emitting a dangling import.
            runtime_dst = out_path.parent / "cobolRuntime.mjs"
            _write(runtime_dst, read_runtime_asset("cobolRuntime.mjs"))
            print(f"[{source_name}] wrote {out_path}", file=sys.stderr)
            print(f"[{source_name}] wrote {runtime_dst}", file=sys.stderr)
            # The reactive machine is the one you most want to LOOK at - its waits and
            # publishes are the new system's message contract - so it gets a drawable
            # JSON beside the runnable module, like every other machine view.
            if args.target == "reactive":
                view = out_path.with_name(base + ".reactive.json")
                _write(view, _json.dumps(build_reactive_view(machine),
                                         indent=args.indent) + "\n")
                print(f"[{source_name}] wrote {view}", file=sys.stderr)
    else:
        text = machine.to_json(machine_only=args.machine_only, indent=args.indent)
        if out_path is None:
            print(text)          # stdout carries the bundle only - one stream, one doc
        else:
            _write(out_path, text + "\n")
            print(f"[{source_name}] wrote {out_path}", file=sys.stderr)
            # A plain run yields the four JSON views of one program, each answering a
            # different question: the faithful machine (what it does), the business
            # distillation (which steps matter), the lineage table (where each field's
            # value came from), and the reactive machine (what replaces it). All four are
            # things you READ or DRAW - the runnable modules stay behind their own
            # --target. Each is opt-out-able.
            _write_business_companion(out_path)
            _write_lineage_companion(out_path)
            _write_reactive_companion(out_path)
            _write_artifacts_companion(out_path)

    if args.summary:
        n_states = len(machine.config.get("states", {}))
        iface = machine.bundle()["interface"]
        print(
            f"[{machine.program_id}] {n_states} state(s), "
            f"{len(machine.provenance)} provenance entr(ies), "
            f"{len(machine.flags)} flag(s), "
            f"{len(iface['perimeterStates'])} perimeter state(s)",
            file=sys.stderr,
        )
        if iface["endpoints"]:
            print("  external interface:", file=sys.stderr)
            for ep in iface["endpoints"]:
                print(f"    {ep['type']:9} {ep['endpoint']:24} "
                      f"({', '.join(ep['directions'])})", file=sys.stderr)
        for state, d in iface["perimeterStates"].items():
            io = []
            if d["gets"]:
                io.append("gets " + ", ".join(d["gets"]))
            if d["creates"]:
                io.append("creates " + ", ".join(d["creates"]))
            print(f"  PERIMETER {state} [{d['region']}] ({d.get('perimeter', '?')}): "
                  f"{'; '.join(io)}", file=sys.stderr)
        for f in machine.flags:
            print(f"  FLAG {f['paragraph']} (line {f['line']}): {f['message']}", file=sys.stderr)

    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
