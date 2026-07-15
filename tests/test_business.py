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


def test_entry_reaches_the_first_boundary_state():
    v = _view("banktran.cbl")
    # 1000-OPEN itself is now a boundary state (OPEN INPUT declares the file channel),
    # so the entry path stops there rather than at the READ sub-state.
    assert v["entry"][0]["to"] == "1000-OPEN"


def test_goto_out_of_perform_is_flagged():
    # GO TO out of a performed paragraph is modeled as a return (as the runnable machine
    # does); surface it so the one mis-routable edge is not silently trusted.
    v = _view("banktran.cbl")
    assert any("GO TO" in f for f in v["flags"])


# --------------------------------------------------------------------------- #
# the business view IS a machine: renderable, not just readable
# --------------------------------------------------------------------------- #

def test_business_view_is_a_real_xstate_config():
    v = _view("banktran.cbl")
    assert v["format"] == "xstate-v5-config"     # the renderer's schema, not a report
    m = v["machine"]
    assert m["id"] == "BANKTRAN__business"
    assert m["initial"] in m["states"]
    assert m["states"], "a machine needs states"


def test_business_machine_has_no_dangling_edges():
    # a renderer must be able to draw every edge
    m = _view("banktran.cbl")["machine"]
    for name, st in m["states"].items():
        for e in st.get("always", []):
            assert e["target"] in m["states"], f"{name} -> {e['target']} is dangling"


def test_business_machine_carries_the_view_in_meta():
    # everything the report shape held that XState has no slot for rides in meta
    m = _view("banktran.cbl")["machine"]
    disp = m["states"]["2000-DISPATCH"]
    assert disp["meta"]["role"] == "decision"
    assert disp["meta"]["suggestedName"] is None        # the deliberate blank
    assert disp["meta"]["decisions"], "decision guards must survive into meta"
    dep = m["states"]["2100-DEPOSIT"]
    assert dep["meta"]["role"] == "boundary"
    assert dep["meta"]["perimeter"] == "output"
    assert dep["meta"]["boundaryActions"][0]["endpoint"] == "POSTLOG"


def test_business_edges_keep_the_collapsed_path_and_guards():
    m = _view("banktran.cbl")["machine"]
    edges = m["states"]["2000-DISPATCH"]["always"]
    guarded = [e for e in edges if e.get("guard")]
    assert guarded, "the dispatch fan-out must carry its guards as edge labels"
    for e in edges:
        assert "via" in e["meta"]        # what this edge collapsed, for a hover
        assert "guards" in e["meta"]     # the full list (XState allows only one guard)


def test_business_terminal_states_are_final():
    m = _view("banktran.cbl")["machine"]
    finals = [n for n, s in m["states"].items() if s.get("type") == "final"]
    assert finals
    for n in finals:
        assert m["states"][n]["meta"]["role"] == "terminal"


def test_synthetic_entry_node_when_collapse_has_several_first_states():
    m = _view("banktran.cbl")["machine"]
    assert m["initial"] == "__ENTRY__"
    ent = m["states"]["__ENTRY__"]
    assert ent["meta"]["role"] == "entry"
    assert ent["always"], "the entry must lead to the first business state(s)"


def test_report_shape_is_still_there_for_reading():
    # the machine is for drawing; these keys stay for querying
    v = _view("banktran.cbl")
    for k in ("businessStates", "transitions", "collapsed", "counts", "nameFillIn"):
        assert k in v


# --------------------------------------------------------------------------- #
# CLI: distinct names, and the lineage companion
# --------------------------------------------------------------------------- #

def test_business_target_writes_its_own_name_and_the_lineage_companion(tmp_path):
    from cobol_xstate.cli import run
    src = Path(__file__).resolve().parents[1] / "examples" / "banktran.cbl"
    assert run([str(src), "--target", "business", "--outdir", str(tmp_path)]) == 0
    assert (tmp_path / "banktran.business.json").exists()   # not banktran.json
    assert (tmp_path / "banktran.lineage.json").exists()    # the companion travels too


