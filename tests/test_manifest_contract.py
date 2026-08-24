"""The artifact manifest is a contract between two distributions that release apart.

This package PRODUCES the manifest; the JCL package CONSUMES it (bind_cobol_artifacts
joins a program's file ddnames to the datasets a job binds), and core consumes it too
(fetch decides what to retrieve from the rows). Once those ship separately, nothing stops
a change here from silently breaking a consumer - and the breakage is invisible in the
worst way: an unbound manifest looks FINE, because its file rows say exactly what an
unbound run's rows say.

So the JCL package keeps a committed copy of a real manifest as its fixture, and this
test regenerates it. A schema change now fails HERE, in the repository that made it,
before it can be released to a consumer that cannot parse it.

When this fails and the change is intended: regenerate the fixture, check the JCL
package's tests still pass against it, and release mainframe-artifacts/COBOL before the JCL side
depends on the new shape.
"""

import json
from pathlib import Path

import pytest

from cobol_xstate.artifacts import build_artifacts
from cobol_xstate.parser import parse_program
from cobol_xstate.statechart import build_machine

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
# The consumer's copy lives in the jcl-dependencies REPOSITORY now (a sibling checkout
# by default; point JCL_DEPENDENCIES_REPO elsewhere). Absent checkout -> the drift
# check skips - the fast in-suite guard is the subset/join tests below, and the drift
# check still runs wherever both repos are checked out together.
import os

FIXTURE = (Path(os.environ.get("JCL_DEPENDENCIES_REPO",
                               Path(__file__).resolve().parents[2] / "jcl-dependencies"))
           / "tests" / "fixtures" / "sqlunld.artifacts.json")


def _manifest():
    src = (EXAMPLES / "sqlunld.cbl").read_text()
    return build_artifacts(build_machine(parse_program(src), source_name="sqlunld.cbl"))


@pytest.mark.skipif(not FIXTURE.exists(),
                    reason="no jcl-dependencies checkout beside this repo")
def test_the_consumers_fixture_still_matches_what_this_package_produces():
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert _manifest() == expected, (
        f"The COBOL artifact manifest has changed shape. {FIXTURE.name} is the copy the "
        f"JCL package binds against - regenerate it, re-run that package's tests, and "
        f"release this side FIRST.")


def test_the_rows_the_join_depends_on_are_present():
    """Named explicitly, so a change that drops one fails with the reason rather than a
    diff. bind_cobol_artifacts matches on kind == 'file' and the ddname."""
    rows = _manifest()["artifacts"]
    files = [r for r in rows if r.get("kind") == "file"]
    assert files, "no file rows: the ddname->DSN join would have nothing to bind"
    for row in files:
        assert "ddname" in row, f"{row.get('artifact')} has no ddname to join on"
        assert row.get("identity") == "program-local", (
            "a ddname is program-local by definition; 'global' would mean the join is "
            "unnecessary, and the JCL side skips those rows")


def test_core_has_an_opinion_about_every_kind_this_package_emits():
    """core.fetch routes on `kind`, and there are exactly two right answers: retrieve it
    as some type (_KIND_TYPE), or deliberately never retrieve it and say why
    (_NEVER_FETCHABLE, which is how `caller` and `spool` are handled).

    A kind in NEITHER table is the dangerous case. It is not an error - the row is simply
    skipped, and reads afterwards as an estate that had nothing.
    """
    from mainframe_artifacts.fetch import _KIND_TYPE, _NEVER_FETCHABLE

    kinds = set()
    for name in ("sqlunld.cbl", "mqcall.cbl", "cicsinq.cbl", "banktran.cbl",
                 "db2diag.cbl", "custrpt.cbl"):
        src = (EXAMPLES / name).read_text()
        m = build_artifacts(build_machine(parse_program(src), source_name=name))
        kinds |= {r.get("kind") for r in m["artifacts"]}
    unrouted = {k for k in kinds if k not in _KIND_TYPE and k not in _NEVER_FETCHABLE}
    assert not unrouted, (
        f"kinds core.fetch has no opinion about: {sorted(unrouted)} - add them to "
        f"_KIND_TYPE to retrieve them, or to _NEVER_FETCHABLE with the reason. Left in "
        f"neither, their rows are silently skipped and read as an estate gap.")
