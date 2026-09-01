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
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from cobol_parser.parse_bundle import ParseBundle, ParseBundleError
from mainframe_artifacts.bundle import EstateBundle, recording_fetcher, write_bundle
from mainframe_artifacts.fetch import fetch_dependencies
from mainframe_artifacts.prefetch import PrefetchResult
from mainframe_artifacts.profiling import StageTimer

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
        return build_lineage(self.machine)

    def business(self) -> dict:
        return build_business_view(self.machine)

    def reactive(self) -> dict:
        """May raise ``ReactiveLoweringError`` (a ``NotImplementedError``): the lowering
        refuses some programs, which is a fact about the program, not a failure."""
        return build_reactive_view(self.machine)

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
                             exts=all_exts, jobs=jobs)

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
            machine.lineage().run()

    bind_jobs: List[Any] = []
    if jcl:
        from .bind import bind_jobs as _bind
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
                                            dynamic=analysis.dynamic_calls(), jobs=jobs)
    return analysis


def gather(source: str, *, source_name: str = "<source>",
           fetcher: Optional[Any] = None,
           fmt: Optional[SourceFormat] = None,
           paths: Sequence[str] = (), exts: Sequence[str] = (),
           dest: str, jobs: int = 1,
           unavailable: Optional[str] = None) -> str:
    """Run the retrieval half where the estate is reachable, and keep what came off it.

    Both stages run - stage 2's plan needs the parse - but no view is written: the
    product is a directory a machine with no estate connection needs nothing else to
    model from. Returns the path to the bundle manifest.
    """
    recorder, answers = recording_fetcher(fetcher) if fetcher is not None else (None, [])
    analysis = analyze(source, source_name=source_name, fmt=fmt, fetcher=recorder,
                       paths=paths, exts=exts, dest=dest, jobs=jobs,
                       unavailable=unavailable)
    return write_bundle(dest, subject_name=source_name, subject_text=source,
                        kind="cobol", prefetch=analysis.prefetch, answers=answers,
                        fetch=analysis.fetch)
