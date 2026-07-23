from pathlib import Path

from cobol_xstate.parser import parse_program
from cobol_xstate.statechart import build_machine
from cobol_xstate.data_division import parse_pic
from cobol_xstate.semantics import parse_operation, parse_condition

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _machine(src):
    return build_machine(parse_program(src))


# -- PICTURE / USAGE / sign typing ----------------------------------------

def test_parse_pic_packed_decimal_scale_and_sign():
    t = parse_pic("S9(5)V99", "COMP-3")
    assert t.category == "numeric"
    assert t.usage == "COMP-3"        # packed decimal
    assert t.digits == 7 and t.scale == 2
    assert t.signed is True


def test_parse_pic_alphanumeric_and_group():
    assert parse_pic("X(8)", None).category == "alphanumeric"
    assert parse_pic(None, None).category == "group"


def test_data_dictionary_captures_types():
    machine = _machine((EXAMPLES / "custrpt.cbl").read_text())
    data = machine.data
    amt = data["CUST-AMT"]["type"]
    assert amt["category"] == "numeric" and amt["usage"] == "COMP-3"
    assert amt["digits"] == 9 and amt["scale"] == 2
    assert data["WS-EOF"]["type"]["category"] == "alphanumeric"
    assert data["CUST-REC"]["type"]["category"] == "group"


def test_88_condition_name_resolved():
    machine = _machine((EXAMPLES / "custrpt.cbl").read_text())
    eof = machine.data["END-OF-FILE"]
    assert eof["kind"] == "condition-name"
    assert eof["of"] == "WS-EOF"
    assert eof["values"] == ["'Y'"]


def test_context_seeded_with_typed_initial_values():
    machine = _machine((EXAMPLES / "custrpt.cbl").read_text())
    ctx = machine.config["context"]
    assert ctx["WS-TOTAL"] == 0          # VALUE ZERO -> 0
    assert ctx["WS-EOF"] == "N"          # VALUE 'N'  -> "N"
    assert ctx["CUST-AMT"] == 0          # numeric default


# -- statement semantics (the MOVE/COMPUTE logic) --------------------------

def test_move_is_an_assignment():
    op = parse_operation("MOVE 'Y' TO WS-EOF")
    assert op["kind"] == "assign"
    assert op["assignments"] == [{"target": "WS-EOF", "expr": "'Y'"}]


def test_add_to_is_accumulate_expression():
    op = parse_operation("ADD CUST-AMT TO WS-TOTAL")
    assert op["kind"] == "arith"
    assert op["assignments"] == [{"target": "WS-TOTAL", "expr": "WS-TOTAL + CUST-AMT"}]


def test_compute_keeps_expression_and_flags_rounding_overflow():
    op = parse_operation("COMPUTE WS-A = WS-B * 2 + 1 ROUNDED ON SIZE ERROR")
    assert op["kind"] == "compute"
    assert op["assignments"] == [{"target": "WS-A", "expr": "WS-B * 2 + 1"}]
    assert op["rounded"] is True and op["onSizeError"] is True


def test_subtract_and_giving():
    assert parse_operation("SUBTRACT 1 FROM WS-COUNT")["assignments"] == \
        [{"target": "WS-COUNT", "expr": "WS-COUNT - (1)"}]
    assert parse_operation("ADD A B GIVING C")["assignments"] == \
        [{"target": "C", "expr": "A + B"}]


# -- condition semantics (the guard logic) --------------------------------

def test_relational_condition_tree():
    assert parse_condition("WS-EOF = 'Y'") == {
        "op": "rel", "left": "WS-EOF", "rel": "=", "right": "'Y'"}


def test_boolean_and_sign_and_class_conditions():
    assert parse_condition("AMT NEGATIVE") == {
        "op": "sign", "operand": "AMT", "sign": "NEGATIVE", "negated": False}
    assert parse_condition("X IS NOT NUMERIC") == {
        "op": "class", "operand": "X", "class": "NUMERIC", "negated": True}
    tree = parse_condition("A > B AND C = 1")
    assert tree["op"] == "and" and len(tree["args"]) == 2


def test_abbreviated_condition_implies_subject():
    # A = 1 OR 2  ->  A = 1 OR A = 2  (implied subject and relational operator)
    assert parse_condition("A = 1 OR 2") == {
        "op": "or", "args": [
            {"op": "rel", "left": "A", "rel": "=", "right": "1"},
            {"op": "rel", "left": "A", "rel": "=", "right": "2"},
        ]}


