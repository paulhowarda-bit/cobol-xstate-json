"""The related-artifact manifest: one row per external thing the program touches
(Db2 tables, files, called programs, queues), each with the identity-resolution chain
its program-local name still needs (docs/mainframe-artifacts.md)."""

from pathlib import Path

import pytest

from cobol_xstate.artifacts import build_artifacts
from cobol_xstate.parser import parse_program
from cobol_xstate.preprocessor import CopybookResolver
from cobol_xstate.statechart import build_machine

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _artifacts_src(proc_body: str, data_body: str = "", env_body: str = "") -> dict:
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. T.\n"
        "       ENVIRONMENT DIVISION.\n" + env_body +
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n" + data_body +
        "       PROCEDURE DIVISION.\n" + proc_body
    )
    return build_artifacts(build_machine(parse_program(src)))


def _artifacts_example(name: str) -> dict:
    # Mirror the CLI, which searches the source's own directory for copybooks.
    resolver = CopybookResolver(paths=[str(EXAMPLES)])
    return build_artifacts(build_machine(
        parse_program((EXAMPLES / name).read_text(), resolver=resolver), source_name=name))


def _by_name(manifest: dict) -> dict:
    return {a["artifact"]: a for a in manifest["artifacts"]}


# --------------------------------------------------------------------------- #
# the artifact kinds the user asked for
# --------------------------------------------------------------------------- #

def test_sql_names_the_table_as_a_global_artifact():
    """'if there is an SQL, the table name is the related artifact'."""
    man = _artifacts_example("sqlunld.cbl")
    acct = _by_name(man)["ACCOUNT"]
    assert acct["kind"] == "db2-table"
    assert acct["io"] == "read"
    # a table name is already catalog-global - nothing binds it to a different identity
    assert acct["identity"] == "global"
    assert "resolvedBy" not in acct or acct["resolvedBy"] is None
    assert "DDL" in acct["needs"]  # what you still need is the columns, not the name


def test_output_file_carries_its_ddname_and_the_dsn_is_flagged_as_jcl():
    """'if it writes to a batch file then it is that batch file' - and the honest limit:
    the ddname is here, the dataset (DSN) is in the JCL."""
    man = _artifacts_example("sqlunld.cbl")
    out = _by_name(man)["OUT-FILE"]
    assert out["kind"] == "file"
    assert out["io"] == "write"
    assert out["ddname"] == "OUTDD"
    assert out["identity"] == "program-local"      # OUT-FILE is a name inside T only
    assert out["resolvedBy"] == "JCL DD statement"
    assert "DSN" in out["needs"]


def test_input_control_file_is_a_read_artifact_with_its_ddname():
    """'if it uses a CNTL file then add the CNTL File' - an input file, named by ddname."""
    man = _artifacts_example("sqlload.cbl")
    inf = _by_name(man)["IN-FILE"]
    assert inf["kind"] == "file"
    assert inf["io"] == "read"
    assert inf["ddname"] == "INDD"


def test_batch_call_names_the_external_program_resolved_by_the_binder():
    """'if it calls an external cobol program then add that program name'."""
    man = _artifacts_example("banktran.cbl")
    pg = _by_name(man)["POSTLOG"]
    assert pg["kind"] == "program"
    assert pg["verbs"] == ["CALL"]
    assert pg["identity"] == "global"              # a load-module name
    assert "binder" in pg["resolvedBy"]


def test_cics_link_and_xctl_resolve_via_the_csd_not_the_binder():
    """A CICS program invocation's target is the installed PROGRAM resource, so its
    resolver is the CSD - naming the binder here would send the reader to the wrong file."""
    man = _artifacts_example("cicsinq.cbl")
    by = _by_name(man)
    for prog in ("POSTLOG", "CLOSEDPG"):
        assert by[prog]["kind"] == "program"
        assert by[prog]["resolvedBy"] == "CICS CSD (DEFINE PROGRAM)"
        assert any(v.startswith("CICS") for v in by[prog]["verbs"])


def test_cics_link_program_data_name_resolves_to_the_module_name():
    """LINK PROGRAM(WS-PGM) with WS-PGM VALUE 'DEMOREST': the manifest row is the
    module name DEMOREST (with the data item it came via), never WS-PGM."""
    man = _artifacts_src(
        "       0000-MAIN.\n"
        "           EXEC CICS LINK PROGRAM(WS-PGM) END-EXEC.\n",
        data_body="       01 WS-PGM PIC X(8) VALUE 'DEMOREST'.\n",
    )
    by = _by_name(man)
    assert "WS-PGM" not in by
    pg = by["DEMOREST"]
    assert pg["kind"] == "program"
    assert pg["identity"] == "global"
    assert pg["via"] == "WS-PGM"
    assert pg["resolvedBy"] == "CICS CSD (DEFINE PROGRAM)"


def test_unresolved_dynamic_program_target_is_not_presented_as_global():
    """When the target data item is set only from another variable, the row must say
    the name is a data item - not claim a working-storage name is a load module."""
    man = _artifacts_src(
        "       0000-MAIN.\n"
        "           MOVE WS-OTHER TO WS-PGM\n"
        "           EXEC CICS LINK PROGRAM(WS-PGM) END-EXEC.\n",
        data_body="       01 WS-PGM PIC X(8).\n"
                  "       01 WS-OTHER PIC X(8).\n",
    )
    pg = _by_name(man)["WS-PGM"]
    assert pg["identity"] == "program-local"
    assert pg["dynamic"] is True
    assert pg["resolvedBy"] is None
    assert "data item" in pg["needs"]
    assert any("dynamic target" in f for f in man["flags"])


def test_dynamic_transid_and_queue_operands_resolve_to_real_names():
    """START TRANSID(data-name) / WRITEQ QUEUE(data-name) resolve exactly like
    PROGRAM(data-name): the row is the resource name, `via` the data item."""
    man = _artifacts_src(
        "       0000-MAIN.\n"
        "           EXEC CICS START TRANSID(WS-TRAN) END-EXEC\n"
        "           EXEC CICS WRITEQ TD QUEUE(WS-Q) FROM(WS-MSG) END-EXEC.\n",
        data_body="       01 WS-TRAN PIC X(4) VALUE 'AB12'.\n"
                  "       01 WS-Q PIC X(8) VALUE 'ERRQ'.\n"
                  "       01 WS-MSG PIC X(80).\n",
    )
    by = _by_name(man)
    assert "WS-TRAN" not in by and "WS-Q" not in by
    assert by["AB12"]["kind"] == "cics-transaction"
    assert by["AB12"]["via"] == "WS-TRAN"
    assert by["AB12"]["identity"] == "global"
    assert by["ERRQ"]["kind"] == "queue"
    assert by["ERRQ"]["via"] == "WS-Q"


