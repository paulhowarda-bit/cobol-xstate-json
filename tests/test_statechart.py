import json
from pathlib import Path

from cobol_xstate.parser import parse_program
from cobol_xstate.statechart import build_machine, _evaluate_when_condition


def test_evaluate_also_thru_any_and_true_build_correct_conditions():
    # EVALUATE a ALSO b ... WHEN x ALSO y  ->  a = x AND b = y
    assert _evaluate_when_condition("A ALSO B", "1 ALSO 2") == "(A = 1) AND (B = 2)"
    # THRU range, ANY (dropped), abbreviated relation, EVALUATE TRUE (object is a condition)
    assert _evaluate_when_condition("WS-N", "1 THRU 5") == "WS-N >= 1 AND WS-N <= 5"
    assert _evaluate_when_condition("A ALSO B", "1 ALSO ANY") == "A = 1"
    assert _evaluate_when_condition("WS-N", "> 100") == "WS-N > 100"
    assert _evaluate_when_condition("TRUE", "WS-X > 5") == "WS-X > 5"

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _machine(src: str):
    return build_machine(parse_program(src))


def _machine_with(src: str, resolver):
    return build_machine(parse_program(src, resolver=resolver))


def _all_edges(machine):
    return [(name, e) for name, s in machine.config["states"].items()
            for e in s.get("always", [])]


def test_custrpt_machine_shape():
    machine = _machine((EXAMPLES / "custrpt.cbl").read_text())
    cfg = machine.config
    assert cfg["id"] == "CUSTRPT"
    assert cfg["initial"] == "0000-MAIN"
    states = cfg["states"]
    # Each paragraph is an entry state; the body compiles to faithful sub-states.
    assert {"0000-MAIN", "1000-INIT", "2000-PROCESS", "3000-TERM"} <= set(states)
    # The driver PERFORM ... UNTIL is a real loop (exit guard + body that loops back).
    assert any(e["meta"]["kind"] == "loop-exit" and "guard" in e for _, e in _all_edges(machine))
    assert any(e["meta"]["kind"] == "loop-body" for _, e in _all_edges(machine))
    # The three phase paragraphs are performed as call-return actions.
    actions = [a for s in states.values() for a in s.get("entry", [])]
    assert {"perform_1000-INIT", "perform_2000-PROCESS", "perform_3000-TERM"} <= set(actions)
    # Termination reaches a final state.
    assert any(s.get("type") == "final" for s in states.values())


def test_conditional_logic_stays_conditional():
    # READ ... AT END MOVE 'Y' TO WS-EOF: the flag-set must be reachable ONLY via the
    # guarded AT_END branch, never folded into an unconditional entry list. This is
    # the whole reason for a Harel statechart over a flattened model.
    machine = _machine((EXAMPLES / "custrpt.cbl").read_text())
    states = machine.config["states"]
    # The READ state runs only the read action unconditionally...
    read_states = [s for s in states.values()
                   if any(a.startswith("read_CUST-FILE") for a in s.get("entry", []))]
    assert read_states
    for s in read_states:
        assert "MOVE_Y_TO_WS-EOF" not in s.get("entry", [])
        # ...and exposes the EOF set behind a guarded AT_END edge.
        at_end = [e for e in s["always"] if e["meta"].get("note") == "AT_END"]
        assert at_end and "guard" in at_end[0]
        target = states[at_end[0]["target"]]
        assert "MOVE_Y_TO_WS-EOF" in target.get("entry", [])


def test_no_invented_logic_guards_and_actions_are_strings():
    machine = _machine((EXAMPLES / "custrpt.cbl").read_text())
    for state in machine.config["states"].values():
        for a in state.get("entry", []):
            assert isinstance(a, str)
        for tr in state.get("always", []):
            assert isinstance(tr.get("guard", ""), str)
            for a in tr.get("actions", []):
                assert isinstance(a, str)


