"""Command-line entry point: COBOL file -> XState v5 JSON statechart."""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path
from typing import List, Optional

# Shared with the JCL front-end: the estate boundary, retrieval, and the CLI plumbing
# both CLIs repeat. Core never imports back into this package.
from mainframe_artifacts.artifact_service import decode_member, load_fetcher
from mainframe_artifacts.bundle import open_bundle
from mainframe_artifacts.cliargs import (add_logging_args, add_output_args,
                                       add_retrieval_args, add_synonym_args,
                                       jobs as _jobs, synonym_lookup)
from mainframe_artifacts.detect import looks_like_jcl as _looks_like_jcl
from cobol_parser import PACKAGE_LOGGER as PARSE_LOGGER
from cobol_parser.parse_bundle import open_parse_bundle
from mainframe_artifacts.logging_setup import PACKAGE_LOGGER as CORE_LOGGER
from mainframe_artifacts.logging_setup import configure_logging
from mainframe_artifacts.output import make_run_dir as _make_run_dir
from mainframe_artifacts.output import run_dir as _run_dir_of
from mainframe_artifacts.output import write_json, write_text
from mainframe_artifacts.profiling import StageTimer
from mainframe_artifacts.report import report_stages as _report_stages

from . import PACKAGE_LOGGER
from .api import analyze, artifact_base, gather, write_views
from .bind import JclSupportMissing
from .bind import jcl_api as _jcl_api
from .errors import CobolXstateError
from .normalizer import SourceFormat
from .runtime_assets import read_runtime_asset

# Explicit name, NOT __name__: this module is also run as `python -m cobol_xstate.cli`,
# where __name__ == "__main__" would put the logger outside the cobol_xstate hierarchy and
# out of configure_logging's reach (so INFO/progress would be silently dropped).
_log = logging.getLogger("cobol_xstate.cli")


def _format(name: Optional[str]) -> Optional[SourceFormat]:
    if name is None:
        return None
    return {"fixed": SourceFormat.FIXED, "free": SourceFormat.FREE}[name]


# Suffix per target. Companions are built from the same base, so every artifact of one
# run has a distinct name and none can land on another's path.
_TARGET_EXT = {"js": ".mjs", "reactive": ".reactive.mjs",
               "lineage": ".lineage.json", "business": ".business.json",
               "artifacts": ".artifacts.json"}


def _artifact_base(args, default_stem: Optional[str], program_id: str) -> str:
    """The shared base name every artifact of this run is built from.

    One derivation, published as ``api.artifact_base`` so an embedding caller names its
    files the way a run names them instead of guessing at ``path.stem`` - which differs
    for exactly the members where it matters (upstream ledger item 35).
    """
    return artifact_base(default_stem, program_id)


def _resolve_out_path(args, base: str, run_dir: Path) -> Path:
    """Where this run's primary artifact goes."""
    return run_dir / f"{base}{_TARGET_EXT.get(args.target, '.json')}"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cobol-xstate",
        description="Parse IBM Enterprise COBOL and emit its control flow as an "
                    "XState v5 JSON Harel statechart (a modernization rewrite contract).",
    )
    p.add_argument("source", help="path to a COBOL source file ('-' for stdin)")
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
                        "artifact manifest (see docs/jcl-target.md). Cataloged PROCs, "
                        "INCLUDE members and control-card datasets are retrieved before "
                        "the parse, so the steps inside them are in the model.")
    p.add_argument("--format", choices=["fixed", "free"],
                   help="source format (default: auto-detect)")
    p.add_argument("-I", "--copybook-path", action="append", default=[],
                   metavar="DIR", help="copybook search directory (repeatable)")
    p.add_argument("--copybook-ext", action="append", default=[], metavar="EXT",
                   help="extra copybook extension to try, e.g. .cpy (repeatable)")
    add_retrieval_args(p)
    # Db2 catalog knowledge: a column-list-less INSERT written under a synonym finds
    # the base table's DECLARE TABLE / DCLGEN column order through it, and its column
    # mappings are then stamped with the BASE table name.
    add_synonym_args(p)
    p.add_argument("--from-parse", metavar="FILE",
                   help="model from a parse bundle written upfront by cobol-parser, "
                        "skipping the parse entirely. The bundle records the sha256 of "
                        "the exact source it parsed and a different source is an error "
                        "- a stale Program is silently wrong everywhere. Composes with "
                        "--from-bundle for a fully offline, parse-free run.")
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
    p.add_argument("--no-dynamic-calls", action="store_true",
                   help="skip the companion <name>.dynamic-calls.json that the default "
                        "run writes alongside the bundle")
    p.add_argument("--bind-jcl", action="append", default=[], metavar="FILE",
                   help="JCL/PROC file(s) whose DD statements bind this COBOL program's "
                        "file ddnames to datasets (repeatable). Each file row the JCL "
                        "resolves gains 'dataset' and 'boundBy' (job/step, with the "
                        "step's run conditions) in the artifacts view - the ddname->DSN "
                        "join closed.")
    add_output_args(p, outdir_help=(
        "directory for output (default: ./out). EVERY file this run produces goes here, "
        "exactly as given with nothing appended - the bundle, all six views, both "
        "retrieval reports, and the artifacts retrieved from the estate (under deps/). "
        "Created with parents if it does not exist."))
    add_logging_args(p)
    return p


