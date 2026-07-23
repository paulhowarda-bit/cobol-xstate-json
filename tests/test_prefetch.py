"""Stage 1: retrieving the members that COMPLETE THE SOURCE TEXT before it is parsed.

The tests that matter here are the two that fail *silently* without this stage - a
dynamic CALL that does not resolve, and a JCL step that does not appear - because
silence is the whole problem. Neither raises, neither is flagged as an error, and both
produce output that looks like a finished answer about a simpler program than the one
that actually runs.
"""

import json

from cobol_xstate.artifacts import build_artifacts
from cobol_xstate.fetch import build_fetch_plan, fetch_dependencies
from cobol_xstate.jcl import parse_jcl
from cobol_xstate.jcl_views import build_jcl_artifacts
from cobol_xstate.parser import parse_program
from cobol_xstate.prefetch import (attribute_resolution, prefetch_cobol, prefetch_jcl)
from cobol_xstate.preprocessor import CopybookResolver
from cobol_xstate.statechart import build_machine

# A copybook that declares the data item a dynamic CALL goes through, and gives it the
# literal VALUE that makes the target provable. This is the ordinary shape on a real
# estate: the subprogram name is a shop-wide constant, so it lives in a shared member.
SUBPGM_CPY = (
    "       01  WS-CONSTANTS.\n"
    "           05  WS-SUBPGM  PIC X(8) VALUE 'POSTLOG '.\n"
    "       COPY RATESCPY.\n"                       # nested: a copybook COPYs a copybook
)
RATES_CPY = "       01  WS-RATE PIC 9(3)V99 VALUE 12.50.\n"

CALLER = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. MAINPGM.\n"
    "       DATA DIVISION.\n"
    "       WORKING-STORAGE SECTION.\n"
    "       COPY SUBPGMS.\n"
    "       PROCEDURE DIVISION.\n"
    "       0000-MAIN.\n"
    "           CALL WS-SUBPGM\n"
    "           GOBACK.\n"
)

# A job whose only step EXECs a cataloged PROC. Everything real is inside the PROC.
JOB = (
    "//PAYJOB   JOB (ACCT),'PAYROLL'\n"
    "//STEP1    EXEC PAYPROC\n"
)
PAYPROC = (
    "//PAYPROC  PROC\n"
    "//PS1      EXEC PGM=DCIOC104\n"
    "//SYSIN    DD DSN=PARM.LIB(SORTCRD),DISP=SHR\n"
    "//OUT      DD DSN=PROD.PAY.MASTER,DISP=SHR\n"
    "//         PEND\n"
)
SORTCRD = "  SORT FIELDS=(1,8,CH,A)\n"

ESTATE = {
    "SUBPGMS": SUBPGM_CPY,
    "RATESCPY": RATES_CPY,
    "PAYPROC": PAYPROC,
    "SORTCRD": SORTCRD,
}


def _mf(store=None, log=None, detected="copybook"):
    """A stand-in for cast_clients.mf_fetch.fetch_artifact, in its real dict shape:
    fetch_artifact(name, type=, copy=) -> {artifact_name, detected_type, found,
    copied_to, source_path, source_location, alternatives}."""
    data = ESTATE if store is None else store

    def fetch_artifact(name, type=None, copy=None):
        if log is not None:
            log.append((name, type))
        text = data.get(name.upper())
        if text is None:
            return {"artifact_name": name, "found": False, "detected_type": None}
        return {"artifact_name": name, "found": True, "detected_type": detected,
                "text": text,
                "source_location": f"PROD.SYSLIB({name})",
                "source_path": rf"\\share\PROD.SYSLIB\{name}",
                "alternatives": [f"TEST.SYSLIB({name})"]}
    return fetch_artifact


def _row(result, member):
    return next(r for r in result.rows if r["member"] == member)


# --------------------------------------------------------------------------- #
# the load-bearing case: what is lost when the text is parsed incomplete
# --------------------------------------------------------------------------- #