def test_unresolved_dynamic_transid_is_not_presented_as_global():
    man = _artifacts_src(
        "       0000-MAIN.\n"
        "           MOVE WS-OTHER TO WS-TRAN\n"
        "           EXEC CICS START TRANSID(WS-TRAN) END-EXEC.\n",
        data_body="       01 WS-TRAN PIC X(4).\n"
                  "       01 WS-OTHER PIC X(8).\n",
    )
    tr = _by_name(man)["WS-TRAN"]
    assert tr["kind"] == "cics-transaction"
    assert tr["identity"] == "program-local"
    assert tr["dynamic"] is True
    assert tr["resolvedBy"] is None
    assert "data item" in tr["needs"]
    assert any("dynamic target" in f for f in man["flags"])


def test_dynamic_call_via_missing_copybook_points_at_the_copybook():
    """CALL CN-X where CN-X (and its VALUE) live in a copybook that was not found:
    the program row must connect the two facts - undeclared identifier + missing
    copybook - so the reader knows exactly which artifact to supply."""
    man = _artifacts_src(
        "       JM0004.\n"
        "           CALL CN-DEMOC104 USING DM01104-PARMS\n"
        "           GOBACK.\n",
        data_body="       COPY DM01104.\n"
                  "       01 WS-DUMMY PIC X.\n",
    )
    by = _by_name(man)
    pg = by["CN-DEMOC104"]
    assert pg["dynamic"] is True
    assert "not declared in the visible source" in pg["needs"]
    assert "DM01104" in pg["needs"]
    # ...and the copybook row itself is present and marked missing.
    assert by["DM01104"]["status"] == "missing"


def test_dynamic_sql_row_is_honest_about_unknown_tables():
    man = _artifacts_src(
        "       0000-MAIN.\n"
        "           EXEC SQL EXECUTE IMMEDIATE :WS-SQL END-EXEC.\n",
        data_body="       01 WS-SQL PIC X(200).\n",
    )
    row = _by_name(man)["<dynamic-sql>"]
    # Its own kind, not db2-table: a run-time statement is not a catalog identity, and
    # counting it as one made the db2-read/db2-write pattern proofs claim unload/load
    # jobs that are not provable.
    assert row["kind"] == "db2-dynamic-sql"
    assert row["identity"] == "program-local"
    assert row["dynamic"] is True
    assert row["io"] == "read-write"          # could be either - direction unknowable
    assert row["resolvedBy"] is None
    assert "assembled at run time" in row["needs"]
    assert any("<dynamic-sql>" in f for f in man["flags"])
    # ...and it no longer feeds the unload/load pattern proofs on its own.
    assert man["patterns"] == []


def test_ambiguous_dynamic_program_target_lists_its_candidates():
    man = _artifacts_src(
        "       0000-MAIN.\n"
        "           MOVE 'PGMA' TO WS-PGM\n"
        "           EXEC CICS LINK PROGRAM(WS-PGM) END-EXEC\n"
        "           MOVE 'PGMB' TO WS-PGM\n"
        "           EXEC CICS LINK PROGRAM(WS-PGM) END-EXEC.\n",
        data_body="       01 WS-PGM PIC X(8).\n",
    )
    pg = _by_name(man)["WS-PGM"]
    assert pg["dynamic"] is True
    assert pg["candidates"] == ["PGMA", "PGMB"]
    assert "PGMA" in pg["needs"] and "PGMB" in pg["needs"]


def test_a_batch_call_row_lists_its_candidates_and_how_far_to_trust_them():
    """Ledger item 39: the CICS row above always named its literals, a CALL row over the
    same kind of item did not. The row now also says whether the list is proven stored
    (`evidence`) and whether a variable MOVE means it is not the whole set."""
    man = _artifacts_src(
        "       0000-MAIN.\n"
        "           MOVE WS-OTHER TO WS-MIXED\n"
        "           CALL WS-MIXED\n"
        "           CALL WS-COND.\n",
        data_body="       01 WS-MIXED PIC X(8) VALUE 'PGMA'.\n"
                  "       01 WS-OTHER PIC X(8).\n"
                  "       01 WS-COND  PIC X(8).\n"
                  "          88 COND-B VALUE 'PGMB'.\n",
    )
    mixed, cond = _by_name(man)["WS-MIXED"], _by_name(man)["WS-COND"]
    assert (mixed["candidates"], mixed["evidence"], mixed["hasVariableAssignment"]) == \
        (["PGMA"], "assigned", True)
    assert "PGMA" in mixed["needs"]
    assert (cond["candidates"], cond["evidence"]) == (["PGMB"], "declared-88")
    assert "hasVariableAssignment" not in cond


# --------------------------------------------------------------------------- #
# the two structural patterns the example corpus is named for
# --------------------------------------------------------------------------- #

def test_unload_pattern_is_detected():
    man = _artifacts_example("sqlunld.cbl")
    assert any(p.startswith("unload:") for p in man["patterns"])


def test_load_pattern_is_detected():
    man = _artifacts_example("sqlload.cbl")
    assert any(p.startswith("load:") for p in man["patterns"])


# --------------------------------------------------------------------------- #
# honesty: what is NOT an artifact, and what cannot be resolved here
# --------------------------------------------------------------------------- #

def test_response_registers_are_excluded_not_listed_as_artifacts():
    """SQLCODE/FILE STATUS is the program reacting to a subsystem, not a second thing it
    touches - it must not appear as a related artifact."""
    man = _artifacts_example("sqlunld.cbl")
    assert "DB2" not in _by_name(man)
    excluded = {e["name"] for e in man["excluded"]}
    assert "DB2" in excluded


def test_handled_condition_is_excluded():
    man = _artifacts_example("cicsinq.cbl")
    assert "NOTFND" not in _by_name(man)
    assert any(e["name"] == "NOTFND" and e["endpointType"] == "condition"
               for e in man["excluded"])


def test_file_without_select_has_no_ddname_and_is_flagged():
    """A file READ with no FILE-CONTROL SELECT: even the ddname is unknown, so the row
    says so and the manifest flags it rather than inventing a binding."""
    man = _artifacts_src(
        "       0000-MAIN.\n"
        "           READ TRAN-FILE AT END CONTINUE END-READ\n"
        "           STOP RUN.\n"
    )
    tf = _by_name(man)["TRAN-FILE"]
    assert tf["kind"] == "file"
    assert "ddname" not in tf
    assert tf["resolvedBy"] is None
    assert any("TRAN-FILE" in f for f in man["flags"])


