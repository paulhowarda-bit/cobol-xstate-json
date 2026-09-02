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


def _converging_handlers_machine(handlers: int, whens: int, width: int):
    """The CICS shape that made a FIFO worklist re-solve a 4,236-line program 25 times
    over (26.5 pops per state; 3.0 in reverse postorder).

    A dispatch EVALUATE fans out to `handlers` handler paragraphs of DIFFERENT lengths.
    Each moves literals of its own - a distinct origin per site - into a `width`-leaf
    record a READ filled, then performs one shared edit routine, which performs SEND-MAP,
    a `whens`-way EVALUATE. Breadth-first, handler h reaches the shared routine at depth
    ~2h, so the routine's map changes once per handler and every change fans out through
    the EVALUATE to everything after it.

    The origins MUST differ per path. When every handler moved the same READ-filled
    leaves, each arrival added nothing to the union, the join never re-fired, and the
    shape measured a clean 1.0 - which is how the fixtures above stayed blind to this.
    Even so it reproduces a fraction of the real program's waste (FIFO measured 1.6
    here), so the threshold below is a floor on the scheduler, not a ceiling on the bug.
    """
    src = [
        "       IDENTIFICATION DIVISION.",
        "       PROGRAM-ID. CONVERGE.",
        "       ENVIRONMENT DIVISION.",
        "       INPUT-OUTPUT SECTION.",
        "       FILE-CONTROL.",
        "           SELECT CUSTFILE ASSIGN TO CUSTIN.",
        "       DATA DIVISION.",
        "       FILE SECTION.",
        "       FD CUSTFILE.",
        "       01 CUST-IN PIC X(2000).",
        "       WORKING-STORAGE SECTION.",
        "       01 WS-EOF PIC X VALUE 'N'.",
        "       01 WS-AID PIC X(2).",
        "       01 WS-OUT PIC X(10).",
        "       01 WS-MSG PIC X(10).",
        "       01 WS-REC.",
    ]
    src += [f"          05 WS-F{i:03d} PIC X(10)." for i in range(width)]
    src += [
        "       LINKAGE SECTION.",
        "       01 DFHCOMMAREA.",
        "          05 CA-ID PIC X(8).",
        "       PROCEDURE DIVISION.",
        "       0000-MAIN.",
        "           OPEN INPUT CUSTFILE",
        "           READ CUSTFILE INTO WS-REC",
        "               AT END MOVE 'Y' TO WS-EOF",
        "           END-READ",
        "           EVALUATE WS-AID",
    ]
    for h in range(1, handlers + 1):
        src += [f"               WHEN '{h:02d}'",
                f"                   PERFORM {h:04d}-HANDLE THRU {h:04d}-EXIT"]
    src += [
        "               WHEN OTHER",
        "                   PERFORM 3000-SEND-MAP THRU 3000-EXIT",
        "           END-EVALUATE",
        "           DISPLAY WS-OUT",
        "           STOP RUN.",
    ]
    for h in range(1, handlers + 1):
        src += [f"       {h:04d}-HANDLE."]
        for k in range(h * 2):                     # handler h is 2h statements long
            src.append(f"           MOVE 'L{h:02d}{k:02d}' TO WS-F{(h * 5 + k) % width:03d}")
        src += [
            f"           MOVE CA-ID TO WS-F{h % width:03d}",
            f"           MOVE 'M{h:02d}' TO WS-MSG",
            "           PERFORM 9000-EDIT THRU 9000-EXIT.",
            f"       {h:04d}-EXIT.",
            "           EXIT.",
        ]
    src += [
        "       9000-EDIT.",
        "           IF WS-MSG = 'X'",
        "               MOVE WS-F000 TO WS-OUT",
        "           END-IF",
        "           PERFORM 3000-SEND-MAP THRU 3000-EXIT.",
        "       9000-EXIT.",
        "           EXIT.",
        "       3000-SEND-MAP.",
        "           EVALUATE TRUE",
    ]
    for w in range(whens):
        src += [f"               WHEN WS-F{w % width:03d} = 'A'",
                f"                   MOVE WS-F{w % width:03d} TO WS-OUT"]
    src += [
        "           END-EVALUATE",
        "           DISPLAY WS-MSG.",
        "       3000-EXIT.",
        "           EXIT.",
    ]
    return build_machine(parse_program("\n".join(src) + "\n"))


