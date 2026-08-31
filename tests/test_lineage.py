"""Stage 6 projection: field lineage across the external boundary (--target lineage).

Every assertion here is hand-checkable against examples/lineage.cbl, which is written so
each row has one obviously-correct answer: the caller passes LK-CUST/LK-QTY, the program
ACCEPTs a rate, CALLs SUBFEE BY REFERENCE, STRINGs two fields, and writes a file.
"""

from pathlib import Path

import pytest

from cobol_xstate.lineage import build_lineage
from cobol_xstate.parser import parse_program
from cobol_xstate.statechart import build_machine

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _lin(name: str) -> dict:
    src = (EXAMPLES / name).read_text()
    return build_lineage(build_machine(parse_program(src), source_name=name))


def _row(d: dict, field: str, direction: str = "output") -> dict:
    rows = [r for r in d["rows"] if r["field"] == field and r["direction"] == direction]
    assert rows, f"no {direction} row for {field}"
    return rows[0]


def _origins(row: dict) -> set:
    return {o["event"] for o in row["origins"]}


# --------------------------------------------------------------------------- #
# shape
# --------------------------------------------------------------------------- #

def test_lineage_shape():
    d = _lin("lineage.cbl")
    assert d["format"] == "cobol-xstate-lineage"
    assert d["program"] == "LINEAGE"
    for r in d["rows"]:
        assert r["direction"] in ("input", "output")
        assert r["event"].startswith(("GET.", "CREATE."))
        assert "field" in r and "changedByProgram" in r and "origins" in r


# --------------------------------------------------------------------------- #
# the core question: which event is responsible for this field?
# --------------------------------------------------------------------------- #

def test_linkage_value_traced_to_the_caller_two_hops():
    # MOVE LK-CUST TO WS-NAME; MOVE WS-NAME TO OUT-NAME; WRITE.
    # OUT-NAME's value originates with the caller, two assignments back.
    r = _row(_lin("lineage.cbl"), "OUT-NAME")
    assert _origins(r) == {"GET.CALLER.CALLER"}
    assert r["changedByProgram"] is True          # the program does MOVE it


def test_computed_field_carries_every_contributing_origin():
    # COMPUTE OUT-FEE = LK-QTY * WS-RATE -> caller AND console.
    r = _row(_lin("lineage.cbl"), "OUT-FEE")
    assert _origins(r) == {"GET.CALLER.CALLER", "GET.CONSOLE.SYSIN"}


def test_input_event_field_is_not_a_program_change():
    # ACCEPT fills WS-RATE from outside; the program did not compute it.
    r = _row(_lin("lineage.cbl"), "WS-RATE", direction="input")
    assert _origins(r) == {"GET.CONSOLE.SYSIN"}
    assert r["changedByProgram"] is False


def test_call_by_reference_is_a_maybe_origin_naming_the_program():
    # CALL 'SUBFEE' USING WS-REF: the callee may rewrite it and we cannot see inside.
    r = _row(_lin("lineage.cbl"), "WS-REF")
    o = next(x for x in r["origins"] if x["event"] == "CREATE.PROGRAM.SUBFEE")
    assert o["maybe"] is True
    assert o["resolvedBy"] == "SUBFEE"            # names what would resolve it


def test_string_dependency_is_modeled_even_though_its_value_is_not():
    # STRING WS-NAME WS-REF INTO WS-MEMO; MOVE WS-MEMO TO OUT-MEMO.
    # The value semantics of STRING are not modeled, but the DEPENDENCY is - so the
    # chain survives and OUT-MEMO carries both contributors.
    r = _row(_lin("lineage.cbl"), "OUT-MEMO")
    assert "GET.CALLER.CALLER" in _origins(r)         # via WS-NAME <- LK-CUST
    assert "CREATE.PROGRAM.SUBFEE" in _origins(r)     # via WS-REF <- maybe SUBFEE


def test_group_unions_its_children():
    d = _lin("lineage.cbl")
    rec = _origins(_row(d, "OUT-REC"))
    kids = set()
    for f in ("OUT-NAME", "OUT-FEE", "OUT-MEMO"):
        kids |= _origins(_row(d, f))
    assert rec == kids


# --------------------------------------------------------------------------- #
# flow: loops, PERFORM call/return
# --------------------------------------------------------------------------- #

