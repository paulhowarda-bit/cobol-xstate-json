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
