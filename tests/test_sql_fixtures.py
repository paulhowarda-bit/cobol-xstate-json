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