def test_accumulator_in_a_loop_resolves_to_the_file_not_itself():
    # custrpt: ADD CUST-AMT TO WS-TOTAL inside a READ loop, then DISPLAY WS-TOTAL.
    # WS-TOTAL depends on itself across iterations; the self-reference must collapse
    # and leave the file READ as the origin.
    r = _row(_lin("custrpt.cbl"), "WS-TOTAL")
    assert _origins(r) == {"GET.FILE.CUST-FILE"}
    assert r["changedByProgram"] is True


def test_origin_crosses_a_perform_boundary():
    # lineage.cbl writes OUT-REC in 0000-MAIN, but its fields are set inside the
    # PERFORMed 1000-BUILD. The call must be followed for the origins to reach the WRITE.
    assert _origins(_row(_lin("lineage.cbl"), "OUT-NAME")) == {"GET.CALLER.CALLER"}


def test_unload_traces_db2_row_to_the_written_record():
    # sqlunld: FETCH INTO :WS-ID -> MOVE WS-ID TO OUT-ID -> WRITE OUT-REC.
    d = _lin("sqlunld.cbl")
    assert _origins(_row(d, "OUT-ID")) == {"GET.DB2.ACCOUNT"}
    assert _origins(_row(d, "OUT-BAL")) == {"GET.DB2.ACCOUNT"}


def test_every_fixture_produces_lineage_without_crashing():
    for f in sorted(EXAMPLES.glob("*.cbl")):
        d = build_lineage(build_machine(parse_program(f.read_text()), source_name=f.name))
        assert d["format"] == "cobol-xstate-lineage"
        assert isinstance(d["rows"], list)


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #

def _run_dir(root):
    """Where a run writes: --outdir itself, taken literally with nothing appended."""
    return Path(root)


def test_cli_lineage_target_writes_its_own_file(tmp_path):
    import json
    from cobol_xstate.cli import run
    rc = run([str(EXAMPLES / "lineage.cbl"), "--target", "lineage",
              "--outdir", str(tmp_path)])
    assert rc == 0
    out = _run_dir(tmp_path) / "lineage.lineage.json"   # peer artifact, not the bundle
    assert out.exists()
    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["format"] == "cobol-xstate-lineage"


# --------------------------------------------------------------------------- #
# the lineage json is a COMPANION of the bundle: one run writes both
# --------------------------------------------------------------------------- #

def test_default_run_writes_bundle_and_lineage_side_by_side(tmp_path):
    import json
    from cobol_xstate.cli import run
    rc = run([str(EXAMPLES / "lineage.cbl"), "--outdir", str(tmp_path)])
    assert rc == 0
    d = _run_dir(tmp_path)
    bundle, lin = d / "lineage.json", d / "lineage.lineage.json"
    assert bundle.exists() and lin.exists()      # the machine, and its table
    assert json.loads(bundle.read_text(encoding="utf-8"))["format"] == "xstate-v5-config"
    assert json.loads(lin.read_text(encoding="utf-8"))["format"] == "cobol-xstate-lineage"


def test_the_lineage_companion_lands_in_the_same_run_directory(tmp_path):
    """Every artifact of a run shares one directory - there is no mechanism that could
    separate a companion from its bundle."""
    from cobol_xstate.cli import run
    assert run([str(EXAMPLES / "lineage.cbl"), "--outdir", str(tmp_path)]) == 0
    d = _run_dir(tmp_path)
    assert (d / "lineage.json").exists()
    assert (d / "lineage.lineage.json").exists()


def test_no_lineage_opts_out(tmp_path):
    from cobol_xstate.cli import run
    assert run([str(EXAMPLES / "lineage.cbl"), "--no-lineage",
                "--outdir", str(tmp_path)]) == 0
    d = _run_dir(tmp_path)
    assert (d / "lineage.json").exists()
    assert not (d / "lineage.lineage.json").exists()


def test_machine_only_writes_the_bare_config_alone(tmp_path):
    from cobol_xstate.cli import run
    assert run([str(EXAMPLES / "lineage.cbl"), "--machine-only",
                "--outdir", str(tmp_path)]) == 0
    d = _run_dir(tmp_path)
    assert (d / "lineage.json").exists()
    assert not (d / "lineage.lineage.json").exists()


def test_the_bundle_is_the_faithful_machine_not_a_view(tmp_path):
    import json
    from cobol_xstate.cli import run
    assert run([str(EXAMPLES / "lineage.cbl"), "--outdir", str(tmp_path)]) == 0
    bundle = json.loads(
        (_run_dir(tmp_path) / "lineage.json").read_text(encoding="utf-8"))
    assert bundle["format"] == "xstate-v5-config"
    assert bundle["metadata"].get("view") is None


