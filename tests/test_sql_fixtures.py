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


def test_dml_write_fields_are_currently_empty():
    # KNOWN GAP: INSERT/UPDATE/DELETE do not yet capture their VALUES/SET host variables,
    # so the renderer cannot show WHAT is written (only SELECT/FETCH capture INTO fields).
    iface = _iface("sqldml.cbl")
    for e in iface["events"]:
        if e["verb"] in ("INSERT", "UPDATE", "DELETE"):
            assert e["fields"] == []


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


def test_unload_cursor_endpoint_is_unresolved_gap():
    # KNOWN GAP: a FETCH has no FROM clause, and DECLARE C1 CURSOR FOR ... FROM ACCOUNT is
    # not linked, so the Db2 endpoint renders as "<cursor>" instead of the table ACCOUNT
    # (the cursor name C1 is not captured either). Blocks showing which table is unloaded.
    iface = _iface("sqlunld.cbl")
    assert "<cursor>" in _dirs(iface, "db2")


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


def test_read_and_write_endpoints_are_asymmetric_gap():
    # KNOWN GAP: READ's endpoint is the FILE name (IN-FILE) but WRITE's is the RECORD name
    # (OUT-REC) - the renderer must normalise record->file to pair a load with its unload.
    read_iface = _iface("sqlload.cbl")
    write_iface = _iface("sqlunld.cbl")
    assert "IN-FILE" in _dirs(read_iface, "file")     # READ -> file name
    assert "OUT-REC" in _dirs(write_iface, "file")    # WRITE -> record name