def test_origins_fixpoint_holds_near_one_visit_per_state_on_converging_paths(monkeypatch):
    """Reverse postorder, in passes: a state runs about once per pass and the passes
    settle at the loop depth. Counts only the fixpoint's runs (the row-emitting pass
    hands `_apply` a list), so the number IS the scheduler's waste."""
    runs = []
    real = _Lineage._apply

    def counting(self, name, st, incoming, rows):
        if rows is None:
            runs.append(1)
        return real(self, name, st, incoming, rows)

    monkeypatch.setattr(_Lineage, "_apply", counting)
    lin = _Lineage(_converging_handlers_machine(40, 80, 150))
    lin.run()
    assert len(runs) < 1.3 * len(lin.states), (
        f"{len(runs)} transfer-function runs for {len(lin.states)} states - a FIFO "
        f"queue measured 1.6 per state on this shape and 26.5 on the real program")


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


# --------------------------------------------------------------------------- #
# lineage's call-graph construction and its `partial` test
#
# Upstream ledger item 23. On the estate the lineage stage was 99% of all modelling time,
# and one program family took nineteen minutes to emit 400 KB while programs of the same
# size took seconds. The reaching-origins worklist above was NOT it (about two visits per
# state). Two other things were quadratic in the program: `_successors` rescanned every
# state for every PERFORM node to find the performed extent's exits (sites x states -
# 8.4 million extent tests on an 8,400-state program), and `_conditions_of` decoded the
# MAY bitmask - a union that holds most of the guard vocabulary at every state deep in
# the program - into a frozenset once per state and again per row, only to compute the
# boolean `partial`. The shape that triggers both is the IBM-shop house style: a PERFORM
# per paragraph, `PERFORM p THRU p-EXIT`, an EVALUATE per paragraph, one utility
# paragraph performed from many sites. Measured on that shape at 800 paragraphs: 38.7s
# before, 1.0s after, byte-identical output.
# --------------------------------------------------------------------------- #

_A36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _perform_thru_machine(paras: int, whens: int = 4):
    """A program in the house style that made this quadratic: MAIN performs every
    paragraph as `PERFORM nnnn-PARA THRU nnnn-EXIT`; each paragraph is an EVALUATE with
    `whens` distinct WHENs (each a distinct guard) plus an IF; every fourth one also
    performs a shared utility. PERFORM sites and states both grow with `paras`, so a
    sites x states scan is quadratic while the call graph it builds is linear."""
    NF = 20
    src = [
        "       IDENTIFICATION DIVISION.",
        "       PROGRAM-ID. PERFTHRU.",
        "       DATA DIVISION.",
        "       WORKING-STORAGE SECTION.",
        "       01 WS-CODE PIC X(2) VALUE '00'.",
        "       01 WS-NAME PIC X(30).",
        "       01 WS-ACC  PIC 9(5) VALUE 0.",
        "       01 X000    PIC X(10).",
    ]
    src += [f"       01 F{i:03d} PIC 9(5) VALUE 0." for i in range(NF)]
    src += [
        "       LINKAGE SECTION.",
        "       01 DFHCOMMAREA.",
        "          05 CA-ID  PIC X(8).",
        "          05 CA-AMT PIC 9(7).",
        "       PROCEDURE DIVISION.",
        "       0000-MAIN.",
    ]
    src += [f"           PERFORM {i:04d}-PARA THRU {i:04d}-EXIT" for i in range(1, paras + 1)]
    src += [f"           DISPLAY F{k:03d}" for k in range(8)]
    src += ["           DISPLAY WS-NAME", "           STOP RUN."]
    for i in range(1, paras + 1):
        k = (i - 1) % NF
        src += [f"       {i:04d}-PARA.", "           EVALUATE WS-CODE"]
        for w in range(whens):
            t = ((i - 1) * whens + w) % 1296
            src.append(f"               WHEN '{_A36[t // 36]}{_A36[t % 36]}'")
            src.append(f"                   {'MOVE' if w % 2 == 0 else 'ADD'} CA-AMT "
                       f"TO F{(k + w) % NF:03d}")
            if w == 0 and i % 4 == 0:
                src.append("                   PERFORM 9000-COMMON THRU 9000-EXIT")
        src += [
            "               WHEN OTHER",
            f"                   MOVE 1 TO F{k:03d}",
            "           END-EVALUATE",
            f"           IF F{k:03d} > {i}",
            "               MOVE CA-ID TO WS-NAME",
            "           END-IF.",
            f"       {i:04d}-EXIT.",
            "           EXIT.",
        ]
    src += [
        "       9000-COMMON.",
        "           MOVE CA-ID TO X000",
        "           ADD CA-AMT TO WS-ACC",
        "           DISPLAY X000.",
        "       9000-EXIT.",
        "           EXIT.",
    ]
    return build_machine(parse_program("\n".join(src) + "\n"))


