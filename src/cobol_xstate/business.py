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

import copy
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
#: The collapse walk visits each (state, top PERFORM frame) at most once per source state
#: and business guard set, so most programs need steps in proportion to their size. The
#: exception is a program whose PERFORMs could recurse: it is walked per whole stack
#: (`_call_sites`), and stacks can be exponentially many. A state with several call
#: contexts is walked a second time, from all of them at once, for each transition's
#: shortest route (`_build_flow`). Every step costs about 10 us, so 250,000 is a few
#: seconds. No example program needs more than 33, an 11,000-line generated one needs
#: 9,213, a thousand-report splitter 143,131, and 400 overlapping PERFORM ... THRU
#: ranges 2,403 (`_closed`).
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
        self._control_memo: Dict[str, bool] = {}
        self._quiet = False           # the call-site pre-pass steps without flagging
        self._budget = budget
        self._steps = 0
        self._found: List[dict] = []         # the flow's transitions, for the refusal
        self._arrived: Dict[str, set] = {}   # per source state: places walked (`_walk`)
        self._contexts = 0
        # Frame refinement (`_region_tables`, `_fit`): which states can reach no
        # PERFORM, the lowest paragraph each can still reach, and the refined frames.
        self._region: set = set()
        self._lo: Dict[str, float] = {}
        self._refined: Dict[tuple, tuple] = {}
        self._frames: Dict[tuple, tuple] = {}
        self._returns: Dict[tuple, bool] = {}       # refined config -> can it return
        self._spans: Dict[frozenset, Optional[Tuple[int, int]]] = {}
        self._calls: Dict[str, tuple] = {}       # call state -> its resolved PERFORM
        self._pushed: Dict[str, tuple] = {}      # call state -> the frame it pushes
        self._below: Optional[dict] = None          # `_call_sites`' answer, once known
        # class -> the classes its first walk found inside its frame; None: cut short
        self._inside: Dict[tuple, Optional[list]] = {}
        self._closed_set: set = set()                # classes `_closed` has proven
        self._cut = False                            # did the last `_walk` hit `arrived`

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
        got = self._control_memo.get(name)
        if got is None:
            got = self._control_memo[name] = \
                _is_control_guard(name, self.guards.get(name), self.guard_kinds)
        return got

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

    def _step(self, state: str, stack: tuple, check_recursion: bool = True,
              fit: bool = False):
        """Yield ``(label, (next_state, next_stack))`` for one control step from a config.

        ``check_recursion=False`` is for the call-site walk, which runs only once
        `_call_sites` has shown that no PERFORM can land inside a range already on the
        stack - so the check could never fire, and it would need the whole stack.

        ``fit`` refines the next config's top frame to the part of its range still
        reachable (`_fit`). Only `_region_returns` steps that way: the business walk
        steps over frames as PERFORMed, so its configs are the ones the view has always
        been built from, and refinement only decides which of them need no walk
        (`_closed`)."""
        out = self._fit if fit else (lambda s, k: k)
        st = self.states.get(state, {})
        if state in self.finals:                       # program end, or a return
            if stack and stack[-1][1] is not None:
                ret = stack[-1][1]
                yield ({}, (ret, out(ret, stack[:-1])))
            return
        call = self._calls.get(state)
        if call is None:
            # Resolved once per state: a THRU range's paragraph set is as long as the
            # range, and every walk through the call state asked for it again.
            performs = self._perform_names(st)
            call = self._calls[state] = (performs,) + (
                _target_owner(performs[0], self.ordered, self.sections, self._index)
                + (self._continuation(st),) if performs else ())
        performs = call[0]
        if performs:                                   # a call
            name = performs[0]
            owner, init, cont = call[1], call[2], call[3]
            if len(performs) > 1:
                self._flag(f"{state}: {len(performs)} PERFORMs in one state; "
                           f"only {name} followed")
            if owner is None or init is None or init not in self.states:
                self._flag(f"PERFORM {name}: target unresolved; call not followed")
                if cont is not None:
                    yield ({}, (cont, out(cont, stack)))
            elif check_recursion and any(owner & fr[0] for fr in stack):
                self._flag(f"recursive PERFORM {name}; not followed")
                if cont is not None:
                    yield ({}, (cont, out(cont, stack)))
            else:
                frame = self._pushed.get(state)
                if frame is None:
                    frame = self._pushed[state] = (frozenset(owner), cont)
                yield ({}, (init, out(init, stack + (frame,))))
            return
        for lab, target in _successors(st):            # ordinary edges
            if target not in self.states:
                continue
            if stack:
                owner, ret = stack[-1][0], stack[-1][1]
                if _para_of(target) not in owner or target == "__END__":   # leaves -> return
                    if ret is not None:
                        yield (lab, (ret, out(ret, stack[:-1])))
                else:
                    yield (lab, (target, out(target, stack)))
            else:
                yield (lab, (target, stack))

    # -- frame refinement ---------------------------------------------------
    #
    # Restart-from-step code PERFORMs STEP-k THRU STEP-EXIT for every k, so the frame of
    # each range is a different config at every step it holds: N ranges, N^2/2 configs,
    # every later step walked once per range that holds it (a 400-step program needed
    # 644,000 walk steps, over the budget). But at STEP-j the steps before j cannot be
    # reached again, and a frame's range is only ever asked one question - does this
    # target leave it - about targets the walk can still reach. So a frame is refined to
    # the part of its range that can still be reached, and at STEP-j every range from an
    # earlier step is the same frame: STEP-j THRU STEP-EXIT.
    #
    # Only where no PERFORM can be reached (a "region" state): a frame's range there is
    # never asked about again by a PERFORM's recursion test, nor does anything get
    # PERFORMed on top of it, so what sits under a frame is still keyed by the frame as
    # PERFORMed (`_call_sites`). A refined frame carries that along - ``(range, return
    # point, what it sits on)`` - so two configs fall in one CLASS only when everything
    # they can still do is the same: the same reachable range, the same return point,
    # and the same frames, in the same order, to return under.
    #
    # The class is NOT the config the view is built from. Merging configs into their
    # class changes the order the worklist meets them, and the order of the transitions
    # list is that order: a frame reached a second way, while the first way's configs
    # still wait on the worklist, has its own copies walked first. So the walk stays over
    # frames as PERFORMed, and the class only answers one question (`_closed`): would
    # walking this config change anything? A config whose class has been walked, and
    # every class it can reach without leaving its frame too, would find no transition
    # and push only more such configs - its walk, and the cascade under it, is the
    # quadratic work, and it is skipped. Everything else is walked as before, in the same
    # order, so the view is the one the unrefined walk gives, byte for byte.

    def _region_tables(self) -> None:
        """Fill ``_region`` (the states from which no PERFORM is reachable) and ``_lo``
        (per state, the lowest paragraph position of any target reachable from it).

        One pass over the flat graph's strongly connected components (iterative Tarjan,
        which emits a component only after every component it reaches)."""
        states = self.states
        succ = {n: [t for _, t in _successors(st) if t in states]
                for n, st in states.items()}
        inf = float("inf")
        index: Dict[str, int] = {}
        low: Dict[str, int] = {}
        on: set = set()
        path: List[str] = []
        comp: Dict[str, int] = {}
        has_call: List[bool] = []
        lo: List[float] = []
        n = 0
        for root in states:
            if root in index:
                continue
            index[root] = low[root] = n
            n += 1
            path.append(root)
            on.add(root)
            work = [(root, iter(succ[root]))]
            while work:
                v, it = work[-1]
                for w in it:
                    if w not in index:
                        index[w] = low[w] = n
                        n += 1
                        path.append(w)
                        on.add(w)
                        work.append((w, iter(succ[w])))
                        break
                    if w in on:
                        low[v] = min(low[v], index[w])
                else:
                    work.pop()
                    if work:
                        u = work[-1][0]
                        low[u] = min(low[u], low[v])
                    if low[v] == index[v]:
                        c = len(has_call)
                        members = []
                        while True:
                            w = path.pop()
                            on.discard(w)
                            comp[w] = c
                            members.append(w)
                            if w == v:
                                break
                        call, least = False, inf
                        for m in members:
                            if any(a.startswith("perform_")
                                   for a in (states[m].get("entry", []) or [])):
                                call = True
                            for t in succ[m]:
                                least = min(least, self._index.get(_para_of(t), inf))
                                ct = comp[t]
                                if ct != c:
                                    call = call or has_call[ct]
                                    least = min(least, lo[ct])
                        has_call.append(call)
                        lo.append(least)
        self._region = {s for s in states if not has_call[comp[s]]}
        self._lo = {s: lo[comp[s]] for s in states}

    def _fit(self, state: str, stack: tuple) -> tuple:
        """``stack`` with its top frame refined to the paragraphs of its range that can
        still be reached from ``state`` - when ``state`` is a region state.

        Once `_call_sites` has answered, a refined frame is ``(range, return point,
        what it sits on)``, the last taken from the frame as PERFORMed; before that (and
        when the per-stack walk is kept, where nothing is hidden under a frame) it is
        ``(range, return point)``."""
        if not stack or state not in self._region:
            return stack
        top = stack[-1]
        key = (top, self._lo[state])
        got = self._refined.get(key)
        if got is None:
            owner, ret = top[0], top[1]
            lo = key[1]
            span = self._span_of(owner)
            if span is not None:
                first = max(span[0], lo)
                kept = _Span(first, span[1], self._index) if first <= span[1] \
                    else frozenset()
            else:
                kept = frozenset(p for p in owner if self._index[p] >= lo)
                if len(kept) == len(owner):
                    kept = owner
            if len(top) == 3:
                got = (kept, ret, top[2])
            elif self._below is not None:
                got = (kept, ret, tuple(self._below.get(top, ())))
            else:
                got = (kept, ret)
            got = self._frames.setdefault(got, got)
            self._refined[key] = got
        return stack if got is top else stack[:-1] + (got,)

    def _span_of(self, owner) -> Optional[Tuple[int, int]]:
        """``(first, last)`` paragraph positions when ``owner`` is every paragraph
        between two positions - a PERFORM ... THRU range, or one already refined - so
        refining it is two numbers, not a copy of the rest of the range. ``None`` for any
        other set, and whenever the paragraph order repeats a name (a position then does
        not name one paragraph)."""
        if isinstance(owner, _Span):
            return owner.first, owner.last
        if len(owner) < 2 or len(self._index) != len(self.ordered):
            return None
        got = self._spans.get(owner, False)
        if got is False:
            at = list(map(self._index.__getitem__, owner))
            first, last = min(at), max(at)
            got = self._spans[owner] = \
                (first, last) if last - first + 1 == len(owner) else None
        return got

    def _region_returns(self, state: str, frame: tuple) -> bool:
        """Can control return out of ``frame`` from ``(state, frame)``, a region config?

        A property of the config, not of whichever frame was refined into it, so it is
        answered once: the unknown configs reachable are collected, then "returns" is
        propagated back from each that steps out of the frame. Whatever is left cannot."""
        start = (state, frame)
        if start in self._returns:
            return self._returns[start]
        preds: Dict[tuple, List[tuple]] = {}
        order: List[tuple] = []
        local = {start}
        todo = [start]
        yes: List[tuple] = []
        while todo:
            c = todo.pop()
            order.append(c)
            for _, (nxt, nstack) in self._step(c[0], (c[1],), False, fit=True):
                if not nstack:
                    yes.append(c)
                    continue
                d = (nxt, nstack[-1])
                known = self._returns.get(d)
                if known is not None:
                    if known:
                        yes.append(c)
                    continue
                preds.setdefault(d, []).append(c)
                if d not in local:
                    local.add(d)
                    todo.append(d)
        true = set(yes)
        while yes:
            for p in preds.get(yes.pop(), ()):
                if p not in true:
                    true.add(p)
                    yes.append(p)
        for c in order:
            self._returns[c] = c in true
        return self._returns[start]

    def _class_of(self, cfg: tuple):
        """The class of a business-walk config (see the notes above `_region_tables`):
        its state and its stack with the top frame refined - or ``None`` when the state
        can still reach a PERFORM, or nothing is on the stack, and the config is a class
        of its own."""
        state, stack = cfg
        if not stack or state not in self._region:
            return None
        return (state, self._fit(state, stack))

    def _closed(self, cls) -> bool:
        """Would walking a config of class ``cls`` now change anything?

        No, when every class it can reach without leaving its frame (``_inside``, what
        the first walk of each class found there) has been walked, and none of those
        walks was cut short by a place another walk had already passed (``None``). A
        config of a walked class is a copy of the walked one under another frame as
        PERFORMed: its steps are the same, so are the transitions it finds, and every
        place it can reach outside its frame has been passed already (the frames of a
        class return to the same point, under the same frames). All its walk can do is
        push copies of what the walked one found inside the frame, and for a closed
        class those are configs of closed classes too. Once closed, always closed."""
        if cls in self._closed_set:
            return True
        seen = {cls}
        todo = [cls]
        while todo:
            c = todo.pop()
            if c in self._closed_set:
                continue
            inside = self._inside.get(c)
            if inside is None:              # not walked yet, or its walk was cut short
                return False
            for d in inside:
                if d not in seen:
                    seen.add(d)
                    todo.append(d)
        self._closed_set.update(seen)
        return True

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
                work.append((state, top, top is not None and state in self._region))

        reach(initial, None)
        self._quiet = True
        try:
            while work:
                state, top, region = work.pop()
                if region:
                    # A frame entering a region (see `_fit`): nothing is PERFORMed in
                    # there, so all the walk through it can do is return. Asked once per
                    # refined config, not walked once per frame - which is the same
                    # thing, at the same point, as the walk it replaces: every config
                    # that walk visited was a region config, so between here and its
                    # first return nothing else could happen.
                    cls = self._fit(state, (top,))[0]
                    if top[1] is not None and self._region_returns(state, cls):
                        returned[top] = None
                        for low in below.get(top, ()):
                            reach(top[1], low)
                    continue
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
        # Two ranges sharing a paragraph with a path from one to the other: asked from
        # the path side. Pairing every two ranges that share a paragraph instead was
        # cubic in restart-from-step code, where N ranges each hold every later step -
        # 10.7 million pairs for 400 steps, none of them with a path.
        for x in up:
            got, todo = set(), [x]
            while todo:
                for n in up.get(todo.pop(), ()):
                    if n not in got:
                        got.add(n)
                        todo.append(n)
            for y in got:
                if not x.isdisjoint(y):
                    return True
        return False

    def _walk(self, cfgs: List[tuple], below, arrived: set) -> List[dict]:
        """From a business config, walk through technical configs (following calls and
        returns) to the next business / terminal configs, collecting the guards, events
        and technical states (``via``) met on the way.

        **One route per business step.** Every state the walk steps THROUGH is
        technical, and a technical state has no business guard on its edges (that is what
        makes it technical), so only the source's own first step can carry one - an IF's
        branch, an EVALUATE's WHEN. Past it a route only picks up control guards: loop
        exits, AT END, ON OVERFLOW. So the walk is plain reachability, once per first
        step: a place reached once under a set of business guards is not walked again
        under the same set, whichever loops the later route left on the way. The route
        kept is the one with the fewest guards, because the walk is ordered by guard
        count, and every place a later route would go, the kept one goes too.

        Comparing whole guard sets instead - as this did - kept every route whose loop
        exits differed, and returning up through nested PERFORM ... UNTIL loops every
        combination of exits differs: k loops per paragraph, d levels deep, made k^(d-1)
        transitions between one pair of states, and walked all of them (3 loops, 6
        levels: 5,469 transitions among 20 business states; 10 levels exceeded three
        million steps). They were one business step each.

        **One walk per config, whatever is under it.** In the call-site form (``below``
        given, see `_call_sites`) a config's stack is its top frame alone, standing for
        every stack that frame can sit on. Nothing the walk does depends on what is
        under that frame until it returns out of it - and then it continues, in the same
        walk, at the frame's return point once for each frame that can sit there
        (``None``: nothing, the top level), each again standing for everything under it.
        A config is ``(state, stack, hidden)``: ``hidden`` says the bottom frame of
        ``stack`` stands for an unknown rest. Re-running the walk one frame deeper for
        each possible caller, as this did, walked the part before the return again for
        every full stack - k^d of them for k callers per level, d levels deep. Without
        ``below`` the stack is the whole stack and nothing is hidden.

        **Several configs of one state at once.** ``cfgs`` are configs of the same
        state, and the walk starts from all of them together, so the route it keeps to
        each place is the one with the fewest guards from ANY of them (see
        `_build_flow`). ``arrived`` holds ``(config, events, business guards)`` of every
        place already walked; the caller decides how widely it is shared.

        Over an EXPLICIT worklist, not the interpreter's stack: as recursion a chain of
        a hundred nested PERFORMs overflowed the default 1000-frame limit, and the
        RecursionError took every companion view down with it.

        Each result's ``guards`` and ``via`` are still cons chains (`_chain` reads them),
        and ``business`` is its set of business guard names.
        """
        results: List[dict] = []
        src_state = cfgs[0][0]
        callsite = below is not None

        # `via` and `guards` ride as CONS CHAINS - (head, rest) - rather than lists:
        # extending a list copies it, so a long path paid for itself again at every
        # step. Only the chains that become results are flattened.
        # (guard count, tie-break, config, business guards, guards, events, via). The
        # tie-break is the insertion counter: breadth-first within a guard count, and no
        # two configs ever compared. Deepest-first follows a loop round and round, while
        # breadth-first reaches the loop's exits immediately.
        heap: List[tuple] = []
        seq = 0
        # Was the walk cut short inside its own frame, at a place an earlier walk had
        # already passed (`_closed` then never takes this walk as a class's template)?
        start = cfgs[0][1]
        mine: set = set()
        self._cut = False

        def push(lab, nxt, hidden, count, biz, guards, events, via):
            nonlocal seq
            guard = lab.get("guard")
            if guard:
                guards = (self._guard_dict(guard), guards)
                count += 1
                if not self._control_guard(guard):
                    biz = biz | {guard}
            if lab.get("event"):
                events = events + (lab["event"],)
            state, stack = nxt
            if hidden and not stack:
                # Returned out of the frame standing for the unknown rest: carry on at
                # its return point under each frame that can be there.
                for low in below.get(cur_stack[0], ()):
                    heapq.heappush(heap, (count, seq, (state, (low,) if low else (),
                                                       low is not None),
                                          biz, guards, events, via))
                    seq += 1
            else:
                heapq.heappush(heap, (count, seq, (state, stack, hidden),
                                      biz, guards, events, via))
                seq += 1

        for state, cur_stack in cfgs:
            if (state in self.finals) and not cur_stack:
                continue
            for lab, nxt in self._step(state, cur_stack, not callsite):
                push(lab, nxt, callsite and bool(cur_stack), 0, frozenset(), None, (),
                     None)

        while heap:
            self._steps += 1
            if self._budget and self._steps > self._budget:
                raise ViewBudgetExceeded(
                    "business", self._budget, "walk steps", "--business-budget",
                    f"stopped walking from {src_state} after {self._contexts:,} call "
                    f"context(s) and {len(self._found):,} business transition(s)")
            count, _, c, biz, guards, events, via = heapq.heappop(heap)
            state, stack, hidden = c
            key = (c, events, biz)
            if key in arrived:
                if stack == start and key not in mine:
                    self._cut = True
                continue
            arrived.add(key)
            mine.add(key)
            if ((state in self.finals) and not stack) or self._is_business(state):
                results.append({"to": state,
                                "to_config": (state, stack[-1:] if callsite else stack),
                                "guards": guards, "business": biz,
                                "events": list(events), "via": via})
                continue
            via2 = (state, via)
            cur_stack = stack
            for lab, nxt in self._step(state, stack, not callsite):
                push(lab, nxt, hidden, count, biz, guards, events, via2)
        return results

    def _build_flow(self) -> Tuple[List[dict], List[dict]]:
        """Collapse the machine into (entry edges, business transitions) by reachability over
        configurations from the initial state - so a technical initial (a loop head) and every
        performed paragraph are followed correctly.

        A configuration's stack is its top PERFORM frame alone when `_call_sites` allows
        it, the whole stack otherwise. The transitions are the same set either way; the
        call-site form can find them in a different order, and keep a different one of
        several collapsed paths (`via`) for the same edge.

        A transition is one business step: (from, to, its BUSINESS guards, events). A
        second route for the same step - reached from another call context, through
        other loop exits, or with the same guards met in another order - is not a
        second transition.

        **The route a transition shows is its fewest-guard one from ANY of the state's
        call contexts**, which takes two passes. The first finds the configs and the
        transitions. The walks from one state's configs share where they have been
        (``_arrived``): a place one of them reached under some business guards, another
        reaching it under the same guards finds nothing new - the same targets, so the
        same steps and the same configs to walk next. A shared read paragraph PERFORMed
        from a thousand places is a thousand configs, all returning into the same
        main-line loops; walked once per config, that was a thousand times a thousand
        steps. Never shared across source STATES: a place one state's walk reached is
        still a real step from another.

        But sharing hands each place to whichever config got there first, and that
        config's route was the one kept. From the read paragraph under report 1, the
        route on to report 1,000 exits 999 report loops, so every transition from the
        read to a later report was written out with up to 999 loop exits - when under
        the report just before it, the same step exits one. So the second pass walks
        each state that has more than one config ONCE, from all of them together, in
        order of guard count: the first route to reach a step is the fewest-guard one
        from any context, and it replaces the first pass's. Which transitions there are
        does not change, only the route each one shows."""
        initial = self.config.get("initial")
        entry: List[dict] = []
        transitions: List[dict] = self._found
        if initial not in self.states:
            return entry, transitions

        self._region_tables()
        below = self._call_sites()
        # From here a refined frame carries what it sits on (`_fit`, `_class_of`).
        self._below = below
        self._refined.clear()
        worklist: List[tuple] = []
        if self._is_business(initial):
            entry.append({"to": initial, "guards": [], "events": [], "via": []})
            worklist.append((initial, ()))
        else:
            for nb in self._walk([(initial, ())], below, set()):
                entry.append({"to": nb["to"], "guards": _chain(nb["guards"]),
                              "events": nb["events"], "via": _chain(nb["via"])})
                worklist.append(nb["to_config"])

        seen_cfg: set = set()
        tr_at: Dict[tuple, dict] = {}                 # step key -> its transition
        cfgs_of: Dict[str, List[tuple]] = {}          # state -> its configs, in order
        while worklist:
            cfg = worklist.pop()
            if cfg in seen_cfg:
                continue
            cls = self._class_of(cfg)
            if cls is not None and self._closed(cls):
                # A copy of a walked config under another frame as PERFORMed, with
                # nothing it could reach left unwalked: walking it would find no
                # transition and push only more of the same (`_closed`).
                continue
            seen_cfg.add(cfg)
            self._contexts = len(seen_cfg)
            state, stack = cfg
            if (state in self.finals) and not stack:
                continue
            cfgs_of.setdefault(state, []).append(cfg)
            found = self._walk([cfg], below, self._arrived.setdefault(state, set()))
            if cls is not None and cls not in self._inside:
                self._inside[cls] = None if self._cut else [
                    self._class_of(nb["to_config"]) for nb in found
                    if nb["to_config"][1] == stack]
            for nb in found:
                k = (state, nb["to"], nb["business"], tuple(nb["events"]))
                if k not in tr_at:
                    tr_at[k] = {"from": state, "to": nb["to"], "via": nb["via"],
                                "guards": nb["guards"], "events": nb["events"],
                                "label": None}
                    transitions.append(tr_at[k])
                worklist.append(nb["to_config"])
        self._arrived.clear()

        for state, cfgs in cfgs_of.items():
            if len(cfgs) < 2:
                continue
            best: set = set()
            for nb in self._walk(cfgs, below, set()):
                k = (state, nb["to"], nb["business"], tuple(nb["events"]))
                if k not in best and k in tr_at:
                    best.add(k)
                    tr_at[k]["guards"], tr_at[k]["via"] = nb["guards"], nb["via"]
        for t in transitions:
            t["guards"], t["via"] = _chain(t["guards"]), _chain(t["via"])
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
        tables = _Tables()
        for t in entry + transitions:
            t["guards"] = [tables.guard(g) for g in t["guards"]]
            t["via"] = [tables.state(n) for n in t["via"]]
        config = self._as_machine(business_states, entry, transitions, tables)

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
            # What the ids in `entry`, `transitions` and the edges' labels index - each
            # thing written once, however many routes use it (see `_Tables`).
            "guards": tables.guards,
            "stateNames": tables.states,
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

    @staticmethod
    def _guard_label(ids: List[int], tables: "_Tables", route: int) -> Optional[str]:
        """A single edge label from the guards (ids) accumulated along a collapsed path:
        its business guards, else all of them (`_label_guards`). XState allows one guard
        per transition, so one guard is named as itself, and more than one as
        ``guards#<route>`` - the index of the route the edge's ``meta`` names - rather
        than every name joined with AND, which on a route through a thousand report
        loops was a label of 998 names. The label is a function of the route, so it
        points there instead of writing the guards a second time."""
        chosen = _label_guards(ids, tables.guards)
        if not chosen:
            return None
        if len(chosen) == 1:
            return tables.guards[chosen[0]]["name"]
        return f"{_GUARD_SET_LABEL}{route}"

    def _as_machine(self, business_states: Dict[str, dict], entry: List[dict],
                    transitions: List[dict], tables: "_Tables") -> dict:
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

        # An edge names its transition (`meta.transition`, an index into `transitions`)
        # instead of carrying a second copy of its route, guards and events.
        for i, t in enumerate(transitions):
            src = states.get(t["from"])
            if src is None or src.get("type") == "final":
                continue
            edge: dict = {}
            g = self._guard_label(t["guards"], tables, i)
            if g:
                edge["guard"] = g
            edge["target"] = t["to"]
            edge["meta"] = {"transition": i}
            src.setdefault("always", []).append(edge)

        # A synthetic entry node: the collapse can reach several first business states
        # under different guards, which one XState `initial` cannot express.
        if entry:
            ent: dict = {"meta": {"role": "entry",
                                  "note": "synthetic: the program's first business "
                                          "state(s) after collapsing scaffolding"}}
            for i, e in enumerate(entry):
                edge = {}
                g = self._guard_label(e["guards"], tables, i)
                if g:
                    edge["guard"] = g
                edge["target"] = e["to"]
                edge["meta"] = {"entry": i}               # an index into `entry`
                ent.setdefault("always", []).append(edge)
            states[self._ENTRY] = ent

        cfg: dict = {"id": f"{self.machine.program_id}__business", "states": states}
        if entry:
            cfg["initial"] = self._ENTRY
        elif states:
            cfg["initial"] = next(iter(states))
        return cfg