def test_a_dynamic_call_resolves_only_once_its_copybook_has_been_prefetched():
    """THE case this stage exists for.

    CALL WS-SUBPGM is provable only if the single literal reaching WS-SUBPGM is visible,
    and that literal is a VALUE clause in a copybook. Without the copybook the item is
    not declared, the target stays a runtime name, and POSTLOG is never a row to fetch -
    with no error anywhere. The program simply appears not to call anything."""
    # BEFORE: no estate service, copybook not on disk.
    blind = build_artifacts(build_machine(parse_program(CALLER)))
    targets = {r["artifact"] for r in blind["artifacts"] if r["kind"] == "program"}
    assert "POSTLOG" not in targets
    assert any(r.get("dynamic") for r in blind["artifacts"]), \
        "the unresolved target should at least be flagged as dynamic"
    # ...and the fetch plan therefore refuses to fetch it, correctly but uselessly.
    assert all(p["status"] == "skipped"
               for p in build_fetch_plan(blind) if p["artifact"] == "WS-SUBPGM")

    # AFTER: stage 1 retrieves SUBPGMS first, so the parse sees the VALUE clause.
    pre = prefetch_cobol(CALLER, _mf())
    program = parse_program(CALLER, resolver=CopybookResolver(store=pre.store))
    seeing = build_artifacts(build_machine(program))
    targets = {r["artifact"] for r in seeing["artifacts"] if r["kind"] == "program"}
    assert "POSTLOG" in targets

    # ...and it is now a real row in the fetch plan, requested as a program - probed by
    # language (cobol -> asm), not assumed to be cobol.
    plan = {p["artifact"]: p for p in build_fetch_plan(seeing)}
    assert plan["POSTLOG"]["status"] == "planned"
    assert plan["POSTLOG"]["type"] is None
    assert plan["POSTLOG"]["probeTypes"] == ["cobol", "asm"]


def test_the_manifest_says_which_rows_it_owes_to_prefetch():
    """Otherwise the improvement is invisible: the row looks like it was always
    resolvable, and no reader can tell an accurate model from a lucky one."""
    pre = prefetch_cobol(CALLER, _mf())
    program = parse_program(CALLER, resolver=CopybookResolver(store=pre.store))
    man = attribute_resolution(
        build_artifacts(build_machine(program)), program, pre.store)
    row = next(r for r in man["artifacts"] if r["artifact"] == "POSTLOG")
    assert row["resolvedBy"]["stage"] == "prefetch"
    assert row["resolvedBy"]["member"] == "SUBPGMS"


def test_a_jcl_step_inside_a_cataloged_proc_appears_only_after_prefetch():
    """The control-file half of the same failure. Every real step of PAYJOB lives in
    PAYPROC; parsed without it the job has no programs and no datasets at all."""
    blind = build_jcl_artifacts(parse_jcl(JOB, resolver=None))
    assert not [r for r in blind["artifacts"] if r["kind"] == "program"]

    pre = prefetch_jcl(JOB, _mf())
    job = parse_jcl(JOB, resolver=pre.resolver())
    seeing = build_jcl_artifacts(job)
    assert "DCIOC104" in {r["artifact"] for r in seeing["artifacts"]
                          if r["kind"] == "program"}
    assert "PROD.PAY.MASTER" in {r["artifact"] for r in seeing["artifacts"]
                                 if r["kind"] == "dataset"}


# --------------------------------------------------------------------------- #
# the closure
# --------------------------------------------------------------------------- #

def test_the_cobol_closure_follows_nested_copy():
    """A copybook that COPYs another copybook has a hole in it exactly like the program
    did, so one level of retrieval is not enough."""
    pre = prefetch_cobol(CALLER, _mf())
    assert _row(pre, "SUBPGMS")["status"] == "fetched"
    assert _row(pre, "RATESCPY")["status"] == "fetched"
    assert _row(pre, "RATESCPY")["for"] == "COPY inside SUBPGMS"


def test_the_jcl_closure_reaches_a_control_card_named_inside_a_proc():
    """SORTCRD is named by a DD inside PAYPROC - so it cannot be discovered until
    PAYPROC has been retrieved. A single-pass scan of the JCL file finds neither."""
    log = []
    pre = prefetch_jcl(JOB, _mf(log=log))
    assert [n for n, _ in log] == ["PAYPROC", "SORTCRD"]     # in that order, necessarily
    row = _row(pre, "SORTCRD")
    assert row["status"] == "fetched"
    assert row["dataset"] == "PARM.LIB(SORTCRD)"     # requested as the member within


def test_a_member_reached_many_ways_is_fetched_once():
    """A member COPYd by several others costs one round-trip, not one per reference."""
    store = dict(ESTATE,
                 ACPY="       COPY SHARED.\n",
                 BCPY="       COPY SHARED.\n",
                 SHARED="       01 SHARED-REC PIC X(80).\n")
    src = CALLER.replace("       COPY SUBPGMS.\n",
                         "       COPY ACPY.\n       COPY BCPY.\n")
    log = []
    pre = prefetch_cobol(src, _mf(store, log=log))
    assert [n for n, _ in log].count("SHARED") == 1
    assert _row(pre, "SHARED")["status"] == "fetched"


def test_a_cycle_terminates():
    store = {"ONE": "       COPY TWO.\n", "TWO": "       COPY ONE.\n"}
    src = CALLER.replace("       COPY SUBPGMS.\n", "       COPY ONE.\n")
    pre = prefetch_cobol(src, _mf(store))
    assert {_row(pre, m)["status"] for m in ("ONE", "TWO")} == {"fetched"}


