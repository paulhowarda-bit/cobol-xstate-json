"""Command-line entry point: COBOL file -> XState v5 JSON statechart."""

from __future__ import annotations

import argparse
import importlib.util
import sys
import webbrowser
from pathlib import Path
from typing import List, Optional

from .emitter import emit_setup_module
from .normalizer import SourceFormat
from .parser import parse_program
from .preprocessor import CopybookResolver
from .statechart import build_machine


def _format(name: Optional[str]) -> Optional[SourceFormat]:
    if name is None:
        return None
    return {"fixed": SourceFormat.FIXED, "free": SourceFormat.FREE}[name]


def _load_renderer():
    """Import the standalone viz/render_statechart.py by file path.

    The HTML viewer lives outside the package (it is a self-contained tool with
    its own vendored JS assets), so it is loaded on demand only when the html
    target is requested — keeping the json/js paths dependency-free.
    """
    repo_root = Path(__file__).resolve().parents[2]
    mod_path = repo_root / "viz" / "render_statechart.py"
    if not mod_path.exists():
        raise SystemExit(
            f"error: html target needs the renderer at {mod_path}, which is missing.")
    spec = importlib.util.spec_from_file_location("_viz_render_statechart", mod_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cobol-xstate",
        description="Parse IBM Enterprise COBOL and emit its control flow as an "
                    "XState v5 JSON Harel statechart (a modernization rewrite contract).",
    )
    p.add_argument("source", help="path to a COBOL source file ('-' for stdin)")
    p.add_argument("-o", "--output", help="write output here (default: stdout)")
    p.add_argument("--target", choices=["json", "js", "html"], default="json",
                   help="json = the XState config bundle (default); js = a runnable "
                        "XState v5 setup() ES module backed by the decimal runtime; "
                        "html = a self-contained interactive statechart diagram")
    p.add_argument("--html", dest="target", action="store_const", const="html",
                   help="shorthand for --target html (.cbl -> interactive diagram)")
    p.add_argument("--open", action="store_true", dest="open_browser",
                   help="with --html, open the written diagram in the default browser")
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
    else:
        path = Path(args.source)
        if not path.exists():
            print(f"error: no such file: {path}", file=sys.stderr)
            return 2
        source = path.read_text(errors="replace")
        source_name = path.name
        search_paths.append(str(path.parent))  # look beside the source by default

    default_exts = ("", ".cpy", ".CPY", ".cbl", ".cob", ".copy", ".CBL")
    resolver = CopybookResolver(
        paths=search_paths,
        exts=tuple(args.copybook_ext) + default_exts,
    )
    program = parse_program(source, _format(args.format), resolver=resolver)
    machine = build_machine(program, source_name=source_name)

    if args.target == "js":
        text = emit_setup_module(machine)
        if args.output:
            out_path = Path(args.output)
            out_path.write_text(text)
            # Drop the decimal runtime beside the module so its import resolves.
            runtime_src = Path(__file__).resolve().parents[2] / "runtime" / "cobolRuntime.mjs"
            if runtime_src.exists():
                (out_path.parent / "cobolRuntime.mjs").write_text(
                    runtime_src.read_text())
        else:
            print(text)
    elif args.target == "html":
        renderer = _load_renderer()
        html, graph, used_cdn = renderer.render_html(machine.config)
        if args.output:
            out_path = Path(args.output)
        elif args.source == "-":
            out_path = Path("statechart.html")
        else:
            out_path = Path(args.source).with_suffix(".html")
        out_path.write_text(html, encoding="utf-8")
        idx = graph["index"]
        print(
            f"Wrote {out_path}  ({len(html) // 1024} KB) — "
            f"{len(idx['states'])} states, {len(idx['transitions'])} transitions"
            + ("  [CDN fallback: needs network]" if used_cdn else "  [offline, self-contained]"),
            file=sys.stderr,
        )
        if args.open_browser:
            uri = out_path.resolve().as_uri()
            webbrowser.open(uri)
            print(f"opened {uri}", file=sys.stderr)
    else:
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