class _Span:
    """The paragraphs at positions ``first`` .. ``last`` of the paragraph order: a
    refined PERFORM ... THRU range (`_BusinessView._fit`), held as its bounds. It answers
    the one question a frame's range is asked - is this paragraph in it - and is equal to
    another span with the same bounds."""

    __slots__ = ("first", "last", "_index")

    def __init__(self, first: int, last: int, index: Dict[str, int]) -> None:
        self.first, self.last, self._index = first, last, index

    def __contains__(self, para: str) -> bool:
        i = self._index.get(para)
        return i is not None and self.first <= i <= self.last

    def __eq__(self, other) -> bool:
        return isinstance(other, _Span) and \
            (self.first, self.last) == (other.first, other.last)

    def __hash__(self) -> int:
        return hash((_Span, self.first, self.last))


#: The prefix of an edge label standing for its route's guards: ``guards#<i>``, where
#: ``i`` is the index the edge's ``meta`` names (`_BusinessView._guard_label`).
_GUARD_SET_LABEL = "guards#"


def _label_guards(ids: list, guards: list) -> list:
    """The guards an edge's label stands for: its route's business guards, else all."""
    return [i for i in ids if guards[i]["kind"] == "business"] or list(ids)


class _Tables:
    """The business view's interned tables: each guard and each technical state
    written ONCE, and every route pointing at them by index.

    A route is the guards and technical states one business step passes through, and
    on a report splitter they are long and genuine: the step from the main paragraph to
    report k exits k-1 report loops, so a thousand reports meant half a million guards
    on the routes - each written out whole, condition tree and all, for 33 distinct
    ones, and every route written twice (once more on its chart edge). That was 926 MB
    for one program. Indexes into these tables say exactly the same thing:
    `expand_business_view` rebuilds the full form from them.
    """

    def __init__(self) -> None:
        self.guards: List[dict] = []
        self.states: List[str] = []
        self._guard_ids: Dict[str, int] = {}
        self._state_ids: Dict[str, int] = {}

    def guard(self, g: dict) -> int:
        i = self._guard_ids.get(g["name"])
        if i is None:
            i = self._guard_ids[g["name"]] = len(self.guards)
            self.guards.append(g)
        return i

    def state(self, name: str) -> int:
        i = self._state_ids.get(name)
        if i is None:
            i = self._state_ids[name] = len(self.states)
            self.states.append(name)
        return i