def _run_jcl(args, source: str, source_name: str, default_stem: Optional[str],
              paths: List[str], timing_sink=None) -> int:
    """Parse a JCL job / PROC and emit its lineage + artifact manifest.

    Stage 1 runs first and must: a cataloged PROC, an INCLUDE member and a control-card
    dataset each carry ``EXEC PGM=`` steps and DD statements that appear nowhere in the
    JCL file itself. Parsed without them - as this path used to - those steps do not
    show up as programs, as datasets, or at all, and the job reads as far simpler than
    it is."""
    import json as _json

    timer = StageTimer(_log, args.timing, source_name, sink=timing_sink)
    fetcher, why = _service(args, source_name)
    base = _artifact_base(args, default_stem, "job")
    # Same ordering constraint as the COBOL path: prefetch writes into the run directory,
    # so the directory's name must be known before anything is parsed. The JOB/PROC name
    # is on the first statement, so scan for it.
    run_dir = _run_dir_of(args.outdir)
    err = _make_run_dir(run_dir)
    if err:
        _log.error(f"error: {err}")
        return 2
    deps = str(run_dir / "deps")
    # `paths`, not args.copybook_path: run() already appended the JCL file's own parent,
    # which is where a cataloged PROC or INCLUDE member most often sits. Passing only the
    # -I list meant a PROC beside the job was never found locally, and every EXEC step and
    # DD inside it silently vanished from the model.
    try:
        jcl_api = _jcl_api()
    except JclSupportMissing as exc:
        _log.error(f"error: {exc}")
        return 2
    analysis = jcl_api.analyze(source, source_name=source_name, fetcher=fetcher,
                               paths=list(paths), dest=deps, unavailable=why,
                               jobs=_jobs(args), timer=timer)
    job, pre, fetched = analysis.job, analysis.prefetch, analysis.fetch
    base = _artifact_base(args, default_stem, job.name or "job")

    for suffix, obj in ((".jcl.artifacts.json", analysis.artifacts()),
                        (".jcl.lineage.json", analysis.lineage()),
                        (".jcl.prefetch.json", pre.report()),
                        (".jcl.fetch.json", fetched)):
        path = run_dir / f"{base}{suffix}"
        path.write_text(_json.dumps(obj, indent=args.indent) + "\n", encoding="utf-8")
        _log.info(f"[{source_name}] wrote {path}")
    _report_stages(_log, source_name, pre, fetched)

    if args.summary:
        lineage = analysis.lineage()
        _log.info(f"[{job.name or 'JOB'}] {len(job.steps)} step(s), "
              f"{len(lineage['datasets'])} dataset(s), "
              f"{len(lineage['dataflow'])} dataflow edge(s), "
              f"{len(lineage['fieldLineage'])} field-lineage step(s), "
              f"{len(job.flags)} flag(s)")
        for f in job.flags:
            _log.info(f"  FLAG {f}")
    timer.report()
    return 0


def _service(args, source_name: str):
    """The estate artifact service for this run, and why it is missing if it is.

    Never fatal. A run without the service still parses whatever is on the local search
    path and still writes its reports - they simply say, per member, that nothing was
    ever looked for. Failing the run instead would be worse: it would make the tool
    unusable exactly where it is most often used first, on a laptop with a handful of
    members and no estate connection."""
    fetcher, why = load_fetcher(args.copybook_fetcher)
    if fetcher is None:
        _log.warning(f"[{source_name}] WARNING: {why}")
    return fetcher, why




