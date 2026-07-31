"""The package boundaries, enforced rather than asserted in prose.

Three distributions ship from this tree: cobol_xstate_core (retrieval and the estate
boundary), cobol_xstate_jcl (JCL), and cobol_xstate (COBOL). The whole point of the split
is that the two front-ends are PEERS - a JCL install carries no COBOL modelling engine,
and neither imports the other - with core underneath both.

Nothing about the source layout enforces that; a single stray import would erase it while
every test still passed. So these tests import each package with the others genuinely
unavailable.

A note on how, because getting it wrong is easy and silent: ``sys.meta_path`` finders are
consulted through ``find_spec``. ``find_module`` was REMOVED in Python 3.12, so a blocker
that only defines it is ignored entirely and every one of these tests passes vacuously.
"""

import subprocess
import sys
import textwrap

import pytest

# Run each case in its own interpreter. Blocking a module that a previous test already
# imported would do nothing (it is in sys.modules), and unpicking that inside one process
# is exactly the kind of cleverness that ends in a vacuous pass.
_PREAMBLE = textwrap.dedent("""
    import sys
    for _tree in ("core/src", "cobol/src", "jcl/src"):
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
    script = _PREAMBLE % (tuple(blocked),) + textwrap.dedent(body)
    return subprocess.run([sys.executable, "-c", script],
                          capture_output=True, text=True)


def test_the_blocker_actually_blocks():
    """Guard the guard: if this passes when it should not, every test below is vacuous."""
    proc = _run_isolated(["cobol_xstate"], "import cobol_xstate")
    assert proc.returncode != 0
    assert "BLOCKED cobol_xstate" in proc.stderr


def test_core_needs_neither_front_end():
    proc = _run_isolated(["cobol_xstate", "cobol_xstate_jcl"], """
        import cobol_xstate_core
        from cobol_xstate_core.prefetch import Prefetcher, PrefetchResult
        from cobol_xstate_core.fetch import fetch_dependencies, build_fetch_plan
        from cobol_xstate_core.bundle import open_bundle, write_bundle
        from cobol_xstate_core.artifact_service import load_fetcher
        print("OK")
    """)
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


def test_the_jcl_package_needs_no_cobol_modelling_engine():
    """The environment-separation claim, at the code level: a JCL box that never
    installed the COBOL package can still parse a job and build both of its views."""
    proc = _run_isolated(["cobol_xstate"], """
        from cobol_xstate_jcl.api import analyze
        a = analyze("//J JOB\\n//S EXEC PGM=IEFBR14\\n//D DD DSN=A.B,DISP=SHR\\n",
                    retrieve=False)
        assert len(a.job.steps) == 1
        assert a.lineage()["datasets"]
        assert a.artifacts()["artifacts"]
        print("OK")
    """)
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


def test_a_cobol_install_without_the_jcl_package_is_a_complete_install(tmp_path):
    """Everything except the one join keeps working, and a default run still writes all
    eight files."""
    proc = _run_isolated(["cobol_xstate_jcl"], f"""
        import io, contextlib, os
        from cobol_xstate.cli import run
        out = {str(tmp_path / "o")!r}
        with contextlib.redirect_stderr(io.StringIO()):
            rc = run(["cobol/examples/accum.cbl", "--outdir", out, "-q"])
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
    proc = _run_isolated(["cobol_xstate_jcl"], f"""
        import io, contextlib
        from cobol_xstate.cli import run
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = run(["cobol/examples/accum.cbl", "--outdir", {str(tmp_path / "o")!r},
                      "--bind-jcl", "jcl/examples/acctunld.jcl", "-q"])
        print("RC", rc)
        print(err.getvalue())
    """)
    assert proc.returncode == 0, proc.stderr
    assert "RC 2" in proc.stdout
    assert "cobol-xstate-jcl" in proc.stdout
    assert "pip install cobol-xstate[jcl]" in proc.stdout


def test_importing_the_cobol_package_does_not_pull_in_the_jcl_one():
    """bind.py reaches for the JCL package lazily. If any module imported it at the top
    level, the COBOL library would carry a dependency it does not need."""
    proc = _run_isolated([], """
        import sys
        import cobol_xstate
        import cobol_xstate.cli
        import cobol_xstate.api
        assert "cobol_xstate_jcl" not in sys.modules, sorted(
            m for m in sys.modules if m.startswith("cobol_xstate_jcl"))
        print("OK")
    """)
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


@pytest.mark.parametrize("module", ["parser", "views", "prefetch", "api"])
def test_no_jcl_module_imports_a_front_end(module):
    """Read the source rather than the runtime: an import inside a rarely-taken branch
    would not show up in a passing import test."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "jcl" / "src" / "cobol_xstate_jcl"
           / f"{module}.py").read_text(encoding="utf-8")
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "cobol_xstate." not in stripped and "import cobol_xstate\n" not in stripped + "\n", \
                f"cobol_xstate_jcl/{module}.py imports the COBOL package: {stripped}"