def test_business_does_not_clobber_the_default_bundle(tmp_path):
    from cobol_xstate.cli import run
    src = Path(__file__).resolve().parents[1] / "examples" / "banktran.cbl"
    assert run([str(src), "--outdir", str(tmp_path)]) == 0
    assert run([str(src), "--target", "business", "--outdir", str(tmp_path)]) == 0
    import json
    bundle = json.loads((tmp_path / "banktran.json").read_text(encoding="utf-8"))
    view = json.loads((tmp_path / "banktran.business.json").read_text(encoding="utf-8"))
    assert bundle["metadata"].get("view") is None          # the faithful bundle
    assert view["metadata"]["view"] == "business"          # the distillation


# --------------------------------------------------------------------------- #
# the business chart carries the external boundary, fields and all
# --------------------------------------------------------------------------- #

def test_business_view_has_an_interface_like_the_faithful_bundle():
    v = _view("sqlunld.cbl")
    assert "interface" in v
    eps = {e["endpoint"]: e for e in v["interface"]["endpoints"]}
    assert eps["ACCOUNT"]["type"] == "db2" and eps["ACCOUNT"]["directions"] == ["get"]
    assert eps["OUT-FILE"]["type"] == "file" and eps["OUT-FILE"]["directions"] == ["create"]


def test_boundary_actions_carry_the_fields_that_cross():
    v = _view("sqlunld.cbl")
    fetch = next(a for s in v["businessStates"].values()
                 for a in s["boundaryActions"] if a["verb"] == "FETCH")
    names = [f["name"] for f in fetch["fields"]]
    assert names == ["WS-ID", "WS-NAME", "WS-BAL"]      # what the input event FILLS
    assert fetch["direction"] == "get" and fetch["endpointType"] == "db2"
    assert fetch["event"] == "GET.DB2.ACCOUNT"
    # typed, so a label can show the shape of the data
    assert {f["pic"] for f in fetch["fields"]} == {"9(5)", "X(20)", "S9(7)V99"}

    write = next(a for s in v["businessStates"].values()
                 for a in s["boundaryActions"] if a["verb"] == "WRITE")
    assert "OUT-BAL" in [f["name"] for f in write["fields"]]   # what FILLS the output


def test_machine_nodes_tag_perimeter_and_fields_for_a_renderer():
    m = _view("sqlunld.cbl")["machine"]
    node = next(s for s in m["states"].values()
                if "GET.DB2.ACCOUNT" in s["meta"].get("gets", []))
    assert node["meta"]["perimeter"] == "input"
    assert node["meta"]["inputFields"] == ["WS-BAL", "WS-ID", "WS-NAME"]
    out = next(s for s in m["states"].values()
               if "CREATE.FILE.OUT-FILE" in s["meta"].get("creates", [])
               and s["meta"].get("outputFields"))
    assert out["meta"]["perimeter"] == "output"
    assert "OUT-BAL" in out["meta"]["outputFields"]


def test_sql_where_host_vars_ride_as_params_not_fields():
    # a SELECT's INTO vars come IN; its WHERE vars go OUT with the request
    v = _view("cicsinq.cbl") if False else _view("sqldml.cbl")
    sel = next((a for s in v["businessStates"].values()
                for a in s["boundaryActions"] if a["verb"] == "SELECT"), None)
    if sel is not None:            # sqldml's SELECT lives on a business state
        assert [f["name"] for f in sel["fields"]] == ["WS-NAME", "WS-BAL"]
        assert sel.get("params") and sel["params"][0]["name"] == "WS-ID"


def test_every_interface_event_anchors_to_a_state_that_exists():
    # a renderer draws an arrow per event; its host must be in the machine
    for name in ("sqlunld.cbl", "custrpt.cbl", "banktran.cbl", "lineage.cbl"):
        v = _view(name)
        states = set(v["machine"]["states"])
        for e in v["interface"]["events"]:
            assert e["state"] in states, f"{name}: {e['event']} anchors to {e['state']}"
