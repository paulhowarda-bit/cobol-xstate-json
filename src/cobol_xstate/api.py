"""The COBOL front-end as a library: analyze a program, get every view of it.

Everything the CLI does to a COBOL source lives here, so that driving this from another
Python program is the same code path the command line takes rather than a second one
that drifts. The CLI keeps what is genuinely its own - argument parsing, reading files
off disk, exit codes - and calls this.

:class:`Analysis` is the memoization the CLI used to open-code as a pair of closures. A
default run reaches the artifact manifest three times (the fetch stage, the
``.artifacts.json`` companion, the ``.dynamic-calls.json`` companion) and each call used
to rebuild it over the whole machine to produce the same object. The ordering inside
:meth:`Analysis.artifacts` is load-bearing and matches what the CLI did exactly: build,
then bind any JCL, then attribute stage-1 resolutions, then annotate from the dynamic
calls - which are themselves built from the PRE-annotation manifest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from cobol_parser.parse_bundle import ParseBundle, ParseBundleError
from mainframe_artifacts.bundle import EstateBundle, recording_fetcher, write_bundle
from mainframe_artifacts.fetch import fetch_dependencies
from mainframe_artifacts.output import write_json, write_text
from mainframe_artifacts.prefetch import PrefetchResult
from mainframe_artifacts.profiling import StageTimer

from . import PRODUCER
from .artifacts import build_artifacts
from .business import build_business_view
from .dynamic_calls import annotate_artifacts, build_dynamic_calls
from .emitter import emit_setup_module
from .lineage import build_lineage
from .normalizer import SourceFormat, detect_source_format
from .parser import parse_program
from .prefetch import attribute_resolution, prefetch_cobol
from .preprocessor import CopybookResolver
from .reactive import build_reactive_view, emit_reactive_module
from .statechart import Machine, build_machine

_log = logging.getLogger(__name__)

#: The extensions a copybook search tries, after any the caller adds.
DEFAULT_EXTS: Tuple[str, ...] = ("", ".cpy", ".CPY", ".cbl", ".cob", ".copy", ".CBL")


@dataclass
class Analysis:
    """One analyzed COBOL program, and every view that can be projected from it.

    The view builders are memoized because several of them are genuinely expensive and a
    default run asks for the same object more than once.
    """

    machine: Machine
    program: Any
    prefetch: PrefetchResult
    source_name: str = "<source>"
    fetch: Optional[dict] = None
    #: Parsed JCL jobs whose DD statements bind this program's file ddnames to datasets.
    bind_jobs: Sequence[Any] = ()
    #: Members the estate could not supply during the parse itself (see CopybookResolver).
    copybook_errors: Sequence[Tuple[str, str]] = ()

    _art: Optional[dict] = field(default=None, repr=False)
    _dyn: Optional[dict] = field(default=None, repr=False)
    _lin: Optional[dict] = field(default=None, repr=False)
    _bus: Optional[dict] = field(default=None, repr=False)
    _rea: Optional[dict] = field(default=None, repr=False)

    # -- the views ----------------------------------------------------------
    def _dynamic_from(self, manifest: dict) -> dict:
        """Built ONCE, from the pre-annotation manifest, and reused.

        The artifact manifest is annotated from this report, so the fetch plan inherits
        the pointer to whatever names an unresolvable target; it is also written as its
        own view. Both must be the same object."""
        if self._dyn is None:
            self._dyn = build_dynamic_calls(self.machine, manifest)
        return self._dyn

    def artifacts(self) -> dict:
        """Db2 tables, files, called programs and queues this program touches."""
        if self._art is None:
            art = build_artifacts(self.machine)
            if self.bind_jobs:
                from .bind import bind_manifest
                art = bind_manifest(art, self.bind_jobs)
            # Name the rows that exist only because stage 1 ran, so the improvement is
            # visible rather than implied...
            art = attribute_resolution(art, self.program, self.prefetch.store)
            # ...and tell the rows that CANNOT be resolved where their answer lives.
            self._art = annotate_artifacts(art, self._dynamic_from(art))
        return self._art

    def dynamic_calls(self) -> dict:
        """Targets this program does NOT name, and the artifact that does."""
        return self._dynamic_from(self.artifacts())

    def lineage(self) -> dict:
        """Field-level dataflow: (external event, field) -> origin event and guards.

        The fixpoint underneath is already memoized on the Machine, so this guard buys
        object identity rather than time - which is what a caller holding the view both
        to write it and to load it back actually needs."""
        if self._lin is None:
            self._lin = build_lineage(self.machine)
        return self._lin

    def business(self) -> dict:
        """Scaffolding collapsed to boundary, decision and calculation states.

        The only one of the three that genuinely recomputed end to end: it builds a fresh
        _BusinessView every call, over a Machine-cached interface."""
        if self._bus is None:
            self._bus = build_business_view(self.machine)
        return self._bus

    def reactive(self) -> dict:
        """May raise ``ReactiveLoweringError`` (a ``NotImplementedError``): the lowering
        refuses some programs, which is a fact about the program, not a failure.

        Cached on success only. A refusal leaves the field None and so re-raises on every
        call, which is correct: the refusal is a property of the program, not a failure
        to be remembered."""
        if self._rea is None:
            self._rea = build_reactive_view(self.machine)
        return self._rea

    def machine_json(self, *, machine_only: bool = False, indent: int = 2) -> str:
        return self.machine.to_json(machine_only=machine_only, indent=indent)

    def js_module(self) -> str:
        return emit_setup_module(self.machine)

    def reactive_module(self) -> str:
        return emit_reactive_module(self.machine)


def _resolve_format(source: str, fmt: Optional[SourceFormat],
                    source_name: str) -> SourceFormat:
    """The caller's format, or a detected one - saying which, and how sure.

    A silent wrong guess corrupts every downstream stage, so it is surfaced either way.
    """
    if fmt is not None:
        return fmt
    det = detect_source_format(source)
    level = "detected" if det.is_confident else "WARNING: low-confidence"
    _log.info(f"[{source_name}] {level} source format = {det.format.value} "
              f"({det.confidence:.0%}: {det.reason})")
    if not det.is_confident:
        _log.warning("  -> if the output looks corrupted, re-run with "
                     "--format fixed|free to override.")
    return det.format


def analyze(source: str, *, source_name: str = "<source>",
            fmt: Optional[SourceFormat] = None,
            bundle: Optional[EstateBundle] = None,
            parse: Optional[ParseBundle] = None,
            fetcher: Optional[Any] = None,
            retrieve: bool = True,
            paths: Sequence[str] = (), exts: Sequence[str] = (),
            dest: Optional[str] = None,
            unavailable: Optional[str] = None,
            jobs: int = 1,
            jcl: Sequence[Tuple[str, Any]] = (),
            synonyms: Optional[Dict[str, str]] = None,
            synonym_resolver: Optional[Callable[[str], Optional[str]]] = None,
            timer: Optional[StageTimer] = None) -> Analysis:
    """Retrieve, parse and model one COBOL program.

    Four ways to reach the estate, all explicit:

    ``fetcher=f``      ask the estate through ``f`` (the normal run)
    ``fetcher=None``   no client: resolve from ``paths`` only, and say per member in the
                       report that nothing was ever looked for
    ``retrieve=False`` retrieval deliberately OFF - reported as such, so it cannot be
                       mistaken for an estate that had nothing
    ``bundle=b``       replay a gathered estate; needs no network at all

    ``parse=p`` skips the parse: the ``Program`` comes rehydrated from a parse bundle
    written upfront by ``cobol-parser``. The bundle refuses a source whose hash is not
    the one it parsed - a stale Program is wrong everywhere at once, silently. It
    composes with ``bundle=`` (the estate replay) for a fully offline, parse-free run.

    ``jcl`` is ``[(name, text)]`` of JCL whose DD statements bind this program's file
    ddnames to datasets. Supplying it requires the optional JCL package.

    ``synonyms`` maps Db2 SYNONYM/ALIAS table names to their base tables - catalog
    knowledge supplied as input, never guessed. It lets a column-list-less INSERT
    written under a synonym find the base table's DECLARE TABLE / DCLGEN column order.
    ``synonym_resolver`` is the same knowledge as a callable the host supplies
    (``(name) -> base | None``, see ``mainframe_artifacts.protocol.SynonymResolver``),
    asked at the point of need for whatever the map does not hold; a resolver that
    raises is a flagged failed lookup, never "not a synonym".
    """
    timer = timer or StageTimer(_log, False, source_name)
    if parse is not None:
        parse.check_source(source, source_name=source_name)
        if fmt is not None and fmt != parse.fmt:
            raise ParseBundleError(
                f"format {fmt.value!r} was requested, but the parse bundle was "
                f"produced as {parse.fmt.value!r}; drop the override or re-run the "
                f"producer")
        fmt = parse.fmt
    else:
        fmt = _resolve_format(source, fmt, source_name)
    all_exts = tuple(exts) + DEFAULT_EXTS
    paths = list(paths)

    if bundle is not None:
        # The bundle IS the service. `unavailable` is carried across so the replay's
        # report says what the gather run's said rather than claiming a healthy estate
        # the gather run never had.
        fetcher = bundle.fetcher()
        unavailable = unavailable or bundle.unavailable
    elif not retrieve:
        fetcher = None
        unavailable = unavailable or ("retrieval was disabled for this run, so this "
                                      "member was never looked for")

    # STAGE 1, before the parse: the parse is what produces the dependency manifest, so a
    # copybook that does not arrive takes its VALUE clauses out of the model, and a
    # dynamic CALL proved by one of those then stays an unresolved runtime name - so the
    # program it calls never even becomes a row to fetch.
    with timer.stage("prefetch"):
        pre = prefetch_cobol(source, fetcher, paths=paths, dest=dest, fmt=fmt,
                             source_name=source_name, unavailable=unavailable,
                             exts=all_exts, jobs=jobs, producer=PRODUCER)

    if parse is not None:
        # The replay introduces no new branch downstream: the ONE call that differs is
        # parse_program, replaced by rehydration; build_machine and everything after it
        # sees the same Program a live parse would have produced (the byte-stability
        # gate proves "the same" byte for byte). The copybook errors are the producer
        # run's - there is no resolver here to have its own.
        with timer.stage("parse"):
            program = parse.program()
        copybook_errors: Tuple[Tuple[str, str], ...] = parse.copybook_errors
    else:
        resolver = CopybookResolver(
            paths=paths, exts=all_exts, fetcher=fetcher,
            store=pre.store,        # everything stage 1 retrieved, already paid for
        )
        with timer.stage("parse"):
            program = parse_program(source, fmt, resolver=resolver)
        copybook_errors = tuple(getattr(resolver, "fetch_errors", ()))
    with timer.stage("build_machine"):
        machine = build_machine(program, source_name=source_name, synonyms=synonyms,
                                synonym_resolver=synonym_resolver)

    # When timings are collected, force the two memoized analyses now so each is
    # attributed to its own line instead of to whichever companion touches it first. Both
    # run unconditionally later anyway, so this changes total work and emitted bytes by
    # nothing.
    if timer.enabled:
        with timer.stage("interface"):
            machine.interface()
        with timer.stage("lineage-fixpoint"):
            # Sub-spans inside the one stage the estate measured at 93% of runtime:
            # building the lineage graph (the split, its successors, the two condition
            # fixpoints), then the origins worklist, then the row emission.
            with timer.stage("lineage:build"):
                lin = machine.lineage(timer=timer)
            lin.run(timer=timer)

    bind_jobs: List[Any] = []
    if jcl:
        from .bind import bind_jobs as _bind
        with timer.stage("bind-jcl"):
            bind_jobs = _bind(jcl, fetcher=fetcher, paths=paths, dest=dest, result=pre,
                              unavailable=unavailable, jobs=jobs)

    analysis = Analysis(machine=machine, program=program, prefetch=pre,
                        source_name=source_name, bind_jobs=tuple(bind_jobs),
                        copybook_errors=copybook_errors)

    # STAGE 2. Unconditional: retrieving what this program depends on is not a mode of
    # the tool, it is what the tool does.
    with timer.stage("artifacts"):
        art = analysis.artifacts()
    with timer.stage("fetch"):
        analysis.fetch = fetch_dependencies(art, fetcher, dest=dest,
                                            prefetched=pre.store,
                                            unavailable=unavailable,
                                            dynamic=analysis.dynamic_calls(), jobs=jobs,
                                            producer=PRODUCER)
    return analysis


def gather(source: str, *, source_name: str = "<source>",
           fetcher: Optional[Any] = None,
           fmt: Optional[SourceFormat] = None,
           paths: Sequence[str] = (), exts: Sequence[str] = (),
           dest: str, jobs: int = 1,
           unavailable: Optional[str] = None,
           timer: Optional[StageTimer] = None) -> str:
    """Run the retrieval half where the estate is reachable, and keep what came off it.

    Both stages run - stage 2's plan needs the parse - but no view is written: the
    product is a directory a machine with no estate connection needs nothing else to
    model from. Returns the path to the bundle manifest.
    """
    recorder, answers = recording_fetcher(fetcher) if fetcher is not None else (None, [])
    # The timer goes through: a --gather-only --timing run used to report nothing,
    # because the analysis inside built its own disabled timer.
    analysis = analyze(source, source_name=source_name, fmt=fmt, fetcher=recorder,
                       paths=paths, exts=exts, dest=dest, jobs=jobs,
                       unavailable=unavailable, timer=timer)
    return write_bundle(dest, subject_name=source_name, subject_text=source,
                        kind="cobol", prefetch=analysis.prefetch, answers=answers,
                        fetch=analysis.fetch)


# -- the write half -----------------------------------------------------------------
#
# Publishing this beside analyze() is upstream ledger item 35: api.py used to publish the
# ANALYSIS half of a run only, so a program embedding this package could reach every view
# in memory and then had to reimplement the writing - the base-name derivation, the
# suffix per view, and the per-view error boundary - to leave behind what a run leaves
# behind. Reimplementing it means diverging from it silently on the next change here.

#: Every artifact a default run writes, in the order it writes them.
#:
#: The two retrieval reports are part of a run, not a mode of it: what was fetched decides
#: whether the machine is right, so a caller reproducing a run's output needs them.
DEFAULT_TARGETS: Tuple[str, ...] = (
    "prefetch", "fetch", "bundle", "business", "lineage", "reactive", "artifacts",
    "dynamic-calls",
)

#: The filename suffix each target is written under. Every artifact of one run is built
#: from the same base and a DISTINCT suffix, which is what keeps any one of them from
#: landing on another's path (``tests/test_api_write_views.py`` pins the distinctness).
_SUFFIX: Dict[str, str] = {
    "prefetch": ".prefetch.json",
    "fetch": ".fetch.json",
    "bundle": ".json",
    "business": ".business.json",
    "lineage": ".lineage.json",
    "reactive": ".reactive.json",
    "artifacts": ".artifacts.json",
    "dynamic-calls": ".dynamic-calls.json",
}

#: The two retrieval reports: a record of what the estate was asked for and what came
#: back, written like a view but not built like one.
_REPORTS = frozenset({"prefetch", "fetch"})

#: The views written behind their own error boundary. The bundle and the two reports are
#: the run's product - a failure there IS the failure of the run and must reach the
#: caller - while a companion that crashes leaves the usable artifacts usable.
#:
#: Once the bundle is on disk the run HAS usable output and must keep saying so: a batch
#: caller reads a failure as "no usable output" and discards the valid files it already
#: has (the SUMPGM01 false negative - a valid bundle + lineage thrown away over a crash in
#: a later view). A companion that CRASHES - as opposed to one the lowering REFUSES, which
#: is handled a line above it - is therefore a loud warning naming the view and the
#: reason, never a failure of the run.
_ISOLATED = frozenset({"business", "lineage", "reactive", "artifacts", "dynamic-calls"})


def artifact_base(stem: Optional[str], program_id: Optional[str] = None) -> str:
    """The shared base name every artifact of one run is built from.

    Derived from the SOURCE stem, never by chopping a written filename at its first dot -
    a source called ``MY.PROG.cbl`` would otherwise yield companions named ``MY.*``, and
    one called ``X.business.cbl`` would have its bundle silently overwritten by the
    business view landing on the same path. A source with no usable stem (stdin) falls
    back to the PROGRAM-ID, which is why this takes both.
    """
    return stem or program_id or "machine"


def _source_stem(source_name: Optional[str]) -> Optional[str]:
    """The stem to build artifact names from, or None if this source has no filename.

    ``<stdin>`` and ``<source>`` are placeholders, not paths: a run reading from a pipe
    has no stem and falls back to the PROGRAM-ID, exactly as the CLI does.
    """
    if not source_name or source_name.startswith("<"):
        return None
    return Path(source_name).stem


def write_views(analysis: Analysis, dest, *, base: Optional[str] = None,
                targets: Optional[Sequence[str]] = None, indent: int = 2,
                machine_only: bool = False, timer: Optional[StageTimer] = None,
                debug: bool = False) -> Dict[str, Path]:
    """Write the run's artifacts for ``analysis`` into ``dest``; return ``{name: Path}``.

    ``base`` defaults to the same derivation the CLI uses (the source stem, else the
    PROGRAM-ID); ``targets`` to :data:`DEFAULT_TARGETS`, the set a default run writes. A
    caller wanting only the bundle and lineage passes those two, and nothing else is
    computed - the views are built here, one at a time, as each is written.

    ``machine_only`` trims the BUNDLE to the machine alone (what ``--machine-only``
    writes). It does not change the target set: pass ``targets`` for that.

    Per-view failures are isolated exactly as the CLI isolates them. A companion that
    CRASHES is a warning naming the view; a ``reactive`` view the lowering REFUSES is a
    note, because the refusal is a fact about the program rather than a failure of the
    run. Either way that name is absent from the returned mapping and the rest of the
    run's artifacts are still written. The bundle and the two retrieval reports are not
    isolated - they are the run's product, and a failure there is the run's failure.
    ``debug=True`` re-raises instead of isolating (what ``--debug`` does).
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if base is None:
        base = artifact_base(_source_stem(analysis.source_name),
                             analysis.machine.program_id)
    wanted = DEFAULT_TARGETS if targets is None else tuple(targets)
    unknown = [t for t in wanted if t not in _SUFFIX]
    if unknown:
        raise ValueError(f"unknown target(s) {', '.join(sorted(unknown))}; "
                         f"known targets are {', '.join(DEFAULT_TARGETS)}")
    timer = timer or StageTimer(_log, False, analysis.source_name)

    # Built lazily, one per target, so `targets` really does decide what is COMPUTED and
    # not merely what is written: the views are the expensive half of a run.
    def _view(name: str):
        if name == "prefetch":
            return analysis.prefetch.report()
        if name == "fetch":
            return analysis.fetch
        return getattr(analysis, name.replace("-", "_"))()

    written: Dict[str, Path] = {}
    for name in DEFAULT_TARGETS:          # a fixed order, never the caller's iteration
        if name not in wanted:
            continue
        path = dest / (base + _SUFFIX[name])
        try:
            if name in _REPORTS:
                # Retrieval already happened, inside analyze(); writing its record is not
                # a view build and takes no `view:<name>` stage, so a --timing run reads
                # the same here as it does from the CLI.
                write_json(path, _view(name), indent)
            else:
                # A timing line per view: one number over six view builds, six
                # serializations and six writes could not say which view a slow run was
                # slow in. `views` (the CLI's total) is the sum of these.
                with timer.stage(f"view:{name}"):
                    if name == "bundle":
                        write_text(path, analysis.machine_json(machine_only=machine_only,
                                                               indent=indent) + "\n")
                    else:
                        write_json(path, _view(name), indent)
        except Exception as exc:
            if name == "reactive" and isinstance(exc, NotImplementedError):
                # The lowering REFUSES some programs (CICS handler regions, recursive
                # PERFORM). That is a fact about the program rather than a failure of the
                # run, so it stays a note even under debug: there is no defect here to
                # get a traceback for. From any other view a NotImplementedError is a
                # defect, and takes the boundary below like any other crash.
                _log.info(f"[{analysis.source_name}] note: no reactive view - {exc}")
                continue
            if debug or name not in _ISOLATED:
                raise
            _log.warning(f"[{analysis.source_name}] WARNING: {name} view failed "
                         f"({type(exc).__name__}: {exc}) - the other artifacts of "
                         f"this run are unaffected; re-run with --debug for the "
                         f"full traceback")
            _log.debug("companion view traceback", exc_info=True)
            continue
        _log.info(f"[{analysis.source_name}] wrote {path}")
        written[name] = path
    return written