def test_every_referenced_name_has_provenance():
    machine = _machine((EXAMPLES / "custrpt.cbl").read_text())
    prov = machine.provenance
    for name, state in machine.config["states"].items():
        assert name in prov and prov[name]["kind"] == "state"
        for a in state.get("entry", []):
            assert a in prov, f"missing provenance for action {a}"
        for tr in state.get("always", []):
            if "guard" in tr:
                assert tr["guard"] in prov
            for a in tr.get("actions", []):
                assert a in prov


def test_terminator_marks_final():
    machine = _machine(
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           STOP RUN.\n"
    )
    assert machine.config["states"]["0000-MAIN"].get("type") == "final"


def test_search_when_and_at_end_are_real_guarded_branches():
    src = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. SRCHT.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01 WS-TAB.
          05 WS-ENT OCCURS 5 PIC 99.
       01 WS-IDX PIC 99 VALUE 1.
       01 WS-FOUND PIC X VALUE 'N'.
       PROCEDURE DIVISION.
       0000-MAIN.
           SEARCH WS-ENT VARYING WS-IDX
               AT END MOVE 'N' TO WS-FOUND
               WHEN WS-ENT (WS-IDX) = 42
                   MOVE 'Y' TO WS-FOUND
           END-SEARCH
           STOP RUN.
