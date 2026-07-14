"""Stage 6b business-view distillation: classify states, collapse technical scaffolding.

Pure-Python tests over the projection - no node needed. They pin the classification
(boundary / decision / technical / terminal), the collapse (technical states removed and
their edges contracted, loop-back + quit semantics preserved), the business-vs-control guard
labelling, internal-action stripping, and the honest PERFORM flag.
"""

from cobol_xstate.business import build_business_view, _is_control_guard
from cobol_xstate.parser import parse_program
from cobol_xstate.statechart import build_machine
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _view(name):
    src = (EXAMPLES / name).read_text()
    return build_business_view(build_machine(parse_program(src), source_name=name))


def _tx(view, frm, to):
    return [t for t in view["transitions"] if t["from"] == frm and t["to"] == to]


# --------------------------------------------------------------------------- #
# guard classification
# --------------------------------------------------------------------------- #

def test_loop_and_atend_guards_are_control():
    assert _is_control_guard("UNTIL_WS-EOF_eq_Y", None)
    assert _is_control_guard("TRAN-FILE_atEnd", None)
    assert _is_control_guard("X", {"op": "raw", "text": "?"})


def test_business_relational_guard_is_not_control():
    assert not _is_control_guard("WS-TRAN-TYPE_eq_D",
                                 {"op": "rel", "left": "WS-TRAN-TYPE", "rel": "=", "right": "'D'"})


# --------------------------------------------------------------------------- #
# txnflat: flat loop + dispatch
# --------------------------------------------------------------------------- #

def test_counts_collapse_loop_head_and_noop():
    v = _view("txnflat.cbl")
    assert v["counts"] == {"faithfulStates": 10, "businessStates": 8, "collapsed": 2}
    collapsed = {c["state"] for c in v["collapsed"]}
    # the loop head (UNTIL guard only) and the WHEN OTHER CONTINUE no-op are technical
    assert "0000-MAIN" in collapsed          # loop head
    assert "0000-MAIN__seq8" in collapsed     # CONTINUE no-op


def test_dispatch_state_is_a_business_decision():
    v = _view("txnflat.cbl")
    eval2 = v["businessStates"]["0000-MAIN__eval2"]
    assert eval2["role"] == "decision"
    assert {d["field"] for d in eval2["decisions"]} == {"WS-TRAN-TYPE"}


def test_accept_state_is_boundary_and_strips_internal_move():
    v = _view("txnflat.cbl")
    seq9 = v["businessStates"]["0000-MAIN__seq9"]
    assert seq9["role"] == "boundary"
    assert seq9["gets"] == ["GET.CONSOLE.SYSIN"]
    assert "MOVE_OK_TO_WS-STATUS" in seq9["internalSteps"]      # internal detail dropped
    assert all("MOVE" not in a["verb"] for a in seq9["boundaryActions"])


def test_collapsing_the_loop_head_preserves_loopback_and_quit():
    v = _view("txnflat.cbl")
    # a business outcome (deposit display) loops back to the ACCEPT state, and also exits
    # to the terminal under the loop's UNTIL guard - both through the collapsed loop head.
    loopback = _tx(v, "0000-MAIN__seq3", "0000-MAIN__seq9")
    quit_ = _tx(v, "0000-MAIN__seq3", "0000-MAIN__end1")
    assert loopback and "0000-MAIN" in loopback[0]["via"]
    assert quit_ and quit_[0]["guards"][0]["kind"] == "control"


def test_decision_edges_are_labelled_business_vs_control():
    v = _view("txnflat.cbl")
    d_edge = _tx(v, "0000-MAIN__eval2", "0000-MAIN__seq3")   # WHEN 'D'
    assert d_edge and d_edge[0]["guards"][0]["kind"] == "business"
    assert d_edge[0]["guards"][0]["field"] == "WS-TRAN-TYPE"


def test_names_are_left_as_fill_in():
    v = _view("txnflat.cbl")
    for name, d in v["businessStates"].items():
        assert d["suggestedName"] is None
    assert "0000-MAIN__eval2" in v["nameFillIn"]["states"]
    assert v["businessStates"]["0000-MAIN__end1"]["role"] == "terminal"
    # terminals do not need a business name
    assert "0000-MAIN__end1" not in v["nameFillIn"]["states"]


# --------------------------------------------------------------------------- #
# sqlsel: SELECT + SQLCODE response
# --------------------------------------------------------------------------- #

def test_response_branch_is_boundary_and_decision():
    v = _view("sqlsel.cbl")
    assert v["counts"]["businessStates"] == 3
    if2 = v["businessStates"]["0000-MAIN__if2"]
    assert if2["role"] == "boundary+decision"
    assert if2["gets"] == ["GET.RESPONSE.DB2"]
    assert any(d["field"] == "SQLCODE" for d in if2["decisions"])


# --------------------------------------------------------------------------- #
# banktran: PERFORM-aware collapse recovers the dispatch through call/return
# --------------------------------------------------------------------------- #

def _outs(v, frm):
    return {t["to"] for t in v["transitions"] if t["from"] == frm}


def test_perform_aware_collapse_follows_the_dispatch():
    v = _view("banktran.cbl")
    # the dispatcher's four out-of-line PERFORM branches become real business edges,
    # recovered by following the perform_ call states (not flagged-and-skipped).
    assert {"2100-DEPOSIT", "2200-WITHDRAW", "2300-INQUIRY", "2900-ERROR"} <= \
        _outs(v, "2000-DISPATCH")
    d = [t for t in v["transitions"]
         if t["from"] == "2000-DISPATCH" and t["to"] == "2100-DEPOSIT"][0]
    assert d["guards"][0]["field"] == "WS-TRAN-TYPE" and d["guards"][0]["kind"] == "business"
    assert any("2000-DISPATCH__seq9" in x for x in d["via"])   # via perform_2100-DEPOSIT


def test_perform_returns_loop_back_to_the_read():
    v = _view("banktran.cbl")
    # after posting the deposit, control returns to the driver loop and reads the next record
    assert "2000-DISPATCH__io7" in _outs(v, "2100-DEPOSIT")


def test_entry_reaches_the_first_read():
    v = _view("banktran.cbl")
    assert v["entry"][0]["to"] == "1000-OPEN__io5"


def test_goto_out_of_perform_is_flagged():
    # GO TO out of a performed paragraph is modeled as a return (as the runnable machine
    # does); surface it so the one mis-routable edge is not silently trusted.
    v = _view("banktran.cbl")
    assert any("GO TO" in f for f in v["flags"])