# --------------------------------------------------------------------------- #
# cross-program join keys: rows from N programs must be concatenable
# --------------------------------------------------------------------------- #

def test_every_row_names_its_program():
    """`program` lives on the ROW, not just at the top of the file: rows from many
    programs get concatenated to answer 'what touches this state?', and a top-level
    field does not survive that."""
    d = _lin("custrpt.cbl")
    assert d["rows"]
    assert all(r["program"] == "CUSTRPT" for r in d["rows"])


def test_copybook_field_carries_its_member_as_the_shared_identity():
    """A field name is program-LOCAL. What proves two programs touch the same state is a
    shared declaration - here, the copybook."""
    from cobol_xstate.parser import CopybookResolver
    src = (EXAMPLES / "cicsinq.cbl").read_text()
    m = build_machine(parse_program(src, resolver=CopybookResolver(paths=[str(EXAMPLES)])),
                      source_name="cicsinq.cbl")
    rows = {r["field"]: r for r in build_lineage(m)["rows"]}
    assert rows["CUST-BALANCE"]["member"] == "CUSTREC"


def test_file_record_field_carries_its_file():
    rows = {r["field"]: r for r in _lin("custrpt.cbl")["rows"]}
    assert rows["CUST-AMT"]["file"] == "CUST-FILE"      # FD children inherit it
    assert rows["CUST-REC"]["file"] == "CUST-FILE"


def test_inline_field_has_no_identity_key_rather_than_a_guessed_one():
    """WS-TOTAL is declared inline: nothing in the code proves another program's
    similarly-named field is the same state. It must carry NEITHER key - an honest
    'unresolvable' beats a plausible match."""
    rows = {r["field"]: r for r in _lin("custrpt.cbl")["rows"]}
    ws = rows["WS-TOTAL"]
    assert "member" not in ws and "file" not in ws


# --------------------------------------------------------------------------- #
# guard conditions: the other half of a business rule
# --------------------------------------------------------------------------- #
#
# "Where did this value come from" names the writer; the CONDITION is the rule. For a
# requirements reader, "DAILYPOST changes the balance" and "DAILYPOST changes the balance
# WHEN the transaction is a deposit" are different statements, and only the second is
# worth anything. examples/condlin.cbl is written so every row has one right answer.

def _cond_row(d: dict, state: str) -> dict:
    rows = [r for r in d["rows"]
            if r["state"] == state and r["field"] == "OUT-CODE"
            and r["direction"] == "output"]
    assert len(rows) == 1, f"{state}: expected one OUT-CODE row, got {len(rows)}"
    return rows[0]


def _exprs(row: dict):
    return {c["expr"] for c in row.get("conditions", [])}


def test_a_guarded_write_reports_the_guard():
    row = _cond_row(_lin("condlin.cbl"), "1000-GUARDED__seq2")
    assert _exprs(row) == {"CUST-ACTIVE"}
    assert not row.get("conditionsPartial")


def test_the_write_inside_a_tail_if_is_reported_at_all():
    """The regression that motivated the _successors fix: a paragraph whose last
    statement is `IF X ... END-IF` branches INWARD on X and falls out of the performed
    range otherwise. Wiring the return used to replace the whole successor list, deleting
    the inward branch - so this WRITE, and every event inside any tail IF, silently
    produced no row at all. Absence of a row reads as "this program never does that"."""
    d = _lin("condlin.cbl")
    assert any(r["state"] == "1000-GUARDED__seq2" for r in d["rows"])


def test_an_if_else_that_rejoins_is_not_conditional():
    """Both branches reach the WRITE, so nothing guards it. `A` and `NOT A` must cancel
    rather than pile up - and it must not be flagged partial either, or every join in
    every program would carry a warning that means nothing."""
    row = _cond_row(_lin("condlin.cbl"), "2000-REJOIN__seq3")
    assert "conditions" not in row
    assert not row.get("conditionsPartial")


def test_when_other_reports_the_negation_of_the_branches_before_it():
    """WHEN OTHER carries no guard of its own. Its condition is exactly the negation of
    every WHEN above it - which is the business rule ("none of the known kinds")."""
    row = _cond_row(_lin("condlin.cbl"), "3000-OTHER__seq8")
    assert _exprs(row) == {"NOT (WS-KIND = 'P')", "NOT (WS-KIND = 'Q')"}
    assert all(c["negated"] for c in row["conditions"])


