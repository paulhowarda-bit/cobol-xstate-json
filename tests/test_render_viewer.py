"""Regression guards for the viz/ statechart viewer.

These lock in the two rendering bugs found by manual inspection so they cannot
silently come back. Both bugs were *structural*, not numerical, so the strongest
guard is a deterministic invariant check rather than a pixel sample:

  1. Occlusion — every transition was drawn, but the viewer painted the edge
     layer BEFORE the node layer, so the root OR-state container (which wraps the
     whole COBOL program) painted its opaque fill over all edges. Invariant: the
     `nodes` group is appended before the `edges` group, so edges always paint on
     top and CANNOT be occluded by a node fill. (A proof, not an elementFromPoint
     sample of a few edges.)

  2. Visibility — entry/exit/do/transition-action labels were all tagged
     `lod-l3`, so they only appeared at zoom >= 0.9; at the normal fit zoom
     (LOD 2) none showed. Invariant: those four are tagged `lod-l2`.

Plus end-to-end checks that the data actually carries transitions and that the
emitted HTML is well-formed and self-contained.
"""
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VIZ = ROOT / "viz"
VENDOR = VIZ / "vendor"
EXAMPLES = ROOT / "examples"

# load the standalone renderer by path (it lives outside the package)
sys.path.insert(0, str(VIZ))
_spec = importlib.util.spec_from_file_location(
    "render_statechart", VIZ / "render_statechart.py")
render_statechart = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(render_statechart)

from cobol_xstate.parser import parse_program  # noqa: E402
from cobol_xstate.statechart import build_machine  # noqa: E402

VIEWER_JS = (VENDOR / "viewer.js").read_text(encoding="utf-8")
VIEWER_CSS = (VENDOR / "viewer.css").read_text(encoding="utf-8")
LAYOUT_JS = (VENDOR / "layout_boot.js").read_text(encoding="utf-8")

EXAMPLE_SRCS = ["banktran", "custrpt", "cicsinq"]


def _machine_config(name):
    src = (EXAMPLES / f"{name}.cbl").read_text(errors="replace")
    return build_machine(parse_program(src)).config


# -- 1. occlusion invariant: nodes paint before edges ----------------------

def test_node_layer_appended_before_edge_layer():
    """If nodes are appended after edges, container fills occlude transitions."""
    i_nodes = VIEWER_JS.find('.attr("class", "nodes")')
    i_edges = VIEWER_JS.find('.attr("class", "edges")')
    assert i_nodes != -1 and i_edges != -1, "could not find the layer groups"
    assert i_nodes < i_edges, (
        "edges layer must be appended AFTER nodes layer so transitions paint on "
        "top of (and are never occluded by) container fills")


def test_boundary_edges_also_above_nodes():
    i_nodes = VIEWER_JS.find('.attr("class", "nodes")')
    i_bedges = VIEWER_JS.find('.attr("class", "boundary-edges")')
    assert i_bedges > i_nodes, "boundary (external I/O) edges must also paint above nodes"


# -- 2. visibility invariant: behavior shows at the fit zoom (LOD 2) --------

@pytest.mark.parametrize("snippet,what", [
    ('`compartment ${cls} lod-l2`', "entry/exit/SR compartments"),
    ('"activity-badge lod-l2"', "do/activity badges"),
    ('"ac lod-l2"', "transition actions"),
])
def test_behavior_labels_visible_at_mid_zoom(snippet, what):
    assert snippet in VIEWER_JS, (
        f"{what} must be tagged lod-l2 (visible at fit zoom), not lod-l3")


def test_behavior_labels_not_regated_to_l3():
    # the specific tags that previously hid behavior must not reappear as lod-l3
    assert 'compartment ${cls} lod-l3' not in VIEWER_JS
    assert '"activity-badge lod-l3"' not in VIEWER_JS
    assert '"ac lod-l3"' not in VIEWER_JS


# -- conditional vs sequential edge encoding -------------------------------

def test_conditional_edges_are_classed_distinctly():
    """Guarded transitions get a `conditional` class; unguarded autos `seq`."""
    assert '" conditional"' in VIEWER_JS and '" seq"' in VIEWER_JS
    # and the CSS must style them differently (decision color + dashed vs plumbing)
    assert ".edge.conditional path" in VIEWER_CSS
    assert ".edge.seq path" in VIEWER_CSS
    cond_rule = VIEWER_CSS.split(".edge.conditional path", 1)[1].split("}", 1)[0]
    assert "stroke-dasharray" in cond_rule, "conditional edges must be dashed"


