"""The `Analysis` view surface: every builder is asked at most once.

The class docstring promises "the view builders are memoized because several of them are
genuinely expensive and a default run asks for the same object more than once." Three of
the five did not follow it (upstream ledger batch 10, item 29). These tests pin the promise
so the next view added inherits it.

Worth recording what measurement showed, because the ledger's stated reason was wrong and
someone will otherwise re-derive it: `lineage()` was ALREADY returning the same object,
memoized one layer down on the Machine (`statechart._lineage_cache`, `_Lineage._view`), and
`reactive()`'s expensive lowering was already cached too (`_lowered_cache`). Only
`business()` genuinely recomputed end to end. The guards here buy object identity, which is
what a caller holding a view both to write it and to load it back needs; they do not buy
back a fixpoint that was never being recomputed.
"""

from pathlib import Path

import pytest

import cobol_xstate.api
from cobol_xstate.api import analyze
from cobol_xstate.errors import ReactiveLoweringError

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
SOURCE = (EXAMPLES / "custrpt.cbl").read_text()
#: Its reactive lowering refuses - a CICS handler region lowers to type:parallel.
REFUSED_SOURCE = (EXAMPLES / "cicsinq.cbl").read_text()


def _analysis(source: str = SOURCE, name: str = "custrpt.cbl"):
    return analyze(source, source_name=name, retrieve=False)


def test_each_view_is_built_once():
    """Asking twice returns the same object, so an expensive builder runs once."""
    a = _analysis()
    assert a.lineage() is a.lineage()
    assert a.business() is a.business()
    assert a.reactive() is a.reactive()
    assert a.artifacts() is a.artifacts()          # already true; pinned so it stays true
    assert a.dynamic_calls() is a.dynamic_calls()  # likewise


def test_the_lineage_builder_is_called_once(monkeypatch):
    calls = []
    real = cobol_xstate.api.build_lineage
    monkeypatch.setattr(cobol_xstate.api, "build_lineage",
                        lambda m: (calls.append(1), real(m))[1])
    a = _analysis()
    a.lineage()
    a.lineage()
    assert len(calls) == 1


def test_the_business_builder_is_called_once(monkeypatch):
    """The one that genuinely recomputed: _BusinessView is rebuilt per call."""
    calls = []
    real = cobol_xstate.api.build_business_view
    monkeypatch.setattr(cobol_xstate.api, "build_business_view",
                        lambda m, b: (calls.append(1), real(m, b))[1])
    a = _analysis()
    a.business()
    a.business()
    assert len(calls) == 1


def test_a_refused_reactive_lowering_raises_every_time():
    """A refusal is a fact about the program, not a cached failure.

    Caching on success only is what makes this correct without special-casing: the field
    stays None when the builder raises, so the next call re-raises rather than returning a
    remembered failure - or, worse, None."""
    a = _analysis(REFUSED_SOURCE, "cicsinq.cbl")
    with pytest.raises(ReactiveLoweringError):
        a.reactive()
    with pytest.raises(ReactiveLoweringError):
        a.reactive()


def test_a_memoized_view_is_not_shared_between_analyses():
    """The cache is per-Analysis. Two runs of the same source are two objects, because a
    caller that mutates one must not reach into the other."""
    assert _analysis().lineage() is not _analysis().lineage()


def test_a_contained_programs_dependencies_are_requested_from_the_estate():
    """Ledger item 55, end to end: what a contained program calls and reads reaches the
    fetch stage as this member's dependency, and a program contained here is never
    asked for."""
    asked = []

    def fetcher(name, type=None, copy=None):
        asked.append(name.upper())
        return {"artifact_name": name, "found": False, "detected_type": None}

    a = analyze((EXAMPLES / "nestdeps.cbl").read_text(), source_name="nestdeps.cbl",
                fetcher=fetcher)
    # LOGWRITE is what NESTSQL passes NESTLOG to call: requested like any program.
    assert {"EXTMOD", "INNERMOD", "OUTERMOD", "ACCOUNT_MASTER", "LOGDD",
            "LOGWRITE"} <= set(asked)
    assert not {"NESTSQL", "NESTLOG", "NESTDEPS", "LK-PGM"} & set(asked)
    status = {r["artifact"]: r["status"] for r in a.fetch["artifacts"]}
    assert status["NESTLOG"] == status["NESTSQL"] == "skipped"
    assert "LK-PGM" not in status


def _not_found(log):
    def fetcher(name, type=None, copy=None):
        log.append((name.upper(), type))
        return {"artifact_name": name, "found": False, "detected_type": None}
    return fetcher


def test_each_fetch_row_says_which_contained_program_it_is_for():
    """A contained program's manifest rows are tagged with it; the fetch rows built from
    them used to drop the tag, so the report could not say whose dependency each was."""
    a = analyze((EXAMPLES / "nestdeps.cbl").read_text(), source_name="nestdeps.cbl",
                fetcher=_not_found([]))
    manifest = [(r.get("containedProgram"), r["artifact"])
                for r in a.artifacts()["artifacts"]]
    fetched = [(r.get("containedProgram"), r["artifact"]) for r in a.fetch["artifacts"]]
    assert fetched == manifest
    assert ("NESTLOG", "LOGWRITE") in fetched and (None, "OUTERMOD") in fetched