def test_select_assign_gives_the_ddname():
    man = _artifacts_src(
        "       0000-MAIN.\n"
        "           OPEN OUTPUT RPT-FILE\n"
        "           WRITE RPT-REC\n"
        "           CLOSE RPT-FILE\n"
        "           STOP RUN.\n",
        data_body="",
        env_body=(
            "       INPUT-OUTPUT SECTION.\n"
            "       FILE-CONTROL.\n"
            "           SELECT RPT-FILE ASSIGN TO RPTDD\n"
            "               ORGANIZATION IS SEQUENTIAL.\n"
        ),
    )
    rpt = _by_name(man)["RPT-FILE"]
    assert rpt["ddname"] == "RPTDD"
    assert rpt["organization"] == "SEQUENTIAL"
    assert rpt["identity"] == "program-local"


# --------------------------------------------------------------------------- #
# copybooks — a compile-time dependency, listed alongside the runtime artifacts
# --------------------------------------------------------------------------- #

def test_a_resolved_copybook_is_listed_as_a_compile_time_artifact():
    """'does it add dependent copybooks to the list' — yes: cicsinq COPYs CUSTREC, which
    resolves from examples/custrec.cpy (the source's own directory is searched)."""
    man = _artifacts_example("cicsinq.cbl")
    cb = _by_name(man).get("CUSTREC")
    assert cb is not None, "the copybook dependency is missing from the manifest"
    assert cb["kind"] == "copybook"
    assert cb["dependency"] == "compile-time"     # not a runtime endpoint
    assert "io" not in cb                          # so it carries no read/write direction
    assert cb["status"] == "expanded"
    assert cb["via"] == "COPY"
    assert cb["contributes"]["dataItems"] >= 1     # it brought record fields into the model
    assert cb["identity"] == "program-local"       # unique only within a library
    assert "SYSLIB" in cb["resolvedBy"]


def test_runtime_artifacts_are_tagged_runtime():
    man = _artifacts_example("cicsinq.cbl")
    cust = _by_name(man)["CUST"]                    # the Db2 table
    assert cust["dependency"] == "runtime"


def test_a_missing_copybook_is_listed_and_flagged():
    """The most useful copybook fact is a dependency the model could NOT see. A COPY of a
    member not on the search path is listed as missing and flagged, never silently."""
    man = _artifacts_src(
        "       0000-MAIN.\n"
        "           DISPLAY WS-X\n"
        "           STOP RUN.\n",
        data_body="       COPY NOPE.\n       01 WS-X PIC 9.\n",
    )
    cb = _by_name(man).get("NOPE")
    assert cb is not None and cb["kind"] == "copybook"
    assert cb["status"] == "missing"
    assert cb["resolvedBy"] is None
    assert "ABSENT" in cb["needs"] or "not found" in cb["needs"]
    assert any("NOPE" in f for f in man["flags"])


def test_replacing_is_recorded_on_the_copybook_row():
    """COPY ... REPLACING renames the member's fields per program — a false-join hazard the
    row must surface (docs/mainframe-artifacts.md). Here the member is missing, but the
    REPLACING fact is still captured from the COPY statement itself."""
    man = _artifacts_src(
        "       0000-MAIN.\n           STOP RUN.\n",
        data_body="       COPY NOPE REPLACING ==:TAG:== BY ==WS==.\n       01 WS-X PIC 9.\n",
    )
    cb = _by_name(man)["NOPE"]
    assert cb.get("replacing") is True


def test_an_sql_include_with_no_period_is_a_copybook_row_and_expanded():
    """Ledger item 86: the period after END-EXEC is optional. Without one, the member was
    neither fetched nor expanded, and the record it declares was missing from the model."""
    man = _artifacts_example("sqlincnp.cbl")
    rows = {a["artifact"]: a for a in man["artifacts"] if a["kind"] == "copybook"}
    assert sorted(rows) == ["CUSTREC", "SQLCA", "XSQLERRP"]
    assert {r["via"] for r in rows.values()} == {"EXEC SQL INCLUDE"}
    assert rows["CUSTREC"]["status"] == "expanded"
    assert rows["CUSTREC"]["contributes"]["dataItems"] >= 1
    assert rows["SQLCA"]["status"] == rows["XSQLERRP"]["status"] == "missing"


def test_programs_with_no_copy_have_no_copybook_rows():
    man = _artifacts_example("sqlunld.cbl")            # no COPY
    assert not any(a["kind"] == "copybook" for a in man["artifacts"])


# --------------------------------------------------------------------------- #
# shape
# --------------------------------------------------------------------------- #

def test_manifest_shape_and_stable_ordering():
    man = _artifacts_example("cicsinq.cbl")
    assert man["format"] == "cobol-xstate-artifacts"
    assert man["program"] == "CICSINQ"
    # tables sort before programs before the caller (kind priority), so the manifest
    # reads in a stable, category order regardless of source order.
    kinds = [a["kind"] for a in man["artifacts"]]
    assert kinds.index("db2-table") < kinds.index("program")
    assert kinds.index("program") < kinds.index("caller")


def test_no_name_shaped_sentinel_reaches_the_manifest_for_a_padded_operand():
    """`endpoint or "<program>"` is a name-shaped sentinel, and a manifest row is a
    FETCHABLE row: `<program>` reached this repo's fetch path and was reported as a
    missing application program - a fabricated dependency. The sentinel is retained
    deliberately for operands that genuinely cannot be parsed; this pins that a merely
    blank-padded one is not one of them, and that two padded targets stay two rows.
    """
    manifest = _artifacts_src(
        "       0000-MAIN.\n"
        "           IF WS-FLAG = 'Y'\n"
        "               EXEC CICS XCTL PROGRAM('CALLA000 ') END-EXEC\n"
        "           ELSE\n"
        "               EXEC CICS XCTL PROGRAM ('CALLA099 ') END-EXEC\n"
        "           END-IF\n"
        "           EXEC CICS READ FILE('CUSTFIL ') INTO(WS-REC) END-EXEC.\n",
        "       01  WS-FLAG   PIC X VALUE 'N'.\n"
        "       01  WS-REC    PIC X(80).\n")
    names = _by_name(manifest)
    assert not [n for n in names if n.startswith("<")]
    assert names["CALLA000"]["kind"] == "program"
    assert names["CALLA099"]["kind"] == "program"
    assert names["CUSTFIL"]["kind"] == "file"


