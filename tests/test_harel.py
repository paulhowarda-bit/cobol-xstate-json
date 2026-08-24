"""Stage 7: the emitted contract is a Harel-derived statechart (a real statechart, in
XState v5 - which is a restricted subset of Harel, so negated events / activities /
static reactions stay encoded rather than expressed).

The compiler's IR is a flat FSM with mangled names, PERFORM as a marker action, and
source-order fall-through between paragraphs. That is convenient to analyse but it is not
a statechart, and its fall-through edges describe a path the program never takes. These
tests pin the artifact: hierarchy is real nesting, PERFORM is a real call/return, and the
never-executed chain is gone.
"""

from pathlib import Path

import pytest

from cobol_xstate.parser import parse_program
from cobol_xstate.statechart import build_machine

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _bundle(name: str) -> dict:
    src = (EXAMPLES / name).read_text()
    return build_machine(parse_program(src), source_name=name).bundle()


def _walk(states: dict):
    """Yield (key, node) for every state at every depth."""
    for k, s in states.items():
        yield k, s
        if "states" in s:
            yield from _walk(s["states"])


def _ids(bundle: dict) -> set:
    out = set()
    for _k, s in _walk(bundle["machine"]["states"]):
        if "id" in s:
            out.add(s["id"])
    for c in bundle["charts"].values():
        for _k, s in _walk(c["states"]):
            if "id" in s:
                out.add(s["id"])
    return out


def _targets(states: dict):
    for _k, s in _walk(states):
        for t in s.get("always", []) or []:
            if t.get("target"):
                yield t["target"]
        inv = s.get("invoke")
        if inv and inv.get("onDone", {}).get("target"):
            yield inv["onDone"]["target"]


# --------------------------------------------------------------------------- #
# hierarchy
# --------------------------------------------------------------------------- #

def test_paragraphs_are_compound_states_not_mangled_names():
    b = _bundle("banktran.cbl")
    top = b["machine"]["states"]
    # 0000-MAIN's structural states nest INSIDE it rather than sitting beside it
    assert "states" in top["0000-MAIN"], "the paragraph must be a compound OR-state"
    assert top["0000-MAIN"]["meta"]["kind"] == "paragraph"
    assert "0000-MAIN__loop3" not in top, "mangled sibling leaked to the top level"
    children = top["0000-MAIN"]["states"]
    assert "loop3" in children and "_entry" in children
    # entering the paragraph lands on its own entry state
    assert top["0000-MAIN"]["initial"] == "_entry"


def test_every_leaf_keeps_its_cobol_name_as_its_id():
    b = _bundle("banktran.cbl")
    ids = _ids(b)
    assert "0000-MAIN" in ids            # the paragraph's entry
    assert "0000-MAIN__loop3" in ids     # a structural state keeps its flat address
    # and provenance still resolves for those names
    for name in ("0000-MAIN", "0000-MAIN__loop3"):
        assert name in b["provenance"]


# --------------------------------------------------------------------------- #
# PERFORM is a real call/return
# --------------------------------------------------------------------------- #

def test_perform_is_resolved_to_invoke_and_return():
    b = _bundle("banktran.cbl")
    invokes = [s for _k, s in _walk(b["machine"]["states"]) if "invoke" in s]
    assert invokes, "PERFORM must become a real call"
    for s in invokes:
        assert s["invoke"]["src"].startswith("actor:")
        assert s["invoke"]["onDone"]["target"]        # ...with a return


def test_no_perform_marker_survives_in_the_contract():
    b = _bundle("banktran.cbl")
    for _k, s in _walk(b["machine"]["states"]):
        for a in s.get("entry", []) or []:
            assert not a.startswith("perform_"), f"unresolved marker {a}"


def test_each_performed_paragraph_has_its_own_chart():
    b = _bundle("banktran.cbl")
    assert set(b["charts"]) >= {"actor:1000-OPEN", "actor:2000-DISPATCH",
                                "actor:2100-DEPOSIT", "actor:9000-CLOSE"}
    for chart in b["charts"].values():
        assert chart["initial"] and chart["states"]


def test_perform_section_chart_covers_the_whole_section():
    # the section's member paragraphs must be inside the callee's chart
    b = _bundle("sectperf.cbl")
    chart = b["charts"]["actor:1000-CALC"]
    ids = {s["id"] for _k, s in _walk(chart["states"]) if "id" in s}
    assert {"1010-STEP1", "1020-STEP2"} <= ids


# --------------------------------------------------------------------------- #
# the lie is gone
# --------------------------------------------------------------------------- #

def test_never_executed_fall_through_is_pruned():
    """2100-DEPOSIT is only ever entered via PERFORM, so control returns to the
    dispatcher. The IR chained it to the next paragraph in the file - a path the program
    never takes. The contract must not claim it."""
    b = _bundle("banktran.cbl")
    top = b["machine"]["states"]
    for para in ("2100-DEPOSIT", "2200-WITHDRAW", "2300-INQUIRY", "2900-ERROR"):
        assert para not in top, f"{para} is a callee; it must not sit in the main flow"
    # and no edge anywhere in the main chart points at that phantom chain
    assert "#2200-WITHDRAW" not in set(_targets(top))


def test_callee_chart_returns_instead_of_falling_through():
    # inside 2100-DEPOSIT's chart, control ends at the return - not at 2200-WITHDRAW
    b = _bundle("banktran.cbl")
    chart = b["charts"]["actor:2100-DEPOSIT"]
    assert "__RET__" in chart["states"]
    tgts = set(_targets(chart["states"]))
    assert "#2200-WITHDRAW" not in tgts and "2200-WITHDRAW" not in tgts


# --------------------------------------------------------------------------- #
# well-formedness: a renderer must be able to draw it
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", ["banktran.cbl", "custrpt.cbl", "sectperf.cbl",
                                  "sqlunld.cbl", "accum.cbl", "lineage.cbl"])
def test_every_target_resolves_to_a_real_id(name):
    b = _bundle(name)
    ids = _ids(b)
    for chart in [b["machine"]] + list(b["charts"].values()):
        for t in _targets(chart["states"]):
            assert t.startswith("#"), f"{name}: target {t} is not an absolute id"
            assert t[1:] in ids, f"{name}: dangling target {t}"


@pytest.mark.parametrize("name", ["banktran.cbl", "custrpt.cbl", "cicsinq.cbl",
                                  "fileerr.cbl", "sorter.cbl", "timesexit.cbl"])
def test_bundle_is_well_formed_for_every_shape(name):
    b = _bundle(name)
    assert b["machine"]["states"]
    # a compound state must name an initial child that exists
    for _k, s in _walk(b["machine"]["states"]):
        if "states" in s:
            assert s["initial"] in s["states"], f"{name}: bad initial in {_k}"


def test_perimeter_tags_survive_the_restructuring():
    # build_interface annotates the IR's nodes; the Harel view is derived from it, so
    # the boundary must still be visible on the nested nodes.
    b = _bundle("custrpt.cbl")
    found = [s for _k, s in _walk(b["machine"]["states"])
             if s.get("meta", {}).get("perimeter")]
    found += [s for c in b["charts"].values() for _k, s in _walk(c["states"])
              if s.get("meta", {}).get("perimeter")]
    assert found, "perimeter tags were lost when the machine was restructured"
