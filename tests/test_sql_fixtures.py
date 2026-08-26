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
    # INSERT/UPDATE capture the host variables they WRITE as event fields, so the
    # renderer can show WHAT is written (was a known gap).
    iface = _iface("sqldml.cbl")
    for e in iface["events"]:
        if e["verb"] in ("INSERT", "UPDATE"):
            assert e["fields"], f"{e['verb']} must carry its host variables"
        assert all(not f.startswith(":") for f in e["fields"])
    upd = next(e for e in iface["events"] if e["verb"] == "UPDATE")
    assert upd["fields"] == ["WS-BAL"]                 # SET BALANCE = :WS-BAL
    ins = next(e for e in iface["events"] if e["verb"] == "INSERT")
    assert set(ins["fields"]) == {"WS-ID", "WS-NAME", "WS-BAL"}


def test_dml_where_clause_variables_are_parameters_not_fields():
    """A row SELECTOR is not data written, and must not be reported as a field.

    `UPDATE ACCOUNT SET BALANCE = :WS-BAL WHERE ID = :WS-ID` writes one column from one
    variable; :WS-ID picks the row. Carried among the fields it reached the graph loader
    as a write with no column mapping - an "unmapped field" - which is where thousands
    of the v52 warnings came from (docs/issues/unmapped-fields-v52.md, Issue 2). A
    SELECT has always reported its WHERE variables as `params`; these are the same
    thing, said the same way.
    """
    iface = _iface("sqldml.cbl")
    upd = next(e for e in iface["events"] if e["verb"] == "UPDATE")
    assert upd["params"] == ["WS-ID"]
    assert "WS-ID" not in upd["fields"]
    # A DELETE writes nothing anywhere: every host variable it has is a filter.
    dele = next(e for e in iface["events"] if e["verb"] == "DELETE")
    assert dele["fields"] == []
    assert dele["params"] == ["WS-ID"]
    # An INSERT with no WHERE splits into nothing - every VALUES variable is written.
    ins = next(e for e in iface["events"] if e["verb"] == "INSERT")
    assert "params" not in ins


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
    """SUM(DEBIT, CREDIT) must not break the comma split, and is not a column - its
    receiver gets an explicit `derived` entry, so a consumer can SKIP it without also
    hiding a genuinely unrecovered field (the two used to be indistinguishable: both
    were just absent)."""
    iface = _iface("sqlcols.cbl")
    sel = next(e for e in iface["events"]
               if e["verb"] == "SELECT" and e["endpoint"] == "LEDGER")
    assert [(c.get("column"), c["hostVar"], c.get("derived", False))
            for c in sel["columns"]] == [("ID", "WS-ID", False),
                                         (None, "WS-TOT", True)]


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


# --------------------------------------------------------------------------- #
# sqlgaps: the write half, and the shapes that must FLAG rather than fall quiet
# --------------------------------------------------------------------------- #
#
# Everything below was found on a real estate run: 5,370 unrecovered-mapping warnings
# over 1,124 field names. The point of each test is not only that the mapping is now
# recovered, but that whatever is still NOT recovered says so out loud - an absent
# mapping that nobody reports reads exactly like a program with nothing to map.


def _flags(name):
    src = (EXAMPLES / name).read_text()
    return " ".join(f["message"] for f in build_machine(parse_program(src)).flags)


def test_insert_maps_each_column_to_its_values_item():
    """INSERT is the WRITE half of the cross-program state identity. Without it a
    program that only ever inserts contributes no column evidence at all, and its rows
    look unrelated to the rows every reader of that table selects."""
    iface = _iface("sqlgaps.cbl")
    ins = next(e for e in iface["events"]
               if e["verb"] == "INSERT" and e["endpoint"] == "ACCOUNT" and e.get("columns"))
    assert {c["column"]: c["hostVar"] for c in ins["columns"]} == {
        "ID": "WS-ID", "NAME": "WS-NAME", "BAL": "WS-BAL"}
    assert ins["columns"][0]["table"] == "ACCOUNT"


