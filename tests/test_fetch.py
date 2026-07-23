"""Stage 7: retrieving every dependent artifact - called programs, copybooks, DDL,
mapsets, control members - through a caller-supplied estate service, and reporting
honestly which rows were never fetchable in the first place."""

import json

from cobol_xstate.artifacts import build_artifacts
from cobol_xstate.fetch import build_fetch_plan, fetch_dependencies
from cobol_xstate.parser import parse_program
from cobol_xstate.preprocessor import CopybookResolver
from cobol_xstate.statechart import build_machine

CALLEE = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. DCIOC104.\n"
    "       PROCEDURE DIVISION.\n"
    "       0000-MAIN.\n"
    "           EXEC SQL SELECT BAL INTO :WS-BAL FROM ACCOUNT END-EXEC\n"
    "           CALL 'AUDITLOG'\n"
    "           GOBACK.\n"
)
LEAF = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. AUDITLOG.\n"
    "       PROCEDURE DIVISION.\n"
    "       0000-MAIN.\n"
    "           DISPLAY 'AUDIT'\n"
    "           GOBACK.\n"
)
STORE = {
    "DCIOC104": CALLEE,
    "AUDITLOG": LEAF,
    "ACCOUNT": "CREATE TABLE ACCOUNT (BAL DECIMAL(11,2));\n",
    "MENUMAP": "MENUMAP  DFHMSD TYPE=MAP\n",
    "CUSTCPY": "       01 CUST-REC PIC X(80).\n",
}


def _fetcher(store=None, log=None):
    data = STORE if store is None else store

    def fetch(name, type=None):
        if log is not None:
            log.append((name, type))
        text = data.get(name.upper())
        if text is None:
            return {"artifact_name": name, "found": False}
        return {"artifact_name": name, "found": True, "text": text,
                "source_path": rf"\\share\{name}"}
    return fetch


def _manifest(proc_body: str, data_body: str = "", env_body: str = "",
              resolver=None) -> dict:
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. MAINPGM.\n"
        "       ENVIRONMENT DIVISION.\n" + env_body +
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n" + data_body +
        "       PROCEDURE DIVISION.\n" + proc_body
    )
    return build_artifacts(build_machine(parse_program(src, resolver=resolver)))


def _by(report, artifact):
    return next(r for r in report["artifacts"] if r["artifact"] == artifact)


# --------------------------------------------------------------------------- #
# the plan: what WOULD be fetched, and what is not fetchable at all
# --------------------------------------------------------------------------- #

def test_plan_covers_every_artifact_kind_with_its_retrieval_type():
    man = _manifest(
        "       0000-MAIN.\n"
        "           CALL 'DCIOC104'\n"
        "           EXEC SQL SELECT BAL INTO :WS-B FROM ACCOUNT END-EXEC\n"
        "           EXEC CICS SEND MAP('MENUMAP') END-EXEC\n"
        "           EXEC CICS WRITEQ TS QUEUE('ERRQ') END-EXEC.\n",
        data_body="       01 WS-B PIC 9(5).\n")
    plan = {p["artifact"]: p for p in build_fetch_plan(man)}
    # A called program carries NO assumed language: it is probed in likelihood order, and
    # whichever request retrieves it is the finding.
    assert plan["DCIOC104"]["type"] is None
    assert plan["DCIOC104"]["probeTypes"] == ["cobol", "asm"]
    assert plan["ACCOUNT"]["type"] == "ddl"
    assert plan["MENUMAP"]["type"] == "bms"
    assert plan["ERRQ"]["type"] == "csd"
    assert all(p["status"] == "planned"
               for p in (plan["DCIOC104"], plan["ACCOUNT"], plan["MENUMAP"]))


def test_a_file_is_requested_by_its_ddname_not_the_program_local_name():
    man = _manifest(
        "       0000-MAIN.\n"
        "           OPEN INPUT CNTL-FILE\n"
        "           READ CNTL-FILE AT END CONTINUE END-READ.\n",
        env_body="       INPUT-OUTPUT SECTION.\n"
                 "       FILE-CONTROL.\n"
                 "           SELECT CNTL-FILE ASSIGN TO CNTLDD.\n")
    row = next(p for p in build_fetch_plan(man) if p["artifact"] == "CNTL-FILE")
    assert row["status"] == "planned"
    assert row["request"] == "CNTLDD"          # and NOT "CNTLDD." - see parser fix
    assert row["requestedAs"] == "ddname"


