"""The related-artifact manifest: one row per external thing the program touches
(Db2 tables, files, called programs, queues), each with the identity-resolution chain
its program-local name still needs (docs/mainframe-artifacts.md)."""

from pathlib import Path

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
    """LINK PROGRAM(WS-PGM) with WS-PGM VALUE 'FBSPREST': the manifest row is the
    module name FBSPREST (with the data item it came via), never WS-PGM."""
    man = _artifacts_src(
        "       0000-MAIN.\n"
        "           EXEC CICS LINK PROGRAM(WS-PGM) END-EXEC.\n",
        data_body="       01 WS-PGM PIC X(8) VALUE 'FBSPREST'.\n",
    )
    by = _by_name(man)
    assert "WS-PGM" not in by
    pg = by["FBSPREST"]
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
        "           CALL CN-DCIOC104 USING DC01104-PARMS\n"
        "           GOBACK.\n",
        data_body="       COPY DC01104.\n"
                  "       01 WS-DUMMY PIC X.\n",
    )
    by = _by_name(man)
    pg = by["CN-DCIOC104"]
    assert pg["dynamic"] is True
    assert "not declared in the visible source" in pg["needs"]
    assert "DC01104" in pg["needs"]
    # ...and the copybook row itself is present and marked missing.
    assert by["DC01104"]["status"] == "missing"


def test_dynamic_sql_row_is_honest_about_unknown_tables():
    man = _artifacts_src(
        "       0000-MAIN.\n"
        "           EXEC SQL EXECUTE IMMEDIATE :WS-SQL END-EXEC.\n",
        data_body="       01 WS-SQL PIC X(200).\n",
    )
    row = _by_name(man)["<dynamic-sql>"]
    assert row["kind"] == "db2-table"
    assert row["identity"] == "program-local"
    assert row["dynamic"] is True
    assert row["io"] == "read-write"          # could be either - direction unknowable
    assert row["resolvedBy"] is None
    assert "assembled at run time" in row["needs"]
    assert any("<dynamic-sql>" in f for f in man["flags"])


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