def test_abbreviated_condition_implies_subject_only():
    # A > 1 AND < 9  ->  A > 1 AND A < 9  (subject implied, operator restated)
    assert parse_condition("A > 1 AND < 9") == {
        "op": "and", "args": [
            {"op": "rel", "left": "A", "rel": ">", "right": "1"},
            {"op": "rel", "left": "A", "rel": "<", "right": "9"},
        ]}


def test_abbreviated_condition_carries_not_into_implied_term():
    # A NOT = 1 AND 2  ->  A NOT = 1 AND A NOT = 2 (the NOT is part of the implied operator)
    assert parse_condition("A NOT = 1 AND 2") == {
        "op": "and", "args": [
            {"op": "not", "arg": {"op": "rel", "left": "A", "rel": "=", "right": "1"}},
            {"op": "not", "arg": {"op": "rel", "left": "A", "rel": "=", "right": "2"}},
        ]}


def test_full_relation_after_connective_is_not_abbreviated():
    # A = 1 OR B = 2 keeps B as a new subject (not an abbreviated object of A)
    tree = parse_condition("A = 1 OR B = 2")
    assert tree["args"][1] == {"op": "rel", "left": "B", "rel": "=", "right": "2"}


def test_decimal_literal_operand_is_not_broken_by_the_point():
    # The decimal point must not split the literal into 500 / 00 (which forced a raw
    # fallback before): a comparison against a decimal constant is a real relation.
    assert parse_condition("B-PATIENT-WGT > 500.00") == {
        "op": "rel", "left": "B-PATIENT-WGT", "rel": ">", "right": "500.00"}
    assert parse_condition("H-BMI < 18.5") == {
        "op": "rel", "left": "H-BMI", "rel": "<", "right": "18.5"}


def test_arithmetic_expression_operand_in_condition():
    # A relational operand may be an arithmetic expression (not just a single term).
    assert parse_condition("WS-A + WS-B > WS-LIMIT") == {
        "op": "rel", "left": "WS-A + WS-B", "rel": ">", "right": "WS-LIMIT"}


def test_parenthesized_relations_join_with_connective():
    # ( rel ) AND ( rel ) - parentheses group sub-conditions; decimals inside must work.
    tree = parse_condition("( H-PATIENT-AGE > 17 ) AND ( H-BMI < 18.5 )")
    assert tree["op"] == "and"
    assert tree["args"][0] == {"op": "rel", "left": "H-PATIENT-AGE", "rel": ">", "right": "17"}
    assert tree["args"][1] == {"op": "rel", "left": "H-BMI", "rel": "<", "right": "18.5"}


def test_logical_keyword_before_paren_is_not_a_subscript():
    # AND ( ... ) must not be mis-tokenized as a subscripted reference AND(...).
    tree = parse_condition("A = 1 AND ( B = 2 )")
    assert tree["op"] == "and" and len(tree["args"]) == 2


def test_arithmetic_and_multidim_subscript_operands_are_kept_whole():
    # A subscript that is an arithmetic expression or a multi-dimension list is preserved
    # as a single relational operand (faithful), not split into a raw fallback.
    assert parse_condition("W-SUB1 > WWM-PTR ( WWM-INDX - 1 )") == {
        "op": "rel", "left": "W-SUB1", "rel": ">", "right": "WWM-PTR(WWM-INDX - 1)"}
    assert parse_condition("TBL ( I , J ) = 5") == {
        "op": "rel", "left": "TBL(I,J)", "rel": "=", "right": "5"}


def test_unmodelable_condition_falls_back_to_raw_honestly():
    # A nested subscript is beyond this static recovery: raw, not a wrong guess.
    assert parse_condition("TBL(IDX(I)) = 1")["op"] == "raw"


def test_88_value_thru_is_a_range_not_two_singletons():
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. RNG.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01  WS-CODE  PIC 9(2) VALUE 0.\n"
        "           88  VALID-CODE  VALUE 1 THRU 9.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           IF VALID-CODE MOVE 1 TO WS-CODE END-IF\n"
        "           STOP RUN.\n"
    )
    machine = _machine(src)
    vc = machine.data["VALID-CODE"]
    assert vc["kind"] == "condition-name"
    assert vc["values"] == []                 # endpoints not flattened into singletons
    assert vc["ranges"] == [["1", "9"]]


def test_88_range_emits_bounded_guard():
    from cobol_xstate.emitter import emit_setup_module
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. RNG.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01  WS-CODE  PIC 9(2) VALUE 0.\n"
        "           88  VALID-CODE  VALUE 1 THRU 9.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           IF VALID-CODE MOVE 1 TO WS-CODE END-IF\n"
        "           STOP RUN.\n"
    )
    mod = emit_setup_module(_machine(src))
    assert ('(rel(context["WS-CODE"], ">=", "1", true) && '
            'rel(context["WS-CODE"], "<=", "9", true))') in mod