def test_a_file_with_no_ddname_is_skipped_with_the_reason():
    man = _manifest(
        "       0000-MAIN.\n"
        "           READ MYSTERY-FILE AT END CONTINUE END-READ.\n")
    row = next(p for p in build_fetch_plan(man) if p["artifact"] == "MYSTERY-FILE")
    assert row["status"] == "skipped"
    assert "no ddname or dataset" in row["reason"]
    assert "--bind-jcl" in row["reason"]


def test_a_dynamic_name_is_never_fetched():
    """WS-UNKNOWN is a data item. Requesting it would retrieve nothing - or worse, an
    unrelated member that happens to share the name."""
    man = _manifest(
        "       0000-MAIN.\n"
        "           MOVE WS-OTHER TO WS-UNKNOWN\n"
        "           CALL WS-UNKNOWN.\n",
        data_body="       01 WS-UNKNOWN PIC X(8).\n"
                  "       01 WS-OTHER   PIC X(8).\n")
    row = next(p for p in build_fetch_plan(man) if p["artifact"] == "WS-UNKNOWN")
    assert row["status"] == "skipped"
    assert "data item" in row["reason"]


def test_caller_and_spool_are_skipped():
    man = _manifest(
        "       0000-MAIN.\n"
        "           DISPLAY 'HI'.\n")
    plan = {p["artifact"]: p for p in build_fetch_plan(man)}
    assert plan["SYSOUT"]["status"] == "skipped"
    assert "runtime destination" in plan["SYSOUT"]["reason"]


# --------------------------------------------------------------------------- #
# fetching, including the transitive walk
# --------------------------------------------------------------------------- #

def test_fetches_every_kind_and_records_the_source():
    man = _manifest(
        "       0000-MAIN.\n"
        "           CALL 'DCIOC104'\n"
        "           EXEC CICS SEND MAP('MENUMAP') END-EXEC.\n")
    rep = fetch_dependencies(man, _fetcher())
    assert _by(rep, "DCIOC104")["status"] == "fetched"
    assert _by(rep, "DCIOC104")["source"] == r"\\share\DCIOC104"
    assert _by(rep, "MENUMAP")["status"] == "fetched"
    assert rep["counts"]["fetched"] == 2


def test_only_immediate_dependencies_are_fetched():
    """MAINPGM -> DCIOC104 -> (ACCOUNT, AUDITLOG). Only DCIOC104 is this program's
    dependency; what DCIOC104 in turn needs is a question about DCIOC104, answered by
    running the tool on it - with its own prefetch and its own complete parse. Walking
    on from here would answer it from a parse that had never been prefetched, which is
    exactly the shortfall stage 1 exists to prevent."""
    man = _manifest(
        "       0000-MAIN.\n"
        "           CALL 'DCIOC104'.\n")
    rep = fetch_dependencies(man, _fetcher())
    assert [r["artifact"] for r in rep["artifacts"] if r["status"] == "fetched"] \
        == ["DCIOC104"]


def test_one_member_is_one_round_trip_however_many_rows_reach_it():
    """A JCL job that both EXECs PAYPROC and names it as a called program produces two
    rows for one member. Asking the estate twice for the same thing is waste that scales
    with the estate, so the second row reports what happened instead of repeating it."""
    calls = []
    man = {"program": "PAYJOB", "artifacts": [
        {"artifact": "AUDITLOG", "kind": "program", "dependency": "runtime"},
        {"artifact": "AUDITLOG", "kind": "proc", "dependency": "compile-time"},
    ]}
    rep = fetch_dependencies(man, _fetcher(log=calls))
    assert [n for n, _ in calls].count("AUDITLOG") == 1
    assert [r["status"] for r in rep["artifacts"]] == ["fetched", "already-fetched"]


def test_a_prefetched_member_is_reported_but_not_requested_again():
    """Stage 1 already paid for it. It still appears in the report - a reader tracing
    what this run retrieved needs the whole list - but the service is not asked twice."""
    calls = []
    man = _manifest(
        "       0000-MAIN.\n"
        "           CALL 'DCIOC104'.\n")
    rep = fetch_dependencies(man, _fetcher(log=calls),
                             prefetched={"DCIOC104": ("...text...", "PROD.SRCLIB")})
    row = _by(rep, "DCIOC104")
    assert row["status"] == "prefetched"
    assert row["source"] == "PROD.SRCLIB"
    assert calls == []


def test_not_found_is_distinct_from_skipped():
    man = _manifest(
        "       0000-MAIN.\n"
        "           CALL 'NOSUCHPG'.\n")
    rep = fetch_dependencies(man, _fetcher())
    assert _by(rep, "NOSUCHPG")["status"] == "not-found"


