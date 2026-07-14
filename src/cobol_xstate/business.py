"""Stage 6b (overlay) - the BUSINESS-VIEW distillation.

A *read-only* projection over the faithful machine. It classifies every emitted state as
**business** (it crosses the program boundary, or it makes a decision on business data) or
**technical** (loop mechanics, a no-op branch, pure control-flow scaffolding), then collapses
the technical states out of the graph - contracting each into the edges that pass through it -
so what remains is the business state machine. It invents nothing: every surviving state and
transition traces back to the faithful machine, and every business *name* is left as a
``suggestedName: null`` fill-in for a human (or an LLM) to supply, because mapping COBOL
identifiers to business vocabulary is the one step this pass cannot infer.

State roles:
  * **boundary**  - the state is a perimeter state (file/Db2/console/terminal/caller I/O).
  * **decision**  - the state branches on a *business* condition (not a loop/at-end guard).
  * **terminal**  - a final state (program end).
  * **technical** - none of the above; collapsed away.

Scope of this prototype (see docs/reactive-target.md sibling notes): FLAT, single-region
machines, where the ``always``/``on`` graph is the real control flow. Out-of-line ``PERFORM``
(a call, modeled as an actor / entry action, not an edge) and ``type: parallel`` machines
need call/return contraction and are flagged, not faked.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from . import interface as _iface
from .emitter import _para_of, _target_owner
from .statechart import Machine


# --------------------------------------------------------------------------- #
# guard / action classification
# --------------------------------------------------------------------------- #

def _is_control_guard(name: str, tree: Optional[dict]) -> bool:
    """A guard that rides on control-flow mechanics, not a business condition: a loop's
    ``UNTIL_...`` test, a file ``..._atEnd`` end-of-stream, or an unmodeled ``{op:'raw'}``."""
    if name.startswith("UNTIL_") or name.endswith("_atEnd") or name.endswith("_atend"):
        return True
    if isinstance(tree, dict) and tree.get("op") == "raw":
        return True
    return False


def _guard_field(tree: Optional[dict]) -> Optional[str]:
    """The primary data item a (relational/class/sign) guard tests, for the business label."""
    if not isinstance(tree, dict):
        return None
    if "left" in tree and isinstance(tree["left"], str):
        return tree["left"]
    for k in ("operand", "subject"):
        if isinstance(tree.get(k), str):
            return tree[k]
    for v in tree.values():
        got = _guard_field(v) if isinstance(v, (dict, list)) else None
        if got:
            return got
    return None


# --------------------------------------------------------------------------- #
# graph helpers over the faithful config
# --------------------------------------------------------------------------- #

def _successors(st: dict) -> List[Tuple[dict, str]]:
    """Outgoing (label, target) pairs. label = {'guard': name} or {'event': name} or {}."""
    out: List[Tuple[dict, str]] = []
    for e in st.get("always", []) or []:
        lab = {"guard": e["guard"]} if e.get("guard") else {}
        if e.get("target"):
            out.append((lab, e["target"]))
    for ev, handler in (st.get("on", {}) or {}).items():
        for h in (handler if isinstance(handler, list) else [handler]):
            if isinstance(h, dict) and h.get("target"):
                out.append(({"event": ev}, h["target"]))
    return out


class _BusinessView:
    def __init__(self, machine: Machine):
        self.machine = machine
        self.config = machine.config
        self.states: Dict[str, dict] = self.config.get("states", {})
        self.guards: Dict[str, dict] = machine.semantics.get("guards", {})
        self.actions: Dict[str, dict] = machine.semantics.get("actions", {})
        self.provenance = machine.provenance
        iface = _iface.build_interface(
            machine.config, machine.semantics, machine.provenance,
            data=machine.data, using=machine.using, returning=machine.returning,
            files=getattr(machine, "files", {}) or {})
        self.iface = iface
        self.perimeter = iface["perimeterStates"]
        self.files = getattr(machine, "files", {}) or {}
        self._dv = _iface._DataView(machine.data)
        self._cursors = _iface._cursor_tables(machine.provenance)
        self.ordered: List[str] = machine.paragraph_order
        self.sections: Dict[str, List[str]] = getattr(machine, "sections", {}) or {}
        self.finals = {n for n, st in self.states.items() if st.get("type") == "final"}
        self.flags: List[str] = []

    def _flag(self, msg: str) -> None:
        if msg not in self.flags:
            self.flags.append(msg)

    # -- classification -----------------------------------------------------
    def role(self, name: str, st: dict) -> str:
        if st.get("type") == "final":
            return "terminal"
        boundary = name in self.perimeter
        decision = any(
            e.get("guard") and not _is_control_guard(e["guard"], self.guards.get(e["guard"]))
            for e in (st.get("always", []) or [])
        )
        if boundary and decision:
            return "boundary+decision"
        if boundary:
            return "boundary"
        if decision:
            return "decision"
        return "technical"

    def _is_business(self, name: str) -> bool:
        return self.role(name, self.states[name]) != "technical"

    # -- control-flow model (call/return aware) -----------------------------
    #
    # A *configuration* is ``(state, stack)`` where ``stack`` is a tuple of call frames
    # ``(owner_paragraphs, return_target)``. An out-of-line ``PERFORM P`` is a call: push a
    # frame and jump to P's entry; when control leaves P's owned paragraph(s) - a fall-through
    # past the range, a GO TO out, or the ``__END__`` sentinel - that is the *return* (pop the
    # frame, resume at the saved continuation). This mirrors exactly how the emitter lowers
    # PERFORM to invoke/``__RET__`` (``_target_owner`` / ``_reroute_to_return``), so the
    # business flow matches the runnable machine's call semantics. A final reached at the top
    # level (empty stack) is program end; reached inside a call it is a return.

    def _perform_names(self, st: dict) -> List[str]:
        return [a[len("perform_"):] for a in (st.get("entry", []) or [])
                if a.startswith("perform_")]

    def _continuation(self, st: dict) -> Optional[str]:
        """Where control resumes after a PERFORM returns = the call state's fall-through."""
        for e in st.get("always", []) or []:
            if e.get("target"):
                return e["target"]
        return None

    def _guard_dict(self, name: str) -> dict:
        tree = self.guards.get(name)
        return {"name": name, "condition": tree, "field": _guard_field(tree),
                "kind": "control" if _is_control_guard(name, tree) else "business"}

    def _step(self, state: str, stack: tuple):
        """Yield ``(label, (next_state, next_stack))`` for one control step from a config."""
        st = self.states.get(state, {})
        if state in self.finals:                       # program end, or a return
            if stack and stack[-1][1] is not None:
                yield ({}, (stack[-1][1], stack[:-1]))
            return
        performs = self._perform_names(st)
        if performs:                                   # a call
            name = performs[0]
            owner, init = _target_owner(name, self.ordered, self.sections)
            cont = self._continuation(st)
            if len(performs) > 1:
                self._flag(f"{state}: {len(performs)} PERFORMs in one state; "
                           f"only {name} followed")
            if owner is None or init is None or init not in self.states:
                self._flag(f"PERFORM {name}: target unresolved; call not followed")
                if cont is not None:
                    yield ({}, (cont, stack))
            elif any(owner & fo for fo, _ in stack):
                self._flag(f"recursive PERFORM {name}; not followed")
                if cont is not None:
                    yield ({}, (cont, stack))
            else:
                yield ({}, (init, stack + ((frozenset(owner), cont),)))
            return
        for lab, target in _successors(st):            # ordinary edges
            if target not in self.states:
                continue
            if stack:
                owner, ret = stack[-1]
                if _para_of(target) not in owner or target == "__END__":   # leaves -> return
                    if ret is not None:
                        yield (lab, (ret, stack[:-1]))
                else:
                    yield (lab, (target, stack))
            else:
                yield (lab, (target, stack))

    def _next_business(self, cfg: tuple) -> List[dict]:
        """From a business config, walk through technical configs (following calls/returns)
        to the next business / terminal configs, accumulating guards, events, and via-states."""
        results: List[dict] = []
        seen: set = set()

        def walk(c: tuple, guards: List[dict], events: List[str], via: List[str]) -> None:
            state, stack = c
            if ((state in self.finals) and not stack) or self._is_business(state):
                results.append({"to": state, "to_config": c, "guards": guards,
                                "events": events, "via": via})
                return
            key = (state, stack, tuple(g["name"] for g in guards), tuple(events))
            if key in seen:
                return
            seen.add(key)
            for lab, nxt in self._step(state, stack):
                g2 = guards + [self._guard_dict(lab["guard"])] if lab.get("guard") else guards
                e2 = events + [lab["event"]] if lab.get("event") else events
                walk(nxt, g2, e2, via + [state])

        src_state, src_stack = cfg
        if (src_state in self.finals) and not src_stack:
            return results
        for lab, nxt in self._step(src_state, src_stack):
            g0 = [self._guard_dict(lab["guard"])] if lab.get("guard") else []
            e0 = [lab["event"]] if lab.get("event") else []
            walk(nxt, g0, e0, [])
        return results

    def _build_flow(self) -> Tuple[List[dict], List[dict]]:
        """Collapse the machine into (entry edges, business transitions) by reachability over
        configurations from the initial state - so a technical initial (a loop head) and every
        performed paragraph are followed correctly."""
        initial = self.config.get("initial")
        entry: List[dict] = []
        transitions: List[dict] = []
        if initial not in self.states:
            return entry, transitions

        worklist: List[tuple] = []
        if self._is_business(initial):
            entry.append({"to": initial, "guards": [], "events": [], "via": []})
            worklist.append((initial, ()))
        else:
            for nb in self._next_business((initial, ())):
                entry.append({"to": nb["to"], "guards": nb["guards"],
                              "events": nb["events"], "via": nb["via"]})
                worklist.append(nb["to_config"])

        seen_cfg: set = set()
        tr_seen: set = set()
        while worklist:
            cfg = worklist.pop()
            if cfg in seen_cfg:
                continue
            seen_cfg.add(cfg)
            state, stack = cfg
            if (state in self.finals) and not stack:
                continue
            for nb in self._next_business(cfg):
                k = (state, nb["to"], tuple(g["name"] for g in nb["guards"]),
                     tuple(nb["events"]))
                if k not in tr_seen:
                    tr_seen.add(k)
                    transitions.append({"from": state, "to": nb["to"], "via": nb["via"],
                                        "guards": nb["guards"], "events": nb["events"],
                                        "label": None})
                worklist.append(nb["to_config"])
        return entry, transitions

    # -- per-state business summary ----------------------------------------
    def _state_summary(self, name: str, st: dict, role: str) -> dict:
        boundary_actions, internal_steps = [], []
        primary_prov = None
        for aname in st.get("entry", []) or []:
            prov = self.provenance.get(aname, {})
            hits = _iface._classify(aname, prov.get("cobol", ""),
                                    self.actions.get(aname), self._dv,
                                    self.files, self._cursors)
            if hits:
                for hit in hits:
                    boundary_actions.append({"action": aname, "verb": hit["verb"],
                                             "endpoint": hit["endpoint"],
                                             "direction": hit["direction"]})
                primary_prov = primary_prov or prov
            else:
                internal_steps.append(aname)
        peri = self.perimeter.get(name, {})
        decisions = []
        for e in (st.get("always", []) or []):
            g = e.get("guard")
            if g and not _is_control_guard(g, self.guards.get(g)):
                tree = self.guards.get(g)
                decisions.append({"guard": g, "field": _guard_field(tree), "condition": tree})
        if primary_prov is None and internal_steps:
            primary_prov = self.provenance.get(internal_steps[0], {})
        cobol = None
        if primary_prov:
            cobol = {"line": primary_prov.get("line"), "text": primary_prov.get("cobol")}
        elif st.get("meta", {}).get("cobolLine"):
            cobol = {"line": st["meta"]["cobolLine"], "text": None}
        return {
            "role": role,
            "gets": peri.get("gets", []),
            "creates": peri.get("creates", []),
            "boundaryActions": boundary_actions,
            "decisions": decisions,
            "internalSteps": internal_steps,
            "cobol": cobol,
            "suggestedName": None,   # FILL-IN: business name for this state
        }

    # -- assemble -----------------------------------------------------------
    def build(self) -> dict:
        if self.config.get("type") == "parallel":
            self._flag("type:parallel (handler regions) - business view not lowered")
        if any("__goto" in n for n in self.states):
            self._flag("machine contains GO TO; a GO TO out of a performed paragraph is "
                       "modeled as a return (as the runnable machine does) - a cross-"
                       "paragraph jump may be routed to the caller instead of the target")

        business_names = [n for n in self.states if self._is_business(n)]
        technical_names = [n for n in self.states if not self._is_business(n)]

        business_states = {}
        for n in business_names:
            st = self.states[n]
            business_states[n] = self._state_summary(n, st, self.role(n, st))

        # Collapse to the business flow by reachability over configurations (call/return
        # aware), so out-of-line PERFORM is followed - not flagged-and-skipped.
        entry, transitions = self._build_flow()

        return {
            "format": "cobol-xstate-business-view",
            "program": self.machine.program_id,
            "source": self.machine.source_name,
            "note": ("Read-only distillation of the faithful machine: technical scaffolding "
                     "collapsed, only boundary-crossing and business-decision states kept. "
                     "Nothing invented - every state/transition traces to the faithful "
                     "machine. 'suggestedName'/'label' are null: supply the business "
                     "vocabulary (the one step this pass cannot infer)."),
            "entry": entry,
            "businessStates": business_states,
            "transitions": transitions,
            "collapsed": [{"state": n, "reason": "technical scaffolding"}
                          for n in technical_names],
            "counts": {"faithfulStates": len(self.states),
                       "businessStates": len(business_names),
                       "collapsed": len(technical_names)},
            "nameFillIn": {
                "states": [n for n in business_states
                           if business_states[n]["role"] != "terminal"],
                "transitions": len(transitions),
            },
            "flags": self.flags,
        }


def build_business_view(machine: Machine) -> dict:
    """Return the business-view distillation overlay for ``machine`` (pure read; see module)."""
    return _BusinessView(machine).build()