# --------------------------------------------------------------------------- #
# the honesty rules: four different reasons a member is not here
# --------------------------------------------------------------------------- #

def test_local_members_cost_no_round_trip(tmp_path):
    (tmp_path / "SUBPGMS.cpy").write_text("       01 WS-SUBPGM PIC X(8).\n")
    log = []
    pre = prefetch_cobol(CALLER, _mf(log=log), paths=[str(tmp_path)])
    assert _row(pre, "SUBPGMS")["status"] == "local"
    assert [n for n, _ in log] == []          # the service was never asked


def test_not_found_and_error_and_no_service_are_three_different_facts():
    """Each leads somewhere different: fix nothing / fix the connection / install the
    client. Collapsing them into 'missing' destroys the only useful information."""
    absent = prefetch_cobol(CALLER, _mf({}))
    assert _row(absent, "SUBPGMS")["status"] == "not-found"
    assert "asked and had nothing" in _row(absent, "SUBPGMS")["reason"]

    def boom(name, type=None, copy=None):
        raise ConnectionError("share unreachable")
    broken = prefetch_cobol(CALLER, boom)
    row = _row(broken, "SUBPGMS")
    assert row["status"] == "error"
    assert "ConnectionError" in row["error"]
    assert "NOT evidence the member is absent" in row["reason"]

    none = prefetch_cobol(CALLER, None, unavailable="mf_fetch is not installed")
    assert _row(none, "SUBPGMS")["status"] == "no-service"
    assert "mf_fetch is not installed" in _row(none, "SUBPGMS")["reason"]
    assert none.report()["serviceAvailable"] is False


def test_the_missing_list_names_the_holes():
    """Every downstream view is read as if it were complete, so what is absent has to be
    nameable - a count cannot tell you which dynamic CALL stayed unresolved and why."""
    pre = prefetch_cobol(CALLER, _mf({}))
    assert pre.missing == ["SUBPGMS"]


def test_the_library_a_member_came_from_is_recorded_with_its_alternatives():
    """Two programs 'using SUBPGMS' are the same dependency only if the same member
    resolved. The origin is the evidence; the alternatives are the ambiguity."""
    pre = prefetch_cobol(CALLER, _mf())
    row = _row(pre, "SUBPGMS")
    assert row["source"] == "PROD.SYSLIB(SUBPGMS)"
    assert row["alternatives"] == ["TEST.SYSLIB(SUBPGMS)"]
    assert row["detectedType"] == "copybook"


def test_retrieved_members_are_collected_for_a_later_run(tmp_path):
    pre = prefetch_cobol(CALLER, _mf(), dest=str(tmp_path))
    assert {p.name for p in tmp_path.iterdir()} == {"SUBPGMS.cpy", "RATESCPY.cpy"}
    # and that directory is now usable as a -I path, with no service at all
    again = prefetch_cobol(CALLER, None, paths=[str(tmp_path)])
    assert {r["status"] for r in again.rows} == {"local"}


def test_the_report_is_self_describing():
    rep = prefetch_cobol(CALLER, _mf(), source_name="MAINPGM.cbl").report()
    assert rep["format"] == "cobol-xstate-prefetch"
    assert rep["source"] == "MAINPGM.cbl"
    assert rep["counts"]["fetched"] == 2
    assert "before the parse" in rep["note"]
    json.dumps(rep)                    # the report must be serializable as written


# --------------------------------------------------------------------------- #
# the handoff to stage 2
# --------------------------------------------------------------------------- #

def test_stage_two_does_not_re_request_what_stage_one_retrieved():
    pre = prefetch_cobol(CALLER, _mf())
    program = parse_program(CALLER, resolver=CopybookResolver(store=pre.store))
    man = build_artifacts(build_machine(program))
    log = []
    rep = fetch_dependencies(man, _mf(log=log), prefetched=pre.store)
    statuses = {r["artifact"]: r["status"] for r in rep["artifacts"]}
    assert statuses["SUBPGMS"] == "prefetched"
    assert "SUBPGMS" not in [n for n, _ in log]
    # ...while the program the prefetch made visible IS fetched, in stage 2
    assert "POSTLOG" in [n for n, _ in log]


# --------------------------------------------------------------------------- #
# --copybook-ext must reach stage 1, not only the parse (review finding J14)
# --------------------------------------------------------------------------- #