def test_insert_literal_slot_maps_no_field_and_says_so():
    """`VALUES (:WS-ID, CURRENT TIMESTAMP)` writes STAMP from no program field. ID is
    still proven; the column with nothing behind it is reported, not dropped."""
    iface, flags = _iface("sqlgaps.cbl"), _flags("sqlgaps.cbl")
    ins = next(e for e in iface["events"] if e["endpoint"] == "AUDITLOG")
    assert [(c["column"], c["hostVar"]) for c in ins["columns"]] == [("ID", "WS-ID")]
    assert "STAMP" in flags


def test_insert_without_a_column_list_refuses_rather_than_guessing():
    """`INSERT INTO ACCOUNT VALUES (...)` targets the table's DECLARED column order,
    which is not in the source. Zipping it against the VALUES order would invent the
    very identity the mapping exists to prove."""
    assert "INSERT without an explicit column list" in _flags("sqlgaps.cbl")


def test_rowset_fetch_binds_its_cursor_not_the_positioning_keyword():
    """`FETCH NEXT ROWSET FROM C2` - a scan that does not know the positioning keywords
    binds "ROWSET" as the cursor name. That costs more than the mapping: ROWSET matches
    no DECLARE, so the FETCH's endpoint degrades to a phantom `<cursor ROWSET>` instead
    of the table the rows actually come from, and that name propagates into the artifact
    manifest and on into retrieval."""
    iface = _iface("sqlgaps.cbl")
    assert "<cursor ROWSET>" not in _dirs(iface, "db2")
    fetch = next(e for e in iface["events"]
                 if e["verb"] == "FETCH" and e["endpoint"] == "ACCOUNT")
    assert [(c["column"], c["hostVar"]) for c in fetch["columns"]] == \
        [("ID", "WS-ID"), ("NAME", "WS-NAME")]


def test_count_star_is_not_select_star():
    """`SELECT ID, COUNT(*)` has its column list right there. Reading the star inside the
    function as `SELECT *` reported the list as absent - a WRONG answer rather than a
    missing one - and discarded ID's provable mapping along with it."""
    iface, flags = _iface("sqlgaps.cbl"), _flags("sqlgaps.cbl")
    assert "SELECT *" not in flags
    sel = next(e for e in iface["events"] if e["verb"] == "SELECT")
    assert [(c.get("column"), c["hostVar"], c.get("derived", False))
            for c in sel["columns"]] == [("ID", "WS-ID", False),
                                         (None, "WS-N", True)]
    # ...while the slot that genuinely names no column is still reported
    assert "WS-N has no column identity" in flags


def test_fetch_without_a_visible_declare_is_flagged():
    """A cursor DECLAREd in a copybook that did not arrive - or prepared dynamically -
    leaves its FETCH with host variables and no columns. Whether that is recoverable is
    the reviewer's call to make from the flag, not ours to make by staying silent."""
    assert "no DECLARE for cursor C9 is visible" in _flags("sqlgaps.cbl")


def test_why_no_mapping_reaches_the_event_itself():
    """The interface EVENT is what downstream tooling reads, and add() rebuilds it
    key-by-key - so the why-no-mapping note must be copied there explicitly, or an
    unrecoverable site is indistinguishable from a parser failure at exactly the
    consumer that cannot see the flags."""
    iface = _iface("sqlgaps.cbl")
    noted = {(e["verb"], e["endpoint"]): e["columnNote"]
             for e in iface["events"] if e.get("columnNote")}
    assert "no DECLARE for cursor C9" in noted[("FETCH", "<cursor C9>")]
    assert "no column identity" in noted[("SELECT", "ACCOUNT")]      # COUNT(*) slot
    assert "STAMP" in noted[("INSERT", "AUDITLOG")]                  # literal VALUES slot
    assert "without an explicit column list" in noted[("INSERT", "ACCOUNT")]
    # ...and a fully-proven mapping carries no note
    ins = next(e for e in iface["events"]
               if e["verb"] == "INSERT" and e["endpoint"] == "ACCOUNT"
               and e.get("columns"))
    assert "columnNote" not in ins

# --------------------------------------------------------------------------- #
# sqlwscsr: the cursor DECLARE lives in WORKING-STORAGE (whole-stream scan)
# --------------------------------------------------------------------------- #

