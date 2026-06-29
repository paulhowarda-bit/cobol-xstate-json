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


def test_redefines_is_flagged_as_unmodeled_aliasing():
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
    assert any("REDEFINES" in f["message"] and "byte-aliasing" in f["message"]
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
