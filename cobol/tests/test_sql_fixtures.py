"""Reference fixtures for the external-interface renderer: Db2 DML, cursor unload, file load.

These pin the *renderer-relevant invariants* of the interface overlay for the three SQL
patterns - the endpoint types and directions the boundary drawing depends on - without
over-fitting to endpoint spellings that are still in flux (see the known-gap tests, which
document current behaviour so a later fix is a deliberate change, not a silent one).
"""

from cobol_xstate.parser import parse_program
from cobol_xstate.statechart import build_machine
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _iface(name):
    src = (EXAMPLES / name).read_text()
    return build_machine(parse_program(src), source_name=name).bundle()["interface"]


def _dirs(iface, etype):
    """endpoint -> sorted directions, for endpoints of a given type."""
    return {e["endpoint"]: e["directions"] for e in iface["endpoints"] if e["type"] == etype}


def _verbs(iface):
    return {(e["endpointType"], e["verb"]) for e in iface["events"]}


# --------------------------------------------------------------------------- #
# sqldml: all four DML verbs on one table
# --------------------------------------------------------------------------- #

def test_dml_table_has_both_directions():
    iface = _iface("sqldml.cbl")
    # ACCOUNT is read (SELECT) and written (UPDATE/INSERT/DELETE) -> a bidirectional Db2 node
    assert _dirs(iface, "db2")["ACCOUNT"] == ["create", "get"]
    verbs = _verbs(iface)
    assert ("db2", "SELECT") in verbs
    assert ("db2", "UPDATE") in verbs and ("db2", "INSERT") in verbs and ("db2", "DELETE") in verbs
    # the SELECT carries its INTO host variables as event fields
    sel = next(e for e in iface["events"] if e["verb"] == "SELECT")
    assert sel["fields"] == ["WS-NAME", "WS-BAL"]


def test_dml_write_fields_carry_host_variables():
    # INSERT/UPDATE/DELETE capture their VALUES/SET/WHERE host variables as event
    # fields, so the renderer can show WHAT is written (was a known gap).
    iface = _iface("sqldml.cbl")
    for e in iface["events"]:
        if e["verb"] in ("INSERT", "UPDATE", "DELETE"):
            assert e["fields"], f"{e['verb']} must carry its host variables"
            assert all(not f.startswith(":") for f in e["fields"])
    upd = next(e for e in iface["events"] if e["verb"] == "UPDATE")
    assert set(upd["fields"]) == {"WS-BAL", "WS-ID"}


# --------------------------------------------------------------------------- #
# sqlunld: cursor FETCH -> file WRITE  (Db2 -> file unload)
# --------------------------------------------------------------------------- #

def test_unload_is_db2_get_plus_file_create():
    iface = _iface("sqlunld.cbl")
    verbs = _verbs(iface)
    assert ("db2", "FETCH") in verbs          # reads rows from Db2
    assert ("file", "WRITE") in verbs         # writes them to a file
    assert any(e["endpointType"] == "response" for e in iface["events"])  # SQLCODE 100 end
    # the FETCH carries its INTO host variables
    fetch = next(e for e in iface["events"] if e["verb"] == "FETCH")
    assert fetch["fields"] == ["WS-ID", "WS-NAME", "WS-BAL"]


def test_unload_cursor_endpoint_resolves_to_its_table():
    # DECLARE C1 CURSOR FOR SELECT ... FROM ACCOUNT is linked, so the FETCH's Db2
    # endpoint is the table ACCOUNT, not "<cursor>" (was a known gap).
    iface = _iface("sqlunld.cbl")
    dirs = _dirs(iface, "db2")
    assert "ACCOUNT" in dirs and "<cursor>" not in dirs


# --------------------------------------------------------------------------- #
# sqlload: file READ -> table INSERT  (file -> Db2 load)
# --------------------------------------------------------------------------- #

def test_load_is_file_get_plus_db2_create():
    iface = _iface("sqlload.cbl")
    verbs = _verbs(iface)
    assert ("file", "READ") in verbs          # reads records from a file
    assert ("db2", "INSERT") in verbs         # inserts them into a table
    assert _dirs(iface, "db2")["ACCOUNT"] == ["create"]   # INSERT resolves the table name
    assert "IN-FILE" in _dirs(iface, "file")  # READ uses the file name


def test_read_and_write_endpoints_unify_on_the_file_name():
    # READ's endpoint is the FILE name; WRITE names its RECORD but the FD association
    # resolves it to the physical file, so both directions share one endpoint (was a
    # known gap: the WRITE previously surfaced as a separate OUT-REC "file").
    read_iface = _iface("sqlload.cbl")
    write_iface = _iface("sqlunld.cbl")
    assert "IN-FILE" in _dirs(read_iface, "file")     # READ -> file name
    wdirs = _dirs(write_iface, "file")
    assert "OUT-FILE" in wdirs and "OUT-REC" not in wdirs
    # and the WRITE event carries the record's fields
    wr = next(e for e in write_iface["events"] if e["verb"] == "WRITE")
    assert "OUT-REC" in wr["fields"]