def expand_business_view(view: dict) -> dict:
    """The business view with every index resolved: a copy in which each route's
    ``guards`` are the guard objects and its ``via`` the state names, each chart edge's
    ``meta`` carries its route again, and a ``guards#N`` label is the names of the
    guards it stands for joined with AND - the form ``formatVersion`` 4 wrote. It holds exactly what the
    view holds, for a reader that wants the full form and can afford its size."""
    v = copy.deepcopy(view)
    guards = v.pop("guards")
    names = v.pop("stateNames")
    ids = {}                                  # route -> its guard ids, before resolving
    for t in v["entry"] + v["transitions"]:
        ids[id(t)] = t["guards"]
        t["guards"] = [dict(guards[i]) for i in t["guards"]]
        t["via"] = [names[i] for i in t["via"]]
    for st in v["machine"]["states"].values():
        for edge in st.get("always", []) or []:
            m = edge.get("meta") or {}
            if "transition" in m:
                route = v["transitions"][m["transition"]]
                edge["meta"] = {"via": route["via"], "guards": route["guards"],
                                "events": route["events"], "label": route["label"]}
            else:
                route = v["entry"][m["entry"]]
                edge["meta"] = {"via": route["via"], "guards": route["guards"],
                                "events": route["events"]}
            label = edge.get("guard")
            if label and label.startswith(_GUARD_SET_LABEL):
                edge["guard"] = " AND ".join(
                    guards[i]["name"] for i in _label_guards(ids[id(route)], guards))
    return v


def build_business_view(machine: Machine, budget: int = BUSINESS_BUDGET,
                        stats: Optional[dict] = None) -> dict:
    """Return the business-view distillation overlay for ``machine`` (pure read; see module).

    Raises `ViewBudgetExceeded` when the walk needs more than ``budget`` steps (0: no
    limit) - see `BUSINESS_BUDGET`.

    ``stats``, when given, receives ``walkSteps``: the walk steps this view took, the
    unit the budget counts. On a refusal it is the budget itself - every step allowed
    was taken - so a run can say how far it got as well as that it stopped."""
    view = _BusinessView(machine, budget)
    try:
        return view.build()
    finally:
        if stats is not None:
            stats["walkSteps"] = min(view._steps, budget) if budget else view._steps
