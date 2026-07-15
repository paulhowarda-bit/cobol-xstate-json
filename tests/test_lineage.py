"""Stage 6 projection: field lineage across the external boundary (--target lineage).

Every assertion here is hand-checkable against examples/lineage.cbl, which is written so
each row has one obviously-correct answer: the caller passes LK-CUST/LK-QTY, the program
ACCEPTs a rate, CALLs SUBFEE BY REFERENCE, STRINGs two fields, and writes a file.
"""

from pathlib import Path

import pytest

from cobol_xstate.lineage import build_lineage
from cobol_xstate.parser import parse_program
from cobol_xstate.statechart import build_machine

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _lin(name: str) -> dict:
    src = (EXAMPLES / name).read_text()
    return build_lineage(build_machine(parse_program(src), source_name=name))


def _row(d: dict, field: str, direction: str = "output") -> dict:
    rows = [r for r in d["rows"] if r["field"] == field and r["direction"] == direction]
    assert rows, f"no {direction} row for {field}"
    return rows[0]


def _origins(row: dict) -> set:
    return {o["event"] for o in row["origins"]}


# --------------------------------------------------------------------------- #
# shape
# --------------------------------------------------------------------------- #

def test_lineage_shape():
    d = _lin("lineage.cbl")
    assert d["format"] == "cobol-xstate-lineage"
    assert d["program"] == "LINEAGE"
    for r in d["rows"]:
        assert r["direction"] in ("input", "output")
        assert r["event"].startswith(("GET.", "CREATE."))
        assert "field" in r and "changedByProgram" in r and "origins" in r


# --------------------------------------------------------------------------- #
# the core question: which event is responsible for this field?
# --------------------------------------------------------------------------- #

def test_linkage_value_traced_to_the_caller_two_hops():
    # MOVE LK-CUST TO WS-NAME; MOVE WS-NAME TO OUT-NAME; WRITE.
    # OUT-NAME's value originates with the caller, two assignments back.
    r = _row(_lin("lineage.cbl"), "OUT-NAME")
    assert _origins(r) == {"GET.CALLER.CALLER"}
    assert r["changedByProgram"] is True          # the program does MOVE it


def test_computed_field_carries_every_contributing_origin():
    # COMPUTE OUT-FEE = LK-QTY * WS-RATE -> caller AND console.
    r = _row(_lin("lineage.cbl"), "OUT-FEE")
    assert _origins(r) == {"GET.CALLER.CALLER", "GET.CONSOLE.SYSIN"}


def test_input_event_field_is_not_a_program_change():
    # ACCEPT fills WS-RATE from outside; the program did not compute it.
    r = _row(_lin("lineage.cbl"), "WS-RATE", direction="input")
    assert _origins(r) == {"GET.CONSOLE.SYSIN"}
    assert r["changedByProgram"] is False


def test_call_by_reference_is_a_maybe_origin_naming_the_program():
    # CALL 'SUBFEE' USING WS-REF: the callee may rewrite it and we cannot see inside.
    r = _row(_lin("lineage.cbl"), "WS-REF")
    o = next(x for x in r["origins"] if x["event"] == "CREATE.PROGRAM.SUBFEE")
    assert o["maybe"] is True
    assert o["resolvedBy"] == "SUBFEE"            # names what would resolve it


def test_string_dependency_is_modeled_even_though_its_value_is_not():
    # STRING WS-NAME WS-REF INTO WS-MEMO; MOVE WS-MEMO TO OUT-MEMO.
    # The value semantics of STRING are not modeled, but the DEPENDENCY is - so the
    # chain survives and OUT-MEMO carries both contributors.
    r = _row(_lin("lineage.cbl"), "OUT-MEMO")
    assert "GET.CALLER.CALLER" in _origins(r)         # via WS-NAME <- LK-CUST
    assert "CREATE.PROGRAM.SUBFEE" in _origins(r)     # via WS-REF <- maybe SUBFEE


def test_group_unions_its_children():
    d = _lin("lineage.cbl")
    rec = _origins(_row(d, "OUT-REC"))
    kids = set()
    for f in ("OUT-NAME", "OUT-FEE", "OUT-MEMO"):
        kids |= _origins(_row(d, f))
    assert rec == kids


# --------------------------------------------------------------------------- #
# flow: loops, PERFORM call/return
# --------------------------------------------------------------------------- #

def test_accumulator_in_a_loop_resolves_to_the_file_not_itself():
    # custrpt: ADD CUST-AMT TO WS-TOTAL inside a READ loop, then DISPLAY WS-TOTAL.
    # WS-TOTAL depends on itself across iterations; the self-reference must collapse
    # and leave the file READ as the origin.
    r = _row(_lin("custrpt.cbl"), "WS-TOTAL")
    assert _origins(r) == {"GET.FILE.CUST-FILE"}
    assert r["changedByProgram"] is True


def test_origin_crosses_a_perform_boundary():
    # lineage.cbl writes OUT-REC in 0000-MAIN, but its fields are set inside the
    # PERFORMed 1000-BUILD. The call must be followed for the origins to reach the WRITE.
    assert _origins(_row(_lin("lineage.cbl"), "OUT-NAME")) == {"GET.CALLER.CALLER"}


def test_unload_traces_db2_row_to_the_written_record():
    # sqlunld: FETCH INTO :WS-ID -> MOVE WS-ID TO OUT-ID -> WRITE OUT-REC.
    d = _lin("sqlunld.cbl")
    assert _origins(_row(d, "OUT-ID")) == {"GET.DB2.ACCOUNT"}
    assert _origins(_row(d, "OUT-BAL")) == {"GET.DB2.ACCOUNT"}


def test_every_fixture_produces_lineage_without_crashing():
    for f in sorted(EXAMPLES.glob("*.cbl")):
        d = build_lineage(build_machine(parse_program(f.read_text()), source_name=f.name))
        assert d["format"] == "cobol-xstate-lineage"
        assert isinstance(d["rows"], list)


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #

def test_cli_lineage_target_writes_its_own_file(tmp_path):
    import json
    from cobol_xstate.cli import run
    rc = run([str(EXAMPLES / "lineage.cbl"), "--target", "lineage",
              "--outdir", str(tmp_path)])
    assert rc == 0
    out = tmp_path / "lineage.lineage.json"      # peer artifact, not the bundle
    assert out.exists()
    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["format"] == "cobol-xstate-lineage"
