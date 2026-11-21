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

from . import VIEW_SCHEMA_VERSION
from . import interface as _iface
from .errors import ViewBudgetExceeded
from .emitter import (
    _para_of, _target_owner, edge_target, iter_transitions, order_index,
    perform_target as _perform_target,
)
from .statechart import Machine


# --------------------------------------------------------------------------- #
# guard / action classification
# --------------------------------------------------------------------------- #

def _is_control_guard(name: str, tree: Optional[dict],
                      kinds: Optional[Dict[str, str]] = None) -> bool:
    """Does this guard ride on control-flow mechanics rather than a business condition?

    The answer is now RECORDED, not guessed: the compiler tags every guard it synthesizes
    from a loop's exit test, a file's end-of-stream / INVALID KEY, an ON SIZE ERROR /
    EXCEPTION handler, or SEARCH AT END as ``control`` in ``semantics['guardKinds']`` -
    at the one point where the kind is known for certain. When the tag is present it is
    authoritative.

    The old name/tree heuristic that stood in for it was wrong in BOTH directions, and
    the tag exists to end that. It missed every mechanical guard whose name did not end
    in ``atend`` (``INVALID KEY``, ``AT END-OF-PAGE``, ``ON SIZE ERROR`` all read as
    *business decisions*), and it treated every unparsed ``{op:'raw'}`` condition as
    control, silently deleting real business branches the parser simply could not model
    yet. Both are removed. The residual name test survives only as a fallback for guards
    that carry no tag - guards minted by another producer (the reactive lowering) or by
    data predating the tag - and it deliberately no longer folds ``raw`` into control.
    """
    tag = (kinds or {}).get(name)
    if tag is not None:
        return tag == "control"
    return name.startswith("UNTIL_") or name.lower().endswith("atend")


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
    """Outgoing (label, target) pairs. label = {'guard': name} or {'event': name} or {}.

    Bare-string handler targets (``on: {EVENT: "__H_x"}`` from
    ``statechart._build_handlers_region``) are followed too - handling only the dict form
    silently dropped every watch->handler edge. Single-sourced via emitter.iter_transitions
    / edge_target so this and the other transition walkers cannot drift on that again."""
    out: List[Tuple[dict, str]] = []
    for ev, e in iter_transitions(st):
        tgt = edge_target(e)
        if not tgt:
            continue
        if ev is None:
            lab = {"guard": e["guard"]} if isinstance(e, dict) and e.get("guard") else {}
        else:
            lab = {"event": ev}
        out.append((lab, tgt))
    return out


#: How many walk steps one business view may take before it is refused, by default.
#:
#: The collapse walk can need exponentially many steps: a technical region whose paragraphs
#: are both PERFORMed and fallen into doubles its distinct stacks at every level, and a
#: program whose PERFORMs could recurse is walked per whole stack (`_call_sites`). One
#: count covers both - every step costs about 10 us and at most about 1 KB while its walk
#: is live - so 250,000 is a few seconds and at most a couple of hundred MB. No example
#: program needs more than 35, and an 11,000-line generated one needs 5,614.
#:
#: Exceeding it is a FAILURE of this view, never a shorter answer: a cut-off walk would
#: return a transition list that reads as complete, and nothing downstream could tell.
#: `ViewBudgetExceeded` says how far it got and how to lift the limit; 0 means none.
BUSINESS_BUDGET = 250_000