def _perform_sites(lin) -> int:
    return sum(1 for st in lin.states.values() if lin._perform_of(st))


def test_perform_thru_fixture_has_the_shape_it_claims():
    """The guards below count operations against this shape; a fixture whose PERFORMs
    silently failed to resolve would make every one of them vacuous."""
    lin = _Lineage(_perform_thru_machine(20))
    view = lin.run()
    assert not [f for f in lin.flags if "unresolved" in f or "inverted" in f]
    assert view["rows"], "the boundary DISPLAYs must produce rows"
    assert _perform_sites(lin) > 20          # one per paragraph plus the shared utility
    assert any("PERFORMed from" in f for f in lin.flags), "the shared utility must merge"


def test_return_wiring_does_not_scan_every_state_per_perform_site(monkeypatch):
    """The exits of a performed extent are found once per distinct target, from an
    index of paragraph -> states. The paragraph lookup used to run once per (PERFORM
    site, state, edge) - sites x states - and it is what this counts."""
    calls = []
    real = lineage_mod._para_of

    def counting(key):
        calls.append(1)
        return real(key)

    monkeypatch.setattr(lineage_mod, "_para_of", counting)
    lin = _Lineage(_perform_thru_machine(40))
    assert calls, "the paragraph lookup is no longer routed through _para_of - guard blind"
    assert len(calls) < 3 * len(lin.states), (
        f"{len(calls)} paragraph lookups for {len(lin.states)} states and "
        f"{_perform_sites(lin)} PERFORM sites - the extent scan is back")


def test_perform_targets_are_resolved_once_per_distinct_target(monkeypatch):
    """`_target_owner` walks `ordered` (an `index` per THRU endpoint), so resolving a
    target per site - twice, through `perform_target` and again in `_successors` - was a
    second sites x paragraphs term. Now: once per PERFORM node's action name, and once
    per distinct target for the extent."""
    import cobol_xstate.emitter as emitter_mod
    calls = []
    real = emitter_mod._target_owner

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(emitter_mod, "_target_owner", counting)
    monkeypatch.setattr(lineage_mod, "_target_owner", counting)
    lin = _Lineage(_perform_thru_machine(40))
    during_build = len(calls)                # the counts below resolve targets again
    sites = _perform_sites(lin)
    targets = {lin._perform_of(st) for st in lin.states.values()} - {None}
    assert during_build and sites > len(targets), "the utility must be performed from many sites"
    # perform_target may try the name and its `_2`-trimmed form: at most two per site,
    # plus one per distinct target for the extent.
    assert during_build <= 2 * sites + len(targets) + 1, (
        f"{during_build} target resolutions for {sites} sites / {len(targets)} targets")