def test_a_callee_of_two_programs_of_one_member_is_asked_for_once():
    """NESTDEPS's GLOBAL WS-TARGET says OUTERMOD, and NESTLOG calls through it too: one
    manifest row per program (ledger item 55 never merges rows by name), one request to
    the estate, and a fetch row per program saying whose it is."""
    src = (EXAMPLES / "nestdeps.cbl").read_text()
    src = src.replace("01  WS-TARGET  PIC X(8) VALUE 'OUTERMOD'.",
                      "01  WS-TARGET  PIC X(8) GLOBAL VALUE 'OUTERMOD'.")
    src = src.replace("           CALL LK-PGM\n",
                      "           CALL LK-PGM\n           CALL WS-TARGET\n")
    asked = []
    a = analyze(src, source_name="nestdup.cbl", fetcher=_not_found(asked))
    assert [r.get("containedProgram") for r in a.artifacts()["artifacts"]
            if r["artifact"] == "OUTERMOD"] == [None, "NESTLOG"]
    assert asked.count(("OUTERMOD", "cobol")) == asked.count(("OUTERMOD", "asm")) == 1
    rows = [r for r in a.fetch["artifacts"] if r["artifact"] == "OUTERMOD"]
    assert [(r.get("containedProgram"), r["status"]) for r in rows] == [
        (None, "not-found"), ("NESTLOG", "not-found")]
    assert "already requested in this run" in rows[1]["reason"]


def test_a_contained_programs_dynamic_call_candidates_say_whose_call_it_is():
    """Both programs have a WS-PGM that two literals reach, so `forDynamicCall` alone
    cannot say whose call a candidate is for; the contained program's carry its name."""
    src = """\
       IDENTIFICATION DIVISION.
       PROGRAM-ID. OUTPICK.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-PGM  PIC X(8).
       01  WS-FLAG PIC X.
       PROCEDURE DIVISION.
       0000-MAIN.
           IF WS-FLAG = 'A'
              MOVE 'OUTONE' TO WS-PGM
           ELSE
              MOVE 'OUTTWO' TO WS-PGM
           END-IF
           CALL WS-PGM
           CALL 'INPICK'
           GOBACK.
       IDENTIFICATION DIVISION.
       PROGRAM-ID. INPICK.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-PGM  PIC X(8).
       01  WS-FLAG PIC X.
       PROCEDURE DIVISION.
       1000-PICK.
           IF WS-FLAG = 'A'
              MOVE 'INONE' TO WS-PGM
           ELSE
              MOVE 'INTWO' TO WS-PGM
           END-IF
           CALL WS-PGM
           GOBACK.
       END PROGRAM INPICK.
       END PROGRAM OUTPICK.
"""
    a = analyze(src, source_name="nestpick.cbl", fetcher=_not_found([]))
    got = {r["artifact"]: (r["forDynamicCall"], r.get("containedProgram"))
           for r in a.fetch["artifacts"] if r.get("forDynamicCall")}
    assert got == {"OUTONE": ("WS-PGM", None), "OUTTWO": ("WS-PGM", None),
                   "INONE": ("WS-PGM", "INPICK"), "INTWO": ("WS-PGM", "INPICK")}


# ledger item 137: COPY of a member named like the program never copies the program
_SELFCOPY = """\
       IDENTIFICATION DIVISION.
       PROGRAM-ID. SELFCOPY.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WORK-AREAS.
           05  WS-COUNT        PIC 9(4).
       LINKAGE SECTION.
       01  DFHCOMMAREA.
           COPY SELFCOPY.
       PROCEDURE DIVISION USING DFHCOMMAREA.
       0000-MAIN.
           MOVE CA-KEY TO WS-COUNT
           GOBACK.
"""
_SELFCOPY_CPY = "           05  CA-KEY          PIC 9(4).\n"


def _serving_selfcopy(name, type=None, copy=None):
    if name.upper() == "SELFCOPY":
        return {"found": True, "text": _SELFCOPY_CPY,
                "source_location": "LIB(SELFCOPY)"}
    return {"artifact_name": name, "found": False, "detected_type": None}


def test_a_copy_of_the_programs_own_name_gets_the_member_not_the_source(tmp_path):
    src = tmp_path / "SELFCOPY.cbl"
    src.write_text(_SELFCOPY)
    a = analyze(_SELFCOPY, source_name=src.name, paths=[str(tmp_path)],
                fetcher=_serving_selfcopy, source_path=str(src))
    data = a.machine.data
    assert data["CA-KEY"]["section"] == "LINKAGE"
    assert data["CA-KEY"]["member"] == "SELFCOPY"
    assert data["WS-COUNT"]["section"] == "WORKING-STORAGE"
    assert not [k for k, v in data.items()
                if v.get("member") == "SELFCOPY" and k in ("WORK-AREAS", "WS-COUNT")]


def test_without_the_source_path_the_old_self_copy_still_happens(tmp_path):
    """The control: the path sweep finding the source is what the guard stops."""
    src = tmp_path / "SELFCOPY.cbl"
    src.write_text(_SELFCOPY)
    a = analyze(_SELFCOPY, source_name=src.name, paths=[str(tmp_path)],
                fetcher=_serving_selfcopy)
    assert "CA-KEY" not in a.machine.data