def _machine(name, synonyms=None):
    src = (EXAMPLES / name).read_text()
    return build_machine(parse_program(src), source_name=name, synonyms=synonyms)


def test_working_storage_cursor_declare_still_correlates_its_fetch():
    """The statement compiler never walks the DATA DIVISION, but production code keeps
    cursor DECLAREs there (beside the DCLGEN, often in a copybook). 77% of one measured
    estate's unmapped lineage fields were FETCHes on exactly such cursors."""
    m = _machine("sqlwscsr.cbl")
    fetch = next(e for e in m.interface()["events"] if e["verb"] == "FETCH")
    assert [(c["column"], c["hostVar"]) for c in fetch["columns"]] == [
        ("FUND_A", "WS-FUND"), ("ACCOUNT_N", "WS-ACCT"), ("BALANCE_A", "WS-BAL")]
    assert not any("FETCH" in f["message"] for f in m.flags)


def test_working_storage_cursor_names_the_real_table_endpoint():
    """Without the scan the endpoint degraded to `<cursor ACCT_CSR>` - a phantom that
    propagated into the artifact manifest and on into retrieval."""
    iface = _iface("sqlwscsr.cbl")
    assert "T_MMAA_ACC_ANAL" in _dirs(iface, "db2")
    assert not any(e["endpoint"].startswith("<cursor")
                   for e in iface["events"] if e["endpointType"] == "db2")


def test_whole_stream_scan_records_the_declaration_with_provenance():
    prog = parse_program((EXAMPLES / "sqlwscsr.cbl").read_text())
    (decl,) = prog.sql_cursors
    assert decl["cursor"] == "ACCT_CSR"
    assert decl["selectList"] == ["FUND_A", "ACCOUNT_N", "BALANCE_A"]
    assert decl["table"] == "T_MMAA_ACC_ANAL"
    assert decl["line"] > 0 and decl["member"] is None


# --------------------------------------------------------------------------- #
# sqldclgen: DECLARE TABLE (DCLGEN) resolves a column-list-less INSERT
# --------------------------------------------------------------------------- #

def test_declare_table_gives_a_column_list_less_insert_its_columns():
    """`INSERT INTO T VALUES (:H)` states no columns; Db2 defines the slots as the
    table's declared order - which the DCLGEN's DECLARE TABLE states in the source."""
    m = _machine("sqldclgen.cbl")
    ins = next(e for e in m.interface()["events"]
               if e["verb"] == "INSERT" and e["endpoint"] == "T_MFER_ERROR")
    assert [(c["column"], c["hostVar"]) for c in ins["columns"]] == [
        ("MFER_ERROR", "MFER-ERROR")]
    assert "columnNote" not in ins
    assert not any("T_MFER_ERROR" in f["message"] for f in m.flags)


def test_declare_table_scan_records_the_declared_order():
    prog = parse_program((EXAMPLES / "sqldclgen.cbl").read_text())
    tables = {t["table"]: t["columns"] for t in prog.declared_tables}
    assert tables == {"T_MFER_ERROR": ["MFER_ERROR"],
                      "T_RTAC_ACCOUNT": ["ACCT_ID", "ACCT_NAME"]}


def test_synonym_insert_without_a_map_flags_and_names_the_remedy():
    """The DECLARE is for the BASE table; the INSERT writes the SYNONYM. That join is
    catalog knowledge - refusing to guess it, and saying what input closes it, is the
    contract."""
    m = _machine("sqldclgen.cbl")
    flagged = [f["message"] for f in m.flags if "RTAC_ACCOUNT" in f["message"]]
    assert flagged and "synonym map" in flagged[0]


