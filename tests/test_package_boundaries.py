"""The package boundaries, enforced rather than asserted in prose.

One distribution ships from this tree - cobol_xstate (the statechart modelling engine) -
over mainframe_artifacts (retrieval and the estate boundary) and cobol_parser (the COBOL
parse front-end) from the mainframe-common repository, with jcl_dependencies in its own
repository. The point of the split is that the two ESTATE front-ends (COBOL, JCL) are
peers - a JCL install carries no COBOL modelling engine, and neither imports the other -
with mainframe-artifacts underneath both, and that the PARSE front-end stands alone: other programs can
parse COBOL to a Program without carrying the modelling engine.

Nothing about the source layout enforces that; a single stray import would erase it while
every test still passed. So these tests import each package with the others genuinely
unavailable.

A note on how, because getting it wrong is easy and silent: ``sys.meta_path`` finders are
consulted through ``find_spec``. ``find_module`` was REMOVED in Python 3.12, so a blocker
that only defines it is ignored entirely and every one of these tests passes vacuously.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# The source trees the child interpreters run against: this repo's src plus the
# mainframe-artifacts/ and cobol-parser/ trees from the mainframe-common sibling checkout (override with
# MAINFRAME_COMMON_REPO - the same discovery conftest.py performs). When the
# distributions are pip-installed instead, the checkout paths simply do not exist and
# the inserts are inert - the installed packages carry the run.
_REPO = Path(__file__).resolve().parents[1]
_COMMON = Path(os.environ.get("MAINFRAME_COMMON_REPO",
                              _REPO.parent / "mainframe-common"))
_TREES = (str(_COMMON / "mainframe-artifacts" / "src"), str(_COMMON / "cobol-parser" / "src"),
          str(_REPO / "src"))

# Run the cases in fresh interpreters, never in pytest's own. Blocking a module that a
# previous test already imported would do nothing (it is in sys.modules), and unpicking
# that inside one process is exactly the kind of cleverness that ends in a vacuous pass.
# One interpreter per distinct blocked set, though, not per case: see `_CHILDREN`.
_PREAMBLE = textwrap.dedent("""
    import sys
    for _tree in %r:
        sys.path.insert(0, _tree)

    class Blocker:
        def __init__(self, *blocked):
            self.blocked = blocked
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in self.blocked:
                raise ImportError("BLOCKED " + name)
            return None

    sys.meta_path.insert(0, Blocker(*%r))