def test_occurs_seeds_context_as_array():
    machine = _machine((EXAMPLES / "tblsum.cbl").read_text())
    assert machine.config["context"]["TBL-AMT"] == [0, 0, 0, 0, 0]
    assert machine.data["TBL-AMT"]["occurs"] == 5


def test_redefines_across_pictures_flags_byte_reinterpretation():
    # 9(4) numeric redefined as X(4) alphanumeric: genuine byte reinterpretation.
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. RD.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01  WS-A      PIC 9(4).\n"
        "       01  WS-B REDEFINES WS-A PIC X(4).\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           STOP RUN.\n"
    )
    machine = _machine(src)
    assert machine.data["WS-B"]["redefines"] == "WS-A"
    assert any("REDEFINES" in f["message"] and "byte reinterpretation" in f["message"]
               and "NOT modeled" in f["message"] for f in machine.flags)


def test_redefines_same_category_flagged_as_value_alias():
    # X(4) redefined as X(4): same category/size -> reported as a safe value alias.
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. RD2.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01  WS-A      PIC X(4).\n"
        "       01  WS-B REDEFINES WS-A PIC X(4).\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           STOP RUN.\n"
    )
    machine = _machine(src)
    assert any("REDEFINES" in f["message"] and "ALIAS" in f["message"]
               for f in machine.flags)


def test_group_occurs_is_flagged_not_silently_modeled():
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. GRP.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01  TBL-ENTRY OCCURS 3.\n"
        "           05  TE-AMT  PIC 9(3).\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           STOP RUN.\n"
    )
    machine = _machine(src)
    assert any("OCCURS on group" in f["message"] for f in machine.flags)


def test_end_to_end_compute_overflow_and_sign_flagged_and_captured():
    src = (
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01  WS-A  PIC S9(5)V99 COMP-3 VALUE 0.\n"
        "       01  WS-B  PIC 9(3) VALUE 0.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-X.\n"
        "           COMPUTE WS-A = WS-B * 2 ROUNDED ON SIZE ERROR\n"
        "               DISPLAY 'OVF'\n"
        "           END-COMPUTE\n"
        "           IF WS-A NEGATIVE\n"
        "               GO TO 9000-Z\n"
        "           END-IF\n"
        "           STOP RUN.\n"
        "       9000-Z.\n"
        "           STOP RUN.\n"
    )
    machine = _machine(src)
    # COMPUTE captured as an assignment with the real expression...
    computes = [s for s in machine.semantics["actions"].values() if s["kind"] == "compute"]
    assert computes and computes[0]["assignments"][0]["target"] == "WS-A"
    # ...ON SIZE ERROR flagged as an overflow path to replicate...
    assert any("ON SIZE ERROR" in f["message"] for f in machine.flags)
    # ...and the sign condition captured as a guard expression.
    assert any(t.get("op") == "sign" and t.get("sign") == "NEGATIVE"
               for t in machine.semantics["guards"].values())


# --------------------------------------------------------------------------- #
# DIVIDE ... REMAINDER / reference-modified stores / OCCURS DEPENDING
# --------------------------------------------------------------------------- #

def test_divide_remainder_receiver_is_modeled():
    from cobol_xstate.semantics import parse_operation
    spec = parse_operation("DIVIDE 7 BY 2 GIVING WS-Q REMAINDER WS-R")
    assigns = {a["target"]: a["expr"] for a in spec["assignments"]}
    assert assigns["WS-Q"] == "7 / 2"
    assert assigns["WS-R"] == "7 - ( WS-Q * 2 )"
    # quotient must be assigned BEFORE the remainder reads it
    targets = [a["target"] for a in spec["assignments"]]
    assert targets.index("WS-Q") < targets.index("WS-R")


def test_divide_into_remainder_orients_operands():
    from cobol_xstate.semantics import parse_operation
    spec = parse_operation("DIVIDE 2 INTO 7 GIVING WS-Q REMAINDER WS-R")
    assigns = {a["target"]: a["expr"] for a in spec["assignments"]}
    assert assigns["WS-Q"] == "7 / 2"
    assert assigns["WS-R"] == "7 - ( WS-Q * 2 )"