def test_a_manifest_row_keeps_the_literals_it_refused_as_names():
    """Ledger item 50: the row a consumer joins on carries `rejectedCandidates` too, and
    its `needs` names only the literals that can be a program."""
    rows = _by_name(_artifacts_example("callfilter.cbl"))
    nxt, mm = rows["WS-TO-PGM"], rows["WS-PH-PGM"]
    assert nxt["candidates"] == ["PGMTO001", "PGMTO002"]
    assert nxt["rejectedCandidates"] == [
        {"literal": "RUN COMPLETED NORMALLY OK", "reason": "length"}]
    assert "RUN COMPLETED" not in nxt["needs"]
    assert "candidates" not in mm and mm["rejectedCandidates"][0]["reason"] == "wildcard"


def test_a_placeholder_and_an_untraced_record_are_rows_that_say_what_they_are():
    """Ledger item 51: neither row may read as a file a ddname could be joined on. The
    old rows said "no SELECT/ASSIGN found for this file" - a diagnosis of a file that
    does not exist."""
    rec = _by_name(_artifacts_example("fdrecord.cbl"))["C-REC"]
    assert (rec["endpointUnresolved"], rec["identity"], rec["resolvedBy"]) == \
        ("record", "program-local", None)
    assert "record name, not a file" in rec["needs"] and "FDCRECX" in rec["needs"]
    ph = _by_name(_artifacts_example("cicsnofile.cbl"))["<file>"]
    assert ph["endpointUnresolved"] == "no-operand"
    assert "placeholder" in ph["needs"] and "SELECT" not in ph["needs"]


# --------------------------------------------------------------------------- #
# ledger item 55: a CONTAINED program's dependencies are the member's
# --------------------------------------------------------------------------- #

def _by_unit(manifest: dict) -> dict:
    """(containedProgram or None for the main program, artifact) -> row."""
    return {(a.get("containedProgram"), a["artifact"]): a for a in manifest["artifacts"]}


def test_a_contained_programs_calls_and_tables_are_dependencies_of_the_member():
    """NESTSQL is parsed out of NESTDEPS, and what it touches used to go with it: the
    member's manifest held only the internal CALL 'NESTSQL'. Each row names the unit it
    came from and the source line of the statement."""
    rows = _by_unit(_artifacts_example("nestdeps.cbl"))
    ext = rows[("NESTSQL", "EXTMOD")]
    assert (ext["kind"], ext["identity"], ext["lines"]) == ("program", "global", [30])
    assert ext["classification"] == "unresolved"     # fetchable, decided by the estate
    tbl = rows[("NESTSQL", "ACCOUNT_MASTER")]
    assert (tbl["kind"], tbl["io"], tbl["lines"]) == ("db2-table", "read", [32])
    log = rows[("NESTLOG", "LOG-FILE")]               # a unit nested two deep
    assert (log["kind"], log["io"], log["ddname"]) == ("file", "write", "LOGDD")
    assert log["lines"] == [54, 56]
    # The main program's own rows carry no unit, and keep their own lines.
    assert "containedProgram" not in rows[(None, "OUTERMOD")]
    assert rows[(None, "NESTSQL")]["lines"] == [17]


def test_a_contained_programs_dynamic_call_resolves_through_its_own_storage():
    """Both programs declare WS-TARGET, with different VALUEs. NESTSQL's CALL WS-TARGET
    is INNERMOD - its own storage - and never the main program's OUTERMOD."""
    rows = _by_unit(_artifacts_example("nestdeps.cbl"))
    assert rows[("NESTSQL", "INNERMOD")]["via"] == "WS-TARGET"
    assert rows[("NESTSQL", "INNERMOD")]["lines"] == [31]
    assert rows[(None, "OUTERMOD")]["via"] == "WS-TARGET"
    assert ("NESTSQL", "OUTERMOD") not in rows and (None, "INNERMOD") not in rows


def test_a_call_to_any_program_contained_here_stays_internal_and_is_never_fetched():
    """NESTLOG is contained in NESTSQL, NESTSQL in NESTDEPS: a CALL to either, from
    whichever program of this source, is internal - never a member to request."""
    from mainframe_artifacts.fetch import build_fetch_plan
    man = _artifacts_example("nestdeps.cbl")
    rows = _by_unit(man)
    for key in ((None, "NESTSQL"), ("NESTSQL", "NESTLOG")):
        assert rows[key]["classification"] == "internal-nested"
        assert rows[key]["identity"] == "internal"
    plan = {p["artifact"]: p for p in build_fetch_plan(man)}
    assert plan["NESTSQL"]["status"] == plan["NESTLOG"]["status"] == "skipped"
    assert plan["EXTMOD"]["status"] == plan["INNERMOD"]["status"] == "planned"
    assert plan["ACCOUNT_MASTER"]["status"] == "planned"


def test_a_contained_programs_parameter_holds_what_its_in_source_caller_passes():
    """NESTLOG calls whatever its caller passes in LK-PGM. Only this source can call a
    contained program, and its one caller, NESTSQL, passes WS-LOGPGM (VALUE 'LOGWRITE'):
    so the target is LOGWRITE - a fetchable program, proved through LK-PGM - and not a
    flagged data item."""
    man = _artifacts_example("nestdeps.cbl")
    rows = _by_unit(man)
    row = rows[("NESTLOG", "LOGWRITE")]
    assert (row["kind"], row["identity"], row["via"], row["lines"]) == \
        ("program", "global", "LK-PGM", [58])
    assert row["classification"] == "unresolved"     # fetchable, decided by the estate
    assert ("NESTLOG", "LK-PGM") not in rows
    assert man["flags"] == []


# --------------------------------------------------------------------------- #
# a contained program's parameters hold what the CALLs in its source pass
# --------------------------------------------------------------------------- #

def _program(name, proc, ws=(), lk=(), using=(), inner="", common=False):
    """One program unit: its storage, its PROCEDURE DIVISION, the units it contains."""
    out = ["IDENTIFICATION DIVISION.",
           f"PROGRAM-ID. {name}{' IS COMMON' if common else ''}."]
    if ws or lk:
        out.append("DATA DIVISION.")
    if ws:
        out += ["WORKING-STORAGE SECTION.", *ws]
    if lk:
        out += ["LINKAGE SECTION.", *lk]
    out += ["PROCEDURE DIVISION" + (" USING " + " ".join(using) if using else "") + ".",
            f"{name}-MAIN.", *("    " + s for s in proc), "    GOBACK."]
    return ("".join(f"       {line}\n" for line in out) + inner
            + f"       END PROGRAM {name}.\n")