def test_prefetch_honors_a_custom_copybook_extension_on_disk(tmp_path):
    """Stage 1 tried only its built-in extensions, so a member saved under a custom
    --copybook-ext was reported MISSING (and, with a live service, fetched from the
    estate, shadowing the local file) even though the parse then resolved it fine."""
    cpy = tmp_path / "cpy"
    cpy.mkdir()
    (cpy / "CUSTREC.xyz").write_text(
        "           05 CUST-ID  PIC X(8).\n"
        "           05 CUST-BAL PIC 9(7)V99.\n")
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. USESCPY.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01 WS-CUST.\n"
        "       COPY CUSTREC.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           MOVE 0 TO CUST-BAL\n"
        "           STOP RUN.\n"
    )
    # no fetcher: the member exists ONLY as the local .xyz file. With the extension
    # threaded through, stage 1 finds it locally; without it, it is never looked for.
    pre = prefetch_cobol(src, None, paths=[str(cpy)], exts=(".xyz",))
    row = _row(pre, "CUSTREC")
    assert row["status"] == "local"
    assert row["source"].endswith("CUSTREC.xyz")


# --------------------------------------------------------------------------- #
# retrieving a level at a time
#
# Members within one level of the closure do not depend on each other, so the run used to
# cost the SUM of the estate's latencies where it could cost the maximum - measured at
# 7.9s of a 19.5s run, for about forty copybooks at a couple of hundred milliseconds each.
# What must not change is the report: its row order is part of its output, and if that
# followed the order answers happened to arrive, the same run against the same estate
# would produce different bytes twice in a row.
# --------------------------------------------------------------------------- #

# Wide (several members per level) and deep (three levels), so there is something to
# overlap AND more than one level to get in the wrong order. The outcomes are mixed on
# purpose: a level whose members all succeed cannot show that a not-found, an error and a
# skip still land in the right places among them.
WIDE_ESTATE = {
    "L1A": "       COPY L2A.\n       COPY L2B.\n",
    "L1B": "       COPY L2C.\n       COPY L2A.\n",     # L2A reached twice, from two parents
    "L1C": "       01 C PIC X.\n",
    "L2A": "       COPY L3A.\n",
    "L2B": "       01 B PIC X.\n",
    "L2C": "       01 D PIC X.\n",
    "L3A": "       01 E PIC X.\n",
}
WIDE_SRC = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. WIDEPGM.\n"
    "       DATA DIVISION.\n"
    "       WORKING-STORAGE SECTION.\n"
    "       COPY L1A.\n"
    "       COPY L1B.\n"
    "       COPY GONE.\n"                              # not-found
    "       COPY BOOM.\n"                              # the request itself fails
    "       COPY L1C.\n"
    "       PROCEDURE DIVISION.\n"
    "       0000-MAIN.\n"
    "           STOP RUN.\n"
)


def _jittered(peak):
    """An estate whose answers come back out of request order, which is the only way to
    catch a report that files rows as they arrive."""
    import random
    import threading
    import time
    state = {"now": 0}
    lock = threading.Lock()

    def fetch_artifact(name, type=None, copy=None):
        with lock:
            state["now"] += 1
            peak[0] = max(peak[0], state["now"])
        try:
            time.sleep(random.uniform(0.002, 0.02))
            if name.upper() == "BOOM":
                raise RuntimeError("the estate refused this request")
            text = WIDE_ESTATE.get(name.upper())
            if text is None:
                return {"artifact_name": name, "found": False}
            return {"artifact_name": name, "found": True, "text": text,
                    "detected_type": "copybook",
                    "source_location": f"PROD.SYSLIB({name})"}
        finally:
            with lock:
                state["now"] -= 1
    return fetch_artifact


def test_a_level_is_retrieved_concurrently_and_still_reported_in_its_own_order():
    peak = [0]
    seq = prefetch_cobol(WIDE_SRC, _jittered(peak), jobs=1).report()
    assert peak[0] == 1, "jobs=1 must not start a second request; it is the escape hatch"

    for attempt in range(5):
        peak[0] = 0
        par = prefetch_cobol(WIDE_SRC, _jittered(peak), jobs=8).report()
        assert json.dumps(par, indent=2) == json.dumps(seq, indent=2), (
            f"attempt {attempt}: the report followed the estate's timing, not the plan")
        assert peak[0] > 1, "nothing actually overlapped - the test proves nothing"


def test_every_outcome_survives_the_concurrent_path_distinctly():
    """The reasons must stay apart under concurrency too. 'the estate had nothing' and
    'we could not ask' lead to different next actions, and an exception raised in another
    thread is exactly the kind of thing that gets flattened into a generic miss."""
    rep = prefetch_cobol(WIDE_SRC, _jittered([0]), jobs=8).report()
    by = {r["member"]: r for r in rep["members"]}
    assert by["GONE"]["status"] == "not-found"
    assert by["BOOM"]["status"] == "error"
    assert "the estate refused this request" in by["BOOM"]["error"]
    assert by["L3A"]["status"] == "fetched", "the third level must still be reached"
    # ...and one member named by two parents is still one round-trip and one row
    assert [r["member"] for r in rep["members"]].count("L2A") == 1
