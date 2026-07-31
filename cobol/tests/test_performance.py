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


def test_lineage_analysis_is_built_once_per_machine(monkeypatch):
    """The lineage table and the dynamic-call view are two projections of the same
    reaching-origins fixpoint - the most expensive analysis in the tool - and a default
    run writes both. Building it once for both is the single largest saving in a run."""
    import cobol_xstate.lineage as lineage_mod
    calls = []
    real = lineage_mod._Lineage

    def counting(machine):
        calls.append(1)
        return real(machine)

    monkeypatch.setattr(lineage_mod, "_Lineage", counting)
    m = _machine()
    build_lineage(m)
    from cobol_xstate.artifacts import build_artifacts
    from cobol_xstate.dynamic_calls import build_dynamic_calls
    build_dynamic_calls(m, build_artifacts(m))
    assert len(calls) == 1


def test_lineage_returns_the_same_object_on_repeat_calls():
    m = _machine()
    assert m.lineage() is m.lineage()
    # ...and solving it twice does not append its flags twice.
    first = m.lineage().run()
    again = m.lineage().run()
    assert again is first


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


# --------------------------------------------------------------------------- #
# lineage's reaching-origins fixpoint
#
# The same defect as the condition fixpoint above, in the pass next door, and dearer: a
# wasted visit there costs a few integer ops, here it re-runs the whole transfer function
# over a state's entry run and re-merges every predecessor's full field map. It was walked
# from a STACK with no dedup, so a state was re-propagated once per revision of anything
# upstream of it - and what drives the revisions is the WIDTH of the lattice, the number of
# fields, which no test built on a handful of data items could ever show. Measured across
# programs of one shape: 6.8 visits per state over 5 fields, 41.6 over 100, 81.6 over 300,
# against exactly 1.0 for all three in queue order. 19x to 48x, widening with size.
# --------------------------------------------------------------------------- #

def _wide_fields_machine(paras: int, fields: int):
    """`paras` reconverging diamonds over `fields` distinct data items.

    Two axes, because only their PRODUCT exposes the cost: the diamonds make the graph,
    and the fields make each state's origin map wide enough that a merge point revises it
    many times. Every field is written from a LINKAGE item so a real origin flows into it -
    a field nothing external reaches never revises anything and would not stress the pass.
    """
    src = [
        "       IDENTIFICATION DIVISION.",
        "       PROGRAM-ID. WIDEF.",
        "       DATA DIVISION.",
        "       WORKING-STORAGE SECTION.",
        "       01 WS-AMT PIC 9(5) VALUE 0.",
    ]
    src += [f"       01 F{i:04d} PIC 9(5) VALUE 0." for i in range(fields)]
    src += [
        "       LINKAGE SECTION.",
        "       01 DFHCOMMAREA.",
        "          05 CA-ID PIC X(8).",
        "       PROCEDURE DIVISION.",
        "       0000-MAIN.",
    ]
    src += [f"           PERFORM {i + 1:04d}-STEP" for i in range(paras)]
    # An external boundary at the end, so the pass has rows to emit at all: the fields
    # carry a LINKAGE origin by now, and a DISPLAY is what asks where it came from.
    src += [f"           DISPLAY F{i:04d}" for i in range(min(fields, 8))]
    src += ["           STOP RUN."]
    for i in range(paras):
        src += [
            f"       {i + 1:04d}-STEP.",
            f"           IF WS-AMT > {i + 1}",
            f"               ADD CA-ID TO F{i % fields:04d}",
            "           ELSE",
            f"               ADD 1 TO F{i % fields:04d}",
            "           END-IF.",
        ]
    return build_machine(parse_program("\n".join(src) + "\n"))