class _BusinessView:
    def __init__(self, machine: Machine, budget: int = BUSINESS_BUDGET):
        self.machine = machine
        self.config = machine.config
        self.states: Dict[str, dict] = self.config.get("states", {})
        self.guards: Dict[str, dict] = machine.semantics.get("guards", {})
        self.guard_kinds: Dict[str, str] = machine.semantics.get("guardKinds", {})
        self.actions: Dict[str, dict] = machine.semantics.get("actions", {})
        self.provenance = machine.provenance
        iface = machine.interface()
        self.iface = iface
        self.perimeter = iface["perimeterStates"]
        self.files = getattr(machine, "files", {}) or {}
        self._dv = _iface._DataView(machine.data, machine.fd_records)
        self._cursors = _iface._cursor_tables(machine.provenance)
        # host-var <-> Db2 column correlation for a cursor FETCH; without it a FETCH's
        # boundary action carried no columns (only the interface build passed this).
        self._cursor_cols = _iface._cursor_columns(machine.semantics, machine.provenance)
        self._cursor_derivs = _iface._cursor_derivations(machine.semantics)
        self._cursor_refs = _iface._cursor_table_refs(machine.provenance)
        # ...and the whole-stream scan, for a DECLARE that never became an action.
        _iface.seed_cursor_maps(self._cursors, self._cursor_cols, self._cursor_derivs,
                                getattr(machine, "sql_cursors", None), self._cursor_refs)
        self.ordered: List[str] = machine.paragraph_order
        self.sections: Dict[str, List[str]] = getattr(machine, "sections", {}) or {}
        # Built once: every step of the walk resolves a PERFORM target through it.
        self._index = order_index(self.ordered)
        self.finals = {n for n, st in self.states.items() if st.get("type") == "final"}
        self.flags: List[str] = []
        self._business_memo: Dict[str, bool] = {}
        self._quiet = False           # the call-site pre-pass steps without flagging
        self._under_memo: Dict[tuple, tuple] = {}
        self._budget = budget
        self._steps = 0
        self._found: List[dict] = []         # the flow's transitions, for the refusal
        self._contexts = 0

    def _flag(self, msg: str) -> None:
        if self._quiet:
            return
        if msg not in self.flags:
            self.flags.append(msg)

    # -- classification -----------------------------------------------------
    def role(self, name: str, st: dict) -> str:
        if st.get("type") == "final":
            return "terminal"
        boundary = name in self.perimeter
        decision = any(
            e.get("guard") and not self._control_guard(e["guard"])
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

    def _control_guard(self, name: str) -> bool:
        return _is_control_guard(name, self.guards.get(name), self.guard_kinds)

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
        return [_perform_target(a, self.ordered, self.sections, self._index)
                for a in (st.get("entry", []) or []) if a.startswith("perform_")]

    def _continuation(self, st: dict) -> Optional[str]:
        """Where control resumes after a PERFORM returns = the call state's fall-through."""
        for e in st.get("always", []) or []:
            if e.get("target"):
                return e["target"]
        return None

    def _guard_dict(self, name: str) -> dict:
        tree = self.guards.get(name)
        return {"name": name, "condition": tree, "field": _guard_field(tree),
                "kind": "control" if self._control_guard(name) else "business"}

    def _step(self, state: str, stack: tuple, check_recursion: bool = True):
        """Yield ``(label, (next_state, next_stack))`` for one control step from a config.

        ``check_recursion=False`` is for the call-site walk, which runs only once
        `_call_sites` has shown that no PERFORM can land inside a range already on the
        stack - so the check could never fire, and it would need the whole stack."""
        st = self.states.get(state, {})
        if state in self.finals:                       # program end, or a return
            if stack and stack[-1][1] is not None:
                yield ({}, (stack[-1][1], stack[:-1]))
            return
        performs = self._perform_names(st)
        if performs:                                   # a call
            name = performs[0]
            owner, init = _target_owner(name, self.ordered, self.sections, self._index)
            cont = self._continuation(st)
            if len(performs) > 1:
                self._flag(f"{state}: {len(performs)} PERFORMs in one state; "
                           f"only {name} followed")
            if owner is None or init is None or init not in self.states:
                self._flag(f"PERFORM {name}: target unresolved; call not followed")
                if cont is not None:
                    yield ({}, (cont, stack))
            elif check_recursion and any(owner & fo for fo, _ in stack):
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

    def _call_sites(self):
        """What can sit under each PERFORM frame - or ``None`` when the per-stack walk
        has to be kept.

        A frame is a call site: (the range it owns, where it returns to). This is
        reachability over (state, top frame) from the initial state, and while it runs
        it records, for every frame pushed, which frame was on top at the time. Those
        records are the whole truth about the stacks: a stack is realizable exactly when
        each frame in it was pushed on top of the one beneath it somewhere, because
        nothing a PERFORMed range does before it returns depends on what is under it.
        So a state reached with a given top frame is reached under EVERY stack that frame
        can sit on, and the business walk can carry the top frame alone. The count of
        (state, top frame) pairs is at most states times call sites, where the count of
        stacks was exponential in the nesting depth.

        "Nothing depends on what is under it" has one exception: the recursion check,
        which refuses a PERFORM of a range that overlaps one already on the stack. If any
        realizable stack could trigger it, this returns ``None`` and the flow keeps the
        per-stack walk, whose answer it would otherwise change.
        """
        initial = self.config.get("initial")
        below: Dict[tuple, Dict[Optional[tuple], None]] = {}
        returned: Dict[tuple, None] = {}    # frames some walk returns out of
        seen = set()
        work: List[tuple] = []

        def reach(state, top):
            if (state, top) not in seen:
                seen.add((state, top))
                work.append((state, top))

        reach(initial, None)
        self._quiet = True
        try:
            while work:
                state, top = work.pop()
                stack = () if top is None else (top,)
                for _, (nxt, nstack) in self._step(state, stack, False):
                    if len(nstack) > len(stack):                  # a PERFORM
                        frame = nstack[-1]
                        lows = below.setdefault(frame, {})
                        if top not in lows:
                            lows[top] = None
                            if frame in returned:
                                reach(frame[1], top)
                        reach(nxt, frame)
                    elif len(nstack) < len(stack):                # its return
                        returned[top] = None
                        for low in below.get(top, ()):
                            reach(nxt, low)
                    else:
                        reach(nxt, top)
        finally:
            self._quiet = False
        return None if self._may_recurse(below) else below

    @staticmethod
    def _may_recurse(below) -> bool:
        """Could some realizable stack hold two frames whose ranges overlap?

        Asked over the ranges rather than the frames: an edge X -> Y says a range Y was
        PERFORMed while range X was on top. That graph only adds paths (two frames with
        the same range but different call sites become one node), so a "yes" can be
        spurious - which costs speed, never correctness - and a "no" is a proof.
        Overlap happens on a cycle, or between two distinct ranges sharing a paragraph
        (a THRU span and a paragraph inside it, say) with a path from one to the other.
        """
        up: Dict[frozenset, Dict[frozenset, None]] = {}
        nodes: Dict[frozenset, None] = {}
        for frame, lows in below.items():
            nodes[frame[0]] = None
            for low in lows:
                if low is not None:
                    up.setdefault(low[0], {})[frame[0]] = None
                    nodes[low[0]] = None
        colour: Dict[frozenset, int] = {}                 # 1 on the path, 2 finished
        for root in nodes:
            if root in colour:
                continue
            colour[root] = 1
            path = [(root, iter(up.get(root, ())))]
            while path:
                node, it = path[-1]
                nxt = next(it, None)
                if nxt is None:
                    colour[node] = 2
                    path.pop()
                elif colour.get(nxt) == 1:
                    return True                           # a cycle: a range in itself
                elif nxt not in colour:
                    colour[nxt] = 1
                    path.append((nxt, iter(up.get(nxt, ()))))
        owners: Dict[str, List[frozenset]] = {}
        for node in nodes:
            for para in node:
                owners.setdefault(para, []).append(node)
        reach_memo: Dict[frozenset, set] = {}

        def reaches(src):
            got = reach_memo.get(src)
            if got is None:
                got, todo = set(), [src]
                while todo:
                    for n in up.get(todo.pop(), ()):
                        if n not in got:
                            got.add(n)
                            todo.append(n)
                reach_memo[src] = got
            return got

        for nodes_here in owners.values():
            for i, x in enumerate(nodes_here):
                for y in nodes_here[i + 1:]:
                    if y in reaches(x) or x in reaches(y):
                        return True
        return False

    def _next_business(self, cfg: tuple, below=None) -> List[dict]:
        """`_walk` from a config, in the call-site form when ``below`` is given.

        In that form a config's stack is its top frame alone, standing for every stack
        that frame can sit on (`_call_sites`). A walk can run on that, because nothing it
        does depends on what is under the top frame - until it returns out of it. So it
        runs with the bottom frame *hidden*, and if it ever returns out of it, the walk is
        thrown away and run again once per frame that can sit there (``None``: nothing),
        now one frame deeper. Every walk that finishes is therefore exactly the walk from
        each real stack it stands for - same visiting order, same pruning, same results -
        and only the walks that genuinely need more of the stack pay for it.

        Without ``below`` this is the walk from the config's whole stack.
        """
        if below is None:
            return self._walk(cfg[0], cfg[1], False, None)
        state, stack = cfg
        results: List[dict] = []
        contexts: List[tuple] = [(stack, bool(stack))]
        while contexts:
            ctx, hidden = contexts.pop()
            got = self._walk(state, ctx, hidden, below)
            if got is None:
                # Pushed in reverse so they run in `below`'s order.
                deeper = []
                for low in below.get(ctx[0], ()):
                    if low is None:
                        deeper.append((ctx, False))
                    else:
                        chain, more = self._unique_under(low, below)
                        deeper.append((chain + (low,) + ctx, more))
                contexts.extend(reversed(deeper))
            else:
                results.extend(got)
        return results

    def _unique_under(self, frame: tuple, below) -> tuple:
        """``(chain, hidden)``: the frames that MUST be under ``frame``, bottom first,
        down to the first one with a choice of callers (``hidden``) or none at all.

        A restarted walk takes that chain whole. Knowing more of the stack never changes
        a walk until it returns that far, and a frame with one possible caller offers no
        choice to restart over. Without this a 400-deep PERFORM chain restarted its walk
        400 times, one level deeper each time. Memoized: ``below`` is fixed for the run.
        """
        got = self._under_memo.get(frame)
        if got is None:
            chain: List[tuple] = []
            hidden, f = True, frame
            while True:
                lows = below.get(f, ())
                if len(lows) != 1:
                    break
                low = next(iter(lows))
                if low is None:
                    hidden = False
                    break
                chain.append(low)
                f = low
            got = self._under_memo[frame] = (tuple(reversed(chain)), hidden)
        return got

    def _walk(self, src_state: str, src_stack: tuple, hidden: bool, below) -> \
            Optional[List[dict]]:
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

        if (src_state in self.finals) and not src_stack:
            return results
        # `hidden`: src_stack[0] stands for whatever is under it, so returning out of it
        # is a question this walk cannot answer - it answers None, and is run again
        # deeper (`_next_business`). The stack never drops below that frame otherwise:
        # a return out of a pushed frame leaves it in place.

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
        for lab, nxt in self._step(src_state, src_stack, below is None):
            if hidden and not nxt[1]:
                return None
            g0 = (self._guard_dict(lab["guard"]), None) if lab.get("guard") else None
            e0 = (lab["event"],) if lab.get("event") else ()
            names = frozenset([lab["guard"]] if lab.get("guard") else [])
            heapq.heappush(heap, (len(names), seq, nxt, names, g0, e0, None))
            seq += 1

        while heap:
            self._steps += 1
            if self._budget and self._steps > self._budget:
                raise ViewBudgetExceeded(
                    "business", self._budget, "walk steps", "--business-budget",
                    f"stopped walking from {src_state} after {self._contexts:,} call "
                    f"context(s) and {len(self._found):,} business transition(s)")
            _, _, c, names, guards, events, via = heapq.heappop(heap)
            state, stack = c
            prior = arrived.setdefault((state, stack, events), [])
            if any(p <= names for p in prior):
                continue
            prior.append(names)
            if ((state in self.finals) and not stack) or self._is_business(state):
                results.append({"to": state,
                                "to_config": c if below is None else (state, stack[-1:]),
                                "guards": _chain(guards),
                                "events": list(events), "via": _chain(via)})
                continue
            via2 = (state, via)
            for lab, nxt in self._step(state, stack, below is None):
                if hidden and not nxt[1]:
                    return None
                g2 = (self._guard_dict(lab["guard"]), guards) if lab.get("guard") else guards
                e2 = events + (lab["event"],) if lab.get("event") else events
                n2 = names | {lab["guard"]} if lab.get("guard") else names
                heapq.heappush(heap, (len(n2), seq, nxt, n2, g2, e2, via2))
                seq += 1
        return results

    def _build_flow(self) -> Tuple[List[dict], List[dict]]:
        """Collapse the machine into (entry edges, business transitions) by reachability over
        configurations from the initial state - so a technical initial (a loop head) and every
        performed paragraph are followed correctly.

        A configuration's stack is its top PERFORM frame alone when `_call_sites` allows
        it, the whole stack otherwise. The transitions are the same set either way; the
        call-site form can find them in a different order, and keep a different one of
        several collapsed paths (`via`) for the same edge."""
        initial = self.config.get("initial")
        entry: List[dict] = []
        transitions: List[dict] = self._found
        if initial not in self.states:
            return entry, transitions

        below = self._call_sites()
        worklist: List[tuple] = []
        if self._is_business(initial):
            entry.append({"to": initial, "guards": [], "events": [], "via": []})
            worklist.append((initial, ()))
        else:
            for nb in self._next_business((initial, ()), below):
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
            self._contexts = len(seen_cfg)
            state, stack = cfg
            if (state in self.finals) and not stack:
                continue
            for nb in self._next_business(cfg, below):
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
                                    self.files, self._cursors, self._cursor_cols,
                                    self._cursor_derivs, self._cursor_refs)
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
                    if hit.get("columns"):    # a cursor FETCH's host-var <-> Db2 column map
                        ba["columns"] = hit["columns"]
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
            if g and not self._control_guard(g):
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
            # Distinct from the default bundle's `xstate-v5-config`, which this view
            # and the reactive one both used to declare. Three payloads with different
            # schemas and one name meant a consumer holding a JSON object could not tell
            # which it had (upstream ledger batch 10, item 30b).
            "format": "cobol-xstate-business",
            "formatVersion": VIEW_SCHEMA_VERSION,
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


def build_business_view(machine: Machine, budget: int = BUSINESS_BUDGET) -> dict:
    """Return the business-view distillation overlay for ``machine`` (pure read; see module).

    Raises `ViewBudgetExceeded` when the walk needs more than ``budget`` steps (0: no
    limit) - see `BUSINESS_BUDGET`."""
    return _BusinessView(machine, budget).build()