def test_occurs_depending_sized_at_maximum():
    from cobol_xstate.data_division import parse_data_division
    from cobol_xstate.normalizer import normalize
    src = (
        "       01  WS-TAB.\n"
        "           05  WS-CNT   PIC S9(4) COMP.\n"
        "           05  WS-ENTRY PIC X(3) OCCURS 1 TO 50 TIMES\n"
        "                DEPENDING ON WS-CNT.\n"
    )
    lines = normalize(
        "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. T.\n"
        "       DATA DIVISION.\n       WORKING-STORAGE SECTION.\n" + src +
        "       PROCEDURE DIVISION.\n       M. STOP RUN.\n")
    items, by_name = parse_data_division(lines)
    entry = by_name["WS-ENTRY"]
    assert entry.occurs == 50                 # max, not min
    assert entry.occurs_depending == "WS-CNT"


def test_refmod_write_target_is_flagged_and_not_a_phantom_key():
    from cobol_xstate.parser import parse_program
    from cobol_xstate.statechart import build_machine
    from cobol_xstate.emitter import emit_setup_module
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. T.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01  WS-NAME  PIC X(10).\n"
        "       PROCEDURE DIVISION.\n"
        "       M.\n"
        "           MOVE 'AB' TO WS-NAME(1:2)\n"
        "           STOP RUN.\n"
    )
    machine = build_machine(parse_program(src))
    assert any("reference-modified" in f["message"] for f in machine.flags)
    mod = emit_setup_module(machine)
    # never a silent phantom store; the runnable machine fails loudly instead
    assert "notModeled" in mod
    assert 'FIELDS["WS-NAME(1 : 2)"]' not in mod


# --------------------------------------------------------------------------- #
# entry/paragraph boundaries are period-driven (review finding J13)
# --------------------------------------------------------------------------- #

def _items(src):
    return {it.name: it for it in parse_program(src).data_items}


def test_data_clause_continued_onto_a_numeric_line_is_not_a_new_item():
    # OCCURS wrapped onto its own line starting with the count: `05 X OCCURS` / `10 TIMES`
    # used to split into X (no occurs) plus a phantom item named TIMES at level 10.
    items = _items(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. T.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01 WS-TAB OCCURS\n"
        "          10 TIMES PIC X(5).\n"
        "       01 WS-B PIC 9(4) VALUE 0.\n"
    )
    assert "TIMES" not in items, "a continuation line became a phantom data item"
    assert items["WS-TAB"].occurs == 10
    assert items["WS-TAB"].pic == "X(5)"
    assert set(items) == {"WS-TAB", "WS-B"}


def test_a_real_new_entry_after_a_period_still_splits():
    # the boundary check must not over-merge: two terminated entries stay two entries
    items = _items(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. T.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01 WS-A PIC X(3).\n"
        "       05 WS-B PIC X(3).\n"
    )
    assert set(items) == {"WS-A", "WS-B"}


def test_free_format_statement_continuation_is_not_a_phantom_paragraph():
    # a MOVE whose target is on the next line, alone, reads as `NAME.` - the header shape.
    # In free format every line is an Area-A candidate, so it became a phantom paragraph
    # that stole the rest of the real one.
    prog = parse_program(
        ">>SOURCE FORMAT FREE\n"
        "IDENTIFICATION DIVISION.\n"
        "PROGRAM-ID. T.\n"
        "DATA DIVISION.\n"
        "WORKING-STORAGE SECTION.\n"
        "01 WS-SRC PIC 9(4) VALUE 7.\n"
        "01 WS-RESULT PIC 9(4).\n"
        "PROCEDURE DIVISION.\n"
        "0000-MAIN.\n"
        "    MOVE WS-SRC TO\n"
        "    WS-RESULT.\n"
        "    STOP RUN.\n"
    )
    names = [p.name for p in prog.paragraphs]
    assert names == ["0000-MAIN"], f"phantom paragraph(s): {names}"
    verbs = [type(s).__name__ for s in prog.paragraphs[0].statements]
    assert "TerminateStmt" in verbs, "STOP RUN was stolen by the phantom paragraph"


def test_free_format_real_paragraph_header_after_a_period_is_recognized():
    # the boundary gate must still admit a genuine header once the sentence is closed
    prog = parse_program(
        ">>SOURCE FORMAT FREE\n"
        "IDENTIFICATION DIVISION.\n"
        "PROGRAM-ID. T.\n"
        "PROCEDURE DIVISION.\n"
        "0000-MAIN.\n"
        "    PERFORM 1000-SUB.\n"
        "    STOP RUN.\n"
        "1000-SUB.\n"
        "    CONTINUE.\n"
    )
    assert [p.name for p in prog.paragraphs] == ["0000-MAIN", "1000-SUB"]
