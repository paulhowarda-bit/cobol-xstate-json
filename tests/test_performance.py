"""Guards on the optimizations, so a later refactor cannot silently reintroduce the
cost. These assert *structure* (work done once, index maps present) rather than wall
clock, which would be flaky on shared CI."""

import cobol_xstate.interface as iface_mod
import cobol_xstate.statechart as statechart_mod
from cobol_xstate.artifacts import build_artifacts
from cobol_xstate.business import build_business_view
from cobol_xstate.interface import _DataView, _state_index
from cobol_xstate.lexer import Token, tokenize
from cobol_xstate.lineage import build_lineage
from cobol_xstate.normalizer import normalize
from cobol_xstate.parser import parse_program
from cobol_xstate.statechart import build_machine

SRC = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. PERFT.\n"
    "       ENVIRONMENT DIVISION.\n"
    "       INPUT-OUTPUT SECTION.\n"
    "       FILE-CONTROL.\n"
    "           SELECT CUST-FILE ASSIGN TO CUSTDD FILE STATUS IS WS-FS.\n"
    "       DATA DIVISION.\n"
    "       FILE SECTION.\n"
    "       FD CUST-FILE.\n"
    "       01 CUST-REC.\n"
    "          05 CUST-ID   PIC X(8).\n"
    "          05 CUST-NAME PIC X(30).\n"
    "       WORKING-STORAGE SECTION.\n"
    "       01 WS-FS PIC XX.\n"
    "       01 WS-A  PIC X(10).\n"
    "       LINKAGE SECTION.\n"
    "       01 DFHCOMMAREA.\n"
    "          05 CA-ID PIC X(8).\n"
    "       PROCEDURE DIVISION.\n"
    "       0000-MAIN.\n"
    "           READ CUST-FILE INTO CUST-REC AT END CONTINUE END-READ\n"
    "           IF WS-FS = '00'\n"
    "               MOVE CUST-ID TO WS-A\n"
    "           END-IF\n"
    "           DISPLAY WS-A.\n"
)


def _machine():
    return build_machine(parse_program(SRC))


def test_the_interface_overlay_is_built_once_per_machine(monkeypatch):
    """A default run produces four views over one unchanged machine. Each rebuilding
    the overlay meant re-walking every state and re-classifying every entry action
    four to five times per program."""
    calls = []
    real = iface_mod.build_interface

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(iface_mod, "build_interface", counting)
    monkeypatch.setattr(statechart_mod, "build_interface", counting)

    m = _machine()
    m.bundle()
    build_business_view(m)
    build_lineage(m)
    build_artifacts(m)
    assert len(calls) == 1


def test_interface_returns_the_same_object_on_repeat_calls():
    m = _machine()
    assert m.interface() is m.interface()


def test_reactive_is_not_served_the_cached_overlay():
    """The reactive view builds its overlay over a FLATTENED, rewritten config - a
    different input - so it must not be handed the machine's cached one."""
    from cobol_xstate.reactive import build_reactive_view
    m = _machine()
    cached = m.interface()
    view = build_reactive_view(m)
    assert view is not cached
    # ...and the machine's own config is untouched by the reactive lowering.
    assert m.interface() is cached


def test_state_index_finds_every_state_in_one_walk():
    m = _machine()
    index = _state_index(m.config)
    names = set()

    def rec(states):
        for n, st in (states or {}).items():
            names.add(n)
            rec(st.get("states"))

    rec(m.config.get("states", {}))
    assert set(index) == names
    for n in names:
        assert index[n] is not None


def test_dataview_indexes_records_by_file():
    m = _machine()
    dv = _DataView(m.data)
    assert dv.records_of("CUST-FILE") == ["CUST-REC"]
    assert dv.records_of("NO-SUCH-FILE") == []


def test_dataview_leaves_returns_the_record_layout():
    m = _machine()
    dv = _DataView(m.data)
    assert dv.leaves("CUST-REC") == ["CUST-ID", "CUST-NAME"]


def test_token_up_is_precomputed_and_case_insensitive():
    toks = tokenize(normalize(
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           move 1 to Ws-A.\n"))
    words = [t for t in toks if t.kind == "word"]
    assert any(t.up == "MOVE" and t.text == "move" for t in words)
    # is_word compares against the uppercase spelling regardless of source case
    assert any(t.is_word("MOVE") for t in words)
    assert any(t.is_word("WS-A") for t in words)


def test_token_up_survives_explicit_construction():
    assert Token("Move", 1, "word").up == "MOVE"
    assert Token("Move", 1, "word").is_word("MOVE")


def test_dedup_preserves_first_seen_order():
    from cobol_xstate.artifacts import _dedup
    assert _dedup(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]
    assert _dedup([3, 1, 3, 2]) == [3, 1, 2]


def test_strip_arith_clauses_fast_path_matches_the_slow_path():
    from cobol_xstate.semantics import _strip_arith_clauses
    # no clauses -> untouched core, both flags false
    assert _strip_arith_clauses("MOVE A TO B") == ("MOVE A TO B", False, False)
    # clauses present -> stripped and flagged
    core, rounded, size_err = _strip_arith_clauses(
        "COMPUTE X = Y * 2 ROUNDED ON SIZE ERROR MOVE 0 TO X")
    assert core == "COMPUTE X = Y * 2"
    assert rounded and size_err


def test_norm_subscripts_fast_path_is_a_no_op_without_parens():
    from cobol_xstate.semantics import _norm_subscripts
    assert _norm_subscripts("MOVE WS-A TO WS-B") == "MOVE WS-A TO WS-B"
    assert _norm_subscripts("MOVE TBL (I) TO X") == "MOVE TBL(I) TO X"