def _member(src: str):
    """(manifest, rows by unit, dynamic-call rows by (program, item)) for one source."""
    from cobol_xstate.dynamic_calls import build_dynamic_calls
    m = build_machine(parse_program(src))
    man = build_artifacts(m)
    dyn = {(r["program"], r["item"]): r
           for r in build_dynamic_calls(m, man)["dynamicCalls"]}
    return man, _by_unit(man), dyn


_INNER_CALLS_ITS_PARAMETER = _program(
    "INNER", ["CALL LK-PGM"], lk=["01  LK-PGM  PIC X(8)."], using=["LK-PGM"],
    common=True)


def test_every_caller_and_the_programs_own_assignment_are_the_candidates():
    """OUTER passes 'MODA', its sibling B passes 'MODB', and INNER itself moves 'MODC'
    into the parameter. The target is one of the three - flagged, with all three as
    candidates - and never the one literal INNER's own storage holds."""
    inner = _program("INNER", ["MOVE 'MODC' TO LK-PGM", "CALL LK-PGM"],
                     lk=["01  LK-PGM  PIC X(8)."], using=["LK-PGM"], common=True)
    b = _program("B", ["CALL 'INNER' USING WS-B"],
                 ws=["01  WS-B  PIC X(8) VALUE 'MODB'."], common=True)
    src = _program("OUTER", ["CALL 'INNER' USING WS-A", "CALL 'B'"],
                   ws=["01  WS-A  PIC X(8) VALUE 'MODA'."], inner=inner + b)
    man, rows, dyn = _member(src)
    row = rows[("INNER", "LK-PGM")]
    assert row["dynamic"] is True and row["candidates"] == ["MODA", "MODB", "MODC"]
    assert not [k for k in rows if k[1] in ("MODA", "MODB", "MODC")]
    assert "OUTER line 8 passes WS-A" in dyn[("INNER", "LK-PGM")]["why"]
    assert "B line 29 passes WS-B" in dyn[("INNER", "LK-PGM")]["why"]


def test_a_parameter_passed_down_a_chain_of_contained_programs_is_followed():
    """OUTER passes WS-P ('DEEPMOD') to MID, MID passes its own parameter on to INNER,
    and INNER moves it to WS-X before calling: the target is DEEPMOD, and the reason
    walks back through every hop to the literal."""
    inner = _program("INNER", ["MOVE LK-I TO WS-X", "CALL WS-X"],
                     ws=["01  WS-X  PIC X(8)."], lk=["01  LK-I  PIC X(8)."],
                     using=["LK-I"])
    mid = _program("MID", ["CALL 'INNER' USING LK-M"], lk=["01  LK-M  PIC X(8)."],
                   using=["LK-M"], inner=inner)
    src = _program("OUTER", ["CALL 'MID' USING WS-P"],
                   ws=["01  WS-P  PIC X(8) VALUE 'DEEPMOD'."], inner=mid)
    man, rows, dyn = _member(src)
    assert rows[("INNER", "DEEPMOD")]["via"] == "WS-X"
    assert man["flags"] == [] and dyn == {}
    m = build_machine(parse_program(src))
    (label,) = [p["cobol"] for p in m.contained[1].provenance.values()
                if str(p.get("cobol", "")).startswith("CALL WS-X")]
    assert "LK-I is a parameter of INNER" in label
    assert "MID line 17 passes LK-M" in label and "OUTER line 8 passes WS-P" in label


def test_a_dynamic_call_the_caller_resolves_to_a_contained_program_is_a_call_site():
    """OUTER calls INNER through WS-SUB (VALUE 'INNER'): that CALL names INNER as surely
    as CALL 'INNER' does, so what it passes is INNER's parameter."""
    src = _program("OUTER", ["CALL WS-SUB USING WS-A"],
                   ws=["01  WS-SUB  PIC X(8) VALUE 'INNER'.",
                       "01  WS-A    PIC X(8) VALUE 'MODA'."],
                   inner=_INNER_CALLS_ITS_PARAMETER)
    man, rows, dyn = _member(src)
    assert rows[("INNER", "MODA")]["via"] == "LK-PGM"
    assert rows[(None, "INNER")]["classification"] == "internal-nested"
    assert man["flags"] == []


def test_a_dynamic_call_that_may_name_a_contained_program_counts_as_a_call_site():
    """WS-SUB may be 'INNER' or 'OTHER', so that CALL may be INNER's - and what it passes
    may be INNER's parameter. Leaving it out would pin LK-PGM to the one static
    caller's 'MODB'; with it, the target is one of the two, flagged."""
    src = _program("OUTER", ["IF WS-A = 'MODA'", "    MOVE 'INNER' TO WS-SUB",
                             "ELSE", "    MOVE 'OTHER' TO WS-SUB", "END-IF",
                             "CALL WS-SUB USING WS-A", "CALL 'INNER' USING WS-B"],
                   ws=["01  WS-SUB  PIC X(8).",
                       "01  WS-A    PIC X(8) VALUE 'MODA'.",
                       "01  WS-B    PIC X(8) VALUE 'MODB'."],
                   inner=_INNER_CALLS_ITS_PARAMETER)
    man, rows, dyn = _member(src)
    assert rows[("INNER", "LK-PGM")]["candidates"] == ["MODA", "MODB"]
    assert ("INNER", "MODB") not in rows