"""
    machine = _machine(src)
    kinds = [e["meta"]["kind"] for _, e in _all_edges(machine)]
    # WHEN -> a guarded branch; AT END -> a guarded branch; plus a fall-through.
    assert "search-when" in kinds and "search-at-end" in kinds and "search-continue" in kinds
    # The WHEN condition became a real guard (not an opaque action).
    assert machine.semantics["guards"]
    # The opaque index iteration is flagged, not silently dropped.
    assert any("SEARCH" in f["message"] and "index" in f["message"] for f in machine.flags)


def test_dynamic_call_resolved_by_constant_propagation():
    # WS-SUBPGM has VALUE 'POSTLOG' and is never reassigned, so the CALL target
    # resolves statically and is NOT flagged.
    machine = _machine((EXAMPLES / "banktran.cbl").read_text())
    assert machine.flags == []
    actions = [a for s in machine.config["states"].values() for a in s.get("entry", [])]
    assert "call_POSTLOG" in actions
    assert any("POSTLOG" in p.get("cobol", "") for p in machine.provenance.values())


def test_dynamic_call_from_variable_stays_flagged():
    machine = _machine((EXAMPLES / "altswitch.cbl").read_text())
    msgs = " ".join(f["message"] for f in machine.flags)
    assert "dynamic CALL WS-PGM" in msgs
    assert "runtime-determined" in msgs


_CICS_LINK_SRC = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. LNKT.\n"
    "       DATA DIVISION.\n"
    "       WORKING-STORAGE SECTION.\n"
    "       01 WS-PGM PIC X(8) VALUE 'FBSPREST'.\n"
    "       01 WS-AREA PIC X(100).\n"
    "       PROCEDURE DIVISION.\n"
    "       0000-MAIN.\n"
    "           EXEC CICS LINK PROGRAM(WS-PGM) COMMAREA(WS-AREA) END-EXEC\n"
    "           GOBACK.\n"
)


def test_dynamic_cics_link_resolved_by_constant_propagation():
    # PROGRAM(WS-PGM) where WS-PGM has VALUE 'FBSPREST' and is never reassigned:
    # the module name resolves statically, exactly like a dynamic batch CALL.
    machine = _machine(_CICS_LINK_SRC)
    assert machine.flags == []
    actions = [a for s in machine.config["states"].values() for a in s.get("entry", [])]
    assert "link_FBSPREST" in actions
    assert not any("WS-PGM" in a for a in actions)
    prov = machine.provenance["link_FBSPREST"]["cobol"]
    assert "resolved 'FBSPREST'" in prov and "WS-PGM" in prov


def test_dynamic_cics_link_unresolved_stays_flagged():
    src = _CICS_LINK_SRC.replace(
        "       01 WS-PGM PIC X(8) VALUE 'FBSPREST'.\n",
        "       01 WS-PGM PIC X(8).\n"
        "       01 WS-OTHER PIC X(8).\n",
    ).replace(
        "           EXEC CICS LINK",
        "           MOVE WS-OTHER TO WS-PGM\n"
        "           EXEC CICS LINK",
    )
    machine = _machine(src)
    msgs = " ".join(f["message"] for f in machine.flags)
    assert "dynamic CICS LINK PROGRAM(WS-PGM)" in msgs
    actions = [a for s in machine.config["states"].values() for a in s.get("entry", [])]
    assert "link_WS-PGM" in actions


def test_dynamic_cics_xctl_resolves_target_in_final_state_meta():
    src = _CICS_LINK_SRC.replace("VALUE 'FBSPREST'", "VALUE 'NEXTPGM'").replace(
        "           EXEC CICS LINK PROGRAM(WS-PGM) COMMAREA(WS-AREA) END-EXEC\n"
        "           GOBACK.\n",
        "           EXEC CICS XCTL PROGRAM(WS-PGM) COMMAREA(WS-AREA) END-EXEC.\n",
    )
    machine = _machine(src)
    finals = [s for s in machine.config["states"].values()
              if s.get("type") == "final" and s.get("meta", {}).get("target")]
    assert finals and finals[0]["meta"]["target"] == "NEXTPGM"
    assert finals[0]["meta"]["targetVia"] == "WS-PGM"
    msgs = " ".join(f["message"] for f in machine.flags)
    assert "XCTL to NEXTPGM" in msgs


def test_literal_cics_link_program_unchanged():
    src = _CICS_LINK_SRC.replace("PROGRAM(WS-PGM)", "PROGRAM('POSTLOG')")
    machine = _machine(src)
    actions = [a for s in machine.config["states"].values() for a in s.get("entry", [])]
    assert "link_POSTLOG" in actions
    assert machine.flags == []


def test_dynamic_transid_queue_file_operands_resolve_silently():
    # Every CICS resource-name operand gets the same treatment as PROGRAM: a data-name
    # operand whose only reaching value is a literal resolves with no flag.
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. RSRC.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01 WS-TRAN PIC X(4) VALUE 'AB12'.\n"
        "       01 WS-Q PIC X(8) VALUE 'ERRQ'.\n"
        "       01 WS-F PIC X(8) VALUE 'ACCTFILE'.\n"
        "       01 WS-MSG PIC X(80).\n"
        "       01 WS-REC PIC X(80).\n"
        "       01 WS-KEY PIC X(8).\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           EXEC CICS START TRANSID(WS-TRAN) END-EXEC\n"
        "           EXEC CICS WRITEQ TD QUEUE(WS-Q) FROM(WS-MSG) END-EXEC\n"
        "           EXEC CICS READ FILE(WS-F) INTO(WS-REC) RIDFLD(WS-KEY) END-EXEC\n"
        "           GOBACK.\n"
    )
    machine = _machine(src)
    assert machine.flags == []


def test_dynamic_transid_and_file_operands_flagged_when_unresolved():
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. RSRC.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01 WS-TRAN PIC X(4).\n"
        "       01 WS-F PIC X(8).\n"
        "       01 WS-OTHER PIC X(8).\n"
        "       01 WS-REC PIC X(80).\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           MOVE WS-OTHER TO WS-TRAN\n"
        "           MOVE WS-OTHER TO WS-F\n"
        "           EXEC CICS START TRANSID(WS-TRAN) END-EXEC\n"
        "           EXEC CICS READ FILE(WS-F) INTO(WS-REC) END-EXEC\n"
        "           GOBACK.\n"
    )
    msgs = " ".join(f["message"] for f in _machine(src).flags)
    assert "dynamic CICS START TRANSID(WS-TRAN)" in msgs
    assert "dynamic CICS READ FILE(WS-F)" in msgs


def test_return_transid_eib_field_flagged_as_cics_supplied():
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. PSEUDO.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           EXEC CICS RETURN TRANSID(EIBTRNID) END-EXEC.\n"
    )
    msgs = " ".join(f["message"] for f in _machine(src).flags)
    assert "TRANSID(EIBTRNID)" in msgs and "EIB" in msgs


_COPYBOOK_CALL_SRC = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. FBSB066B.\n"
    "       DATA DIVISION.\n"
    "       WORKING-STORAGE SECTION.\n"
    "       COPY DC01104.\n"
    "       PROCEDURE DIVISION.\n"
    "       JM0004.\n"
    "           CALL CN-DCIOC104 USING DC01104-PARMS\n"
    "           GOBACK.\n"
)


def test_dynamic_call_target_from_copybook_value_resolves(tmp_path):
    # The target identifier and its VALUE live in a copybook: once the copybook is on
    # the search path, constant propagation sees the VALUE and resolves the CALL.
    from cobol_xstate.preprocessor import CopybookResolver
    (tmp_path / "DC01104.cpy").write_text(
        "       01 DC01104-CONSTANTS.\n"
        "          05 CN-DCIOC104            PIC X(8)  VALUE 'DCIOC104'.\n"
        "       01 DC01104-PARMS             PIC X(100).\n")
    machine = _machine_with(
        _COPYBOOK_CALL_SRC, resolver=CopybookResolver(paths=[str(tmp_path)]))
    assert machine.flags == []
    actions = [a for s in machine.config["states"].values() for a in s.get("entry", [])]
    assert "call_DCIOC104" in actions


def test_dynamic_call_via_88_level_set_to_true_resolves(tmp_path):
    # The FBSB066B idiom: the target item has NO VALUE clause - an 88-level condition
    # carries the module name, and the program does SET <88> TO TRUE before the CALL.
    # SET stores the 88's VALUE into the parent, so the target is provably constant.
    from cobol_xstate.preprocessor import CopybookResolver
    (tmp_path / "DC01104.cpy").write_text(
        "       01 DC01104-CONSTANTS.\n"
        "          05 CN-DCIOC104            PIC X(08).\n"
        "             88 DCIOC104-MODULE     VALUE 'DCIOC104'.\n"
        "       01 DC01104-PARMS             PIC X(100).\n")
    src = _COPYBOOK_CALL_SRC.replace(
        "           CALL CN-DCIOC104 USING DC01104-PARMS\n",
        "           SET DCIOC104-MODULE TO TRUE\n"
        "           CALL CN-DCIOC104 USING DC01104-PARMS\n")
    machine = _machine_with(src, resolver=CopybookResolver(paths=[str(tmp_path)]))
    assert machine.flags == []
    actions = [a for s in machine.config["states"].values() for a in s.get("entry", [])]
    assert "call_DCIOC104" in actions


def test_dynamic_call_with_88_values_but_no_set_lists_candidates(tmp_path):
    # Same declaration but the SET is not visible (e.g. done in unparsed code): the
    # 88 VALUEs are still the values the program was WRITTEN to use - candidates,
    # reported in the flag, never presented as proof.
    from cobol_xstate.preprocessor import CopybookResolver
    (tmp_path / "DC01104.cpy").write_text(
        "       01 DC01104-CONSTANTS.\n"
        "          05 CN-DCIOC104            PIC X(08).\n"
        "             88 DCIOC104-MODULE     VALUE 'DCIOC104'.\n"
        "       01 DC01104-PARMS             PIC X(100).\n")
    machine = _machine_with(
        _COPYBOOK_CALL_SRC, resolver=CopybookResolver(paths=[str(tmp_path)]))
    msgs = " ".join(f["message"] for f in machine.flags)
    assert "dynamic CALL CN-DCIOC104" in msgs
    assert "88-level" in msgs and "DCIOC104" in msgs


def test_two_88_modules_both_set_stay_ambiguous():
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. TWOMOD.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01 WS-MODULE PIC X(8).\n"
        "          88 MOD-A VALUE 'PGMA'.\n"
        "          88 MOD-B VALUE 'PGMB'.\n"
        "       01 WS-FLAG PIC X.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           IF WS-FLAG = 'A'\n"
        "               SET MOD-A TO TRUE\n"
        "           ELSE\n"
        "               SET MOD-B TO TRUE\n"
        "           END-IF\n"
        "           CALL WS-MODULE\n"
        "           GOBACK.\n"
    )
    machine = _machine(src)
    msgs = " ".join(f["message"] for f in machine.flags)
    assert "dynamic CALL WS-MODULE" in msgs
    assert "PGMA" in msgs and "PGMB" in msgs


def test_dynamic_call_target_in_missing_copybook_diagnosed():
    # Same program, copybook NOT found: the target cannot resolve, and the flag must
    # say the real situation - the identifier is not declared in the visible source
    # and the missing copybook is where its VALUE hides - not "set from variables".
    machine = _machine(_COPYBOOK_CALL_SRC)
    msgs = " ".join(f["message"] for f in machine.flags)
    assert "dynamic CALL CN-DCIOC104" in msgs
    assert "not declared in the visible source" in msgs
    assert "DC01104" in msgs


def test_dynamic_sql_execute_immediate_flagged():
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. DYNSQL.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01 WS-SQL PIC X(200).\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           EXEC SQL EXECUTE IMMEDIATE :WS-SQL END-EXEC\n"
        "           GOBACK.\n"
    )
    msgs = " ".join(f["message"] for f in _machine(src).flags)
    assert "dynamic SQL" in msgs and "assembled at run time" in msgs


def test_alter_modeled_as_context_driven_guard_switch():
    machine = _machine((EXAMPLES / "altswitch.cbl").read_text())
    # The altered paragraph's exit is a guard set over its candidate targets...
    switch = machine.config["states"]["1000-SWITCH"]["always"]
    targets = {e["target"] for e in switch}
    assert targets == {"1100-FIRST", "1200-NORMAL"}
    assert all("guard" in e for e in switch)
    # ...seeded from a typed synthetic context field holding the head GO TO target...
    assert machine.config["context"]["ALT-1000-SWITCH"] == "1100-FIRST"
    assert "ALT-1000-SWITCH" in machine.data          # typed, so the js target stores it
    # ...the ALTER itself is a REAL set-action assignment that flips the switch...
    first = machine.config["states"]["1100-FIRST"]["entry"]
    set_name = next(a for a in first if a.startswith("set_alt_1000-SWITCH_to_1200-NORMAL"))
    sem = machine.semantics["actions"][set_name]
    assert sem["assignments"] == [{"target": "ALT-1000-SWITCH", "expr": "'1200-NORMAL'"}]
    # ...the exit guards are real evaluable conditions over that field...
    guards = machine.semantics["guards"]
    switch_guards = [e["guard"] for e in switch]
    assert all(g in guards for g in switch_guards)
    # ...and it is still flagged as runtime-switched (verify, don't trust blindly).
    assert any("ALTER-switched" in f["message"] for f in machine.flags)


def test_goto_is_exit_transition_suppressing_fallthrough():
    machine = _machine(
        "       PROCEDURE DIVISION.\n"
        "       0000-A.\n"
        "           GO TO 9000-Z.\n"
        "       1000-B.\n"
        "           DISPLAY 'B'.\n"
        "       9000-Z.\n"
        "           STOP RUN.\n"
    )
    a = machine.config["states"]["0000-A"]["always"]
    kinds = {e["meta"]["kind"] for e in a}
    assert kinds == {"goto"}                       # exit transition only
    assert all(e["target"] == "9000-Z" for e in a)  # no fall-through to 1000-B


def test_evaluate_produces_guarded_transitions():
    machine = _machine((EXAMPLES / "banktran.cbl").read_text())
    dispatch = machine.config["states"]["2000-DISPATCH"]
    guards = [e.get("guard") for e in dispatch["always"] if "guard" in e]
    assert len(guards) >= 3  # WHEN 'D' / 'W' / 'I'


def test_bundle_is_json_serializable_and_well_formed():
    machine = _machine((EXAMPLES / "custrpt.cbl").read_text())
    text = machine.to_json()
    obj = json.loads(text)
    assert obj["format"] == "xstate-v5-config"
    assert "machine" in obj and "provenance" in obj and "flags" in obj
    # machine-only path is the bare config
    bare = json.loads(machine.to_json(machine_only=True))
    assert "states" in bare and "format" not in bare


# --------------------------------------------------------------------------- #
# name-collision cluster (review findings J4, J5, J6)
# --------------------------------------------------------------------------- #

def test_duplicate_paragraph_names_in_two_sections_are_kept_distinct():
    """The COMMON-EXIT shop idiom: one paragraph name, once per section. As one shared
    state id the second definition overwrote the first, so BOTH sections' exits ran the
    last body - here section A would MOVE 222 (B's value). Now each is its own state,
    referenced section-locally, and provenance records the source spelling."""
    machine = _machine(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. DUP.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01 WS-A PIC 9(4) VALUE 0.\n"
        "       01 WS-B PIC 9(4) VALUE 0.\n"
        "       PROCEDURE DIVISION.\n"
        "       A-SECTION SECTION.\n"
        "       A-START.\n"
        "           PERFORM COMMON-EXIT.\n"
        "       COMMON-EXIT.\n"
        "           MOVE 111 TO WS-A.\n"
        "       B-SECTION SECTION.\n"
        "       B-START.\n"
        "           PERFORM COMMON-EXIT.\n"
        "       COMMON-EXIT.\n"
        "           MOVE 222 TO WS-B.\n"
    )
    states = machine.config["states"]
    assert "COMMON-EXIT" not in states, "the collided name must not survive as one state"
    a_exit = "COMMON-EXIT_OF_A-SECTION"
    b_exit = "COMMON-EXIT_OF_B-SECTION"
    assert a_exit in states and b_exit in states

    def performs(para):
        return [a for a in states[para]["entry"] if a.startswith("perform_")]

    # each section performs ITS OWN exit, not the other's
    assert performs("A-START") == [f"perform_{a_exit}"]
    assert performs("B-START") == [f"perform_{b_exit}"]
    # the two bodies stayed separate: A moves 111, B moves 222
    a_body = [machine.provenance[x]["cobol"] for x in states[a_exit]["entry"]]
    b_body = [machine.provenance[x]["cobol"] for x in states[b_exit]["entry"]]
    assert "MOVE 111 TO WS-A" in a_body and "MOVE 222 TO WS-B" in b_body
    # provenance still points back to the source name
    assert machine.provenance[a_exit]["bareName"] == "COMMON-EXIT"


def test_no_qualification_when_a_paragraph_name_is_unique():
    machine = _machine((EXAMPLES / "custrpt.cbl").read_text())
    assert not any(p.get("bareName") for p in machine.provenance.values())


def test_two_identical_perform_n_times_get_independent_exit_guards():
    """Both loops read `5 TIMES`, but each has its own synthetic counter. Sharing the
    guard name (the registry dedups on text) made both test the FIRST counter, so the
    second loop ran zero times - or, reordered, never terminated."""
    machine = _machine(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. TWICE.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01 WS-A PIC 9(4) VALUE 0.\n"
        "       01 WS-B PIC 9(4) VALUE 0.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           PERFORM 5 TIMES\n"
        "               ADD 1 TO WS-A\n"
        "           END-PERFORM\n"
        "           PERFORM 5 TIMES\n"
        "               ADD 1 TO WS-B\n"
        "           END-PERFORM\n"
        "           STOP RUN.\n"
    )
    guards = machine.semantics["guards"]
    counters = set()
    for name, st in machine.config["states"].items():
        for e in st.get("always", []) or []:
            g = e.get("guard")
            tree = guards.get(g) if g else None
            if isinstance(tree, dict) and str(tree.get("left", "")).startswith("TIMES-CTR"):
                counters.add(tree["left"])
    # two loops -> two distinct counters, each governing its own loop exit
    assert len(counters) == 2, f"each loop must test its own counter, got {counters}"