def test_always_noise_is_suppressed_as_a_label():
    """ε(always) must not be rendered as a caption; only meaningful text shows."""
    assert "isAuto" in VIEWER_JS
    assert "if (!label.cap && !label.ac) return;" in VIEWER_JS


def test_edges_have_tooltip_with_condition():
    """Every edge carries a <title> tooltip; guarded ones state the condition."""
    assert 'edgeSel.append("title")' in VIEWER_JS
    assert 'when [' in VIEWER_JS  # tooltip phrasing for the guard


def test_guard_operators_prettified_for_display_only():
    """Captions/tooltips render = < > … but the stored guard name is untouched."""
    assert "function prettyGuard" in VIEWER_JS
    assert ', "=")' in VIEWER_JS and ', "<")' in VIEWER_JS and ', ">")' in VIEWER_JS
    assert "prettyGuard(e.guard)" in VIEWER_JS  # applied in caption + tooltip
    # the raw slug guard names in the data are NOT rewritten (search/provenance intact)
    _, graph, _ = render_statechart.render_html(_machine_config("banktran"))
    guards = [e["guard"] for e in graph["edges"] if e.get("guard")]
    assert any("_eq_" in g for g in guards), "raw slug guard names must be preserved"


# -- 3. box sizing fits the now-visible compartment text -------------------

def test_leaf_width_accounts_for_compartment_text():
    """leafSize must size width to entry/exit text, else PERFORMs overflow."""
    assert '"entry / " + a' in LAYOUT_JS and '"exit / " + a' in LAYOUT_JS, (
        "leafSize must include entry/exit compartment text in its width measure")


# -- 4. the data actually carries transitions ------------------------------

@pytest.mark.parametrize("name", EXAMPLE_SRCS)
def test_graph_has_resolved_transitions(name):
    graph = render_statechart.build_graph(_machine_config(name))
    nodes = set(graph["nodes"])
    drawn = [e for e in graph["edges"]
             if not e["internal"] and isinstance(e["target"], str)
             and not e["target"].startswith("#")]
    assert drawn, f"{name}: no drawable transitions in the graph"
    for e in drawn:
        assert e["target"] in nodes, f"{name}: edge {e['id']} -> unknown {e['target']}"


@pytest.mark.parametrize("name", EXAMPLE_SRCS)
def test_states_carry_entry_behavior(name):
    """Sanity: the emitter records COBOL behavior as entry actions to render."""
    graph = render_statechart.build_graph(_machine_config(name))
    total_entry = sum(len(n["entry"]) for n in graph["nodes"].values())
    assert total_entry > 0, f"{name}: no entry behavior to display"


# -- 5. the emitted HTML is well-formed and self-contained -----------------

def test_render_html_is_self_contained_and_carries_edges():
    html, graph, used_cdn = render_statechart.render_html(_machine_config("banktran"))
    # vendored libs present in this repo -> no CDN fallback
    assert not used_cdn, "expected fully offline output with vendored libs"
    # the graph data embedded for the browser carries edges
    m = re.search(r'<script type="application/json" id="raw-graph">(.*?)</script>',
                  html, re.S)
    assert m, "raw-graph data block missing"
    embedded = json.loads(m.group(1))
    assert embedded["edges"], "no edges embedded in the viewer"
    # the three script payloads are inlined
    assert 'id="viewer-src"' in html and "ELK()" in html and "d3" in html
    # no embedded block accidentally closes a script/style tag
    body = html.split("</head>", 1)[1]
    assert "</script>" in body  # the real closers exist
    # ...but inside the json/plain blocks, closers must be escaped
    raw_block = m.group(1)
    assert "</script>" not in raw_block, "unescaped </script> in embedded JSON"


def test_bundle_input_is_accepted():
    """render path accepts both a bare config and the cobol-xstate bundle."""
    cfg = _machine_config("banktran")
    bundle = {"machine": cfg, "metadata": {}, "data": {}}
    assert render_statechart.extract_machine(bundle) is cfg
    assert render_statechart.extract_machine(cfg) is cfg