def run(argv: Optional[List[str]] = None, timing_sink=None) -> int:
    """Parse args, configure logging, and dispatch, behind the top-level error boundary.

    ``timing_sink`` lets an embedding Python program collect this run's per-stage
    timings: it is called once, on a completed run, with
    ``[{"stage": ..., "ms": ...}, ...]`` in call order, so the caller can route them
    into its own timing log. Supplying it turns collection on without ``--timing``;
    nothing reaches stderr unless ``--timing`` is also passed.

    An expected failure (any ``CobolXstateError``) becomes a one-line message + a non-zero
    exit code; an UNEXPECTED exception is reported as an internal error (exit 1) with the
    full traceback shown only under ``--debug`` - never leaked raw to the user."""
    args = build_parser().parse_args(argv)
    # ALL THREE roots: retrieval logs from mainframe_artifacts.*, the parse front-end's
    # from cobol_parser.*, everything else from cobol_xstate.*. Configuring only some
    # leaves the rest propagating to the root logger - or, with no handler anywhere,
    # printing WARNING+ via logging's lastResort, which would make -qq stop being silent.
    configure_logging(verbose=args.verbose or (1 if args.debug else 0), quiet=args.quiet,
                      loggers=(CORE_LOGGER, PARSE_LOGGER, PACKAGE_LOGGER))
    try:
        return _run(args, timing_sink=timing_sink)
    except CobolXstateError as exc:
        # An expected, named failure: str(exc) IS the user-facing explanation.
        _log.error("%s", exc)
        return 1
    except BrokenPipeError:
        # `cobol-xstate ... | head` closes the pipe early; not worth a traceback.
        return 0
    except KeyboardInterrupt:
        _log.error("interrupted")
        return 130
    except Exception as exc:
        if args.debug:
            raise  # the developer asked for the raw traceback
        # The one-liner must carry the ACTUAL failure, not just point at --debug: a
        # caller that captures stderr (a batch tracer, CI) otherwise records "internal
        # error" with the reason hidden behind a re-run it will never do.
        _log.critical("internal error while processing %r: %s: %s - re-run with "
                      "--debug for the full traceback", args.source,
                      type(exc).__name__, exc)
        _log.debug("internal error traceback", exc_info=True)
        return 1