def test_a_disjunction_is_refused_rather_than_half_reported():
    """THE hazard. 4900-EMIT is performed from two guarded sites, so it runs under
    `A OR B` - which a conjunction cannot state. Reporting either guard alone would be a
    plain lie (it would say the write needs A when B alone also does it), and reporting
    nothing silently would read as unconditional. It must report neither and say so."""
    row = _cond_row(_lin("condlin.cbl"), "4900-EMIT")
    assert "conditions" not in row
    assert row["conditionsPartial"] is True
    assert "disjunction" in row["conditionsNote"]


def test_conditions_are_sound_on_the_real_evaluate_program():
    """banktran dispatches on WS-TRAN-TYPE inside a read loop, so the CALL to POSTLOG is
    governed by both the loop test and the branch - and by nothing else."""
    row = next(r for r in _lin("banktran.cbl")["rows"]
               if r["event"] == "CREATE.PROGRAM.POSTLOG")
    assert _exprs(row) == {"NOT (WS-EOF = 'Y')", "WS-TRAN-TYPE = 'D'"}
    assert not row.get("conditionsPartial")


def test_loop_history_does_not_fake_a_partial():
    """The MAY set is contaminated by earlier loop iterations: reaching the deposit
    branch on pass 2 means pass 1 went somewhere else, so `NOT (TRAN-TYPE = D)` is in MAY
    even though the deposit branch plainly requires it to be true. Both polarities of a
    guard must cancel, or every event inside every loop gets a bogus warning."""
    row = next(r for r in _lin("banktran.cbl")["rows"]
               if r["event"] == "CREATE.PROGRAM.POSTLOG")
    assert "WS-TRAN-TYPE = 'D'" in _exprs(row)
    assert not row.get("conditionsPartial")


def test_control_and_business_guards_are_told_apart():
    """A loop's UNTIL test and an EOF check are plumbing; the EVALUATE branch is the
    rule. A reader gathering requirements needs to filter one from the other."""
    row = next(r for r in _lin("banktran.cbl")["rows"]
               if r["event"] == "CREATE.PROGRAM.POSTLOG")
    kinds = {c["expr"]: c["kind"] for c in row["conditions"]}
    assert kinds["NOT (WS-EOF = 'Y')"] == "control"
    assert kinds["WS-TRAN-TYPE = 'D'"] == "business"


def test_each_condition_carries_its_source_line():
    row = _cond_row(_lin("condlin.cbl"), "3000-OTHER__seq8")
    assert all(isinstance(c.get("line"), int) and c["line"] > 0
               for c in row["conditions"])


def test_a_write_site_carries_the_condition_it_happens_under():
    """changedBy names the assignment; without its condition it says a program touches a
    field but not when, which is the half that matters for merging programs by state."""
    d = _lin("custrpt.cbl")
    row = next(r for r in d["rows"] if r.get("changedBy"))
    entry = row["changedBy"][0]
    assert entry["conditions"], "a write inside a read loop is not unconditional"
    assert entry["conditions"][0]["expr"] == "NOT (WS-EOF = 'Y')"


def test_origins_deliberately_carry_no_conditions():
    """An origin reaches a field through a CHAIN of assignments, so its true condition is
    the conjunction along the whole chain. Tagging it with any single link's condition
    would look like the answer without being it - so it carries none, and the note says
    why rather than leaving a reader to assume."""
    d = _lin("lineage.cbl")
    for r in d["rows"]:
        for o in r["origins"]:
            assert "conditions" not in o
    assert "NOT attached to 'origins'" in d["note"]


def test_a_guard_whose_test_was_not_recovered_is_marked_not_invented():
    """ALTER switches and computed GO TO produce a branch whose EXISTENCE is a fact but
    whose test is not recoverable - the machine records it as {op:'raw'}. No example
    program produces one, so this exercises the renderer directly rather than asserting
    it vacuously over a corpus that cannot reach the branch."""
    from cobol_xstate.lineage import _cond_text
    assert _cond_text("SWITCH_1", {"op": "raw", "text": "ALTERed"}, False) is None
    assert _cond_text("MYSTERY", None, False) is None
    assert _cond_text("X", {"op": "rel", "left": "A", "rel": "=", "right": "1"},
                      False) == "A = 1"
    assert _cond_text("X", {"op": "rel", "left": "A", "rel": "=", "right": "1"},
                      True) == "NOT (A = 1)"


