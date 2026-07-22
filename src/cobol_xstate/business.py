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

import heapq
from typing import Dict, List, Optional, Tuple

from . import interface as _iface
from .emitter import _para_of, _target_owner
from .statechart import Machine


# --------------------------------------------------------------------------- #
# guard / action classification
# --------------------------------------------------------------------------- #

def _is_control_guard(name: str, tree: Optional[dict]) -> bool:
    """A guard that rides on control-flow mechanics, not a business condition: a loop's
    ``UNTIL_...`` test, a file ``..._atEnd`` end-of-stream, or an unmodeled ``{op:'raw'}``.

    The end-of-stream test matches the ``notAtEnd`` sense too. Anchoring on ``_atEnd``
    missed it - ``IN-FILE_notAtEnd`` does not end in ``_atEnd`` - so the NOT AT END arm of
    a READ was reported as a *business* decision, which is exactly backwards: it is the
    most mechanical branch in the language.
    """
    if name.startswith("UNTIL_") or name.lower().endswith("atend"):
        return True
    if isinstance(tree, dict) and tree.get("op") == "raw":
        return True
    return False


def _chain(link) -> list:
    """A ``(head, rest)`` cons chain back to the list it stands for, oldest first.

    The collapse walk builds one of these per path instead of extending a list, so that
    carrying a path costs nothing until something actually needs to read it.
    """
    out = []
    while link is not None:
        out.append(link[0])
        link = link[1]
    out.reverse()
    return out


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
        iface = machine.interface()
        self.iface = iface
        self.perimeter = iface["perimeterStates"]
        self.files = getattr(machine, "files", {}) or {}
        self._dv = _iface._DataView(machine.data)
        self._cursors = _iface._cursor_tables(machine.provenance)
        self.ordered: List[str] = machine.paragraph_order
        self.sections: Dict[str, List[str]] = getattr(machine, "sections", {}) or {}
        self.finals = {n for n, st in self.states.items() if st.get("type") == "final"}
        self.flags: List[str] = []
        self._business_memo: Dict[str, bool] = {}

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
        # A state whose entry does real arithmetic/COMPUTE work IS business logic (a
        # pricing/accumulation step), even with no boundary or branch - keep it rather
        # than collapsing it as technical scaffolding.
        if any((self.actions.get(a) or {}).get("kind") in ("arith", "compute")
               for a in (st.get("entry", []) or [])):
            return "calculation"
        return "technical"

    def _is_business(self, name: str) -> bool:
        """Cached: the collapse walk asks this of every state it steps through, once per
        path, and the answer re-derives the whole classification (perimeter lookup, a
        control-guard test per outgoing edge, an action-kind lookup per entry action)
        from data that cannot change after __init__."""
        got = self._business_memo.get(name)
        if got is None:
            got = self._business_memo[name] = \
                self.role(name, self.states[name]) != "technical"
        return got

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

    # A technical region can hold exponentially many distinct configurations even after
    # the subsumption below - a paragraph that is both PERFORMed and fallen into gives
    # every level two continuations - so unlike lineage's fixpoint bound this CANNOT be a
    # proof. It is an admitted policy, and the honest part is the flag: it names the
    # region and says how much it did recover, so a short list is never mistaken for a
    # complete one. Costs about a second when fully spent; the largest example program
    # needs 25 steps for its entire build, so the headroom is four orders of magnitude.
    # A legitimately huge program could still be truncated here - it would also have more
    # business edges than anyone can read, and it would be told so.
    _WALK_STEPS = 100_000

    def _next_business(self, cfg: tuple) -> List[dict]:
        """From a business config, walk through technical configs (following calls/returns)
        to the next business / terminal configs, accumulating guards, events, and via-states.

        Over an EXPLICIT worklist, not the interpreter's stack. As recursion this cost
        about ten Python frames per technical state stepped through, so a chain of a
        hundred nested PERFORMs - each body a MOVE, nothing exotic - overflowed the
        default 1000-frame limit and raised RecursionError. That is not a poor business
        view, it is no business view: the exception escaped `build_business_view`, and
        because a default run writes this companion FIRST it took the lineage, reactive,
        artifacts and dynamic-call companions down with it. The tool reported only the
        machine, for a program it had parsed perfectly well.

        A path is dropped when some earlier arrival at the same ``(state, stack, events)``
        carried a SUBSET of its guards. Reaching a place under ``A and B`` says nothing
        that reaching it under ``A`` alone has not already said, and reaching it under no
        guard at all makes every guarded route to it redundant. Without this, sixteen
        IF/ELSE diamonds that all reconverge - each guard independent of where control
        ends up - produced 2^16 = 65,536 entry edges to ONE state, differing only in which
        subset of the guards happened to be taken. Not merely slow: it asserts 65,536
        business rules where there is not one. Genuine alternatives survive, because
        neither of ``A`` and ``B`` contains the other: an EVALUATE's WHEN arms still fan
        out, and a loop's exit still carries its UNTIL.

        The order is smallest-guard-set-first, which is what makes the subsumption pay:
        the cheapest way to reach somewhere is found before the expensive ones, so those
        are recognised as redundant ON ARRIVAL instead of after they have been explored.
        Depth-first found them in almost exactly the wrong order.
        """
        results: List[dict] = []
        # (state, stack, events) -> the guard-name SETS already used to arrive there.
        # Per call, deliberately: sharing it across calls would silence a config the walk
        # from a LATER business state must also reach, and each of those is a real edge.
        arrived: Dict[tuple, List[frozenset]] = {}

        src_state, src_stack = cfg
        if (src_state in self.finals) and not src_stack:
            return results

        # `via` and `guards` ride as CONS CHAINS - (head, rest) - rather than lists.
        # Extending a list copies it, so a walk whose paths grow long paid for the whole
        # path again at every single step; on one pathological program that copying was
        # 0.8 of the 1.0 seconds. Consing is O(1) and only the handful of chains that
        # become results are ever flattened. `events` stays a tuple: it is short, and it
        # is part of the visited key, which has to be hashable.
        # (guard count, tie-break, config, guard names, guards, events, via). The
        # tie-break is the insertion counter, which makes the walk breadth-first within a
        # guard count and makes the heap entries comparable without ever having to order
        # two configs against each other. Deepest-first was tried and is much worse: it
        # follows a loop round and round - each pass appending another event, so the
        # visited key keeps changing and the subsumption never fires - while
        # breadth-first reaches the loop's exits immediately.
        heap: List[tuple] = []
        seq = 0
        for lab, nxt in self._step(src_state, src_stack):
            g0 = (self._guard_dict(lab["guard"]), None) if lab.get("guard") else None
            e0 = (lab["event"],) if lab.get("event") else ()
            names = frozenset([lab["guard"]] if lab.get("guard") else [])
            heapq.heappush(heap, (len(names), seq, nxt, names, g0, e0, None))
            seq += 1

        steps = 0
        while heap:
            steps += 1
            if steps > self._WALK_STEPS:
                self._flag(f"{src_state}: the collapsed region below it has more distinct "
                           f"paths than this view enumerates; "
                           f"{len(results) or 'NO'} outgoing business edge(s) recovered "
                           f"before the budget ran out - INCOMPLETE, read the faithful "
                           f"machine for this part of the program")
                break
            _, _, c, names, guards, events, via = heapq.heappop(heap)
            state, stack = c
            prior = arrived.setdefault((state, stack, events), [])
            if any(p <= names for p in prior):
                continue
            prior.append(names)
            if ((state in self.finals) and not stack) or self._is_business(state):
                results.append({"to": state, "to_config": c, "guards": _chain(guards),
                                "events": list(events), "via": _chain(via)})
                continue
            via2 = (state, via)
            for lab, nxt in self._step(state, stack):
                g2 = (self._guard_dict(lab["guard"]), guards) if lab.get("guard") else guards
                e2 = events + (lab["event"],) if lab.get("event") else events
                n2 = names | {lab["guard"]} if lab.get("guard") else names
                heapq.heappush(heap, (len(n2), seq, nxt, n2, g2, e2, via2))
                seq += 1
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

    def _pic(self, field: str) -> Optional[str]:
        """The declared picture/category of a field, so a boundary label can show the
        shape of the data crossing, not just its name."""
        it = self.machine.data.get((field or "").upper()) or {}
        t = it.get("type") or {}
        return t.get("pic") or t.get("category")

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
                    # The FIELDS crossing here are the point of a boundary state: an
                    # input event fills them, an output event is filled by them. A
                    # business reader needs the data, not just "it talks to Db2".
                    ba = {"action": aname, "verb": hit["verb"],
                          "endpoint": hit["endpoint"],
                          "endpointType": hit["etype"],
                          "direction": hit["direction"],
                          "event": _iface._event(hit["direction"], hit["etype"],
                                                 hit["endpoint"]),
                          "fields": [{"name": f, "pic": self._pic(f)}
                                     for f in hit["fields"]]}
                    if hit.get("params"):     # data flowing the other way (keys, WHERE)
                        ba["params"] = [{"name": f, "pic": self._pic(f)}
                                        for f in hit["params"]]
                    if prov.get("line"):
                        ba["line"] = prov["line"]
                    boundary_actions.append(ba)
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
        config = self._as_machine(business_states, entry, transitions)

        return {
            "format": "xstate-v5-config",
            "metadata": {
                "program": self.machine.program_id,
                "source": self.machine.source_name,
                "generator": "cobol-xstate 0.1.0 (--target business)",
                "view": "business",
                "disclaimer": (
                    "Read-only BUSINESS distillation of the faithful machine: technical "
                    "scaffolding collapsed, only boundary-crossing, decision, and "
                    "calculation states kept. This is a real XState v5 config so it can "
                    "be rendered, but it is a VIEW, not a runnable machine - the "
                    "collapsed steps are summarised in each state's meta, not executed. "
                    "Nothing invented: every state/transition traces to the faithful "
                    "machine (meta.cobol). 'suggestedName'/'label' are null on purpose - "
                    "supply the business vocabulary, the one step this pass cannot infer."
                ),
            },
            "machine": config,
            # The external perimeter, same shape as the faithful bundle's, so the
            # boundary (endpoints, directions, and the FIELDS crossing) draws here too.
            "interface": self._business_interface(business_names),
            # The same content, indexed for reading rather than drawing.
            "businessStates": business_states,
            "entry": entry,
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

    # -- serialization as a real XState config ------------------------------
    #
    # The distillation IS a state machine (it has an entry, states, and guarded
    # transitions), so it is emitted as one: a renderer that draws the faithful bundle
    # draws this identically, with no special-casing. Everything the report shape
    # carried that XState has no slot for - role, the collapsed `via` path,
    # suggestedName, the stripped internal steps - rides in `meta`, which is exactly
    # where a renderer already looks for provenance and perimeter data.

    _ENTRY = "__ENTRY__"

    def _business_interface(self, business_names: List[str]) -> dict:
        """The external perimeter, re-anchored onto the surviving states.

        Same shape as the faithful bundle's `interface`, so a renderer draws the
        boundary here exactly as it does there - typed endpoint nodes, and arrows to
        the state that performs each crossing, labelled with the fields that cross.

        Boundary states are never collapsed (a perimeter makes a state `boundary`), so
        an event's host survives. The exceptions are re-anchored honestly: the program's
        own parameter events hang off the machine's `initial`, which is often technical,
        so they move to the synthetic `__ENTRY__`; anything else whose host did not
        survive is kept with `collapsedHost` rather than dropped.
        """
        keep = set(business_names)
        initial = self.config.get("initial")
        events: List[dict] = []
        for e in self.iface.get("events", []):
            ev = dict(e)
            host = e.get("state")
            if host in keep:
                pass
            elif host == initial or host == "__ENTRY__":
                ev["state"] = self._ENTRY          # the caller contract, at the entry
            else:
                ev["collapsedHost"] = host          # honest: host collapsed away
                ev["state"] = self._ENTRY
            events.append(ev)
        used = {e["endpoint"] for e in events}
        return {
            "endpoints": [ep for ep in self.iface.get("endpoints", [])
                          if ep["endpoint"] in used],
            "events": events,
            "parameters": self.iface.get("parameters", {}),
        }

    def _guard_label(self, guards: List[dict]) -> Optional[str]:
        """A single edge label from the guards accumulated along a collapsed path.
        XState allows one guard per transition; the full list stays in meta."""
        names = [g["name"] for g in guards if g.get("kind") == "business"] \
            or [g["name"] for g in guards]
        if not names:
            return None
        return names[0] if len(names) == 1 else " AND ".join(names)

    def _as_machine(self, business_states: Dict[str, dict], entry: List[dict],
                    transitions: List[dict]) -> dict:
        states: Dict[str, dict] = {}
        for name, s in business_states.items():
            node: dict = {}
            meta = {k: v for k, v in s.items() if k != "role"}
            meta["role"] = s["role"]
            if s["role"] == "terminal":
                node["type"] = "final"
            if s.get("gets") or s.get("creates"):
                # Tagged on the node itself, like the faithful bundle does, so a
                # consumer reading only `machine` still sees the boundary and the
                # fields crossing it without joining to `interface`.
                meta["perimeter"] = ("input-output" if s.get("gets") and s.get("creates")
                                     else "input" if s.get("gets") else "output")
                meta["inputFields"] = sorted({f["name"]
                                              for a in s.get("boundaryActions", [])
                                              if a["direction"] == "get"
                                              for f in a.get("fields", [])})
                meta["outputFields"] = sorted({f["name"]
                                               for a in s.get("boundaryActions", [])
                                               if a["direction"] == "create"
                                               for f in a.get("fields", [])})
            node["meta"] = meta
            states[name] = node

        for t in transitions:
            src = states.get(t["from"])
            if src is None or src.get("type") == "final":
                continue
            edge: dict = {}
            g = self._guard_label(t["guards"])
            if g:
                edge["guard"] = g
            edge["target"] = t["to"]
            edge["meta"] = {"via": t["via"], "guards": t["guards"],
                            "events": t["events"], "label": t["label"]}
            src.setdefault("always", []).append(edge)

        # A synthetic entry node: the collapse can reach several first business states
        # under different guards, which one XState `initial` cannot express.
        if entry:
            ent: dict = {"meta": {"role": "entry",
                                  "note": "synthetic: the program's first business "
                                          "state(s) after collapsing scaffolding"}}
            for e in entry:
                edge = {}
                g = self._guard_label(e["guards"])
                if g:
                    edge["guard"] = g
                edge["target"] = e["to"]
                edge["meta"] = {"via": e["via"], "guards": e["guards"],
                                "events": e["events"]}
                ent.setdefault("always", []).append(edge)
            states[self._ENTRY] = ent

        cfg: dict = {"id": f"{self.machine.program_id}__business", "states": states}
        if entry:
            cfg["initial"] = self._ENTRY
        elif states:
            cfg["initial"] = next(iter(states))
        return cfg


def build_business_view(machine: Machine) -> dict:
    """Return the business-view distillation overlay for ``machine`` (pure read; see module)."""
    return _BusinessView(machine).build()