def test_synonym_map_resolves_the_insert_and_stamps_the_base_table():
    """With the map supplied, the mapping lands on the BASE table's name - the one the
    DDL declares, which is what cross-program identity joins on."""
    m = _machine("sqldclgen.cbl", synonyms={"RTAC_ACCOUNT": "T_RTAC_ACCOUNT"})
    ins = next(e for e in m.interface()["events"]
               if e["verb"] == "INSERT" and e["endpoint"] == "RTAC_ACCOUNT")
    assert [(c["table"], c["column"], c["hostVar"]) for c in ins["columns"]] == [
        ("T_RTAC_ACCOUNT", "ACCT_ID", "WS-ACCT-ID"),
        ("T_RTAC_ACCOUNT", "ACCT_NAME", "WS-ACCT-NAME")]
    assert not any("RTAC_ACCOUNT" in f["message"] for f in m.flags)
    spec = next(s for s in m.semantics["actions"].values()
                if s.get("verb") == "INSERT" and s.get("table") == "RTAC_ACCOUNT")
    assert spec["columnsFrom"] == "DECLARE TABLE T_RTAC_ACCOUNT via synonym RTAC_ACCOUNT"


# --------------------------------------------------------------------------- #
# sqlproc: EXEC SQL CALL is a stored PROCEDURE, not a table
# --------------------------------------------------------------------------- #

def test_sql_call_is_a_db2_proc_endpoint_not_a_phantom_table():
    """Classified as a table, the call parameters read as 'columns' and downstream
    tooling hunts for Column nodes that cannot exist. db2_proc is the discriminator."""
    iface = _iface("sqlproc.cbl")
    assert _dirs(iface, "db2_proc") == {"PCBEN171": ["create", "get"]}
    assert "PCBEN171" not in _dirs(iface, "db2")
    calls = [e for e in iface["events"] if e["verb"] == "CALL"]
    assert {e["direction"] for e in calls} == {"get", "create"}
    for e in calls:
        assert e["endpointType"] == "db2_proc"
        assert e["fields"] == ["IN-MESSAGE", "OUT-RETURN-CODE"]
        assert "columns" not in e
        assert "not table columns" in e["columnNote"]


def test_sql_call_lands_in_the_artifact_manifest_as_a_stored_procedure():
    from cobol_xstate.artifacts import build_artifacts
    art = build_artifacts(_machine("sqlproc.cbl"))
    row = next(r for r in art["artifacts"] if r["artifact"] == "PCBEN171")
    assert row["kind"] == "db2-stored-procedure"
    assert "signature" in row["needs"]


def test_synonym_map_flag_reaches_the_run(tmp_path):
    """--synonym-map is the CLI door for the catalog knowledge: same run, one input."""
    import json
    from cobol_xstate.cli import run
    smap = tmp_path / "syn.json"
    smap.write_text(json.dumps({"RTAC_ACCOUNT": "T_RTAC_ACCOUNT"}), encoding="utf-8")
    out = tmp_path / "o"
    rc = run([str(EXAMPLES / "sqldclgen.cbl"), "--outdir", str(out), "--no-fetch",
              "--synonym-map", str(smap), "-qq"])
    assert rc == 0
    doc = json.loads((out / "sqldclgen.json").read_text(encoding="utf-8"))
    ins = next(e for e in doc["interface"]["events"]
               if e["verb"] == "INSERT" and e["endpoint"] == "RTAC_ACCOUNT")
    assert [c["table"] for c in ins["columns"]] == ["T_RTAC_ACCOUNT",
                                                    "T_RTAC_ACCOUNT"]
    assert not any("RTAC_ACCOUNT" in f["message"] for f in doc["flags"])


def test_synonym_map_that_is_not_a_string_map_is_exit_2(tmp_path):
    from cobol_xstate.cli import run
    bad = tmp_path / "bad.json"
    bad.write_text('{"A": 1}', encoding="utf-8")
    rc = run([str(EXAMPLES / "sqldclgen.cbl"), "--outdir", str(tmp_path / "o"),
              "--no-fetch", "--synonym-map", str(bad), "-qq"])
    assert rc == 2


# --------------------------------------------------------------------------- #
# sqlqual: qualified host variables
# (docs/issues/unmapped-fields-v52.md, Issue 3)
# --------------------------------------------------------------------------- #

