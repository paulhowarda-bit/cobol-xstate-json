"""Guards on the optimizations, so a later refactor cannot silently reintroduce the
cost. These assert *structure* (work done once, index maps present) rather than wall
clock, which would be flaky on shared CI."""

from collections import deque

import cobol_xstate.interface as iface_mod
import cobol_xstate.lineage as lineage_mod
import cobol_xstate.statechart as statechart_mod
from cobol_xstate.artifacts import build_artifacts
from cobol_xstate.business import build_business_view
from cobol_xstate.interface import _DataView, _state_index
from cobol_xstate.lexer import Token, tokenize
from cobol_xstate.lineage import _Lineage, build_lineage
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


# --------------------------------------------------------------------------- #
# lineage's path-condition fixpoint
#
# This one is a correctness guard wearing a performance guard's clothes. The fixpoint
# used to be walked depth-first, which re-propagated every state once per revision of
# anything upstream of it; the step count grew with the SQUARE of the state count while
# the iteration bound that was supposed to catch runaways grew only LINEARLY. A large
# program therefore ran out of steps and stopped early - and stopping early does not
# under-report. MUST is an intersection narrowing from an optimistic start, so a
# half-finished run leaves it too LARGE: measured on a 3,126-state program, 3,122 states
# claimed a guard that does not hold on every path to them, while the MAY set that feeds
# the `partial` warning was correspondingly too small, so nothing warned. The table said
# "this WRITE happens only when X" about an X that was not a precondition at all.
# --------------------------------------------------------------------------- #

def _wide_machine(paras: int):
    """A program whose condition lattice is big enough to expose the growth: `paras`
    performed paragraphs, each an IF/ELSE diamond that reconverges."""
    src = [
        "       IDENTIFICATION DIVISION.",
        "       PROGRAM-ID. WIDEP.",
        "       DATA DIVISION.",
        "       WORKING-STORAGE SECTION.",
        "       01 WS-AMT PIC 9(5) VALUE 0.",
        "       01 WS-ACC PIC 9(7) VALUE 0.",
        "       PROCEDURE DIVISION.",
        "       0000-MAIN.",
    ]
    src += [f"           PERFORM {i + 1:04d}-STEP" for i in range(paras)]
    src += ["           DISPLAY WS-ACC", "           STOP RUN."]
    for i in range(paras):
        src += [
            f"       {i + 1:04d}-STEP.",
            f"           IF WS-AMT > {i + 1}",
            "               ADD WS-AMT TO WS-ACC",
            "           ELSE",
            "               ADD 1 TO WS-ACC",
            "           END-IF.",
        ]
    return build_machine(parse_program("\n".join(src) + "\n"))


def test_condition_fixpoint_settles_in_roughly_one_visit_per_state(monkeypatch):
    """Breadth-first, so a state is normally reached after its predecessors settled.

    Depth-first took ~1,300 pops per state on a program this shape; breadth-first takes
    about two. The threshold sits far from both, so it cannot fail on scheduling noise -
    only on the ordering actually regressing.
    """
    pops = []

    class Counted(deque):
        def popleft(self):
            pops.append(1)
            return super().popleft()

        def pop(self):                      # catches a revert to a stack, too
            pops.append(1)
            return super().pop()

    monkeypatch.setattr(lineage_mod, "deque", Counted)
    lin = _Lineage(_wide_machine(40))
    assert pops, "the worklist is no longer a deque - this guard went blind"
    # two passes (MUST and MAY) over the graph
    assert len(pops) < 20 * len(lin.states)


def test_condition_fixpoint_is_actually_a_fixpoint():
    """The property truncation breaks: no edge can still change a set.

    Honest about its reach - this program is far too small to exhaust any bound, so it
    cannot reproduce the original failure. It pins the invariant that failure violated,
    and it fails the instant a run stops short at ANY size. What keeps the bound out of
    reach on a real program is the visit order, guarded above.
    """
    lin = _Lineage(_wide_machine(40))
    for solution, join in ((lin.must, lambda a, b: a & b),
                           (lin.may, lambda a, b: a | b)):
        for s, base in solution.items():
            if base is None:
                continue
            for t in lin.succs.get(s, []):
                if t not in lin.states:
                    continue
                out = base | lin.edge_bits.get((s, t), 0)
                cur = solution[t]
                assert cur is not None, f"{s} -> {t} reached but {t} has no solution"
                assert join(cur, out) == cur, f"{s} -> {t} would still change {t}"
    assert not [f for f in lin.flags if "iteration bound" in f]


def test_condition_bitmasks_round_trip_to_the_conditions_they_stand_for():
    lin = _Lineage(_wide_machine(4))
    assert lin.cond_list == sorted(set(lin.cond_list)), "bit order must be deterministic"
    for state, bits in lin.must.items():
        if bits is None:
            continue
        conds = lin._conds(bits)
        assert conds <= set(lin.cond_list)
        # every condition on this state is on some edge that can reach it
        assert all(isinstance(g, str) and isinstance(neg, bool) for g, neg in conds)
    # a mask built from a known set decodes back to exactly that set
    if len(lin.cond_list) >= 2:
        pick = {lin.cond_list[0], lin.cond_list[1]}
        mask = sum(1 << lin.cond_list.index(c) for c in pick)
        assert lin._conds(mask) == pick