@pytest.mark.parametrize("outer, why", [
    # Nothing in this source calls INNER - the source CANNOT be all of its callers.
    (_program("OUTER", [], inner=_INNER_CALLS_ITS_PARAMETER),
     "do not pin it: no CALL in this source names INNER"),
    # The argument holds a literal but is also set from a variable: not pinned.
    (_program("OUTER", ["MOVE WS-IN TO WS-A", "CALL 'INNER' USING WS-A"],
              ws=["01  WS-A   PIC X(8) VALUE 'MODA'.", "01  WS-IN  PIC X(8)."],
              inner=_INNER_CALLS_ITS_PARAMETER),
     "the CALL at OUTER line 10 passes WS-A - WS-A set to ['MODA'] and also to a "
     "variable"),
    # One literal is longer than the caller's item, which then holds a truncation of
    # it - not the literal, even though INNER's wider parameter could hold that.
    (_program("OUTER", ["MOVE 'LONGERNAME' TO WS-A", "CALL 'INNER' USING WS-A"],
              ws=["01  WS-A  PIC X(8) VALUE 'MODA'."],
              inner=_program("INNER", ["CALL LK-PGM"], lk=["01  LK-PGM  PIC X(10)."],
                             using=["LK-PGM"])),
     "not a name: 'LONGERNAME' (length)"),
    # The main program's own parameter comes from outside this source.
    (_program("OUTER", ["CALL 'INNER' USING LK-O"], lk=["01  LK-O  PIC X(8)."],
              using=["LK-O"], inner=_INNER_CALLS_ITS_PARAMETER),
     "passes LK-O - LK-O is declared but never assigned"),
    # Two items for one parameter: they cannot be matched by position.
    (_program("OUTER", ["CALL 'INNER' USING WS-A WS-A"],
              ws=["01  WS-A  PIC X(8) VALUE 'MODA'."],
              inner=_INNER_CALLS_ITS_PARAMETER),
     "names 2 data item(s) for INNER's 1 parameter(s)"),
    # A qualified argument is several words: three of them for three parameters must
    # not be read as three arguments.
    (_program("OUTER", ["CALL 'INNER' USING WS-A OF GRP"],
              ws=["01  GRP.", "    05  WS-A  PIC X(8) VALUE 'MODA'."],
              inner=_program("INNER", ["CALL LK-PGM"],
                             lk=["01  LK-PGM  PIC X(8).", "01  LK-2  PIC X(8).",
                                 "01  LK-3  PIC X(8)."],
                             using=["LK-PGM", "LK-2", "LK-3"])),
     "passes a qualified name or an ADDRESS OF / LENGTH OF operand"),
    # A literal argument is not a data item the CALL records, so the list is short.
    (_program("OUTER", ["CALL 'INNER' USING BY CONTENT 'MODA'"],
              inner=_INNER_CALLS_ITS_PARAMETER),
     "names 0 data item(s) for INNER's 1 parameter(s)"),
    # INNER and B pass their parameters to each other: a cycle proves nothing.
    (_program("OUTER", ["CALL 'INNER' USING WS-A"],
              ws=["01  WS-A  PIC X(8) VALUE 'MODA'."],
              inner=_program("INNER", ["CALL 'B' USING LK-PGM", "CALL LK-PGM"],
                             lk=["01  LK-PGM  PIC X(8)."], using=["LK-PGM"],
                             common=True)
              + _program("B", ["CALL 'INNER' USING LK-B"], lk=["01  LK-B  PIC X(8)."],
                         using=["LK-B"], common=True)),
     "is on a cycle of calls back to"),
])
def test_a_parameter_its_callers_do_not_pin_stays_flagged_and_says_why(outer, why):
    """Every way the CALLs in this source can fail to fix a parameter leaves the target
    a flagged dynamic row - never a guess - and the row's `why` names the failure."""
    man, rows, dyn = _member(outer)
    assert rows[("INNER", "LK-PGM")]["dynamic"] is True
    assert "LK-PGM is a parameter of INNER, and the CALLs in this source do not pin " \
           "it" in dyn[("INNER", "LK-PGM")]["why"]
    assert why in dyn[("INNER", "LK-PGM")]["why"]
    assert any(f.startswith("contained program INNER: program LK-PGM: dynamic target")
               for f in man["flags"])


def test_a_contained_programs_caller_is_excluded_not_sent_to_the_jcl():
    """A contained program's PROCEDURE DIVISION USING is filled by a program in this
    source. A `caller` row would send the reader to a job step or the CSD for it."""
    for name, unit in (("nestdeps.cbl", "NESTLOG"), ("nestedpgm.cbl", "USEMQ")):
        man = _artifacts_example(name)
        assert not [a for a in man["artifacts"] if a["kind"] == "caller"]
        ex = [e for e in man["excluded"] if e.get("containedProgram") == unit]
        assert [(e["name"], e["endpointType"]) for e in ex] == [("CALLER", "caller")]


def test_two_programs_of_one_member_never_share_a_program_local_name():
    """A file name is local to the program that declares it: the main program's CUST-FILE
    and a contained program's are two files on two ddnames, never one merged row."""
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. OUTER.\n"
        "       ENVIRONMENT DIVISION.\n"
        "       INPUT-OUTPUT SECTION.\n"
        "       FILE-CONTROL.\n"
        "           SELECT CUST-FILE ASSIGN TO OUTDD.\n"
        "       DATA DIVISION.\n"
        "       FILE SECTION.\n"
        "       FD  CUST-FILE.\n"
        "       01  CUST-REC  PIC X(80).\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           OPEN OUTPUT CUST-FILE\n"
        "           CALL 'INNER'\n"
        "           GOBACK.\n"
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. INNER.\n"
        "       ENVIRONMENT DIVISION.\n"
        "       INPUT-OUTPUT SECTION.\n"
        "       FILE-CONTROL.\n"
        "           SELECT CUST-FILE ASSIGN TO INDD.\n"
        "       DATA DIVISION.\n"
        "       FILE SECTION.\n"
        "       FD  CUST-FILE.\n"
        "       01  CUST-REC  PIC X(80).\n"
        "       PROCEDURE DIVISION.\n"
        "       1000-IN.\n"
        "           OPEN INPUT CUST-FILE\n"
        "           GOBACK.\n"
        "       END PROGRAM INNER.\n"
        "       END PROGRAM OUTER.\n"
    )
    rows = _by_unit(build_artifacts(build_machine(parse_program(src))))
    assert (rows[(None, "CUST-FILE")]["ddname"], rows[(None, "CUST-FILE")]["io"]) == \
        ("OUTDD", "write")
    assert (rows[("INNER", "CUST-FILE")]["ddname"], rows[("INNER", "CUST-FILE")]["io"]) \
        == ("INDD", "read")


def test_a_call_between_two_programs_contained_here_is_internal():
    """A and B are siblings inside OUTER; A calls B (COMMON). B is a program of this
    source, so from A it is as internal as from OUTER - not a member to request."""
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. OUTER.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           CALL 'A'\n"
        "           GOBACK.\n"
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. A.\n"
        "       PROCEDURE DIVISION.\n"
        "       1000-A.\n"
        "           CALL 'B'\n"
        "           GOBACK.\n"
        "       END PROGRAM A.\n"
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. B IS COMMON.\n"
        "       PROCEDURE DIVISION.\n"
        "       2000-B.\n"
        "           GOBACK.\n"
        "       END PROGRAM B.\n"
        "       END PROGRAM OUTER.\n"
    )
    row = _by_unit(build_artifacts(build_machine(parse_program(src))))[("A", "B")]
    assert (row["classification"], row["identity"]) == ("internal-nested", "internal")