def test_no_service_is_distinct_from_not_found():
    """Nothing was looked for, so nothing being found says nothing about the estate.
    Reporting this as 'not-found' would manufacture evidence of absence."""
    man = _manifest(
        "       0000-MAIN.\n"
        "           CALL 'DCIOC104'.\n")
    rep = fetch_dependencies(man, None, unavailable="no client installed")
    row = _by(rep, "DCIOC104")
    assert row["status"] == "no-service"
    assert "no client installed" in row["reason"]


def test_a_failing_fetcher_is_reported_not_fatal():
    def boom(name, type=None, copy=None):
        raise ConnectionError("share unreachable")

    man = _manifest(
        "       0000-MAIN.\n"
        "           CALL 'DCIOC104'.\n")
    rep = fetch_dependencies(man, boom)
    row = _by(rep, "DCIOC104")
    assert row["status"] == "error"
    assert "ConnectionError" in row["error"]
    assert rep["errors"] and rep["errors"][0]["artifact"] == "DCIOC104"


def test_a_fetcher_without_a_type_keyword_still_works():
    seen = []

    def name_only(name):            # no `type=` / `copy=` in the signature
        seen.append(name)
        return STORE.get(name.upper())

    man = _manifest(
        "       0000-MAIN.\n"
        "           CALL 'DCIOC104'.\n")
    rep = fetch_dependencies(man, name_only)
    assert _by(rep, "DCIOC104")["status"] == "fetched"
    assert seen == ["DCIOC104"]


def test_fetched_artifacts_are_collected_and_usable_as_a_search_path(tmp_path):
    man = _manifest(
        "       0000-MAIN.\n"
        "           CALL 'DCIOC104'\n"
        "           EXEC SQL SELECT BAL INTO :WS-B FROM ACCOUNT END-EXEC.\n",
        data_body="       01 WS-B PIC 9(5).\n")
    rep = fetch_dependencies(man, _fetcher(), dest=str(tmp_path))
    names = {p.name for p in tmp_path.iterdir()}
    assert {"DCIOC104.cbl", "ACCOUNT.sql"} <= names
    assert _by(rep, "DCIOC104")["copiedTo"].endswith("DCIOC104.cbl")


def test_the_service_type_wins_over_our_guess_and_the_disagreement_is_recorded():
    """The first probe is cobol; the estate hands the member back but reports it as an
    assembler module. The estate is looking at the member and we are looking at one
    program's usage of a name, so its answer wins - and the disagreement is a finding
    worth surfacing, not a discrepancy to smooth over."""
    def asm(name, type=None, copy=None):
        return {"artifact_name": name, "found": True, "text": "         CSECT\n",
                "detected_type": "asm", "source_location": "PROD.ASMLIB(DCIOC104)"}

    rep = fetch_dependencies(
        _manifest("       0000-MAIN.\n           CALL 'DCIOC104'.\n"), asm)
    row = _by(rep, "DCIOC104")
    assert row["detectedType"] == "asm"
    assert "requested as cobol" in row["typeNote"]
    assert row["language"] == "asm"
    assert row["languageBasis"] == "estate detected_type"


def test_a_called_program_absent_from_the_cobol_library_is_found_as_asm():
    """The core fix: a CALL says nothing about the callee's language. When the member is
    NOT in the cobol library but IS in the asm library, the probe (cobol -> asm) finds it
    as asm and says so - proven by where it lives, not assumed from the CALL. Before, the
    hardcoded type=cobol made the cobol request the only one, so the ASM module came back
    not-found or mislabelled cobol."""
    def estate(name, type=None, copy=None):
        if type == "asm":
            return {"artifact_name": name, "found": True, "text": "DCIOC104 CSECT\n",
                    "source_location": "PROD.ASMLIB(DCIOC104)"}
        return {"artifact_name": name, "found": False}      # absent from the cobol library

    rep = fetch_dependencies(
        _manifest("       0000-MAIN.\n           CALL 'DCIOC104'.\n"), estate)
    row = _by(rep, "DCIOC104")
    assert row["status"] == "fetched"
    assert row["language"] == "asm"
    assert "asm" in row["languageBasis"] and "cobol not present" in row["languageBasis"]