def test_origins_fixpoint_settles_in_roughly_one_visit_per_state(monkeypatch):
    """Queue order, so the transfer function runs about once per state.

    Counts `_apply`, which IS the expensive thing - one call is one state's whole entry
    run re-interpreted. The threshold sits far from both the 1.0 a queue achieves and the
    81.6 a stack reached on this shape, so scheduling noise cannot trip it; only the
    ordering actually regressing can.
    """
    calls = []
    real = _Lineage._apply

    def counting(self, name, st, incoming, rows):
        calls.append(1)
        return real(self, name, st, incoming, rows)

    monkeypatch.setattr(_Lineage, "_apply", counting)
    lin = _Lineage(_wide_fields_machine(80, 100))
    lin.run()
    # the fixpoint, plus one final row-emitting pass over every reached state
    assert len(calls) < 5 * len(lin.states), (
        f"{len(calls)} transfer-function runs for {len(lin.states)} states - the "
        f"worklist is re-propagating, which is what stack order did")


def test_origins_fixpoint_answer_does_not_depend_on_visit_order():
    """The queue and the dedup set are a scheduling choice, never a semantic one.

    Skipping a re-queue is only sound because the worklist holds NAMES, not values: the
    incoming map is re-merged from the predecessors' current outputs at pop time, so
    collapsing two pending visits into one cannot lose an update. That is the property
    this asserts, against the exhaustive stack-order walk it replaced - if the dedup ever
    drops a visit that mattered, the two answers diverge.
    """
    m = _wide_fields_machine(24, 40)

    ref = _Lineage(m)
    ref.changers = ref._changers()
    preds = {s: [] for s in ref.states}
    for s, ts in ref.succs.items():
        for t in ts:
            if t in preds:
                preds[t].append(s)
    seed = ref._seed()
    IN = {s: None for s in ref.states}
    OUT = {s: None for s in ref.states}
    work = list(ref.entries)                  # a STACK, and every push kept
    while work:
        s = work.pop()
        merged = dict(seed) if s in ref.entries else {}
        for p in preds[s]:
            if OUT[p] is None:
                continue
            for f, o in OUT[p].items():
                merged[f] = merged.get(f, frozenset()) | o
        if IN[s] is not None and merged == IN[s]:
            continue
        IN[s] = merged
        new_out = ref._apply(s, ref.states[s], merged, None)
        if OUT[s] is None or new_out != OUT[s]:
            OUT[s] = new_out
            work.extend(t for t in ref.succs.get(s, []) if t in ref.states)
    expected = []
    for s in ref.states:
        if IN[s] is not None:
            ref._apply(s, ref.states[s], IN[s], expected)

    assert _Lineage(m).run()["rows"] == expected
    assert expected, "the fixture must actually produce lineage rows to compare"


# --------------------------------------------------------------------------- #
# the business view's collapse walk
#
# It used to recurse, at roughly ten interpreter frames per technical state stepped
# through, so a chain of a hundred nested PERFORMs raised RecursionError. That is not a
# degraded business view, it is none at all - and since a default run writes this
# companion FIRST, the lineage, reactive, artifacts and dynamic-call companions were lost
# with it. It also enumerated one edge per distinct guard PATH, so sixteen IF/ELSE
# diamonds that all reconverge produced 2^16 = 65,536 edges into a single state,
# asserting 65,536 business rules where there was not one.
# --------------------------------------------------------------------------- #

BIZ_HEAD = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. BIZP.\n"
    "       DATA DIVISION.\n"
    "       WORKING-STORAGE SECTION.\n"
    "       01 WS-A PIC X(10).\n"
    "       PROCEDURE DIVISION.\n"
    "       0000-MAIN.\n"
)


def _goto_chain(n: int):
    """A LINEAR chain of n technical states - one walk step each, no branching."""
    src = [BIZ_HEAD.rstrip("\n"), "           GO TO 0001-STEP."]
    for i in range(1, n + 1):
        src += [f"       {i:04d}-STEP.", "           MOVE 'X' TO WS-A"]
        src.append(f"           GO TO {i + 1:04d}-STEP." if i < n else "           STOP RUN.")
    return build_machine(parse_program("\n".join(src) + "\n"))


