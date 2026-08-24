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

# Run each case in its own interpreter. Blocking a module that a previous test already
# imported would do nothing (it is in sys.modules), and unpicking that inside one process
# is exactly the kind of cleverness that ends in a vacuous pass.
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


def _run_isolated(blocked, body):
    script = _PREAMBLE % (_TREES, tuple(blocked)) + textwrap.dedent(body)
    return subprocess.run([sys.executable, "-c", script],
                          capture_output=True, text=True)


def test_the_blocker_actually_blocks():
    """Guard the guard: if this passes when it should not, every test below is vacuous."""
    proc = _run_isolated(["cobol_xstate"], "import cobol_xstate")
    assert proc.returncode != 0
    assert "BLOCKED cobol_xstate" in proc.stderr


def test_core_needs_neither_front_end():
    proc = _run_isolated(["cobol_xstate", "jcl_dependencies", "cobol_parser"], """
        import mainframe_artifacts
        from mainframe_artifacts.prefetch import Prefetcher, PrefetchResult
        from mainframe_artifacts.fetch import fetch_dependencies, build_fetch_plan
        from mainframe_artifacts.bundle import open_bundle, write_bundle
        from mainframe_artifacts.artifact_service import load_fetcher
        print("OK")
    """)
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout



def test_a_cobol_install_without_the_jcl_package_is_a_complete_install(tmp_path):
    """Everything except the one join keeps working, and a default run still writes all
    eight files."""
    proc = _run_isolated(["jcl_dependencies"], f"""
        import io, contextlib, os
        from cobol_xstate.cli import run
        out = {str(tmp_path / "o")!r}
        with contextlib.redirect_stderr(io.StringIO()):
            rc = run(["examples/accum.cbl", "--outdir", out, "-q"])
        assert rc == 0, rc
        print("FILES", len([f for f in os.listdir(out) if f.endswith(".json")]))
    """)
    assert proc.returncode == 0, proc.stderr
    # The full default run: the bundle, its five companion views, and both retrieval
    # reports - the eight JSON files CLAUDE.md describes, none of them JCL's to provide.
    assert "FILES 8" in proc.stdout


def test_bind_jcl_without_the_jcl_package_says_exactly_what_to_install(tmp_path):
    """The failure has to be loud. A manifest that was never bound looks FINE - its file
    rows say exactly what an unbound run's rows say - so this must not degrade quietly."""
    # The job file must EXIST: --bind-jcl checks the path before reaching for the JCL
    # package, and a missing file is its own exit-2 with a different (wrong, for this
    # test) message. Any job text will do - the point is the missing PACKAGE.
    job = tmp_path / "some.jcl"
    job.write_text("//J JOB\n//S EXEC PGM=IEFBR14\n", encoding="utf-8")
    proc = _run_isolated(["jcl_dependencies"], f"""
        import io, contextlib
        from cobol_xstate.cli import run
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = run(["examples/accum.cbl", "--outdir", {str(tmp_path / "o")!r},
                      "--bind-jcl", {str(job)!r}, "-q"])
        print("RC", rc)
        print(err.getvalue())
    """)
    assert proc.returncode == 0, proc.stderr
    assert "RC 2" in proc.stdout
    assert "jcl-dependencies" in proc.stdout
    assert "pip install cobol-xstate[jcl]" in proc.stdout


def test_the_parse_front_end_needs_no_modelling_engine():
    """cobol_parser is the standalone product of the split: source in, Program out, with
    the statechart engine genuinely absent - not merely unused."""
    proc = _run_isolated(["cobol_xstate", "jcl_dependencies"], """
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
        print("OK")
    """)
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


def test_importing_the_parse_package_does_not_pull_in_the_modelling_one():
    """The dependency points one way. If cobol_parser ever imported cobol_xstate - even
    lazily, even for one helper - every third-party parser consumer would silently carry
    the whole modelling engine."""
    proc = _run_isolated([], """
        import sys
        import cobol_parser
        # mainframe_artifacts is the legitimate dependency; only the modelling package
        # itself must be absent.
        assert "cobol_xstate" not in sys.modules, sorted(
            m for m in sys.modules
            if m == "cobol_xstate" or m.startswith("cobol_xstate."))
        print("OK")
    """)
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


def test_importing_the_cobol_package_does_not_pull_in_the_jcl_one():
    """bind.py reaches for the JCL package lazily. If any module imported it at the top
    level, the COBOL library would carry a dependency it does not need."""
    proc = _run_isolated([], """
        import sys
        import cobol_xstate
        import cobol_xstate.cli
        import cobol_xstate.api
        assert "jcl_dependencies" not in sys.modules, sorted(
            m for m in sys.modules if m.startswith("jcl_dependencies"))
        print("OK")
    """)
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