def test_an_estate_language_synonym_is_normalised_and_not_a_false_disagreement(tmp_path):
    """An estate that answers 'ASSEMBLER' (or HLASM) means asm: the member saves under
    .asm, the language is 'asm', and asm-vs-assembler is NOT reported as a type
    disagreement (the first probe requested cobol, which IS the real disagreement here)."""
    def estate(name, type=None, copy=None):
        return {"artifact_name": name, "found": True, "text": "X CSECT\n",
                "detected_type": "ASSEMBLER", "source_location": "PROD.ASMLIB(DCIOC104)"}

    rep = fetch_dependencies(
        _manifest("       0000-MAIN.\n           CALL 'DCIOC104'.\n"), estate,
        dest=str(tmp_path))
    row = _by(rep, "DCIOC104")
    assert row["language"] == "asm"                          # folded from ASSEMBLER
    assert row["copiedTo"].endswith("DCIOC104.asm")          # saved under the asm extension
    assert {p.name for p in tmp_path.iterdir()} == {"DCIOC104.asm"}


def test_alternatives_are_recorded_so_the_syslib_choice_is_visible():
    """The same member name in three libraries is the SYSLIB-order ambiguity. Which one
    resolved is a fact; that two others could have is a fact the reader needs too."""
    def many(name, type=None, copy=None):
        return {"artifact_name": name, "found": True, "text": "X\n",
                "source_location": "PROD.SRCLIB(DCIOC104)",
                "alternatives": ["TEST.SRCLIB(DCIOC104)", "DEV.SRCLIB(DCIOC104)"]}

    rep = fetch_dependencies(
        _manifest("       0000-MAIN.\n           CALL 'DCIOC104'.\n"), many)
    row = _by(rep, "DCIOC104")
    assert row["source"] == "PROD.SRCLIB(DCIOC104)"
    assert row["alternatives"] == ["TEST.SRCLIB(DCIOC104)", "DEV.SRCLIB(DCIOC104)"]


def test_a_control_member_is_requested_out_of_its_dataset():
    """`PARM.LIB(SORTCRD)` names a member inside a library. The member is what can be
    requested; which library holds it is the service's business."""
    man = {"artifacts": [{"artifact": "PARM.LIB(SORTCRD)", "kind": "control-card",
                          "dependency": "runtime"}]}
    row = build_fetch_plan(man)[0]
    assert row["status"] == "planned"
    assert row["request"] == "SORTCRD"
    assert row["requestedAs"] == "member"


def test_report_shape_is_self_describing():
    rep = fetch_dependencies(
        _manifest("       0000-MAIN.\n           CALL 'DCIOC104'.\n"), _fetcher())
    assert rep["format"] == "cobol-xstate-fetch"
    assert rep["program"] == "MAINPGM"
    assert "note" in rep and "counts" in rep and "errors" in rep


# --------------------------------------------------------------------------- #
# the parser fix this stage depends on
# --------------------------------------------------------------------------- #

def test_assign_ddname_drops_the_sentence_period():
    prog = parse_program(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. T.\n"
        "       ENVIRONMENT DIVISION.\n"
        "       INPUT-OUTPUT SECTION.\n"
        "       FILE-CONTROL.\n"
        "           SELECT CNTL-FILE ASSIGN TO CNTLDD.\n"
        "       DATA DIVISION.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           OPEN INPUT CNTL-FILE.\n")
    assert prog.files["CNTL-FILE"]["assign"] == "CNTLDD"


def test_two_calls_in_one_sentence_are_two_dependencies():
    """A following CALL used to be consumed as the previous CALL's trailing tokens,
    losing an entire program dependency from every view."""
    man = _manifest(
        "       0000-MAIN.\n"
        "           CALL 'DCIOC104'\n"
        "           CALL 'AUDITLOG'.\n")
    assert {a["artifact"] for a in man["artifacts"] if a["kind"] == "program"} \
        == {"DCIOC104", "AUDITLOG"}


def test_call_with_using_still_binds_its_arguments():
    """...and the fix must not steal a CALL's own USING list."""
    prog = parse_program(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. T.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01 WS-A PIC X.\n"
        "       01 WS-B PIC X.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           CALL 'SUBPGM' USING WS-A WS-B\n"
        "           CALL 'OTHER'.\n")
    from cobol_xstate.model import CallStmt, walk_statements
    calls = [s for s in walk_statements(prog.paragraphs[0].statements)
             if isinstance(s, CallStmt)]
    assert [c.target for c in calls] == ["SUBPGM", "OTHER"]
    assert calls[0].using == ["WS-A", "WS-B"]
    assert calls[1].using == []


