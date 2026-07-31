"""Stage 7 - the emitted contract as a Harel-derived statechart (XState v5).

XState is a *restricted subset* of Harel, not Harel: it has no negated/compound events,
no durative activities, and no static reactions as primitives. This module does not
change that - it makes the contract a real *statechart* (hierarchy, resolved call/return,
no phantom edges) within what the target can express, and the losses stay named rather
than papered over.


The compiler's working representation (``Machine.config``) is deliberately **flat**: one
state per program point, structure encoded in mangled names (``0000-MAIN__loop3``), and
``PERFORM p`` recorded as a marker action. That shape is convenient for the analyses that
walk it (emitter, interface, lineage, business), but it is *not* a statechart:

* **No hierarchy.** ``0000-MAIN__loop3`` / ``__iter4`` / ``__seq2`` are siblings pretending
  to be structure via a naming convention.
* **PERFORM has no target and no return** - it is an action name, so the chart does not
  say where control goes.
* **Worst: the fall-through edges lie.** Paragraphs are chained in source order, so the
  contract shows ``2100-DEPOSIT -> 2200-WITHDRAW``. At run time 2100-DEPOSIT is only ever
  entered via ``PERFORM``, so control *returns to the dispatcher* and that edge never
  fires. The chart claimed a path the program does not take.

This module turns the IR into the artifact:

1. **Resolve PERFORM** into a real call/return, reusing the emitter's tested transform:
   each performed paragraph becomes its own chart, the call site ``invoke``s it, and
   ``onDone`` is the return. (A classical Harel chart has no call stack; ``invoke`` of a
   child chart is the faithful statechart model of a subroutine, and what the renderer
   draws.)
2. **Prune** what is then unreachable - the never-executed physical chain falls away by
   construction, because a callee is only reachable as a callee.
3. **Nest** each paragraph's structural states under a compound OR-state, so hierarchy is
   real nesting rather than a naming convention.

Every leaf keeps its flat name as its ``id``, and every transition targets ``#<id>``. That
makes targeting absolute and position-independent, so nesting cannot break a cross-
paragraph edge (``GO TO``, fall-through, or a return).

Nothing is invented: this is a restructuring of the same states, guards, actions and
provenance the compiler already produced. ``Machine.config`` is left untouched.
"""

from __future__ import annotations

import copy
from typing import Dict, List, Optional, Tuple

from .emitter import (
    _invoke_transform,
    _invoke_transform_parallel,
    _para_of,
    retarget_on,
)

# Names that are not paragraph members: the shared program end, an actor's return, and
# any other sentinel. They stay at the top level of their chart.
_SPECIAL_PREFIX = "__"


def _is_special(name: str) -> bool:
    return name.startswith(_SPECIAL_PREFIX)


def _group_of(name: str) -> Optional[str]:
    """The paragraph a state belongs to, or None for a top-level sentinel."""
    if _is_special(name):
        return None
    return _para_of(name)


def _leaf_key(name: str, group: str) -> str:
    """The child key inside a paragraph's compound state. The paragraph's own entry
    state becomes ``_entry``; ``0000-MAIN__loop3`` becomes ``loop3``."""
    if name == group:
        return "_entry"
    return name[len(group) + 2:] if name.startswith(group + "__") else name


def _retarget(node: dict) -> None:
    """Rewrite every transition target to an absolute ``#id`` reference, in place."""
    for t in node.get("always", []) or []:
        if t.get("target"):
            t["target"] = "#" + t["target"]
    inv = node.get("invoke")
    if inv and inv.get("onDone", {}).get("target"):
        inv["onDone"]["target"] = "#" + inv["onDone"]["target"]
    on = node.get("on")
    if isinstance(on, dict):
        retarget_on(on, lambda t: "#" + t)


def _nest(states: Dict[str, dict]) -> Dict[str, dict]:
    """Group flat states into one compound OR-state per paragraph.

    A paragraph with a single state stays a leaf (a compound wrapping one child would be
    noise). Sentinels (``__END__``, ``__RET__``) stay top-level.
    """
    order: List[str] = []
    groups: Dict[str, Dict[str, dict]] = {}
    for name, st in states.items():
        g = _group_of(name)
        key = g if g is not None else name
        if key not in groups:
            groups[key] = {}
            order.append(key)
        groups[key][name] = st

    out: Dict[str, dict] = {}
    for key in order:
        members = groups[key]
        if len(members) == 1 and key in members:
            node = copy.deepcopy(members[key])
            node["id"] = key
            _retarget(node)
            out[key] = node
            continue
        if _is_special(key):                     # a sentinel never groups
            for n, st in members.items():
                node = copy.deepcopy(st)
                node["id"] = n
                _retarget(node)
                out[n] = node
            continue
        inner: Dict[str, dict] = {}
        initial: Optional[str] = None
        for n, st in members.items():
            node = copy.deepcopy(st)
            node["id"] = n                       # the flat name remains the address
            _retarget(node)
            leaf = _leaf_key(n, key)
            inner[leaf] = node
            if n == key:                         # the paragraph's own entry point
                initial = leaf
        if initial is None:                      # no entry state: keep source order
            initial = next(iter(inner))
        out[key] = {
            "initial": initial,
            "states": inner,
            "meta": {"kind": "paragraph", "paragraph": key},
        }
    return out


def _nest_chart(chart: dict) -> dict:
    """Nest one ``{initial, states}`` chart (the main flow, a region, or an actor)."""
    states = _nest(chart.get("states", {}))
    out = dict(chart)
    out["states"] = states
    init = chart.get("initial")
    if init:
        # The initial may now be a compound; entering it lands on its own initial child.
        out["initial"] = _group_of(init) if _group_of(init) in states else init
    return out


def to_harel(machine) -> Tuple[dict, Dict[str, dict]]:
    """Return ``(config, charts)`` - the contract's Harel view of ``machine``.

    ``config`` is the main chart: hierarchical, with PERFORM resolved to ``invoke`` and
    the never-executed physical fall-through pruned. ``charts`` maps each callee's actor
    name to its own chart, the statechart model of a performed paragraph.
    """
    src = copy.deepcopy(machine.config)
    ordered = machine.paragraph_order
    sections = getattr(machine, "sections", {}) or {}
    charts: Dict[str, dict] = {}

    # Resolve PERFORM with the emitter's own transform - the runnable (`--target js`)
    # target's, tested under real XState - then add the two things a *drawable* statechart
    # needs on top: nest each flat paragraph run into a compound OR-state, and keep the
    # provenance `meta` the emitter strips for runnable JS. The transform is meta-
    # transparent: it propagates whatever `meta` the states it is handed carry, so feeding
    # it the un-stripped config reproduces the (previously duplicated) inline orchestration
    # exactly, now single-sourced. Each concurrent region resolves PERFORM against a pool
    # unioned across all regions (so a handler can PERFORM a main-flow paragraph and vice
    # versa) inside `_invoke_transform_parallel`; here each transformed region is nested.
    if src.get("type") == "parallel":
        new_regions, charts = _invoke_transform_parallel(src["states"], ordered, sections)
        src["states"] = {name: _nest_chart(nr) for name, nr in new_regions.items()}
    elif src.get("states") and src.get("initial"):
        main_new, charts = _invoke_transform(
            src["states"], src["initial"], ordered, sections)
        nested = _nest_chart({"initial": src["initial"], "states": main_new})
        src["states"] = nested["states"]
        src["initial"] = nested["initial"]

    return src, {name: _nest_chart(c) for name, c in charts.items()}