def test_end_of_stream_guards_are_rendered_not_called_unrecoverable():
    """A file's AT END guard is synthesized by the READ lowering and has no expression
    tree - but its meaning is not in doubt. Marking it 'unrecoverable' would cry wolf on
    the most ordinary branch in COBOL and devalue the marker where it matters."""
    from cobol_xstate.lineage import _cond_text
    assert _cond_text("IN-FILE_atEnd", None, False) == "IN-FILE AT END"
    assert _cond_text("IN-FILE_atEnd", None, True) == "NOT (IN-FILE AT END)"
    assert _cond_text("IN-FILE_notAtEnd", None, False) == "IN-FILE NOT AT END"
    for name in ("sqlload.cbl", "custrpt.cbl", "banktran.cbl"):
        for r in _lin(name)["rows"]:
            for c in r.get("conditions", []):
                assert ("expr" in c) ^ bool(c.get("unrecoverable"))
                assert not (c["guard"].lower().endswith("atend")
                            and c.get("unrecoverable"))


def test_the_not_at_end_arm_is_control_not_a_business_decision():
    """`IN-FILE_notAtEnd` does not end in `_atEnd`, so the classifier missed it and
    called the NOT AT END arm of a READ a *business* rule - exactly backwards, and it
    misled `--target business` the same way."""
    from cobol_xstate.business import _is_control_guard
    assert _is_control_guard("IN-FILE_atEnd", None)
    assert _is_control_guard("IN-FILE_notAtEnd", None)
    assert _is_control_guard("UNTIL_WS-EOF_eq_Y", {"op": "rel"})
    assert not _is_control_guard("WS-TRAN-TYPE_eq_D", {"op": "rel"})
    for r in _lin("sqlload.cbl")["rows"]:
        for c in r.get("conditions", []):
            if c["guard"].lower().endswith("atend"):
                assert c["kind"] == "control"


# --------------------------------------------------------------------------- #
# a subprogram whose output IS the COMMAREA (review finding J10)
# --------------------------------------------------------------------------- #

def _lin_src(src: str) -> dict:
    return build_lineage(build_machine(parse_program(src), source_name="sub"))


_SUBFEE = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. SUBFEE.\n"
    "       DATA DIVISION.\n"
    "       WORKING-STORAGE SECTION.\n"
    "       01 WS-RATE PIC 9(3)V99 VALUE 1.50.\n"
    "       LINKAGE SECTION.\n"
    "       01 DFHCOMMAREA.\n"
    "          05 CA-QTY  PIC 9(5).\n"
    "          05 CA-FEE  PIC 9(7)V99.\n"
    "          05 CA-FLAG PIC X.\n"
    "       PROCEDURE DIVISION USING DFHCOMMAREA.\n"
    "       0000-MAIN.\n"
    "           COMPUTE CA-FEE = CA-QTY * WS-RATE\n"
    "           MOVE 'Y' TO CA-FLAG\n"
    "           MOVE 0 TO RETURN-CODE\n"
    "           GOBACK.\n"
)


def test_commarea_output_subprogram_has_a_lineage_table():
    # writing a LINKAGE field is the caller-visible output; the event classifier does not
    # see it, so this table used to be EMPTY while the interface listed the same fields.
    d = _lin_src(_SUBFEE)
    out = {r["field"] for r in d["rows"] if r["direction"] == "output"}
    assert {"CA-FEE", "CA-FLAG", "RETURN-CODE"} <= out, f"missing outputs, got {out}"


def test_commarea_output_field_traces_back_to_the_caller_input():
    # CA-FEE = CA-QTY * WS-RATE, and CA-QTY is a caller input, so its origin is the caller
    d = _lin_src(_SUBFEE)
    fee = _row(d, "CA-FEE")
    assert fee["changedByProgram"] is True
    assert _origins(fee) == {"GET.CALLER.CALLER"}


def test_leaves_lists_every_field_of_a_wide_record():
    from cobol_xstate.interface import _DataView
    lines = ["       IDENTIFICATION DIVISION.", "       PROGRAM-ID. WIDE.",
             "       DATA DIVISION.", "       LINKAGE SECTION.", "       01 DFHCOMMAREA."]
    lines += [f"          05 CA-F{i:03d} PIC X(2)." for i in range(80)]
    lines += ["       PROCEDURE DIVISION USING DFHCOMMAREA.", "       0000-MAIN.",
              "           MOVE 'AB' TO CA-F079.", "           GOBACK."]
    m = build_machine(parse_program("\n".join(lines) + "\n"), source_name="wide")
    leaves = _DataView(m.data).leaves("DFHCOMMAREA")
    # all 80 present - not silently capped at 64
    assert len(leaves) == 80
    assert "CA-F079" in leaves and "CA-F064" in leaves