def _run(args, timing_sink=None) -> int:
    search_paths = list(args.copybook_path)
    if args.source == "-":
        source = sys.stdin.read()
        source_name = "<stdin>"
        default_stem = None  # no filename; fall back to PROGRAM-ID after parsing
    else:
        path = Path(args.source)
        if not path.exists():
            _log.error(f"error: no such file: {path}")
            return 2
        source = decode_member(path.read_bytes())
        source_name = path.name
        default_stem = path.stem  # <stem>.cbl -> <stem>.json by default
        search_paths.append(str(path.parent))  # look beside the source by default

    if args.jcl or _looks_like_jcl(source_name, source):
        return _run_jcl(args, source, source_name, default_stem, search_paths,
                        timing_sink=timing_sink)

    timer = StageTimer(_log, args.timing, source_name, sink=timing_sink)

    if args.gather_only and args.from_bundle:
        _log.error("error: --gather-only writes a bundle and --from-bundle reads one; "
                   "they cannot both apply to a single run")
        return 2
    if args.gather_only and args.from_parse:
        # Not merely odd: the gather's stage-2 plan comes from the parsed model, and
        # tying that record to a pre-parsed Program instead of the live parse would
        # blur which run produced which evidence.
        _log.error("error: --gather-only runs the retrieval half against a live parse; "
                   "it cannot take --from-parse")
        return 2

    parse = None
    if args.from_parse:
        try:
            parse = open_parse_bundle(args.from_parse)
        except CobolXstateError as exc:
            _log.error(f"error: {exc}")
            return 2

    lookup, why_synonyms = synonym_lookup(args)
    if why_synonyms:
        _log.error(f"error: {why_synonyms}")
        return 2
    synonyms = lookup.mapping if lookup is not None else None
    synonym_resolver = lookup.resolver if lookup is not None else None

    bundle = None
    if args.from_bundle:
        try:
            bundle = open_bundle(args.from_bundle)
        except CobolXstateError as exc:
            _log.error(f"error: {exc}")
            return 2
        # Not fatal: re-modelling an edited source against the same estate is a
        # legitimate thing to do. But if the program changed enough to COPY something
        # new, the bundle will have no record of it, and knowing why is worth a line.
        if bundle.source() != source:
            _log.warning(f"[{source_name}] WARNING: this source differs from the one "
                         f"the bundle was gathered for ({bundle.subject_name}); any "
                         f"member it did not ask for is not in the bundle")

    # STAGE 1 runs before the parse, and it writes into the run directory - so the
    # directory has to be settled first, before anything is parsed.
    fetcher, why = (None, None) if bundle is not None else _service(args, source_name)
    run_dir = _run_dir_of(args.outdir)
    err = _make_run_dir(run_dir)
    if err:
        _log.error(f"error: {err}")
        return 2
    deps = str(run_dir / "deps")

    # Reading the --bind-jcl files is the CLI's business (paths, existence, exit codes);
    # the join itself goes through the lazy orchestrator, so this package never imports
    # the JCL one at module level.
    jcl_sources = []
    for jf in args.bind_jcl:
        jp = Path(jf)
        if not jp.exists():
            _log.error(f"error: no such file: {jp} (--bind-jcl)")
            return 2
        jcl_sources.append((jp.name, decode_member(jp.read_bytes())))

    # --gather-only: the retrieval half alone, on the box that can reach the estate.
    # Both stages still run - stage 2's plan needs the parse - but no view is written,
    # because the product of this mode is the bundle.
    if args.gather_only:
        gathered = gather(source, source_name=source_name, fmt=_format(args.format),
                          fetcher=fetcher, paths=search_paths,
                          exts=tuple(args.copybook_ext), dest=args.gather_only,
                          unavailable=why, jobs=_jobs(args), timer=timer)
        _log.info(f"[{source_name}] wrote estate bundle {gathered}")
        _log.info(f"[{source_name}] model from it with: --from-bundle "
                  f"{args.gather_only}")
        timer.report()
        return 0

    try:
        analysis = analyze(source, source_name=source_name, fmt=_format(args.format),
                           fetcher=fetcher, paths=search_paths,
                           exts=tuple(args.copybook_ext), dest=deps, unavailable=why,
                           jobs=_jobs(args), jcl=jcl_sources, timer=timer,
                           bundle=bundle, parse=parse, synonyms=synonyms,
                           synonym_resolver=synonym_resolver,
                           retrieve=not args.no_fetch)
    except JclSupportMissing as exc:
        _log.error(f"error: {exc}")
        return 2

    machine = analysis.machine
    pre = analysis.prefetch
    report = analysis.fetch

    # A copybook fetcher that ERRORED is not the same as a member that does not exist:
    # the model is missing logic for a fixable reason (bad credentials, service down),
    # so say so loudly rather than letting it read as "not on the estate".
    for member, ferr in analysis.copybook_errors:
        _log.warning(f"[{source_name}] WARNING: copybook fetcher failed for {member}: {ferr}")

    base = _artifact_base(args, default_stem, machine.program_id)
    out_path = _resolve_out_path(args, base, run_dir)

    # Which companions this run writes. The five are opt-out-able one by one, and
    # --machine-only takes all of them - the flags decide the TARGET SET, and everything
    # about how each one is named, ordered and isolated lives in api.write_views, which
    # an embedding caller reaches too (upstream ledger item 35).
    opted_out = {"business": args.no_business, "lineage": args.no_lineage,
                 "reactive": args.no_reactive, "artifacts": args.no_artifacts,
                 "dynamic-calls": args.no_dynamic_calls}
    companions = () if args.machine_only else tuple(
        name for name in ("business", "lineage", "reactive", "artifacts", "dynamic-calls")
        if not opted_out[name])

    def _write_views(targets) -> None:
        write_views(analysis, run_dir, base=base, targets=targets, indent=args.indent,
                    machine_only=args.machine_only, timer=timer, debug=args.debug)

    # Both retrieval stages already ran, inside analyze(): retrieving what this program
    # depends on is not a mode of the tool, it is what the tool does.
    # --machine-only suppresses the REPORTS, never the retrieval: what was fetched
    # decides whether the machine is right, so skipping it to save two files would be
    # backwards.
    if not args.machine_only:
        _write_views(("prefetch", "fetch"))
    _report_stages(_log, source_name, pre, report)

    _t_views = timer.start()
    if args.target in ("business", "lineage", "artifacts"):
        # An explicit --target is the PRODUCT of the run, so it is written here and not
        # through write_views: a crash building it is the run failing, not one view of
        # several failing, and it must reach the exit code.
        with timer.stage(f"view:{args.target}"):
            obj = (analysis.lineage() if args.target == "lineage"
                   else analysis.artifacts() if args.target == "artifacts"
                   else analysis.business())
            write_json(out_path, obj, args.indent)
        _log.info(f"[{source_name}] wrote {out_path}")
        # The field-lineage table travels with any machine view: the rows reference the
        # machine's events and fields, so the two are read together.
        if args.target == "business" and "lineage" in companions:
            _write_views(("lineage",))
    elif args.target in ("js", "reactive"):
        try:
            with timer.stage(f"view:{args.target}"):
                text = (analysis.reactive_module() if args.target == "reactive"
                        else analysis.js_module())
        except NotImplementedError as exc:
            # An explicit --target reactive on a program the lowering refuses: report the
            # reason, not a traceback. The refusal is a fact about the program.
            _log.error(f"error: {exc}")
            return 3
        write_text(out_path, text)
        # The emitted module imports ./cobolRuntime.mjs, so the runtime must land beside
        # it. It ships as package data; a missing asset means a broken install and raises
        # rather than emitting a dangling import.
        runtime_dst = out_path.parent / "cobolRuntime.mjs"
        write_text(runtime_dst, read_runtime_asset("cobolRuntime.mjs"))
        _log.info(f"[{source_name}] wrote {out_path}")
        _log.info(f"[{source_name}] wrote {runtime_dst}")
        # The reactive machine is the one you most want to LOOK at - its waits and
        # publishes are the new system's message contract - so it gets a drawable JSON
        # beside the runnable module, like every other machine view. Written here rather
        # than through write_views for the same reason as the primary above: on an
        # explicit --target reactive it is part of the product.
        if args.target == "reactive":
            view = out_path.with_name(base + ".reactive.json")
            write_json(view, analysis.reactive(), args.indent)
            _log.info(f"[{source_name}] wrote {view}")
    else:
        # A plain run yields the six JSON views of one program, each answering a
        # different question: the faithful machine (what it does), the business
        # distillation (which steps matter), the lineage table (where each field's value
        # came from), the reactive machine (what replaces it), the related artifacts
        # (what else it touches), and the dynamic calls (what it invokes but will not
        # name). All are things you READ or DRAW - the runnable modules stay behind their
        # own --target. Each is opt-out-able, and each runs behind write_views' own
        # boundary: the bundle is already usable output, so a crash in one view is a
        # warning about that view, not a failure of the run.
        _write_views(("bundle",) + companions)
    timer.since("views", _t_views)

    def _summary() -> None:
        n_states = len(machine.config.get("states", {}))
        # The cached overlay, not `machine.bundle()["interface"]`: that is the SAME
        # object, but reaching it through bundle() re-ran the whole Harel transform -
        # a second deepcopy of the config and every actor chart - for one log line.
        iface = machine.interface()
        _log.info(
            f"[{machine.program_id}] {n_states} state(s), "
            f"{len(machine.provenance)} provenance entr(ies), "
            f"{len(machine.flags)} flag(s), "
            f"{len(iface['perimeterStates'])} perimeter state(s)")
        if iface["endpoints"]:
            _log.info("  external interface:")
            for ep in iface["endpoints"]:
                _log.info(f"    {ep['type']:9} {ep['endpoint']:24} "
                      f"({', '.join(ep['directions'])})")
        for state, d in iface["perimeterStates"].items():
            io = []
            if d["gets"]:
                io.append("gets " + ", ".join(d["gets"]))
            if d["creates"]:
                io.append("creates " + ", ".join(d["creates"]))
            _log.info(f"  PERIMETER {state} [{d['region']}] ({d.get('perimeter', '?')}): "
                  f"{'; '.join(io)}")
        # Every called program, grouped by classification - which callees are contained
        # here, which are IBM runtime, which resolved to real source, and which remain
        # unresolved (not yet figured out). The roster a migration planner reads first.
        progs = [r for r in report.get("artifacts", []) if r.get("kind") == "program"]
        if progs:
            groups = {}
            for r in progs:
                label = r.get("subsystem") or r.get("classification") or "unresolved"
                groups.setdefault(label, []).append(str(r.get("artifact", "")))
            _log.info("  called programs:")
            for label in sorted(groups):
                names = ", ".join(sorted(set(groups[label])))
                _log.info(f"    {label:18} {names}")
        for f in machine.flags:
            _log.info(f"  FLAG {f['paragraph']} (line {f['line']}): {f['message']}")

    if args.summary:
        # Same boundary as the companion views: every artifact is on disk by now, so a
        # crash while PRINTING the summary must not turn the run's exit code non-zero.
        try:
            _summary()
        except Exception as exc:
            if args.debug:
                raise
            _log.warning(f"[{source_name}] WARNING: --summary failed "
                         f"({type(exc).__name__}: {exc}) - the written artifacts are "
                         f"unaffected; re-run with --debug for the full traceback")
            _log.debug("summary traceback", exc_info=True)

    timer.report()
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
