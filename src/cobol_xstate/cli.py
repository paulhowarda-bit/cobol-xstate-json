"""Command-line entry point: COBOL file -> XState v5 JSON statechart."""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path
from typing import List, Optional

from .errors import CobolXstateError
from .logging_setup import configure_logging
from .artifacts import build_artifacts
from .business import build_business_view
from .emitter import emit_setup_module
from .artifact_service import DEFAULT_FETCHER, decode_member, load_fetcher
from .fetch import fetch_dependencies
from .dynamic_calls import annotate_artifacts, build_dynamic_calls
from .prefetch import attribute_resolution, prefetch_cobol, prefetch_jcl
from .jcl import parse_jcl
from .jcl_views import bind_cobol_artifacts, build_jcl_artifacts, build_jcl_lineage
from .lineage import build_lineage
from .normalizer import SourceFormat, detect_source_format
from .reactive import build_reactive_view, emit_reactive_module
from .parser import parse_program
from .preprocessor import CopybookResolver
from .runtime_assets import read_runtime_asset
from .statechart import build_machine
from .profiling import StageTimer

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

    Derived from the SOURCE stem, never by chopping a written filename at its first dot -
    a source called ``MY.PROG.cbl`` would otherwise yield companions named ``MY.*``, and
    one called ``X.business.cbl`` would have its bundle silently overwritten by the
    business view landing on the same path.
    """
    return default_stem or program_id or "machine"


def _run_dir(args) -> Path:
    """The one directory this run writes into: exactly ``--outdir``, as given.

    Everything lands here - the bundle, every companion view, both retrieval reports, the
    retrieved artifacts (under ``deps/``), and the JS runtime. ``--outdir`` is taken
    literally: the path you give is the path files appear in, with nothing appended.
    There is deliberately no second mechanism that can place a file somewhere else."""
    return Path(args.outdir)


def _make_run_dir(run_dir: Path) -> Optional[str]:
    """Create the run directory, or return the message explaining why we cannot.

    ``exist_ok=True`` only forgives an existing DIRECTORY, so pointing --outdir at an
    existing regular file raised FileExistsError out of main() as a raw traceback -
    while the neighbouring bad-path cases all report cleanly and exit 2."""
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
    except (FileExistsError, NotADirectoryError):
        return f"--outdir {run_dir} exists and is not a directory"
    except OSError as exc:
        return f"cannot create --outdir {run_dir}: {exc}"
    return None


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
    p.add_argument("--outdir", default="out", metavar="DIR",
                   help="directory for output (default: ./out). EVERY file this run "
                        "produces goes here, exactly as given with nothing appended - "
                        "the bundle, all six views, both retrieval reports, and the "
                        "artifacts retrieved from the estate (under deps/). Created "
                        "with parents if it does not exist.")
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
    p.add_argument("--copybook-fetcher", "--fetcher", dest="copybook_fetcher",
                   metavar="MODULE:FUNC",
                   help=f"override the estate artifact service. Every run retrieves its "
                        f"dependencies through {DEFAULT_FETCHER} by default - only the "
                        f"estate knows where its members live - so this is needed only "
                        f"for a differently-named client. FUNC(name, type=, copy=) may "
                        f"return the member text, (text, source), or a dict with "
                        f"text/path/copied_to/detected_type/alternatives")
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
    p.add_argument("--indent", type=int, default=2, help="JSON indent (default: 2)")
    p.add_argument("--summary", action="store_true",
                   help="print a human-readable summary to stderr")
    p.add_argument("--timing", action="store_true",
                   help="print per-stage wall-clock timings to stderr (diagnostic; "
                        "does not affect any output file)")
    p.add_argument("-v", "--verbose", action="count", default=0,
                   help="increase log detail: -v adds DEBUG (swallowed tracebacks and "
                        "internal steps). Diagnostics go to stderr; stdout is unaffected.")
    p.add_argument("-q", "--quiet", action="count", default=0,
                   help="reduce log detail: -q shows only warnings and errors (hides "
                        "progress), -qq shows only errors.")
    p.add_argument("--debug", action="store_true",
                   help="on an unexpected internal error, print the full Python traceback "
                        "instead of a one-line message (for bug reports). Implies -v.")
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


def _run_jcl(args, source: str, source_name: str, default_stem: Optional[str],
              paths: List[str]) -> int:
    """Parse a JCL job / PROC and emit its lineage + artifact manifest.

    Stage 1 runs first and must: a cataloged PROC, an INCLUDE member and a control-card
    dataset each carry ``EXEC PGM=`` steps and DD statements that appear nowhere in the
    JCL file itself. Parsed without them - as this path used to - those steps do not
    show up as programs, as datasets, or at all, and the job reads as far simpler than
    it is."""
    import json as _json

    timer = StageTimer(_log, args.timing, source_name)
    fetcher, why = _service(args, source_name)
    base = _artifact_base(args, default_stem, "job")
    # Same ordering constraint as the COBOL path: prefetch writes into the run directory,
    # so the directory's name must be known before anything is parsed. The JOB/PROC name
    # is on the first statement, so scan for it.
    run_dir = _run_dir(args)
    err = _make_run_dir(run_dir)
    if err:
        _log.error(f"error: {err}")
        return 2
    deps = str(run_dir / "deps")
    # `paths`, not args.copybook_path: run() already appended the JCL file's own parent,
    # which is where a cataloged PROC or INCLUDE member most often sits. Passing only the
    # -I list meant a PROC beside the job was never found locally, and every EXEC step and
    # DD inside it silently vanished from the model.
    with timer.stage("prefetch"):
        pre = prefetch_jcl(source, fetcher, paths=list(paths),
                           dest=deps, source_name=source_name, unavailable=why)

    with timer.stage("parse"):
        job = parse_jcl(source, resolver=pre.resolver(), source_name=source_name)
    with timer.stage("jcl-lineage"):
        lineage = build_jcl_lineage(job)
    with timer.stage("jcl-artifacts"):
        artifacts = build_jcl_artifacts(job)
    base = _artifact_base(args, default_stem, job.name or "job")
    with timer.stage("fetch"):
        fetched = fetch_dependencies(artifacts, fetcher, dest=deps,
                                     prefetched=pre.store, unavailable=why)

    for suffix, obj in ((".jcl.artifacts.json", artifacts),
                        (".jcl.lineage.json", lineage),
                        (".jcl.prefetch.json", pre.report()),
                        (".jcl.fetch.json", fetched)):
        path = run_dir / f"{base}{suffix}"
        path.write_text(_json.dumps(obj, indent=args.indent) + "\n", encoding="utf-8")
        _log.info(f"[{source_name}] wrote {path}")
    _report_stages(source_name, pre, fetched)

    if args.summary:
        _log.info(f"[{job.name or 'JOB'}] {len(job.steps)} step(s), "
              f"{len(lineage['datasets'])} dataset(s), "
              f"{len(lineage['dataflow'])} dataflow edge(s), "
              f"{len(lineage['fieldLineage'])} field-lineage step(s), "
              f"{len(job.flags)} flag(s)")
        for f in job.flags:
            _log.info(f"  FLAG {f}")
    timer.report()
    return 0


def _report_stages(source_name: str, pre, fetched: dict) -> None:
    """One line per stage on stderr, plus the holes named individually.

    The holes get named rather than counted because every downstream view is read as if
    it were complete. A member that did not arrive is the reason a dynamic CALL stayed
    unresolved or a job looks like it has fewer steps than it runs, and a reader who
    cannot see which member that was has no way to tell an accurate model from a short
    one."""
    pc, fc = pre.counts, fetched.get("counts", {})
    _log.info(f"[{source_name}] prefetch: "
          f"{pc.get('fetched', 0)} fetched, {pc.get('local', 0)} local, "
          f"{pc.get('not-found', 0)} not-found, {pc.get('error', 0)} error"
          + (f", {pc.get('no-service', 0)} never looked for"
             if pc.get("no-service") else ""))
    _log.info(f"[{source_name}] fetch: "
          f"{fc.get('fetched', 0)} fetched, {fc.get('prefetched', 0)} already in hand, "
          f"{fc.get('not-found', 0)} not-found, {fc.get('error', 0)} error, "
          f"{fc.get('skipped', 0)} not fetchable")
    for member in pre.missing:
        _log.warning(f"  MISSING {member}: the source text is incomplete without it - data "
              f"items or steps it defines are NOT in the model")
    for err in fetched.get("errors", []):
        _log.warning(f"  FETCH ERROR {err['artifact']}: {err['error']}")


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




def run(argv: Optional[List[str]] = None) -> int:
    """Parse args, configure logging, and dispatch, behind the top-level error boundary.

    An expected failure (any ``CobolXstateError``) becomes a one-line message + a non-zero
    exit code; an UNEXPECTED exception is reported as an internal error (exit 1) with the
    full traceback shown only under ``--debug`` - never leaked raw to the user."""
    args = build_parser().parse_args(argv)
    configure_logging(verbose=args.verbose or (1 if args.debug else 0), quiet=args.quiet)
    try:
        return _run(args)
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
    except Exception:
        if args.debug:
            raise  # the developer asked for the raw traceback
        _log.critical("internal error while processing %r - re-run with --debug for the "
                      "full traceback", args.source)
        _log.debug("internal error traceback", exc_info=True)
        return 1


def _run(args) -> int:
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
        return _run_jcl(args, source, source_name, default_stem, search_paths)

    timer = StageTimer(_log, args.timing, source_name)
    default_exts = ("", ".cpy", ".CPY", ".cbl", ".cob", ".copy", ".CBL")
    fmt = _format(args.format)
    if fmt is None:
        det = detect_source_format(source)
        fmt = det.format
        # A silent wrong guess corrupts every downstream stage, so surface it: state
        # what was picked, and warn (recommending --format) when confidence is low.
        level = "detected" if det.is_confident else "WARNING: low-confidence"
        _log.info(f"[{source_name}] {level} source format = {fmt.value} "
              f"({det.confidence:.0%}: {det.reason})")
        if not det.is_confident:
            _log.warning("  -> if the output looks corrupted, re-run with "
                  "--format fixed|free to override.")

    # STAGE 1. Before the parse, because the parse is what produces the dependency
    # manifest: a copybook that does not arrive takes its VALUE clauses out of the
    # model, and a dynamic CALL target proved by one of those clauses then stays an
    # unresolved runtime name - so the program it calls is never even a row to fetch.
    fetcher, why = _service(args, source_name)
    # The run directory has to be settled BEFORE stage 1, because stage 1 writes into it.
    # Hence the PROGRAM-ID scan rather than machine.program_id, which does not exist yet.
    run_dir = _run_dir(args)
    err = _make_run_dir(run_dir)
    if err:
        _log.error(f"error: {err}")
        return 2
    deps = str(run_dir / "deps")
    with timer.stage("prefetch"):
        pre = prefetch_cobol(source, fetcher, paths=search_paths, dest=deps, fmt=fmt,
                             source_name=source_name, unavailable=why,
                             exts=tuple(args.copybook_ext) + default_exts)

    resolver = CopybookResolver(
        paths=search_paths,
        exts=tuple(args.copybook_ext) + default_exts,
        fetcher=fetcher,
        store=pre.store,        # everything stage 1 retrieved, already paid for
    )
    with timer.stage("parse"):
        program = parse_program(source, fmt, resolver=resolver)
    with timer.stage("build_machine"):
        machine = build_machine(program, source_name=source_name)
    # Under --timing, force the two memoized analyses now so each is attributed to its
    # own line instead of to whichever companion writer happens to touch it first. Both
    # run unconditionally later anyway (stage 2 builds the interface via build_artifacts
    # and the lineage fixpoint via the dynamic-calls view), so pre-warming changes total
    # work and emitted bytes by nothing.
    if args.timing:
        with timer.stage("interface"):
            machine.interface()
        with timer.stage("lineage-fixpoint"):
            machine.lineage().run()

    # A copybook fetcher that ERRORED is not the same as a member that does not exist:
    # the model is missing logic for a fixable reason (bad credentials, service down),
    # so say so loudly rather than letting it read as "not on the estate".
    for member, err in getattr(resolver, "fetch_errors", []):
        _log.warning(f"[{source_name}] WARNING: copybook fetcher failed for {member}: {err}")

    # --bind-jcl: parse each JCL once; the artifacts view is then built through the
    # binding join so its file rows carry the dataset their ddname resolves to. Each is
    # prefetched first, into the SAME store: a ddname the program opens is very often
    # contributed by a PROC rather than by the JCL file itself, so an unresolved PROC
    # here does not merely lose steps - it loses the ddname->DSN binding that is the
    # entire reason for passing the JCL.
    bind_jobs = []
    for jf in args.bind_jcl:
        jp = Path(jf)
        if not jp.exists():
            _log.error(f"error: no such file: {jp} (--bind-jcl)")
            return 2
        jtext = decode_member(jp.read_bytes())
        prefetch_jcl(jtext, fetcher, paths=search_paths, dest=deps,
                     source_name=jp.name, unavailable=why, result=pre)
        bind_jobs.append(parse_jcl(jtext, resolver=pre.resolver(),
                                   source_name=jp.name))

    # The true dynamic calls and where their targets are named. Built once: the artifact
    # manifest is annotated FROM it (so the fetch plan inherits the pointer too), and it
    # is written as its own view.
    dyn_report = None

    def _dynamic_obj(art):
        nonlocal dyn_report
        if dyn_report is None:
            dyn_report = build_dynamic_calls(machine, art)
        return dyn_report

    art_report = None

    def _artifacts_obj():
        # Memoized like its sibling above: a default run reaches this three times (stage
        # 2, the .artifacts.json companion, the .dynamic-calls.json companion) and each
        # call re-ran build_artifacts + bind + attribute + annotate over the whole
        # machine to produce the same object.
        nonlocal art_report
        if art_report is None:
            art = build_artifacts(machine)
            if bind_jobs:
                art = bind_cobol_artifacts(art, bind_jobs)
            # Name the rows that exist only because stage 1 ran, so the improvement is
            # visible rather than implied.
            art = attribute_resolution(art, program, pre.store)
            # ...and tell the rows that CANNOT be resolved where their answer lives.
            art_report = annotate_artifacts(art, _dynamic_obj(art))
        return art_report

    base = _artifact_base(args, default_stem, machine.program_id)
    out_path = _resolve_out_path(args, base, run_dir)

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
            _log.info(f"[{source_name}] note: companion would collide with {beside.name}; "
                  f"writing {path.name} instead")
        _write(path, _json.dumps(obj, indent=args.indent) + "\n")
        _log.info(f"[{source_name}] wrote {path}")

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
        name still needs. A logistics view of the same boundary the interface recovers.
        With --bind-jcl, file rows carry the dataset their ddname resolves to."""
        if args.machine_only or args.no_artifacts:
            return
        _companion(beside, ".artifacts.json", _artifacts_obj())

    def _write_dynamic_companion(beside: Path) -> None:
        """The true dynamic calls: targets this program does NOT name, and the artifact
        that does. Written even when empty - "this program has no unresolvable dynamic
        calls" is a real and reassuring answer, and its absence would be ambiguous
        between that and the view not having run."""
        if args.machine_only or args.no_dynamic_calls:
            return
        _companion(beside, ".dynamic-calls.json", _dynamic_obj(_artifacts_obj()))

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
            _log.info(f"[{source_name}] note: no reactive view - {exc}")
            return
        _companion(beside, ".reactive.json", view)

    # STAGE 2. Unconditional: retrieving what this program depends on is not a mode of
    # the tool, it is what the tool does. Run before the views are written so the two
    # reports land even when a later view refuses.
    with timer.stage("artifacts"):
        _art = _artifacts_obj()
    with timer.stage("fetch"):
        report = fetch_dependencies(_art, fetcher, dest=deps, prefetched=pre.store,
                                    unavailable=why, dynamic=_dynamic_obj(_art))
    # --machine-only suppresses the REPORTS, never the retrieval: what was fetched
    # decides whether the machine is right, so skipping it to save two files would be
    # backwards.
    if not args.machine_only:
        for suffix, obj in ((".prefetch.json", pre.report()), (".fetch.json", report)):
            path = out_path.with_name(f"{base}{suffix}")
            _write(path, _json.dumps(obj, indent=args.indent) + "\n")
            _log.info(f"[{source_name}] wrote {path}")
    _report_stages(source_name, pre, report)

    _t_views = timer.start()
    if args.target in ("business", "lineage", "artifacts"):
        obj = (build_lineage(machine) if args.target == "lineage"
               else _artifacts_obj() if args.target == "artifacts"
               else build_business_view(machine))
        _write(out_path, _json.dumps(obj, indent=args.indent) + "\n")
        _log.info(f"[{source_name}] wrote {out_path}")
        if args.target == "business":
            _write_lineage_companion(out_path)
    elif args.target in ("js", "reactive"):
        try:
            text = (emit_reactive_module(machine) if args.target == "reactive"
                    else emit_setup_module(machine))
        except NotImplementedError as exc:
            # An explicit --target reactive on a program the lowering refuses: report the
            # reason, not a traceback. The refusal is a fact about the program.
            _log.error(f"error: {exc}")
            return 3
        _write(out_path, text)
        # The emitted module imports ./cobolRuntime.mjs, so the runtime must land beside
        # it. It ships as package data; a missing asset means a broken install and raises
        # rather than emitting a dangling import.
        runtime_dst = out_path.parent / "cobolRuntime.mjs"
        _write(runtime_dst, read_runtime_asset("cobolRuntime.mjs"))
        _log.info(f"[{source_name}] wrote {out_path}")
        _log.info(f"[{source_name}] wrote {runtime_dst}")
        # The reactive machine is the one you most want to LOOK at - its waits and
        # publishes are the new system's message contract - so it gets a drawable JSON
        # beside the runnable module, like every other machine view.
        if args.target == "reactive":
            view = out_path.with_name(base + ".reactive.json")
            _write(view, _json.dumps(build_reactive_view(machine),
                                     indent=args.indent) + "\n")
            _log.info(f"[{source_name}] wrote {view}")
    else:
        text = machine.to_json(machine_only=args.machine_only, indent=args.indent)
        _write(out_path, text + "\n")
        _log.info(f"[{source_name}] wrote {out_path}")
        # A plain run yields the six JSON views of one program, each answering a
        # different question: the faithful machine (what it does), the business
        # distillation (which steps matter), the lineage table (where each field's value
        # came from), the reactive machine (what replaces it), the related artifacts
        # (what else it touches), and the dynamic calls (what it invokes but will not
        # name). All are things you READ or DRAW - the runnable modules stay behind their
        # own --target. Each is opt-out-able.
        _write_business_companion(out_path)
        _write_lineage_companion(out_path)
        _write_reactive_companion(out_path)
        _write_artifacts_companion(out_path)
        _write_dynamic_companion(out_path)

    timer.since("views", _t_views)
    if args.summary:
        n_states = len(machine.config.get("states", {}))
        iface = machine.bundle()["interface"]
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

    timer.report()
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