# --------------------------------------------------------------------------- #
# a cursor FETCH's host-var <-> Db2 column correlation (review finding J16 #4)
# --------------------------------------------------------------------------- #

def test_cursor_fetch_columns_reach_the_lineage_fills():
    # only the interface build passed cursor_cols to _classify, so lineage saw every
    # FETCH with an EMPTY column map - and the dynamic-call view, which reads it, lost
    # the "this value comes from TABLE.COLUMN" fact for every cursor program.
    from cobol_xstate.lineage import _Lineage
    lin = _Lineage(build_machine(parse_program((EXAMPLES / "sqlcols.cbl").read_text()),
                                 source_name="sqlcols.cbl"))
    lin.run()
    fetch = [f for f in lin.fills if f.get("verb") == "FETCH"]
    assert fetch, "expected a cursor FETCH"
    cols = fetch[0]["columns"]
    assert {(c["hostVar"], c["column"]) for c in cols} == {("WS-ID", "ID"), ("WS-BAL", "BAL")}
    assert all(c["table"] == "CUSTOMER" for c in cols)


# -- literals are data, not operands (audit finding #6) ---------------------

def test_string_literal_containing_into_does_not_steal_the_receiver():
    """STRING exists to build messages, so its literals contain English. `STRING 'PUT
    INTO QUEUE' ... INTO WS-MSG` found its INTO inside the literal - naming the phantom
    receiver QUEUE and losing WS-MSG's write from the lineage entirely."""
    from cobol_xstate.lineage import _dep_only_flow
    known = {"WS-A", "WS-MSG", "QUEUE", "WS-X", "A", "B", "CNT", "X"}
    recv, src = _dep_only_flow(
        "STRING", "STRING 'PUT INTO QUEUE' WS-A DELIMITED BY SIZE INTO WS-MSG", known)
    assert recv == ["WS-MSG"]
    assert src == ["WS-A"]


def test_unstring_delimiter_literal_does_not_add_receivers():
    from cobol_xstate.lineage import _dep_only_flow
    known = {"WS-X", "A", "B", "X"}
    recv, src = _dep_only_flow(
        "UNSTRING", "UNSTRING WS-X DELIMITED BY 'INTO X' INTO A B", known)
    assert recv == ["A", "B"]
    assert src == ["WS-X"]


def test_inspect_tallying_for_a_literal_spelling_replacing_is_still_a_read():
    """`INSPECT WS-X TALLYING CNT FOR ALL 'REPLACING'` counts occurrences - it never
    writes WS-X. The word REPLACING inside the literal made WS-X a receiver."""
    from cobol_xstate.lineage import _dep_only_flow
    known = {"WS-X", "CNT"}
    recv, src = _dep_only_flow(
        "INSPECT", "INSPECT WS-X TALLYING CNT FOR ALL 'REPLACING'", known)
    assert recv == ["CNT"]
    assert src == ["WS-X"]
    # ...while a REAL REPLACING still rewrites the subject in place.
    recv, src = _dep_only_flow(
        "INSPECT", "INSPECT WS-X REPLACING ALL ' ' BY '0'", known)
    assert recv == ["WS-X"]


# --------------------------------------------------------------------------- #
# a WHERE-clause filter is not a field flowing to Db2
# (docs/issues/unmapped-fields-v52.md, Issue 2)
# --------------------------------------------------------------------------- #

def test_a_dml_row_selector_emits_no_lineage_row():
    """The point of the fields/params split, at the view that consumes it.

    `UPDATE ACCOUNT SET BALANCE = :WS-BAL WHERE ID = :WS-ID` writes BALANCE from
    :WS-BAL. :WS-ID chooses the row. A lineage row for :WS-ID claims a field crossed to
    a column, so the reader goes looking for the column and finds none - which is the
    "unmapped field" the v52 trace reported thousands of. Reported as a parameter it is
    still ON the event, just not claimed as a write.
    """
    d = _lin("sqldml.cbl")
    written = {(r["verb"], r["field"]) for r in d["rows"]
               if r.get("event") == "CREATE.DB2.ACCOUNT" and r["direction"] == "output"}
    assert written == {
        ("UPDATE", "WS-BAL"),                                  # SET BALANCE = :WS-BAL
        ("INSERT", "WS-ID"), ("INSERT", "WS-NAME"), ("INSERT", "WS-BAL"),
    }
    # ("UPDATE", "WS-ID") and ("DELETE", "WS-ID") were both here, and both were the
    # WHERE clause's `ID = :WS-ID`. The DELETE writes nothing at all, so it now
    # contributes no output row whatsoever.