def test_assign_literal_keeps_its_dots():
    prog = parse_program(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. T.\n"
        "       ENVIRONMENT DIVISION.\n"
        "       INPUT-OUTPUT SECTION.\n"
        "       FILE-CONTROL.\n"
        "           SELECT F ASSIGN TO 'PROD.CNTL.FILE'.\n"
        "       DATA DIVISION.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           OPEN INPUT F.\n")
    assert prog.files["F"]["assign"] == "PROD.CNTL.FILE"


# A copybook shared by many members costing one round-trip is now stage 1's guarantee,
# and is tested there: see test_prefetch.py::test_a_member_reached_many_ways_is_fetched_once


# --------------------------------------------------------------------------- #
# retrieving the plan concurrently
#
# The plan is fully known before any of it runs - build_fetch_plan is pure - so there was
# never a reason to discover the second request only after the first had come back. That
# cost 6.5s of a measured 19.5s run. The report is what must not move: its row order is
# part of its output, so it follows the PLAN and never the order the estate answered in.
# --------------------------------------------------------------------------- #


def _slow_fetcher(peak, log=None):
    import random
    import threading
    import time
    state = {"now": 0}
    lock = threading.Lock()

    def fetch(name, type=None):
        with lock:
            state["now"] += 1
            peak[0] = max(peak[0], state["now"])
            if log is not None:
                log.append((name, type))
        try:
            time.sleep(random.uniform(0.002, 0.02))
            if name.upper() == "BOOM":
                raise RuntimeError("the estate refused this request")
            text = STORE.get(name.upper())
            if text is None:
                return {"artifact_name": name, "found": False}
            return {"artifact_name": name, "found": True, "text": text,
                    "source_path": rf"\share\{name}"}
        finally:
            with lock:
                state["now"] -= 1
    return fetch


# Every branch the recording pass can take, in one plan: two fetchable programs, a table,
# a member the estate lacks, one whose request fails outright, a duplicate name, the
# subject itself, a row that was never fetchable, and one stage 1 already paid for.
MIXED = {"program": "MAINPGM", "artifacts": [
    {"artifact": "DCIOC104", "kind": "program", "dependency": "runtime"},
    {"artifact": "ACCOUNT", "kind": "db2-table", "dependency": "runtime"},
    {"artifact": "GONE", "kind": "program", "dependency": "runtime"},
    {"artifact": "BOOM", "kind": "program", "dependency": "runtime"},
    {"artifact": "AUDITLOG", "kind": "program", "dependency": "runtime"},
    {"artifact": "DCIOC104", "kind": "copybook", "dependency": "compile-time"},
    {"artifact": "MAINPGM", "kind": "program", "dependency": "runtime"},
    {"artifact": "CALLER", "kind": "caller", "dependency": "runtime"},
    {"artifact": "CUSTCPY", "kind": "copybook", "dependency": "compile-time"},
]}
PRE = {"CUSTCPY": ("       01 CUST-REC PIC X(80).\n", "PROD.COPYLIB")}


def test_the_plan_is_fetched_concurrently_and_still_reported_in_plan_order():
    peak = [0]
    seq = fetch_dependencies(MIXED, _slow_fetcher(peak), prefetched=PRE, jobs=1)
    assert peak[0] == 1, "jobs=1 must not start a second request; it is the escape hatch"

    for attempt in range(5):
        peak[0] = 0
        par = fetch_dependencies(MIXED, _slow_fetcher(peak), prefetched=PRE, jobs=8)
        assert json.dumps(par, indent=2) == json.dumps(seq, indent=2), (
            f"attempt {attempt}: the report followed the estate's timing, not the plan")
        assert peak[0] > 1, "nothing actually overlapped - the test proves nothing"

    assert [r["status"] for r in seq["artifacts"]] == [
        "fetched", "fetched", "not-found", "error", "fetched",
        "already-fetched", "skipped", "skipped", "prefetched"]


def test_a_duplicate_name_is_still_one_round_trip_when_requests_overlap():
    """The check that made this safe sequentially - is this name already done? - is a
    read-then-write, and two threads would both find it absent and both ask. So duplicates
    collapse when the plan is read, before anything is dispatched."""
    calls = []
    rep = fetch_dependencies(MIXED, _slow_fetcher([0], log=calls), prefetched=PRE, jobs=8)
    assert [n for n, _ in calls].count("DCIOC104") == 1
    # ...and the row that did NOT cause the round-trip is still the second one
    dup = [r for r in rep["artifacts"] if r["artifact"] == "DCIOC104"]
    assert [r["status"] for r in dup] == ["fetched", "already-fetched"]
    # the member stage 1 paid for is never requested at all
    assert "CUSTCPY" not in [n for n, _ in calls]