# --------------------------------------------------------------------------- #
# column <-> host-variable: the cross-program state identity
# --------------------------------------------------------------------------- #
#
# A host-variable NAME is program-local: A's WS-BALANCE and B's CUST-BAL may be the same
# state or unrelated. The COLUMN is the database's, shared by every program that reads
# it - so this mapping is the only thing that proves two programs touch the same state.
# See docs/state-graph-plan.md.

def _cols(iface, verb):
    e = next(x for x in iface["events"] if x["verb"] == verb and x.get("columns"))
    return {c["column"]: c["hostVar"] for c in e["columns"]}, e["columns"][0]["table"]


def test_select_maps_each_column_to_its_host_variable():
    cols, table = _cols(_iface("sqlcols.cbl"), "SELECT")
    assert cols["NAME"] == "WS-NAME"
    assert cols["BAL"] == "WS-BAL"          # C.BAL AS B -> the column is BAL
    assert table == "CUSTOMER"              # ADMIN.CUSTOMER -> the TABLE, not the schema


def test_update_set_maps_pairwise():
    """UPDATE ... SET is explicit rather than positional - the best fidelity there is."""
    iface = _iface("sqlcols.cbl")
    upd = next(e for e in iface["events"] if e["verb"] == "UPDATE")
    cols = {c["column"]: c["hostVar"] for c in upd["columns"]}
    assert cols == {"BAL": "WS-BAL", "STATUS": "WS-ST"}


def test_fetch_correlates_against_its_cursors_declare():
    """A cursor splits the information: the columns are on the DECLARE, the host
    variables on the FETCH. Neither statement alone says which fills which."""
    iface = _iface("sqlcols.cbl")
    fetch = next(e for e in iface["events"] if e["verb"] == "FETCH")
    assert [(c["column"], c["hostVar"]) for c in fetch["columns"]] == \
        [("ID", "WS-ID"), ("BAL", "WS-BAL")]


def test_derived_expression_occupies_a_slot_but_names_no_column():
    """SUM(DEBIT, CREDIT) must not break the comma split, and is not a column."""
    iface = _iface("sqlcols.cbl")
    sel = next(e for e in iface["events"]
               if e["verb"] == "SELECT" and e["endpoint"] == "LEDGER")
    assert [(c["column"], c["hostVar"]) for c in sel["columns"]] == [("ID", "WS-ID")]


def test_indicator_variable_refuses_to_correlate():
    """THE hazard: `INTO :WS-NAME, :WS-BAL:IND-BAL` is 2 columns and 3 host variables.
    A naive positional zip maps BAL -> IND-BAL and states it as fact. Wrong lineage is
    worse than none, so it must emit NO mapping and say why."""
    from cobol_xstate.parser import parse_program
    from cobol_xstate.statechart import build_machine
    m = build_machine(parse_program((EXAMPLES / "sqlcols.cbl").read_text()))
    msgs = " ".join(f["message"] for f in m.flags)
    assert "2 column(s) vs 3 host variable(s)" in msgs
    # ...and no event claims a mapping for that SELECT
    for e in m.bundle()["interface"]["events"]:
        for c in e.get("columns", []):
            assert c["hostVar"] != "IND-BAL"


def test_select_star_is_flagged_not_guessed():
    from cobol_xstate.parser import parse_program
    from cobol_xstate.statechart import build_machine
    m = build_machine(parse_program((EXAMPLES / "sqlcols.cbl").read_text()))
    assert any("SELECT *" in f["message"] for f in m.flags)


def test_columns_survive_into_the_emitted_event():
    """build_interface.add() rebuilds the event dict key-by-key, so a new key on the
    classification hit is dropped unless copied there - and lineage/business read the hit
    directly, so it would appear to work in two of three places."""
    iface = _iface("cicsinq.cbl")
    sel = next(e for e in iface["events"] if e["verb"] == "SELECT")
    assert sel["columns"] == [{"table": "CUST", "column": "NAME", "hostVar": "CUST-NAME"},
                              {"table": "CUST", "column": "BAL", "hostVar": "CUST-BALANCE"}]


def test_qualified_table_names_resolve_to_the_table():
    """FROM SCHEMA.ACCOUNT named the SCHEMA as the endpoint, so two programs reading one
    table looked like they read different ones."""
    from cobol_xstate.interface import _SQL_FROM, _SQL_UPDATE, _SQL_INTO_TABLE
    assert _SQL_FROM.search("SELECT X INTO : Y FROM ADMIN . ACCOUNT").group(1) == "ACCOUNT"
    assert _SQL_FROM.search("SELECT X INTO : Y FROM ACCOUNT").group(1) == "ACCOUNT"
    assert _SQL_UPDATE.search("UPDATE S . CUST SET A = : B").group(1) == "CUST"
    assert _SQL_INTO_TABLE.search("INSERT INTO S . T ( A ) VALUES ( : X )").group(1) == "T"