def test_a_working_storage_cursor_names_its_real_table_here_too():
    """`sqlwscsr.cbl` DECLAREs its cursor in WORKING-STORAGE, where production code
    actually keeps it. The statement parser walks the PROCEDURE DIVISION only, so
    provenance and semantics cannot see that DECLARE; only the whole-stream scan can.

    The interface overlay was seeded from it and every other view was not, so the
    bundle event named `T_MMAA_ACC_ANAL` while this row said `<cursor ACCT_CSR>` - and
    no (event, endpoint) join between the two views was possible for that class, nor
    any join on `origins[].event`.
    """
    d = _lin("sqlwscsr.cbl")
    endpoints = {r["endpoint"] for r in d["rows"]}
    assert endpoints == {"T_MMAA_ACC_ANAL"}
    assert not [e for e in endpoints if str(e).startswith("<cursor")]
    origins = {o["event"] for r in d["rows"] for o in r.get("origins", [])}
    assert all("<cursor" not in e for e in origins)


# --------------------------------------------------------------------------- #
# baseState: the un-split state, so a row can be joined back to its event
# --------------------------------------------------------------------------- #

def test_a_split_state_row_can_be_joined_back_to_its_interface_event():
    """`_split` rewrites a state whose folded entry run contains a `perform_` into
    `p__L1` / `p__L2` / `p__Lend`. `build_interface` performs no such split, so the two
    views describe the SAME statement with different `state` values and a consumer
    joining on `state` silently loses those rows.

    `line` does not rescue the join: action names are content-derived and globally
    deduplicated, so two textually identical statements in different states share one
    provenance line.
    """
    src = (EXAMPLES / "lineage.cbl").read_text()
    m = build_machine(parse_program(src), source_name="lineage.cbl")
    d = build_lineage(m)
    split = [r for r in d["rows"] if r["state"] != r["baseState"]]
    assert split, "expected at least one _split segment row"
    assert all(r["state"].startswith(r["baseState"] + "__L") for r in split)
    # the segment id names no state anywhere; the base state does
    states = set(m.config["states"])
    assert all(r["state"] not in states for r in split)
    assert all(r["baseState"] in states for r in split)
    # ...and it is the state the interface event for the same statement carries
    events = {(e["state"], e["line"]) for e in m.interface()["events"]}
    assert all((r["baseState"], r["line"]) in events for r in split)


def test_an_unsplit_state_reports_itself_as_its_own_base():
    """Emitted unconditionally, so a consumer joins on one key without having to
    recognise the `__L` shape - which is what a workaround has to do, and what makes
    the workaround wrong the moment the shape changes."""
    d = _lin("sqldml.cbl")
    assert d["rows"]
    assert all(r["baseState"] == r["state"] for r in d["rows"])


def test_split_segments_of_one_paragraph_share_one_base_state():
    src = (EXAMPLES / "lineage.cbl").read_text()
    d = build_lineage(build_machine(parse_program(src), source_name="lineage.cbl"))
    bases = {r["state"]: r["baseState"] for r in d["rows"]}
    for state, base in bases.items():
        if "__L" in state:
            assert base == state.split("__L")[0]
    assert len({b for s, b in bases.items() if "__L" in s}) == 1


def test_every_lineage_row_names_a_real_state_through_its_base():
    """The property the key exists for, across the whole corpus rather than one
    fixture: no row's baseState is a synthetic id."""
    for name in ("altswitch.cbl", "lineage.cbl", "sqlwscsr.cbl", "sqldyncsr.cbl"):
        src = (EXAMPLES / name).read_text()
        m = build_machine(parse_program(src), source_name=name)
        states = set(m.config["states"])
        for r in build_lineage(m)["rows"]:
            assert r["baseState"] in states, (name, r["state"], r["baseState"])


# --------------------------------------------------------------------------- #
# memoisation: byte-identical by construction, and scoped so it stays correct
# --------------------------------------------------------------------------- #

def test_classify_is_called_once_per_action_not_once_per_visit():
    """The fixpoint calls `_apply` once per state PER VISIT, and once more in the final
    rows pass. Every argument `_apply` hands `_classify` other than the action name is
    fixed for the instance's lifetime, so the answer is invariant across visits - but
    `_classify` does `mask_literals`, several regex scans and a `_DataView.leaves` walk
    on each one."""
    from cobol_xstate import interface as _iface
    src = (EXAMPLES / "lineage.cbl").read_text()
    m = build_machine(parse_program(src), source_name="lineage.cbl")
    lin = m.lineage()
    calls = []
    real = _iface._classify

    def counting(*a, **k):
        calls.append(a[0])
        return real(*a, **k)

    _iface._classify = counting
    try:
        lin.run()
    finally:
        _iface._classify = real
    assert calls, "expected the fixpoint to classify something"
    assert len(calls) == len(set(calls)), \
        "an action was classified more than once: %r" % (
            sorted({c for c in calls if calls.count(c) > 1}),)