def test_qualified_host_vars_resolve_to_the_elementary_field():
    """`:GFAC . AC-ACC-N` names the field AC-ACC-N, not the group GFAC.

    Reading only the word after the ':' answered the GROUP, so all three VALUES slots
    came back as the one repeated name "GFAC": it matched no column, and the data
    dictionary has no such field for the lineage to thread.
    """
    iface = _iface("sqlqual.cbl")
    ins = next(e for e in iface["events"] if e["verb"] == "INSERT")
    assert ins["fields"] == ["AC-MULTI-CO-N", "AC-ACC-N", "AC-CSDN-C"]
    assert [(c["column"], c["hostVar"]) for c in ins["columns"]] == [
        ("MULTI_CO_N", "AC-MULTI-CO-N"),
        ("ACC_N", "AC-ACC-N"),
        ("CSDN_C", "AC-CSDN-C"),
    ]


def test_qualified_host_var_maps_in_a_set_clause_and_an_into_clause():
    # Three separate colon scanners had to agree on the name: the statement-wide one
    # (fields), the WHERE one (params), and the INTO one (a SELECT's targets).
    iface = _iface("sqlqual.cbl")
    upd = next(e for e in iface["events"]
               if e["verb"] == "UPDATE" and e.get("columns"))
    assert [(c["column"], c["hostVar"]) for c in upd["columns"]] == [("BAL_A", "AC-BAL-A")]
    assert upd["params"] == ["AC-ACC-N"]
    sel = next(e for e in iface["events"] if e["verb"] == "SELECT")
    assert sel["fields"] == ["AC-BAL-A"]
    assert sel["params"] == ["AC-ACC-N"]


def test_a_qualified_value_with_an_indicator_is_still_refused():
    """Resolving the NAME does not make the slot 1:1.

    `:GFAC . AC-BAL-A :WS-IND-BAL` hangs a null indicator off the value, so it fills no
    single column - the same refusal an unqualified `:WS-BAL:IND-BAL` already gets.
    """
    iface = _iface("sqlqual.cbl")
    ind = next(e for e in iface["events"]
               if e["verb"] == "UPDATE" and not e.get("columns"))
    assert "AC-BAL-A" in ind["fields"]
    assert ind["params"] == ["AC-ACC-N"]


def test_qualified_lineage_names_fields_the_data_dictionary_holds():
    """The point of choosing the elementary name: the lineage row is traceable.

    A row for "GFAC" names a GROUP - the reader cannot tell which of its four fields
    crossed, and no column matches it either.
    """
    from cobol_xstate.lineage import build_lineage
    src = (EXAMPLES / "sqlqual.cbl").read_text()
    m = build_machine(parse_program(src), source_name="sqlqual.cbl")
    fields = {r["field"] for r in build_lineage(m)["rows"]}
    assert "GFAC" not in fields
    assert {"AC-MULTI-CO-N", "AC-ACC-N", "AC-CSDN-C", "AC-BAL-A"} <= fields
    assert all(f in m.data for f in fields if f.startswith("AC-"))


# --------------------------------------------------------------------------- #
# sqlderiv: what a DERIVED slot was made of
# (docs/issues/unmapped-fields-v52.md, Issue 1)
# --------------------------------------------------------------------------- #

def _derived(iface, host_var):
    for e in iface["events"]:
        for c in (e.get("columns") or []):
            if c["hostVar"] == host_var:
                return c
    raise AssertionError(f"no column entry for {host_var}")


def test_a_derived_slot_never_gains_a_column_identity():
    """The refusal is the point, and it survives the recovery.

    `SUM(SPOKE_DOL_A)` aggregates over many rows: the host variable is NOT that column,
    and mapping it would be an invented identity that two programs would then appear to
    share. Every derived entry keeps `derived` and gains no `column` key - what it gains
    is provenance about where the expression READ.
    """
    iface = _iface("sqlderiv.cbl")
    for e in iface["events"]:
        for c in (e.get("columns") or []):
            assert not (c.get("derived") and "column" in c)


