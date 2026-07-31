"""Stage 5 emitter: COBOL semantics -> runnable XState v5 setup() module.

Pure-Python tests cover the expression parser and guard-tree lowering directly. The
Node integration tests actually emit a module, instantiate it under XState v5, and check
the decimal data-ops and a full machine run - they skip cleanly when node / xstate are
not available.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from cobol_xstate.emitter import (
    _emit_guard,
    _emit_numeric_expr,
    edge_target,
    emit_setup_module,
    iter_transitions,
    retarget_on,
    segment_entry,
)
from cobol_xstate.parser import parse_program
from cobol_xstate.statechart import build_machine

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples"
RUNTIME = REPO / "src" / "cobol_xstate" / "runtime" / "cobolRuntime.mjs"


def _machine(name):
    src = (EXAMPLES / name).read_text()
    return build_machine(parse_program(src), source_name=name)


# --------------------------------------------------------------------------- #
# expression parser -> decimal-runtime JS
# --------------------------------------------------------------------------- #

def test_expr_add_literal():
    assert _emit_numeric_expr("WS-COUNT + 1") == 'add(D(context["WS-COUNT"]), D("1"))'


def test_expr_subtract_parenthesized():
    # the shape semantics.py produces for SUBTRACT 1 FROM WS-COUNT
    assert _emit_numeric_expr("WS-COUNT - (1)") == 'sub(D(context["WS-COUNT"]), D("1"))'


def test_expr_precedence_mul_over_add():
    js = _emit_numeric_expr("A + B * C")
    assert js == 'add(D(context["A"]), mul(D(context["B"]), D(context["C"])))'


def test_expr_parens_override_precedence():
    js = _emit_numeric_expr("(A + B) * C")
    assert js == 'mul(add(D(context["A"]), D(context["B"])), D(context["C"]))'


def test_expr_power_right_associative():
    js = _emit_numeric_expr("A ** B ** C")
    assert js == 'pow(D(context["A"]), pow(D(context["B"]), D(context["C"])))'


def test_expr_unary_minus():
    assert _emit_numeric_expr("- A") == 'sub(D("0"), D(context["A"]))'


def test_expr_figurative_zero():
    assert _emit_numeric_expr("WS-X + ZERO") == 'add(D(context["WS-X"]), D("0"))'


def test_expr_subscript_read_variable_index():
    assert _emit_numeric_expr("WS-SUM + TBL-AMT(WS-I)") == \
        'add(D(context["WS-SUM"]), D(elem(context["TBL-AMT"], context["WS-I"])))'


def test_expr_subscript_read_literal_index():
    assert _emit_numeric_expr("TBL(3)") == 'D(elem(context["TBL"], "3"))'


def test_expr_unparseable_raises():
    from cobol_xstate.emitter import _ExprError
    with pytest.raises(_ExprError):
        # an arithmetic subscript (space-separated tokens) is out of scope -> not modeled
        _emit_numeric_expr("TBL ( I )")


# --------------------------------------------------------------------------- #
# guard tree -> JS bool
# --------------------------------------------------------------------------- #

def test_guard_relational_alpha():
    tree = {"op": "rel", "left": "WS-TRAN-TYPE", "rel": "=", "right": "'D'"}
    fields = {"WS-TRAN-TYPE": {"category": "alphanumeric", "len": 1}}
    assert _emit_guard(tree, fields) == 'rel(context["WS-TRAN-TYPE"], "=", "D", false)'


def test_guard_relational_numeric():
    tree = {"op": "rel", "left": "WS-COUNT", "rel": ">", "right": "10"}
    fields = {"WS-COUNT": {"category": "numeric", "digits": 4, "scale": 0, "signed": False}}
    assert _emit_guard(tree, fields) == 'rel(context["WS-COUNT"], ">", "10", true)'


def test_guard_and_or_not():
    tree = {
        "op": "or",
        "args": [
            {"op": "rel", "left": "A", "rel": "=", "right": "1"},
            {"op": "not", "arg": {"op": "rel", "left": "B", "rel": "=", "right": "2"}},
        ],
    }
    fields = {"A": {"category": "numeric", "digits": 1, "scale": 0, "signed": False},
              "B": {"category": "numeric", "digits": 1, "scale": 0, "signed": False}}
    js = _emit_guard(tree, fields)
    assert js == '(rel(context["A"], "=", "1", true) || (!rel(context["B"], "=", "2", true)))'


def test_guard_condition_name_or_over_values():
    tree = {"op": "cond-name", "name": "END-OF-FILE", "parent": "WS-EOF", "values": ["'Y'"]}
    fields = {"WS-EOF": {"category": "alphanumeric", "len": 1}}
    assert _emit_guard(tree, fields) == '(rel(context["WS-EOF"], "=", "Y", false))'


def test_guard_raw_is_external():
    assert _emit_guard({"op": "raw", "text": "A = 1 OR 2"}, {}) is None


def test_guard_arithmetic_subscript_evaluates_index():
    # TBL(WWM-INDX - 1): the subscript is an arithmetic expression evaluated with the
    # decimal runtime; elem() coerces the resulting Dec to a 1-based index.
    tree = {"op": "rel", "left": "W-SUB1", "rel": ">", "right": "WWM-PTR(WWM-INDX - 1)"}
    fields = {"W-SUB1": {"category": "numeric", "digits": 4, "scale": 0, "signed": False},
              "WWM-PTR": {"category": "numeric", "digits": 4, "scale": 0, "signed": False,
                          "occurs": 10},
              "WWM-INDX": {"category": "numeric", "digits": 4, "scale": 0, "signed": False}}
    js = _emit_guard(tree, fields)
    assert 'elem(context["WWM-PTR"], sub(D(context["WWM-INDX"]), D("1")))' in js
    assert js.startswith("rel(context[\"W-SUB1\"]")


def test_guard_multidim_subscript_is_external():
    # A multi-dimension subscript is not modeled in the runnable JS -> external guard,
    # never a silently-undefined context["TBL(I,J)"] reference.
    tree = {"op": "rel", "left": "TBL(I,J)", "rel": "=", "right": "5"}
    fields = {"TBL": {"category": "numeric", "digits": 2, "scale": 0, "signed": False}}
    assert _emit_guard(tree, fields) is None


# --------------------------------------------------------------------------- #
# full module structure
# --------------------------------------------------------------------------- #

def test_module_has_setup_and_createmachine():
    mod = emit_setup_module(_machine("banktran.cbl"))
    assert "import { setup, assign } from 'xstate';" in mod
    assert "setup({ actions, guards, actors }).createMachine(machineConfig)" in mod
    assert "export const ops" in mod and "export const guardFns" in mod
    # ADD becomes a decimal store into the receiver's type (sequential op body so
    # later assignments in one statement see earlier stored results)
    assert ('out["WS-COUNT"] = context["WS-COUNT"] = '
            'store(add(D(context["WS-COUNT"]), D("1")), FIELDS["WS-COUNT"]);') in mod


def test_module_routes_io_handler_guard_to_external():
    mod = emit_setup_module(_machine("banktran.cbl"))
    assert '"TRAN-FILE_atEnd"' in mod
    # it must appear in externalGuards, never as an invented guardFn
    assert "externalGuards" in mod
    assert '"TRAN-FILE_atEnd": (context)' not in mod


def test_module_strips_provenance_meta_from_config():
    mod = emit_setup_module(_machine("custrpt.cbl"))
    # 'meta' (cobolLine/kind/note) is provenance; the runnable config must not carry it
    assert '"cobolLine"' not in mod


# --------------------------------------------------------------------------- #
# PERFORM -> invoke (call-return via actors)
# --------------------------------------------------------------------------- #

def test_segment_entry_isolate_puts_each_boundary_alone():
    """isolate=True (emitter invoke nodes / lineage call nodes): a boundary is its own
    segment, with the ops either side kept separate."""
    bnd = {"P"}
    assert segment_entry(["A", "P", "B"], lambda a: a in bnd, isolate=True) \
        == [["A"], ["P"], ["B"]]
    # back-to-back boundaries leave no empty segment; leading/trailing boundaries likewise
    assert segment_entry(["P", "P", "B"], lambda a: a in bnd, isolate=True) \
        == [["P"], ["P"], ["B"]]
    assert segment_entry(["A", "P"], lambda a: a in bnd, isolate=True) \
        == [["A"], ["P"]]


def test_segment_entry_terminate_keeps_boundary_at_segment_tail():
    """isolate=False (reactive per-get states): a boundary ends its segment, so the setup
    actions preceding it stay on the same segment; a trailing ops run is its own segment."""
    bnd = {"G"}
    assert segment_entry(["A", "G", "B", "G", "C"], lambda a: a in bnd, isolate=False) \
        == [["A", "G"], ["B", "G"], ["C"]]
    # no boundary at all: one segment (the whole run) in either mode
    assert segment_entry(["A", "B"], lambda a: a in bnd, isolate=False) == [["A", "B"]]
    assert segment_entry(["A", "B"], lambda a: a in bnd, isolate=True) == [["A", "B"]]


def test_edge_target_reads_both_handler_forms():
    assert edge_target({"target": "S"}) == "S"
    assert edge_target("S") == "S"                  # bare-string handler form
    assert edge_target({"actions": ["a"]}) is None  # action-only handler, no target
    assert edge_target(None) is None


def test_iter_transitions_covers_always_invoke_and_bare_handlers():
    st = {
        "always": [{"target": "A", "guard": "g"}, {"target": "B"}],
        "invoke": {"src": "actor:P", "onDone": {"target": "C"}},
        "on": {"E1": "D", "E2": {"target": "F"}, "E3": [{"target": "G"}, "H"]},
    }
    # default: always + on (bare string, dict, and a list mixing both)
    assert [(ev, edge_target(e)) for ev, e in iter_transitions(st)] == [
        (None, "A"), (None, "B"), ("E1", "D"), ("E2", "F"), ("E3", "G"), ("E3", "H")]
    # invoke=True: the onDone edge appears between the always edges and the handlers
    assert [(ev, edge_target(e)) for ev, e in iter_transitions(st, invoke=True)] == [
        (None, "A"), (None, "B"), (None, "C"),
        ("E1", "D"), ("E2", "F"), ("E3", "G"), ("E3", "H")]


def test_retarget_on_rewrites_and_promotes_bare_string_targets():
    on = {"E1": "x", "E2": {"target": "y", "actions": ["a"]},
          "E3": ["z", {"actions": ["b"]}]}
    retarget_on(on, lambda t: "P__" + t)
    assert on["E1"] == {"target": "P__x"}                     # bare promoted to dict
    assert on["E2"] == {"target": "P__y", "actions": ["a"]}   # dict keeps its other keys
    assert on["E3"] == [{"target": "P__z"}, {"actions": ["b"]}]  # no-target left as is


def test_perform_becomes_invoke_of_actor():
    mod = emit_setup_module(_machine("accum.cbl"))
    assert "export const actorConfigs" in mod
    assert '"src": "actor:1000-STEP"' in mod   # PERFORM 1000-STEP -> invoke its actor
    assert '"onDone"' in mod                    # ...and returns to the caller
    assert "perform_" not in mod                # no no-op PERFORM action survives


def test_nested_perform_builds_nested_actors():
    mod = emit_setup_module(_machine("nestperf.cbl"))
    assert '"actor:1000-OUTER"' in mod and '"actor:2000-INNER"' in mod
    assert '"__RET__"' in mod                   # each actor returns via a final state


def test_perform_thru_builds_a_range_actor():
    mod = emit_setup_module(_machine("thrurange.cbl"))
    assert '"src": "actor:1000-A__THRU__3000-C"' in mod   # PERFORM 1000-A THRU 3000-C
    assert '"actor:1000-A__THRU__3000-C"' in mod          # ...one actor spanning the range
    assert '"initial": "1000-A"' in mod                   # entered at the head paragraph


# --------------------------------------------------------------------------- #
# OCCURS subscript addressing
# --------------------------------------------------------------------------- #

def test_occurs_field_carries_count_and_writes_use_setelem():
    mod = emit_setup_module(_machine("tblsum.cbl"))
    assert '"occurs": 5' in mod                              # FIELDS records the table size
    assert ('out["TBL-AMT"] = context["TBL-AMT"] = setElem(context["TBL-AMT"], "1", '
            'store(D("10"), FIELDS["TBL-AMT"]));') in mod      # MOVE 10 TO TBL-AMT(1)
    assert 'D(elem(context["TBL-AMT"], context["WS-I"]))' in mod  # ADD TBL-AMT(WS-I) ...


# --------------------------------------------------------------------------- #
# SORT / MERGE INPUT/OUTPUT PROCEDURE
# --------------------------------------------------------------------------- #

def test_sort_procedures_become_call_return_invokes():
    mod = emit_setup_module(_machine("sorter.cbl"))
    # INPUT PROCEDURE -> invoke, then the sort effect, then OUTPUT PROCEDURE -> invoke
    assert '"src": "actor:1000-FILL"' in mod
    assert '"src": "actor:2000-EMIT"' in mod
    assert '"sort_SORT-FILE"' in mod
    # the sort itself is a no-op effect, not a data op
    assert '"sort_SORT-FILE": (context)' not in mod


def test_sort_is_flagged_opaque():
    machine = _machine("sorter.cbl")
    assert any("is an opaque effect" in f["message"] for f in machine.flags)


# --------------------------------------------------------------------------- #
# DECLARATIVES / CICS HANDLE -> orthogonal parallel handler region
# --------------------------------------------------------------------------- #

def test_declaratives_become_parallel_handler_region():
    mod = emit_setup_module(_machine("fileerr.cbl"))
    assert '"type": "parallel"' in mod
    assert '"PROGRAM"' in mod and '"HANDLERS"' in mod
    assert '"IO.ERROR.CUST-FILE"' in mod                 # the watcher's trigger event
    assert '"src": "actor:IO-ERR-HANDLER"' in mod        # the USE procedure as an actor


def test_cics_handle_becomes_parallel_handler_region():
    mod = emit_setup_module(_machine("cicsinq.cbl"))
    assert '"type": "parallel"' in mod
    assert '"CICS.NOTFND"' in mod                         # HANDLE CONDITION NOTFND
    assert '"src": "actor:8000-NOTFOUND"' in mod          # ...dispatches to the target


# --------------------------------------------------------------------------- #
# Node integration (skipped when node / xstate are unavailable)
# --------------------------------------------------------------------------- #

NODE = shutil.which("node")
HAS_XSTATE = (REPO / "node_modules" / "xstate" / "package.json").exists()


@pytest.fixture
def repo_tmp():
    """A temp dir *inside* the repo so Node's upward node_modules lookup finds xstate
    (ESM bare specifiers don't honor NODE_PATH)."""
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="emit_", dir=str(REPO)))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _emit_to(tmp_dir, name):
    machine = _machine(name)
    mod_path = tmp_dir / "machine.mjs"
    mod_path.write_text(emit_setup_module(machine))
    (tmp_dir / "cobolRuntime.mjs").write_text(RUNTIME.read_text())
    return mod_path