def test_the_classify_memo_is_scoped_to_the_lineage_instance():
    """BLOCKING property. `_classify` is a module-level function, and the obvious
    reading - memoise it with `lru_cache` - is WRONG: it is invariant per `_Lineage`
    instance, not across CALLERS. Its answer depends on the cursor maps handed to it,
    and reactive builds two provenance-only maps deliberately left unseeded because
    they consume only `h["direction"]`.

    `examples/sqlwscsr.cbl` is the counterexample the repo already ships: its cursor is
    DECLAREd in WORKING-STORAGE, so `exec_sql_fetch` classifies as `T_MMAA_ACC_ANAL`
    from a seeded caller and as `<cursor ACCT_CSR>` from an unseeded one. A
    module-level cache would return whichever ran first and silently corrupt the other,
    with NO test failure - which is why this test exists rather than a comment.
    """
    from cobol_xstate import interface as _iface
    from cobol_xstate.reactive import build_reactive_view
    src = (EXAMPLES / "sqlwscsr.cbl").read_text()
    m = build_machine(parse_program(src), source_name="sqlwscsr.cbl")

    seen = {}
    real = _iface._classify

    def spy(*a, **k):
        out = real(*a, **k)
        for h in out or []:
            seen.setdefault(a[0], set()).add((h.get("etype"), h.get("endpoint")))
        return out

    _iface._classify = spy
    try:
        m.lineage().run()
        build_reactive_view(m)
    finally:
        _iface._classify = real

    # The divergence is real and must stay visible: two different answers for ONE
    # action name across callers. If this ever collapses to one, the memo could safely
    # widen - but until then, widening it is a silent wrong answer.
    assert len(seen["exec_sql_fetch"]) > 1, (
        "expected exec_sql_fetch to classify differently across callers; "
        "got %r" % (seen["exec_sql_fetch"],))
    assert ("db2", "T_MMAA_ACC_ANAL") in seen["exec_sql_fetch"]
    assert ("db2", "<cursor ACCT_CSR>") in seen["exec_sql_fetch"]

    # ...and the lineage view, which is the memo's owner, reports only the seeded one.
    assert {r["endpoint"] for r in build_lineage(m)["rows"]} == {"T_MMAA_ACC_ANAL"}


def test_data_view_leaves_is_memoised_and_answers_the_same():
    from cobol_xstate.interface import _DataView
    data = {
        "REC": {"parent": None},
        "GRP": {"parent": "REC"},
        "A": {"parent": "GRP"}, "B": {"parent": "GRP"},
        "C": {"parent": "REC"},
    }
    dv = _DataView(data)
    first = dv.leaves("REC")
    assert first == ["A", "B", "C"]
    assert dv.leaves("REC") == first
    assert dv.leaves("REC") is first          # the same object, not a rebuilt copy
    assert dv.record_fields("REC") == ["REC", "A", "B", "C"]
    assert dv.record_fields("REC") is dv.record_fields("REC")
    # a second view over different data must not see the first one's answers
    other = _DataView({"REC": {"parent": None}})
    assert other.leaves("REC") == ["REC"]


def test_write_site_dedup_keeps_first_occurrence_order():
    """The `seen` set replaced an `x not in list` scan. It must dedup the same entries
    and keep the same order - the entries have no `state` key, so the key is the
    canonical form of the whole entry, conditions included."""
    from cobol_xstate.lineage import _entry_key
    a = {"action": "MOVE_A", "line": 1, "conditions": [{"guard": "g", "negated": False}]}
    b = {"line": 1, "action": "MOVE_A", "conditions": [{"negated": False, "guard": "g"}]}
    c = {"action": "MOVE_A", "line": 1, "conditions": [{"guard": "g", "negated": True}]}
    assert _entry_key(a) == _entry_key(b)     # key order must not matter
    assert _entry_key(a) != _entry_key(c)     # a differing condition must not collapse
    d = _lin("lineage.cbl")
    for r in d["rows"]:
        entries = r.get("changedBy") or []
        keys = [_entry_key(e) for e in entries]
        assert len(keys) == len(set(keys)), r["field"]