""")


#: One child per distinct blocked set: (blocked packages, {case: source}), the cases run
#: in order in that one interpreter, each check an ``assert``. ``{out}``, ``{bind_out}``
#: and ``{job}`` are filled in by the fixture.
_CHILDREN = {
    "modelling-blocked": (("cobol_xstate",), {
        # Guard the guard: this case must FAIL, with the blocker's message.
        "blocker": "import cobol_xstate\n",
    }),
    "front-ends-blocked": (("cobol_xstate", "jcl_dependencies", "cobol_parser"), {
        "core": """
            import mainframe_artifacts
            from mainframe_artifacts.prefetch import Prefetcher, PrefetchResult
            from mainframe_artifacts.fetch import fetch_dependencies, build_fetch_plan
            from mainframe_artifacts.bundle import open_bundle, write_bundle
            from mainframe_artifacts.artifact_service import load_fetcher
        """,
    }),
    "jcl-blocked": (("jcl_dependencies",), {
        "full_run": """
            import io, contextlib, os
            from cobol_xstate.cli import run
            out = {out!r}
            with contextlib.redirect_stderr(io.StringIO()):
                rc = run(["examples/accum.cbl", "--outdir", out, "-q"])
            assert rc == 0, rc
            # The full default run: the bundle, its five companion views, and both
            # retrieval reports - the eight JSON files CLAUDE.md describes, none of them
            # JCL's to provide.
            files = len([f for f in os.listdir(out) if f.endswith(".json")])
            assert files == 8, files
        """,
        "bind_jcl": """
            import io, contextlib
            from cobol_xstate.cli import run
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = run(["examples/accum.cbl", "--outdir", {bind_out!r},
                          "--bind-jcl", {job!r}, "-q"])
            assert rc == 2, rc
            assert "jcl-dependencies" in err.getvalue(), err.getvalue()
            assert "pip install cobol-xstate[jcl]" in err.getvalue(), err.getvalue()
        """,
    }),
    "modelling-and-jcl-blocked": (("cobol_xstate", "jcl_dependencies"), {
        "parse_front_end": """
            from cobol_parser import parse_program, prefetch_cobol, scan_copy_members
            prog = parse_program(
                "       IDENTIFICATION DIVISION.\\n"
                "       PROGRAM-ID. STANDALONE.\\n"
                "       PROCEDURE DIVISION.\\n"
                "       0000-MAIN.\\n"
                "           PERFORM 1000-A\\n"
                "           STOP RUN.\\n"
                "       1000-A.\\n"
                "           ADD 1 TO WS-N.\\n"
            )
            assert prog.program_id == "STANDALONE"
            assert [p.name for p in prog.paragraphs] == ["0000-MAIN", "1000-A"]
        """,
        "dynamic_call": """
            from cobol_parser import parse_program
            from cobol_parser.analysis import analyze_calls
            prog = parse_program(
                "       IDENTIFICATION DIVISION.\\n"
                "       PROGRAM-ID. DYNCALL.\\n"
                "       DATA DIVISION.\\n"
                "       WORKING-STORAGE SECTION.\\n"
                "       01 WS-PGM PIC X(8) VALUE 'PGMVALUE'.\\n"
                "       PROCEDURE DIVISION.\\n"
                "       0000-MAIN.\\n"
                "           CALL WS-PGM\\n"
                "           GOBACK.\\n"
            )
            res = analyze_calls(prog).resolve("WS-PGM")
            assert (res.confident, res.resolved) == (True, "PGMVALUE"), res
        """,
    }),
    # Nothing blocked: these assert a package is ABSENT, so the order is load-bearing.
    # The parse package's case runs first, before anything imports cobol_xstate, and
    # cobol_parser is not dropped between the two: the second only adds imports, and its
    # assertion is about jcl_dependencies.
    "nothing-blocked": ((), {
        "parse_alone": """
            import sys
            import cobol_parser
            # mainframe_artifacts is the legitimate dependency; only the modelling
            # package itself must be absent.
            assert "cobol_xstate" not in sys.modules, sorted(
                m for m in sys.modules
                if m == "cobol_xstate" or m.startswith("cobol_xstate."))
        """,
        "cobol_alone": """
            import sys
            import cobol_xstate
            import cobol_xstate.cli
            import cobol_xstate.api
            assert "jcl_dependencies" not in sys.modules, sorted(
                m for m in sys.modules if m.startswith("jcl_dependencies"))
        """,
    }),
}

#: What each child runs after the preamble: its cases in order, each inside its own
#: try/except so one failure stops none of the others. Before each case it drops the
#: modelling package's own modules, so each executes it from scratch.
_CHILD = textwrap.dedent("""
    import json, traceback

    def _fresh():
        for n in [n for n in sys.modules
                  if n == "cobol_xstate" or n.startswith("cobol_xstate.")]:
            del sys.modules[n]

    report = {}
    for key, body in CASES.items():
        _fresh()
        try:
            exec(body, {"__name__": "case_" + key})
        except BaseException:
            report[key] = traceback.format_exc()
        else:
            report[key] = None
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(report, fh)
""")


@pytest.fixture(scope="module")
def isolated(tmp_path_factory):
    """case -> its traceback, or None when it passed: one fresh interpreter per blocked
    set rather than per case, since blocking a module already in sys.modules does
    nothing but cases that block the same set can share a child."""
    tmp = tmp_path_factory.mktemp("boundaries")
    # The job file must EXIST: --bind-jcl checks the path before reaching for the JCL
    # package, and a missing file is its own exit-2 with a different (wrong, for this
    # case) message. Any job text will do - the point is the missing PACKAGE.
    job = tmp / "some.jcl"
    job.write_text("//J JOB\n//S EXEC PGM=IEFBR14\n", encoding="utf-8")
    values = {"out": str(tmp / "o"), "bind_out": str(tmp / "b"), "job": str(job)}
    report = {}
    for child, (blocked, cases) in _CHILDREN.items():
        out = tmp / (child + ".json")
        setup = "CASES = %r\nOUT = %r\n" % (
            {k: textwrap.dedent(v).format(**values) for k, v in cases.items()}, str(out))
        proc = subprocess.run([sys.executable, "-c",
                               _PREAMBLE % (_TREES, blocked) + setup + _CHILD],
                              capture_output=True, text=True)
        assert proc.returncode == 0 and out.exists(), (child, proc.stderr)
        report.update(json.loads(out.read_text(encoding="utf-8")))
    return report


def test_the_blocker_actually_blocks(isolated):
    """Guard the guard: if this passes when it should not, every test below is vacuous."""
    assert isolated["blocker"] is not None, "import cobol_xstate succeeded while blocked"
    assert "BLOCKED cobol_xstate" in isolated["blocker"]


def test_core_needs_neither_front_end(isolated):
    assert isolated["core"] is None, isolated["core"]


def test_a_cobol_install_without_the_jcl_package_is_a_complete_install(isolated):
    """Everything except the one join keeps working, and a default run still writes all
    eight files."""
    assert isolated["full_run"] is None, isolated["full_run"]


def test_bind_jcl_without_the_jcl_package_says_exactly_what_to_install(isolated):
    """The failure has to be loud. A manifest that was never bound looks FINE - its file
    rows say exactly what an unbound run's rows say - so this must not degrade quietly."""
    assert isolated["bind_jcl"] is None, isolated["bind_jcl"]


def test_the_parse_front_end_needs_no_modelling_engine(isolated):
    """cobol_parser is the standalone product of the split: source in, Program out, with
    the statechart engine genuinely absent - not merely unused."""
    assert isolated["parse_front_end"] is None, isolated["parse_front_end"]


def test_a_dynamic_call_resolves_with_no_modelling_engine(isolated):
    """The constant propagation that resolves `CALL WS-PGM` is pure over the parse, so a
    consumer that parses COBOL and models nothing resolves the same target the statechart
    does - rather than writing a second propagator that disagrees with this one."""
    assert isolated["dynamic_call"] is None, isolated["dynamic_call"]


def test_importing_the_parse_package_does_not_pull_in_the_modelling_one(isolated):
    """The dependency points one way. If cobol_parser ever imported cobol_xstate - even
    lazily, even for one helper - every third-party parser consumer would silently carry
    the whole modelling engine."""
    assert isolated["parse_alone"] is None, isolated["parse_alone"]


def test_importing_the_cobol_package_does_not_pull_in_the_jcl_one(isolated):
    """bind.py reaches for the JCL package lazily. If any module imported it at the top
    level, the COBOL library would carry a dependency it does not need."""
    assert isolated["cobol_alone"] is None, isolated["cobol_alone"]


