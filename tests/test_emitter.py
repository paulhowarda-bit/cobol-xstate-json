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
    emit_setup_module,
)
from cobol_xstate.parser import parse_program
from cobol_xstate.statechart import build_machine

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples"
RUNTIME = REPO / "runtime" / "cobolRuntime.mjs"


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


# --------------------------------------------------------------------------- #
# full module structure
# --------------------------------------------------------------------------- #

def test_module_has_setup_and_createmachine():
    mod = emit_setup_module(_machine("banktran.cbl"))
    assert "import { setup, assign } from 'xstate';" in mod
    assert "setup({ actions, guards, actors }).createMachine(machineConfig)" in mod
    assert "export const ops" in mod and "export const guardFns" in mod
    # ADD becomes a decimal store into the receiver's type
    assert ('"ADD_1_TO_WS-COUNT": (context) => ({ "WS-COUNT": '
            'store(add(D(context["WS-COUNT"]), D("1")), FIELDS["WS-COUNT"]) })') in mod


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


# --------------------------------------------------------------------------- #
# OCCURS subscript addressing
# --------------------------------------------------------------------------- #

def test_occurs_field_carries_count_and_writes_use_setelem():
    mod = emit_setup_module(_machine("tblsum.cbl"))
    assert '"occurs": 5' in mod                              # FIELDS records the table size
    assert ('"TBL-AMT": setElem(context["TBL-AMT"], "1", '
            'store(D("10"), FIELDS["TBL-AMT"]))') in mod      # MOVE 10 TO TBL-AMT(1)
    assert 'D(elem(context["TBL-AMT"], context["WS-I"]))' in mod  # ADD TBL-AMT(WS-I) ...


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
