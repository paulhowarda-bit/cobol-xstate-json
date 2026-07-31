"""Golden-master / equivalence harness (backlog item 2 — the "prove it" step).

Drives the *whole* emitted machine against recorded COBOL inputs via the reference
driver (runtime/cobolDriver.mjs) and diffs the final context, the DISPLAY output, and
the per-record-cycle trace against hand-computed golden values. Every data mutation
still flows through the emitted `ops` and every branch through the emitted guards — the
driver only supplies the PERFORM call-return and sequential file I/O that stock XState
cannot express (see README "Honest limitations"). So a match here is evidence the
recovered ops+guards+control-flow reproduce the COBOL program, exactly in decimal.

The canonical fixture is custrpt.cbl (sequential read loop accumulating a money total).
Pure-Python tests live in test_emitter.py; these need node + xstate and skip cleanly
otherwise. Records are supplied in field-canonical form (the driver does not re-quantize
the record area on READ).
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from cobol_xstate.emitter import emit_setup_module
from cobol_xstate.parser import parse_program
from cobol_xstate.statechart import build_machine

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples"
RUNTIME = REPO / "src" / "cobol_xstate" / "runtime" / "cobolRuntime.mjs"
DRIVER = REPO / "src" / "cobol_xstate" / "runtime" / "cobolDriver.mjs"

NODE = shutil.which("node")
HAS_XSTATE = (REPO / "node_modules" / "xstate" / "package.json").exists()
pytestmark = pytest.mark.skipif(not (NODE and HAS_XSTATE), reason="node+xstate not available")


@pytest.fixture
def repo_tmp():
    """A temp dir *inside* the repo so Node's upward node_modules lookup finds xstate
    (ESM bare specifiers don't honor NODE_PATH)."""
    d = Path(tempfile.mkdtemp(prefix="gm_", dir=str(REPO)))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _run(tmp_dir, example, files):
    """Emit `example`, drive it over `files`, return the driver's JSON result."""
    machine = build_machine(parse_program((EXAMPLES / example).read_text()),
                            source_name=example)
    (tmp_dir / "machine.mjs").write_text(emit_setup_module(machine))
    (tmp_dir / "cobolRuntime.mjs").write_text(RUNTIME.read_text())
    (tmp_dir / "cobolDriver.mjs").write_text(DRIVER.read_text())
    driver = tmp_dir / "run.mjs"
    driver.write_text(
        "import * as m from './machine.mjs';\n"
        "import { drive } from './cobolDriver.mjs';\n"
        f"const files = {json.dumps(files)};\n"
        "const r = drive(m, { files });\n"
        "process.stdout.write(JSON.stringify(r));\n"
    )
    proc = subprocess.run([NODE, str(driver)], capture_output=True, text=True,
                          cwd=str(tmp_dir), timeout=30)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


def _amts(values):
    return {"CUST-FILE": [{"CUST-AMT": v} for v in values]}


# --------------------------------------------------------------------------- #
# custrpt.cbl — sequential batch read loop accumulating WS-TOTAL
# --------------------------------------------------------------------------- #

def test_money_total_is_exact_decimal(repo_tmp):
    r = _run(repo_tmp, "custrpt.cbl", _amts(["0.10", "0.20", "100.55", "12.34", "0.01"]))
    assert r["context"]["WS-TOTAL"] == "113.20"   # exact, not 113.19999999...
    assert r["display"] == ["113.20"]             # DISPLAY WS-TOTAL in 3000-TERM


def test_per_cycle_trace_matches_golden(repo_tmp):
    # The accumulation lags one READ behind: rec N is ADDed, then rec N+1 is READ.
    r = _run(repo_tmp, "custrpt.cbl", _amts(["0.10", "0.20", "100.55", "12.34", "0.01"]))
    trace = [c["WS-TOTAL"] for c in r["cycles"]]
    assert trace == ["0", "0.10", "0.30", "100.85", "113.19", "113.20"]


def test_empty_file_displays_initial_zero(repo_tmp):
    # 1000-INIT's READ hits AT END immediately; PROCESS never runs.
    r = _run(repo_tmp, "custrpt.cbl", _amts([]))
    assert r["context"]["WS-TOTAL"] == "0"
    assert r["display"] == ["0"]


def test_single_record_quantizes_to_receiver_scale(repo_tmp):
    # 5 stored into 9(11)V99 → scale 2.
    r = _run(repo_tmp, "custrpt.cbl", _amts(["5"]))
    assert r["context"]["WS-TOTAL"] == "5.00"


def test_terminates_within_step_bound(repo_tmp):
    r = _run(repo_tmp, "custrpt.cbl", _amts(["1.00", "2.00", "3.00"]))
    assert r["context"]["WS-TOTAL"] == "6.00"
    assert r["steps"] < 1000   # the AT-END guard actually breaks the loop


# --------------------------------------------------------------------------- #
# notend.cbl — NOT AT END is the per-record path (negation of the AT-END guard)
# --------------------------------------------------------------------------- #

def test_not_at_end_body_runs_for_every_record(repo_tmp):
    files = {"IN-FILE": [{"IN-AMT": v} for v in ["1.50", "2.25", "3.00"]]}
    r = _run(repo_tmp, "notend.cbl", files)
    assert r["context"]["WS-CNT"] == "3"       # NOT AT END ran per record
    assert r["context"]["WS-SUM"] == "6.75"    # and its ADD is exact decimal
    assert r["context"]["WS-EOF"] == "Y"       # AT END still fired at exhaustion


def test_not_at_end_body_skipped_on_empty_file(repo_tmp):
    r = _run(repo_tmp, "notend.cbl", {"IN-FILE": []})
    assert r["context"]["WS-CNT"] == "0"
    assert r["context"]["WS-EOF"] == "Y"