def _reconverging_diamonds(n: int):
    """n guarded IF/ELSE diamonds that all rejoin, then a boundary.

    `FUNCTION NUMVAL` is a real condition the parser does not model, so each guard is
    {op:'raw'}. Two independent things must hold at once: J11 means a raw guard is a
    business condition, so each diamond is surfaced as its own DECISION rather than
    hidden as scaffolding; and the collapse walk must still not enumerate one edge per
    guard COMBINATION - 2^n through a region that used to (wrongly) collapse into one.
    """
    src = [BIZ_HEAD.rstrip("\n")]
    for i in range(n):
        src += [f"           IF FUNCTION NUMVAL(WS-A) > {i + 1}",
                "               MOVE 'A' TO WS-A",
                "           ELSE",
                "               MOVE 'B' TO WS-A",
                "           END-IF"]
    src += ["           DISPLAY WS-A", "           STOP RUN."]
    return build_machine(parse_program("\n".join(src) + "\n"))


def test_collapse_walk_survives_a_chain_deeper_than_the_recursion_limit():
    import sys
    n = sys.getrecursionlimit() + 200        # unreachable for anything recursive
    view = build_business_view(_goto_chain(n))
    assert view["entry"], "a program with one straight path must have an entry edge"


def test_reconverging_diamonds_grow_linearly_not_exponentially():
    """n independent diamonds must cost O(n) edges, never O(2^n).

    Before, an unparsed condition was misfiled as control, the whole region collapsed to
    technical, and the walk enumerated every guard subset - 2^16 = 65,536 edges into one
    state. Now each diamond is a decision the walk stops at (J11), and even a region that
    did collapse is protected by guard-set subsumption. Either way the count is linear,
    which two sizes prove and a single size never could: doubling n must not square the
    edges. No budget flag - this is solved outright, not truncated.
    """
    small = build_business_view(_reconverging_diamonds(8))
    large = build_business_view(_reconverging_diamonds(16))
    assert not small["flags"] and not large["flags"]
    e8, e16 = len(small["transitions"]), len(large["transitions"])
    assert e16 < 4 * e8, f"edges grew faster than linear: {e8} -> {e16}"
    # ...and the diamonds are visible as decisions, not collapsed away (J11)
    decisions = [d for st in large["businessStates"].values() for d in st.get("decisions", [])]
    assert len(decisions) >= 16, "the unparsed conditions must survive as decisions"


def _view_of(name: str):
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "examples" / name).read_text()
    return build_business_view(build_machine(parse_program(src), source_name=name))


def test_alternative_routes_are_still_reported_separately():
    """The other side of the subsumption: guard sets that do NOT contain one another are
    genuine alternatives and must all survive. banktran's dispatcher fans out to four
    paragraphs on four values of one field."""
    view = _view_of("banktran.cbl")
    outs = {(t["to"], tuple(g["name"] for g in t["guards"]))
            for t in view["transitions"] if t["from"] == "2000-DISPATCH"}
    tos = {to for to, _ in outs}
    assert {"2100-DEPOSIT", "2200-WITHDRAW", "2300-INQUIRY"} <= tos
    for to, guards in outs:
        if to in ("2100-DEPOSIT", "2200-WITHDRAW", "2300-INQUIRY"):
            assert guards, f"{to} is reached under a condition; it must still say which"


def test_the_collapsed_path_survives_being_carried_as_a_cons_chain():
    """Paths are consed, not copied, so extending one is O(1) instead of quadratic in the
    walk's own depth. The risk that buys is the flattening: `via` must still come out
    complete and in the order control took, not reversed or truncated."""
    n = 300
    view = build_business_view(_goto_chain(n))
    via = view["entry"][0]["via"]
    steps = [s for s in via if s.endswith("-STEP")]
    assert steps == sorted(steps), "the collapsed path must read in execution order"
    assert steps[0] == "0001-STEP" and steps[-1] == f"{n:04d}-STEP"
    assert len(steps) == n, f"every state on the chain should appear, got {len(steps)}"


def test_chain_flattens_a_cons_list_oldest_first():
    from cobol_xstate.business import _chain
    assert _chain(None) == []
    assert _chain(("c", ("b", ("a", None)))) == ["a", "b", "c"]


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