_GLOBMOD_SRC = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. OUTER.\n"
    "       DATA DIVISION.\n"
    "       WORKING-STORAGE SECTION.\n"
    "       01  WS-PGM     PIC X(8) VALUE 'GLOBMOD' GLOBAL.\n"
    "       PROCEDURE DIVISION.\n"
    "       0000-MAIN.\n"
    "           CALL 'INNER'\n"
    "           GOBACK.\n"
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. INNER.\n"
    "       PROCEDURE DIVISION.\n"
    "       1000-IN.\n"
    "           CALL WS-PGM\n"
    "           GOBACK.\n"
    "       END PROGRAM INNER.\n"
    "       END PROGRAM OUTER.\n"
)


def test_a_contained_program_sees_the_outer_programs_global_item():
    """INNER calls WS-PGM, which only OUTER declares - GLOBAL, VALUE 'GLOBMOD'. GLOBAL
    makes it INNER's item too, so the target is GLOBMOD, and the provenance says whose
    declaration it came through."""
    m = build_machine(parse_program(_GLOBMOD_SRC))
    man = build_artifacts(m)
    rows = _by_unit(man)
    assert rows[("INNER", "GLOBMOD")]["via"] == "WS-PGM"
    assert ("INNER", "WS-PGM") not in rows and man["flags"] == []
    label = m.contained[0].provenance["call_GLOBMOD"]["cobol"]
    assert "WS-PGM is declared GLOBAL in OUTER" in label


def test_the_global_fixture_resolves_every_target_through_the_item_each_program_sees():
    """examples/nestglob.cbl: GLOBLEAF's targets come from NESTGLOB's GLOBAL record and
    GLOBAL item (GLOBMID's own WS-OWNPGM is not GLOBAL, so it is not the one GLOBLEAF
    sees), GLOBMID's from its own WS-OWNPGM, and NESTGLOB's from what GLOBMID's SET
    stores in the item they share. Nothing is left flagged."""
    man = _artifacts_example("nestglob.cbl")
    rows = _by_unit(man)
    got = {k: r["via"] for k, r in rows.items() if r.get("via")}
    assert got == {("GLOBLEAF", "GLOBLOG"): "WS-LOGPGM",
                   ("GLOBLEAF", "OUTRMOD"): "WS-OWNPGM",
                   ("GLOBMID", "MIDMOD"): "WS-OWNPGM",
                   (None, "AUDITV2"): "WS-AUDPGM"}
    assert man["flags"] == []


def test_a_contained_program_never_borrows_the_outer_programs_local_storage():
    """Without GLOBAL, OUTER's WS-PGM is OUTER's alone: INNER's analysis cannot pin it,
    so it stays a flagged dynamic row - never named from the outer VALUE."""
    man = build_artifacts(build_machine(parse_program(
        _GLOBMOD_SRC.replace(" GLOBAL.", "."))))
    rows = _by_unit(man)
    assert rows[("INNER", "WS-PGM")]["dynamic"] is True
    assert not [k for k in rows if k[1] == "GLOBMOD"]
    assert any(f.startswith("contained program INNER: program WS-PGM: dynamic target")
               for f in man["flags"])


def test_a_programs_own_declaration_hides_the_outer_global_one():
    """INNER declares WS-PGM itself ('OWNMOD'): that is the item it calls through, not
    OUTER's GLOBAL one."""
    inner = _program("INNER", ["CALL WS-PGM"],
                     ws=["01  WS-PGM  PIC X(8) VALUE 'OWNMOD'."])
    src = _program("OUTER", ["CALL 'INNER'"],
                   ws=["01  WS-PGM  PIC X(8) VALUE 'GLOBMOD' GLOBAL."], inner=inner)
    man, rows, dyn = _member(src)
    assert rows[("INNER", "OWNMOD")]["via"] == "WS-PGM"
    assert not [k for k in rows if k[1] == "GLOBMOD"] and man["flags"] == []


def test_the_nearest_global_declaration_wins_and_a_local_one_between_is_skipped():
    """LEAF names WS-PGM and WS-LOG. MID declares WS-PGM GLOBAL and WS-LOG without it;
    OUTER declares both GLOBAL. A name is looked up in the program, then in the GLOBAL
    declarations of each container outward: WS-PGM is MID's, and WS-LOG - MID's own
    being invisible to LEAF - OUTER's."""
    leaf = _program("LEAF", ["CALL WS-PGM", "CALL WS-LOG"])
    mid = _program("MID", ["CALL 'LEAF'"],
                   ws=["01  WS-PGM  PIC X(8) VALUE 'MIDPGM' GLOBAL.",
                       "01  WS-LOG  PIC X(8) VALUE 'MIDLOG'."], inner=leaf)
    src = _program("OUTER", ["CALL 'MID'"],
                   ws=["01  WS-PGM  PIC X(8) VALUE 'OUTPGM' GLOBAL.",
                       "01  WS-LOG  PIC X(8) VALUE 'OUTLOG' GLOBAL."], inner=mid)
    man, rows, dyn = _member(src)
    assert rows[("LEAF", "MIDPGM")]["via"] == "WS-PGM"
    assert rows[("LEAF", "OUTLOG")]["via"] == "WS-LOG"
    assert not [k for k in rows if k[1] in ("OUTPGM", "MIDLOG")]


def test_a_global_records_subordinates_and_conditions_are_seen():
    """GLOBAL on the 01 makes its subordinates global: INNER calls WS-AUD, which OUTER
    declares under WS-SHARED with no VALUE, and sets it through the 88-level AUDIT-V2 -
    the target is AUDITV2. OUTER calls WS-AUD too, and INNER's SET is a store into the
    item they share, so OUTER's target is AUDITV2 as well."""
    inner = _program("INNER", ["SET AUDIT-V2 TO TRUE", "CALL WS-AUD"])
    src = _program("OUTER", ["CALL 'INNER'", "CALL WS-AUD"],
                   ws=["01  WS-SHARED GLOBAL.", "    05  WS-AUD  PIC X(8).",
                       "        88  AUDIT-V2  VALUE 'AUDITV2'."], inner=inner)
    man, rows, dyn = _member(src)
    assert rows[("INNER", "AUDITV2")]["via"] == "WS-AUD"
    assert rows[(None, "AUDITV2")]["via"] == "WS-AUD"
    assert man["flags"] == []


