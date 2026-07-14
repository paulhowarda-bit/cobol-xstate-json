"""Command-line entry point: COBOL file -> XState v5 JSON statechart."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from .business import build_business_view
from .emitter import emit_setup_module
from .normalizer import SourceFormat, detect_source_format
from .reactive import emit_reactive_module
from .parser import parse_program
from .preprocessor import CopybookResolver
from .statechart import build_machine


def _format(name: Optional[str]) -> Optional[SourceFormat]:
    if name is None:
        return None
    return {"fixed": SourceFormat.FIXED, "free": SourceFormat.FREE}[name]


def _resolve_out_path(args, default_stem: Optional[str],
                      program_id: str) -> Optional[Path]:
    """Where to write the output, or ``None`` for stdout.

    ``-o -`` -> stdout; ``-o PATH`` -> that exact path; otherwise an auto-named
    ``<stem><ext>`` placed in ``--outdir`` (stem = source name, or PROGRAM-ID for
    stdin). ``Path`` handles relative-vs-absolute; ``.`` is the current directory.
    """
    if args.output == "-":
        return None
    if args.output:
        return Path(args.output)
    stem = default_stem or program_id or "machine"
    ext = ".mjs" if args.target in ("js", "reactive") else ".json"
    return Path(args.outdir) / f"{stem}{ext}"


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
    p.add_argument("--target", choices=["json", "js", "reactive", "business"],
                   default="json",
                   help="json = the XState config bundle (default); js = a runnable "
                        "XState v5 setup() ES module backed by the decimal runtime; "
                        "reactive = an event-driven module whose boundary I/O is push / "
                        "fire-and-forget (see docs/reactive-target.md); business = a "
                        "read-only distillation that collapses technical scaffolding and "
                        "keeps only boundary/decision states (names left as fill-in)")
    p.add_argument("--format", choices=["fixed", "free"],
                   help="source format (default: auto-detect)")
    p.add_argument("-I", "--copybook-path", action="append", default=[],
                   metavar="DIR", help="copybook search directory (repeatable)")
    p.add_argument("--copybook-ext", action="append", default=[], metavar="EXT",
                   help="extra copybook extension to try, e.g. .cpy (repeatable)")
    p.add_argument("--machine-only", action="store_true",
                   help="emit only the bare XState config (omit provenance/flags/notes)")
    p.add_argument("--indent", type=int, default=2, help="JSON indent (default: 2)")
    p.add_argument("--summary", action="store_true",
                   help="print a human-readable summary to stderr")
    return p


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

    out_path = _resolve_out_path(args, default_stem, machine.program_id)
    if out_path is not None:
        # Create the destination directory (and parents) if it does not exist.
        out_path.parent.mkdir(parents=True, exist_ok=True)

    if args.target == "business":
        import json as _json
        text = _json.dumps(build_business_view(machine), indent=args.indent)
        if out_path is None:
            print(text)
        else:
            out_path.write_text(text + "\n")
            print(f"[{source_name}] wrote {out_path}", file=sys.stderr)
    elif args.target in ("js", "reactive"):
        text = (emit_reactive_module(machine) if args.target == "reactive"
                else emit_setup_module(machine))
        if out_path is None:
            print(text)
        else:
            out_path.write_text(text)
            # Drop the decimal runtime beside the module so its import resolves.
            runtime_src = Path(__file__).resolve().parents[2] / "runtime" / "cobolRuntime.mjs"
            if runtime_src.exists():
                (out_path.parent / "cobolRuntime.mjs").write_text(
                    runtime_src.read_text())
            print(f"[{source_name}] wrote {out_path}", file=sys.stderr)
    else:
        text = machine.to_json(machine_only=args.machine_only, indent=args.indent)
        if out_path is None:
            print(text)
        else:
            out_path.write_text(text + "\n")
            print(f"[{source_name}] wrote {out_path}", file=sys.stderr)

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
