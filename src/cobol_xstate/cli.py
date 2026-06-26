"""Command-line entry point: COBOL file -> XState v5 JSON statechart."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from .normalizer import SourceFormat
from .parser import parse_program
from .statechart import build_machine


def _format(name: Optional[str]) -> Optional[SourceFormat]:
    if name is None:
        return None
    return {"fixed": SourceFormat.FIXED, "free": SourceFormat.FREE}[name]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cobol-xstate",
        description="Parse IBM Enterprise COBOL and emit its control flow as an "
                    "XState v5 JSON Harel statechart (a modernization rewrite contract).",
    )
    p.add_argument("source", help="path to a COBOL source file ('-' for stdin)")
    p.add_argument("-o", "--output", help="write JSON here (default: stdout)")
    p.add_argument("--format", choices=["fixed", "free"],
                   help="source format (default: auto-detect)")
    p.add_argument("--machine-only", action="store_true",
                   help="emit only the bare XState config (omit provenance/flags/notes)")
    p.add_argument("--indent", type=int, default=2, help="JSON indent (default: 2)")
    p.add_argument("--summary", action="store_true",
                   help="print a human-readable summary to stderr")
    return p


def run(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.source == "-":
        source = sys.stdin.read()
        source_name = "<stdin>"
    else:
        path = Path(args.source)
        if not path.exists():
            print(f"error: no such file: {path}", file=sys.stderr)
            return 2
        source = path.read_text(errors="replace")
        source_name = path.name

    program = parse_program(source, _format(args.format))
    machine = build_machine(program, source_name=source_name)
    text = machine.to_json(machine_only=args.machine_only, indent=args.indent)

    if args.output:
        Path(args.output).write_text(text + "\n")
    else:
        print(text)

    if args.summary:
        n_states = len(machine.config.get("states", {}))
        print(
            f"[{machine.program_id}] {n_states} state(s), "
            f"{len(machine.provenance)} provenance entr(ies), "
            f"{len(machine.flags)} flag(s)",
            file=sys.stderr,
        )
        for f in machine.flags:
            print(f"  FLAG {f['paragraph']} (line {f['line']}): {f['message']}", file=sys.stderr)

    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