@pytest.mark.skipif(not NODE, reason="node not available")
def test_emitted_module_passes_node_syntax_check(tmp_path):
    mod_path = _emit_to(tmp_path, "banktran.cbl")
    r = subprocess.run([NODE, "--check", str(mod_path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(not (NODE and HAS_XSTATE), reason="node+xstate not available")
def test_emitted_machine_runs_and_computes_decimal(repo_tmp):
    tmp_path = repo_tmp
    mod_path = _emit_to(tmp_path, "banktran.cbl")
    driver = tmp_path / "drive.mjs"
    driver.write_text(
        "import { createActor } from 'xstate';\n"
        "import machine, { ops, guardFns } from './machine.mjs';\n"
        "const A = (c, w) => { if (JSON.stringify(c) !== JSON.stringify(w)) "
        "{ console.error('got', JSON.stringify(c), 'want', JSON.stringify(w)); "
        "process.exit(1);} };\n"
        "A(ops['ADD_1_TO_WS-COUNT']({'WS-COUNT':'0'}), {'WS-COUNT':'1'});\n"
        "A(ops['SUBTRACT_1_FROM_WS-COUNT']({'WS-COUNT':'5'}), {'WS-COUNT':'4'});\n"
        "A(guardFns['WS-TRAN-TYPE_eq_D']({'WS-TRAN-TYPE':'D'}), true);\n"
        "A(guardFns['WS-TRAN-TYPE_eq_D']({'WS-TRAN-TYPE':'W'}), false);\n"
        "const driven = machine.provide({ guards: { 'UNTIL_WS-EOF_eq_Y': () => true } });\n"
        "const actor = createActor(driven); actor.start();\n"
        "if (actor.getSnapshot().status !== 'done') { console.error('not done'); process.exit(1); }\n"
        "process.exit(0);\n"
    )
    r = subprocess.run([NODE, str(driver)], capture_output=True, text=True,
                       cwd=str(tmp_path), timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(not (NODE and HAS_XSTATE), reason="node+xstate not available")
def test_emitted_money_accumulation_is_exact_decimal(repo_tmp):
    tmp_path = repo_tmp
    mod_path = _emit_to(tmp_path, "custrpt.cbl")
    driver = tmp_path / "money.mjs"
    driver.write_text(
        "import { ops } from './machine.mjs';\n"
        "let ctx = { 'WS-TOTAL': '0.00' };\n"
        "for (const amt of ['0.10','0.20','100.55','12.34','0.01'])\n"
        "  ctx = { ...ctx, ...ops['ADD_CUST-AMT_TO_WS-TOTAL']("
        "{ 'WS-TOTAL': ctx['WS-TOTAL'], 'CUST-AMT': amt }) };\n"
        "if (ctx['WS-TOTAL'] !== '113.20') { console.error(ctx['WS-TOTAL']); process.exit(1); }\n"
        "process.exit(0);\n"
    )
    r = subprocess.run([NODE, str(driver)], capture_output=True, text=True,
                       cwd=str(tmp_path), timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(not (NODE and HAS_XSTATE), reason="node+xstate not available")
def test_a_tiny_fractional_value_seeds_exactly_under_xstate(repo_tmp):
    """A `PIC V9(8) VALUE 0.00000001` must reach the running machine as that exact value.
    Through the old float path it seeded 1e-08 and emitted "0.000000" - eight orders of
    magnitude of error in the seed, in a tool whose whole premise is fixed-point decimal."""
    tmp_path = repo_tmp
    m = build_machine(parse_program(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. TINY.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01 WS-RATE PIC V9(8) VALUE 0.00000001.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           MOVE WS-RATE TO WS-RATE\n"
        "           STOP RUN.\n"), source_name="tiny")
    (tmp_path / "machine.mjs").write_text(emit_setup_module(m))
    (tmp_path / "cobolRuntime.mjs").write_text(RUNTIME.read_text())
    driver = tmp_path / "tiny.mjs"
    driver.write_text(
        "import { createActor } from 'xstate';\n"
        "import machine from './machine.mjs';\n"
        "const a = createActor(machine); a.start();\n"
        "const v = String(a.getSnapshot().context['WS-RATE']);\n"
        "if (v !== '0.00000001') { console.error('WS-RATE', v); process.exit(1); }\n"
        "process.exit(0);\n"
    )
    r = subprocess.run([NODE, str(driver)], capture_output=True, text=True,
                       cwd=str(tmp_path), timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr


def _run_to_done(tmp_path, name, expect):
    """Emit `name`, run it under *stock* createActor (no reference driver), and assert the
    final context equals `expect`. This is the end-to-end proof of PERFORM call-return."""
    _emit_to(tmp_path, name)
    driver = tmp_path / "run.mjs"
    driver.write_text(
        "import { createActor } from 'xstate';\n"
        "import machine from './machine.mjs';\n"
        "const a = createActor(machine); a.start();\n"
        "const s = a.getSnapshot();\n"
        "if (s.status !== 'done') { console.error('status', s.status); process.exit(1); }\n"
        f"const want = {json.dumps(expect)};\n"
        "for (const k in want) if (String(s.context[k]) !== want[k]) "
        "{ console.error(k, 'got', s.context[k], 'want', want[k]); process.exit(1); }\n"
        "process.exit(0);\n"
    )
    r = subprocess.run([NODE, str(driver)], capture_output=True, text=True,
                       cwd=str(tmp_path), timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(not (NODE and HAS_XSTATE), reason="node+xstate not available")
def test_perform_until_call_return_runs_under_stock_xstate(repo_tmp):
    # PERFORM 1000-STEP UNTIL WS-I = 5, each call invoking the actor and threading context.
    _run_to_done(repo_tmp, "accum.cbl", {"WS-I": "5", "WS-SUM": "15"})


@pytest.mark.skipif(not (NODE and HAS_XSTATE), reason="node+xstate not available")
def test_nested_perform_threads_context_under_stock_xstate(repo_tmp):
    # 1000-OUTER performs 2000-INNER: context must thread back up two call levels.
    _run_to_done(repo_tmp, "nestperf.cbl", {"WS-SUM": "11"})


@pytest.mark.skipif(not (NODE and HAS_XSTATE), reason="node+xstate not available")
def test_occurs_table_sum_runs_under_stock_xstate(repo_tmp):
    # Write five elements by literal subscript, then sum with a variable subscript.
    _run_to_done(repo_tmp, "tblsum.cbl", {"WS-SUM": "150"})


@pytest.mark.skipif(not (NODE and HAS_XSTATE), reason="node+xstate not available")
def test_perform_varying_steps_the_control_variable(repo_tmp):
    # PERFORM 1000-STEP VARYING WS-I FROM 1 BY 1 UNTIL WS-I > 5: the index must be
    # initialized and stepped each iteration, summing 1..5 = 15 and leaving WS-I = 6.
    _run_to_done(repo_tmp, "varysum.cbl", {"WS-SUM": "15", "WS-I": "6"})


@pytest.mark.skipif(not (NODE and HAS_XSTATE), reason="node+xstate not available")
def test_sort_runs_input_then_output_under_stock_xstate(repo_tmp):
    # INPUT PROCEDURE 1000-FILL (WS-IN=5) runs, then OUTPUT PROCEDURE 2000-EMIT (WS-OUT=7).
    _run_to_done(repo_tmp, "sorter.cbl", {"WS-IN": "5", "WS-OUT": "7"})


@pytest.mark.skipif(not (NODE and HAS_XSTATE), reason="node+xstate not available")
def test_perform_thru_range_runs_all_paragraphs(repo_tmp):
    # PERFORM 1000-A THRU 3000-C runs A, B, C in order then returns: 100 + 20 + 3 = 123.
    _run_to_done(repo_tmp, "thrurange.cbl", {"WS-N": "123"})


@pytest.mark.skipif(not (NODE and HAS_XSTATE), reason="node+xstate not available")
def test_alter_switch_actually_flips_at_runtime(repo_tmp):
    # ALTSWITCH: PERFORM 2000-CYCLE 3 TIMES; each cycle performs 1000-SWITCH whose
    # ALTERed exit starts at 1100-FIRST (which flips the switch to 1200-NORMAL).
    # With real guards over the synthetic ALT- field, the machine runs to done and
    # the switch holds the flipped target (previously an all-guarded dead end).
    _run_to_done(repo_tmp, "altswitch.cbl",
                 {"ALT-1000-SWITCH": "1200-NORMAL", "TIMES-CTR-1": "3"})


@pytest.mark.skipif(not (NODE and HAS_XSTATE), reason="node+xstate not available")
def test_goto_depending_selects_by_index(repo_tmp):
    # GO TO A B C DEPENDING ON WS-BRANCH with WS-BRANCH = 2 must take branch B.
    _run_to_done(repo_tmp, "depending.cbl", {"WS-R": "B"})


@pytest.mark.skipif(not (NODE and HAS_XSTATE), reason="node+xstate not available")
def test_divide_remainder_computes_both_receivers(repo_tmp):
    # DIVIDE 7 BY 2 GIVING WS-Q REMAINDER WS-R: quotient truncates to 3, remainder 1.
    _run_to_done(repo_tmp, "divrem.cbl", {"WS-Q": "3", "WS-R": "1"})


@pytest.mark.skipif(not (NODE and HAS_XSTATE), reason="node+xstate not available")
def test_times_exit_perform_exit_paragraph_stacked_whens(repo_tmp):
    # PERFORM 3 TIMES steps a modeled synthetic counter (WS-T = 6, not an infinite
    # loop); EXIT PARAGRAPH skips the +100; EXIT PERFORM breaks the loop at WS-I = 4;
    # stacked WHEN 1 WHEN 2 fall into the shared body (WS-R = 'A' for WS-X = 1).
    _run_to_done(repo_tmp, "timesexit.cbl", {"WS-T": "6", "WS-I": "4", "WS-R": "A"})


@pytest.mark.skipif(not (NODE and HAS_XSTATE), reason="node+xstate not available")
def test_perform_section_runs_all_member_paragraphs(repo_tmp):
    # PERFORM 1000-CALC where 1000-CALC is a SECTION must run the whole section extent
    # (1010-STEP1 adds 5, 1020-STEP2 adds 7), not just the header pseudo-paragraph;
    # then PERFORM 2000-POST copies the result.
    _run_to_done(repo_tmp, "sectperf.cbl", {"WS-A": "12", "WS-B": "12"})


@pytest.mark.skipif(not (NODE and HAS_XSTATE), reason="node+xstate not available")
def test_declarative_handler_fires_on_its_event(repo_tmp):
    # The USE procedure is orthogonal: it runs only when its error event is sent, and its
    # effect threads back into the shared context.
    _emit_to(repo_tmp, "fileerr.cbl")
    driver = repo_tmp / "run.mjs"
    driver.write_text(
        "import { createActor } from 'xstate';\n"
        "import machine from './machine.mjs';\n"
        "const a = createActor(machine); a.start();\n"
        "if (a.getSnapshot().context['WS-ERR-COUNT'] !== '0') process.exit(1);\n"
        "a.send({ type: 'IO.ERROR.CUST-FILE' });\n"        # simulate the I/O error
        "if (a.getSnapshot().context['WS-ERR-COUNT'] !== '1') "
        "{ console.error(a.getSnapshot().context); process.exit(1); }\n"
        "process.exit(0);\n"
    )
    r = subprocess.run([NODE, str(driver)], capture_output=True, text=True,
                       cwd=str(repo_tmp), timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr


# --------------------------------------------------------------------------- #
# PERFORM target resolution through the registry's _N suffix (review finding J6)
# --------------------------------------------------------------------------- #

def test_perform_of_one_paragraph_two_ways_invokes_the_real_actor():
    """`PERFORM 1000-INIT` and `PERFORM 1000-INIT 3 TIMES` are one paragraph but two
    statements, so the registry names the second `perform_1000-INIT_2`. Slicing off the
    prefix yielded target `1000-INIT_2`, which owns no paragraph - the PERFORM became a
    silent no-op. Both must invoke actor:1000-INIT, and no perform_ marker may survive."""
    mod = emit_setup_module(_machine("perftwice.cbl"))
    assert '"src": "actor:1000-INIT"' in mod
    assert "1000-INIT_2" not in mod            # the phantom target is gone
    assert "perform_" not in mod               # neither PERFORM is a leftover no-op


def test_perform_target_helper_strips_only_a_registry_suffix():
    from cobol_xstate.emitter import perform_target
    ordered = ["0000-MAIN", "1000-INIT"]
    # the real target resolves as-is
    assert perform_target("perform_1000-INIT", ordered) == "1000-INIT"
    # a _2 suffix that does NOT name a paragraph falls back to the base that does
    assert perform_target("perform_1000-INIT_2", ordered) == "1000-INIT"
    # not a perform action at all
    assert perform_target("MOVE_1_TO_WS-A", ordered) is None


@pytest.mark.skipif(not (NODE and HAS_XSTATE), reason="node+xstate not available")
def test_two_identical_times_loops_each_run_to_completion(repo_tmp):
    # Both `PERFORM 5 TIMES` loops must count independently: WS-A = WS-B = 5. With a
    # shared exit guard the second loop tested the first's spent counter and ran 0 times.
    _run_to_done(repo_tmp, "twotimes.cbl", {"WS-A": "5", "WS-B": "5"})


@pytest.mark.skipif(not (NODE and HAS_XSTATE), reason="node+xstate not available")
def test_perform_of_one_paragraph_two_ways_runs_both(repo_tmp):
    # 1000-INIT performed once then 3 times: it must run 4 times total, WS-A = 4. The
    # _2-suffixed PERFORM used to resolve to nothing and be dropped, leaving WS-A = 1.
    _run_to_done(repo_tmp, "perftwice.cbl", {"WS-A": "4"})