def test_an_aggregates_source_column_is_kept_as_provenance():
    iface = _iface("sqlderiv.cbl")
    assert _derived(iface, "W-TOTAL-SPOKE") == {
        "table": "T_MMJT_JRNL_TXN", "hostVar": "W-TOTAL-SPOKE", "derived": True,
        "expression": "SUM", "derivedFrom": ["SPOKE_DOL_A"]}
    # Nested: `VALUE(SUM(COL), 0)` reports the outermost function and recurses through
    # its arguments, so the innermost NAMED column is the source.
    nested = _derived(iface, "W-TOTAL-HST")
    assert nested["expression"] == "VALUE"
    assert nested["derivedFrom"] == ["TRX_RUN_COLL_BAL_A"]


def test_a_slot_with_no_source_column_says_so_with_an_empty_list():
    """An empty `derivedFrom` is a FACT, not a failure to look.

    `SELECT 'Y'` and `COUNT(*)` genuinely read no column, and saying so is what tells
    them apart from a SUM whose source was merely lost - which the reader could not do
    when both were a bare `{"derived": true}`.
    """
    iface = _iface("sqlderiv.cbl")
    assert _derived(iface, "W-EXISTS-SW")["expression"] == "literal"
    assert _derived(iface, "W-EXISTS-SW")["derivedFrom"] == []
    assert _derived(iface, "W-ROW-COUNT")["expression"] == "COUNT"
    assert _derived(iface, "W-ROW-COUNT")["derivedFrom"] == []


def test_an_expression_over_columns_reports_all_of_them():
    assert _derived(_iface("sqlderiv.cbl"), "W-EXTENDED")["derivedFrom"] == ["QTY", "PRICE"]


def test_a_case_expression_refuses_to_name_its_sources():
    """Which branch supplied the value is a RUN-TIME fact.

    Listing both HIGH_C and LOW_C would claim two dependencies where the source proves
    at most one - flag it as derived, do not guess what it derived from.
    """
    case = _derived(_iface("sqlderiv.cbl"), "W-BANDING")
    assert case["expression"] == "CASE"
    assert case["derivedFrom"] == []


def test_a_fetch_takes_its_derivation_from_the_cursors_declare():
    """A cursor splits the evidence: the DECLARE has `SUM(ADJ_A)`, the FETCH has the
    host variable, and neither statement alone says the slot came from ADJ_A."""
    iface = _iface("sqlderiv.cbl")
    fetch = next(e for e in iface["events"] if e["verb"] == "FETCH")
    assert [c.get("column") for c in fetch["columns"]] == [
        "FBSI_BRCH_C", "FBSI_BASE_C", None]
    assert fetch["columns"][2] == {
        "table": "RTOA_ADJUSTMENT", "hostVar": "W-SUM-ADJ", "derived": True,
        "expression": "SUM", "derivedFrom": ["ADJ_A"]}


# --------------------------------------------------------------------------- #
# why the columns are missing, as a token a consumer can branch on
# (docs/issues/unmapped-fields-v52.md, Issue 4)
# --------------------------------------------------------------------------- #

def test_an_unresolvable_event_says_why_in_a_stable_token():
    """One known unknown, said once - not once per field.

    The v52 trace reported each of a cursor-less FETCH's host variables as its own
    unmapped field: 7,444 warnings over 259 events. The prose note always said why, but
    branching on prose means matching wording that was never a contract, so the whole
    event could not be skipped cleanly.
    """
    iface = _iface("sqlgaps.cbl")
    fetch = next(e for e in iface["events"] if e["endpoint"] == "<cursor C9>")
    assert fetch["columnsUnresolved"] == "cursor-declare-missing"
    nocols = next(e for e in iface["events"]
                  if e["verb"] == "INSERT" and e["endpoint"] == "ACCOUNT"
                  and not e.get("columns"))
    assert nocols["columnsUnresolved"] == "insert-no-column-list"


def test_a_residual_note_is_not_an_unresolved_event():
    """A derived slot and a literal-written column are RESOLVED, with a documented gap.

    Both carry a note, and neither is a recovery failure - marking them unresolved would
    put the working mappings that sit beside them out of reach of a consumer that skips
    on the token.
    """
    iface = _iface("sqlgaps.cbl")
    for e in iface["events"]:
        if e.get("columns") and any(c.get("column") for c in e["columns"]):
            assert "columnsUnresolved" not in e