def test_every_program_sharing_a_global_item_sees_what_the_others_store():
    """OUTER's GLOBAL WS-PGM starts as 'GLOBA'; INNER moves 'GLOBB' into it. Both CALL
    it, and in both the target is one of the two - flagged with both candidates, and
    never just the one each program's own text holds."""
    inner = _program("INNER", ["MOVE 'GLOBB' TO WS-PGM", "CALL WS-PGM"])
    src = _program("OUTER", ["CALL 'INNER'", "CALL WS-PGM"],
                   ws=["01  WS-PGM  PIC X(8) VALUE 'GLOBA' GLOBAL."], inner=inner)
    man, rows, dyn = _member(src)
    for unit in (None, "INNER"):
        assert rows[(unit, "WS-PGM")]["candidates"] == ["GLOBA", "GLOBB"]
    assert "INNER stores 'GLOBB'" in dyn[("OUTER", "WS-PGM")]["why"]


def test_an_item_each_sharer_moves_into_a_global_is_resolved_where_it_is_moved():
    """OUTER moves its WS-A ('MODP') into the GLOBAL WS-PGM and INNER moves its WS-B
    ('MODQ'). Each source item is resolved in the program that moves it, so both
    programs' targets are one of MODP / MODQ - and resolving each needs the other's
    analysis, which must not leave either one flagged as a cycle."""
    inner = _program("INNER", ["MOVE WS-B TO WS-PGM", "CALL WS-PGM"],
                     ws=["01  WS-B  PIC X(8) VALUE 'MODQ'."])
    src = _program("OUTER", ["MOVE WS-A TO WS-PGM", "CALL 'INNER'", "CALL WS-PGM"],
                   ws=["01  WS-PGM  PIC X(8) GLOBAL.",
                       "01  WS-A    PIC X(8) VALUE 'MODP'."], inner=inner)
    man, rows, dyn = _member(src)
    for unit, program in ((None, "OUTER"), ("INNER", "INNER")):
        row = rows[(unit, "WS-PGM")]
        assert row["candidates"] == ["MODP", "MODQ"], program
        assert not row.get("hasVariableAssignment"), program


def test_a_value_another_sharer_stores_from_a_variable_keeps_the_global_flagged():
    """INNER moves WS-IN - set from nothing this source pins - into the GLOBAL WS-PGM.
    OUTER's CALL WS-PGM can then reach a value no reading names: flagged, never the
    VALUE alone, and the reason says which program stores what."""
    inner = _program("INNER", ["MOVE WS-IN TO WS-PGM"], ws=["01  WS-IN  PIC X(8)."])
    src = _program("OUTER", ["CALL 'INNER'", "CALL WS-PGM"],
                   ws=["01  WS-PGM  PIC X(8) VALUE 'GLOBA' GLOBAL."], inner=inner)
    man, rows, dyn = _member(src)
    row = rows[(None, "WS-PGM")]
    assert row["dynamic"] is True and row["candidates"] == ["GLOBA"]
    assert not [k for k in rows if k[1] == "GLOBA"]
    why = dyn[("OUTER", "WS-PGM")]["why"]
    assert "WS-PGM is GLOBAL, so the programs OUTER contains share it" in why
    assert "INNER moves WS-IN into it (WS-IN is declared but never assigned" in why


def test_a_contained_programs_unpinned_global_is_not_called_undeclared():
    """INNER's CALL WS-PGM stays flagged (INNER moves an unpinned value into it), but the
    row must not call WS-PGM undeclared - it is OUTER's GLOBAL item, in plain sight - nor
    send the reader looking for a missing copybook."""
    inner = _program("INNER", ["MOVE WS-IN TO WS-PGM", "CALL WS-PGM"],
                     ws=["01  WS-IN  PIC X(8)."])
    src = _program("OUTER", ["CALL 'INNER'"],
                   ws=["01  WS-PGM  PIC X(8) GLOBAL."], inner=inner)
    man, rows, dyn = _member(src)
    row = dyn[("INNER", "WS-PGM")]
    assert "provisional" not in row
    assert "WS-PGM is declared GLOBAL in OUTER" in row["why"]


# --------------------------------------------------------------------------- #
# ledger item 141: a mapless RECEIVE INTO publishes the area what was typed lands in
# --------------------------------------------------------------------------- #

_MAPLESS_DATA = (
    "       01  WS-TIOA-AREA.\n"
    "           05  WS-TIOA-TASK-ID  PIC X(04).\n"
    "           05  FILLER           PIC X(01).\n"
    "           05  WS-TIOA-FUNCTION PIC X(05).\n"
    "       01  WS-BUF               PIC X(1920).\n"
    "       01  WS-MAPAREA           PIC X(80).\n"
    "       01  WS-CONV              PIC X(80).\n"
    "       01  WS-CONVID            PIC X(04).\n"
    "       01  WS-L                 PIC S9(4) COMP.\n")


def test_a_mapless_receive_publishes_its_area_as_a_terminal_input():
    m = _artifacts_src(
        "           EXEC CICS RECEIVE INTO(WS-TIOA-AREA) LENGTH(WS-L) END-EXEC\n"
        "           EXEC CICS RECEIVE INTO(WS-BUF) BUFFER ASIS LENGTH(WS-L)\n"
        "           END-EXEC\n"
        "           EXEC CICS RETURN END-EXEC.\n", _MAPLESS_DATA)
    assert m["terminalInputs"] == [
        {"line": 16, "into": "WS-TIOA-AREA", "buffer": False, "length": "WS-L"},
        {"line": 17, "into": "WS-BUF", "buffer": True, "length": "WS-L"},
    ]
    assert _by_name(m)["terminal"]["resolvedBy"] == "CICS CSD (TRANSACTION -> PROGRAM)"


def test_a_receive_map_and_a_conversation_receive_are_no_terminal_input():
    m = _artifacts_src(
        "           EXEC CICS RECEIVE MAP('ZZM1') MAPSET('ZZSET')\n"
        "                INTO(WS-MAPAREA) END-EXEC\n"
        "           EXEC CICS RECEIVE CONVID(WS-CONVID) INTO(WS-CONV)\n"
        "                LENGTH(WS-L) END-EXEC\n"
        "           EXEC CICS RETURN END-EXEC.\n", _MAPLESS_DATA)
    assert "terminalInputs" not in m