def _reference_successors(lin):
    """The per-state scan `_successors` replaced, kept as the oracle: for every PERFORM
    node, walk EVERY state and keep the ones inside the performed extent that fall out
    of it. Returns (succ, fold_src, fold_dst) built the old way."""
    from cobol_xstate.emitter import _para_of, _target_owner
    owns = lambda node, owner: _para_of(lin.origin_state.get(node, node)) in owner
    succ, returns, fold_src, fold_dst = {}, {}, {}, {}
    for name, st in lin.states.items():
        target = lin._perform_of(st)
        if not target:
            succ[name] = [t for t in lin._edges(st) if t in lin.states]
            continue
        owner, init = _target_owner(target, lin.ordered, lin.sections)
        cont = next(iter(lin._edges(st)), None)
        if owner is None or init not in lin.states:
            succ[name] = [t for t in lin._edges(st) if t in lin.states]
            continue
        succ[name] = [init]
        if cont is None:
            continue
        for s2, st2 in lin.states.items():
            if not owns(s2, owner) or lin._perform_of(st2):
                continue
            edges = lin._edges(st2)
            stay = [t for t in edges if t in lin.states and owns(t, owner)]
            leaves = [t for t in edges if t not in lin.states or not owns(t, owner)]
            if leaves or not edges:
                if s2 not in returns:
                    returns[s2] = list(stay)
                    fold_src[s2] = list(leaves)
                if cont not in returns[s2]:
                    returns[s2].append(cont)
                    fold_dst.setdefault(s2, []).append(cont)
    for s, conts in returns.items():
        succ[s] = list(conts)
    return succ, fold_src, fold_dst


def test_return_wiring_matches_the_per_state_scan():
    """Indexing the extent is a scheduling choice, never a semantic one - and the order
    of every list matters: `succ` order drives the worklist, `fold_src` becomes edge
    conditions. Checked against the old scan on the shapes that stress it: overlapping
    THRU ranges and sections, nested and repeated PERFORMs, recursion, the house style."""
    from pathlib import Path
    examples = Path(__file__).resolve().parents[1] / "examples"
    machines = [_perform_thru_machine(30)]
    for name in ("sectperf.cbl", "thrurange.cbl", "nestperf.cbl", "perftwice.cbl",
                 "calltwice.cbl", "recur.cbl", "timesexit.cbl", "lineage.cbl"):
        src = (examples / name).read_text()
        machines.append(build_machine(parse_program(src), source_name=name))
    checked = 0
    for m in machines:
        lin = _Lineage(m)
        succ, fold_src, fold_dst = _reference_successors(lin)
        assert list(lin.succs) == list(succ)
        assert lin.succs == succ
        assert lin.fold_src == fold_src and list(lin.fold_src) == list(fold_src)
        assert lin.fold_dst == fold_dst and list(lin.fold_dst) == list(fold_dst)
        checked += bool(fold_dst)
    assert checked == len(machines), "every fixture must actually wire at least one return"


def _set_partial(lin, state) -> bool:
    """The definition `partial` had before it moved onto the packed masks, kept as the
    oracle: some condition in MAY - MUST whose guard MAY does not hold both ways."""
    must = lin._conds(lin.must.get(state))
    may = lin._conds(lin.may.get(state))
    return any(not ((g, True) in may and (g, False) in may) for g, _ in (may - must))


def _condition_fixtures():
    from pathlib import Path
    examples = Path(__file__).resolve().parents[1] / "examples"
    out = [_perform_thru_machine(40)]
    for name in ("lineage.cbl", "banktran.cbl", "altswitch.cbl", "condlin.cbl", "notend.cbl"):
        out.append(build_machine(parse_program((examples / name).read_text()), source_name=name))
    return out


def test_partial_by_bit_arithmetic_matches_the_set_definition():
    """Reconverging IF/ELSE, loops (a guard seen in both polarities), the two-IF
    disjunction, and the utility performed from many sites: every reached state must
    answer as the set definition does, and the corpus must contain both answers."""
    seen = set()
    for m in _condition_fixtures():
        lin = _Lineage(m)
        for s in lin.states:
            if lin.must.get(s) is None:
                continue
            got = lin._conditions_of(s)[1]
            assert got == _set_partial(lin, s), s
            seen.add(got)
    assert seen == {True, False}, "the fixtures must exercise both answers"


def test_pair_mask_marks_exactly_the_adjacent_polarity_pairs():
    paired = 0
    for m in _condition_fixtures():
        lin = _Lineage(m)
        cl = lin.cond_list
        both = {g for g, _ in cl if (g, False) in set(cl) and (g, True) in set(cl)}
        marked = {cl[i][0] for i in range(len(cl)) if lin.pair_lo >> i & 1}
        assert marked == both
        for i in range(len(cl)):
            if lin.pair_lo >> i & 1:
                assert cl[i] == (cl[i][0], False) and cl[i + 1] == (cl[i][0], True)
        paired += len(both)
    assert paired, "the corpus must hold guards in both polarities, or nothing was checked"


def test_partial_bits_on_hand_built_masks():
    vocab = [("A", False), ("A", True), ("B", True), ("C", False), ("C", True)]
    pair_lo = _Lineage._pair_mask(vocab)
    assert pair_lo == 0b01001                    # A at bit 0, C at bit 3
    bit = {c: 1 << i for i, c in enumerate(vocab)}

    def expect(must, may):
        m = {c for c in vocab if must & bit[c]}
        y = {c for c in vocab if may & bit[c]}
        want = any(not ((g, True) in y and (g, False) in y) for g, _ in (y - m))
        assert _Lineage._partial_bits(must, may, pair_lo) is want, (must, may)
        return want

    a_both = bit[("A", False)] | bit[("A", True)]
    assert expect(0, a_both) is False            # reconverged: says nothing
    assert expect(0, bit[("B", True)]) is True   # one-sided: a real constraint
    assert expect(bit[("B", True)], a_both | bit[("B", True)]) is False   # B is in MUST
    assert expect(0, bit[("C", True)]) is True   # negated-only is one-sided too
    assert expect(bit[("C", False)], bit[("C", False)]) is False          # MAY == MUST


def test_partial_never_decodes_the_may_mask(monkeypatch):
    """Only MUST is unpacked, and only once per state: the bits `_conds` is handed over
    a whole run must equal the MUST population over the reached states."""
    seen = []
    real = _Lineage._conds

    def counting(self, bits):
        seen.append(bin(bits or 0).count("1"))
        return real(self, bits)

    monkeypatch.setattr(_Lineage, "_conds", counting)
    lin = _Lineage(_perform_thru_machine(40))
    lin.run()
    assert seen, "_conds is no longer used to unpack MUST - this guard went blind"
    must_bits = sum(bin(b).count("1") for b in lin.must.values() if b)
    assert sum(seen) <= must_bits, (
        f"{sum(seen)} bits unpacked against a MUST population of {must_bits} - MAY is "
        f"being decoded again, or a state more than once")
    assert len(seen) <= len(lin.states)


def test_lineage_operation_count_grows_linearly_with_the_program(monkeypatch):
    """The ledger's guard with teeth: the operations that were quadratic, counted at two
    sizes. Doubling the program must not square the work. Wall clock would be flaky;
    these counts are exact."""
    import cobol_xstate.emitter as emitter_mod

    def run(paras):
        n = {"para": 0, "owner": 0, "bits": 0, "apply": 0}
        r_para, r_owner = lineage_mod._para_of, emitter_mod._target_owner
        r_conds, r_apply = _Lineage._conds, _Lineage._apply
        monkeypatch.setattr(lineage_mod, "_para_of",
                            lambda k: (n.__setitem__("para", n["para"] + 1), r_para(k))[1])
        monkeypatch.setattr(emitter_mod, "_target_owner",
                            lambda *a: (n.__setitem__("owner", n["owner"] + 1), r_owner(*a))[1])
        monkeypatch.setattr(lineage_mod, "_target_owner", emitter_mod._target_owner)
        monkeypatch.setattr(_Lineage, "_conds",
                            lambda self, b: (n.__setitem__("bits", n["bits"] + bin(b or 0).count("1")),
                                             r_conds(self, b))[1])
        monkeypatch.setattr(_Lineage, "_apply",
                            lambda self, *a: (n.__setitem__("apply", n["apply"] + 1),
                                              r_apply(self, *a))[1])
        m = _perform_thru_machine(paras)
        _Lineage(m).run()
        monkeypatch.undo()
        return n

    small, large = run(40), run(80)
    for k in small:
        assert small[k] > 0, f"{k} is not counted any more - this guard went blind"
        assert large[k] < 2.6 * small[k], f"{k}: {small[k]} -> {large[k]} for 2x the program"


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
