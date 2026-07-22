"""Stage 6 (projection) - field lineage across the program's external boundary.

Answers, for every external event and every field crossing it: *which event is
responsible for this field's state?* An input event's fields are filled BY that event;
an output event's fields are traced BACK to the event(s) whose data ultimately reached
them. The result is one row per ``(external event, field)``:

    event  direction  field     changedByProgram  changedBy        origins
    WRITE  output     OUT-FEE   true              COMPUTE @line 25  [GET.CALLER.CALLER,
                                                                    GET.CONSOLE.SYSIN]

Each row also carries the ``conditions`` under which its event happens at all. Origins are
only half a business rule - "DAILYPOST changes the balance" is a dependency, "DAILYPOST
changes the balance WHEN the transaction is a deposit" is the rule - and the when-clause
is the half a requirements reader actually needs.

"Changed by a LINKAGE item" is not a separate column: reading a linkage field already
*is* a ``GET.CALLER.CALLER`` event, so it appears in ``origins`` like any other source.

The analysis is a **flow-sensitive** reaching-origins fixpoint over the emitted state
graph: only origins that actually reach an event are reported (an assignment on a branch
that cannot reach this event is excluded). PERFORM is followed as a call and returned
from, using the same target resolution as the runnable emitter, so a value produced
inside a performed paragraph is traced correctly.

Honest limits, all surfaced in ``flags`` rather than guessed:

* **Context-insensitive.** A paragraph performed from two sites is analyzed once with
  the *merged* incoming state, so an origin from call site A can appear at an event
  reached only via call site B. This over-approximates (never misses a real origin, may
  name an extra one) - the safe direction for provenance.
* **CALL ... USING** passes BY REFERENCE by default, and the callee is a different
  program: it *may* rewrite any argument. Those arguments get the CALL as a ``maybe``
  origin naming the program that would resolve it.
* **REDEFINES byte-aliasing, unresolved/multi-dimension subscripts, and paragraphs that
  failed to parse** break a chain. The field is marked ``unknown`` rather than reported
  as having no origin - "we cannot trace this" and "nothing external feeds this" are
  very different answers.
"""

from __future__ import annotations

import re
from collections import deque
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from . import interface as _iface
from .business import _is_control_guard
from .emitter import _para_of, _target_owner
from .statechart import Machine

# An origin: (event name, maybe?, program that would resolve a `maybe`).
Origin = Tuple[str, bool, Optional[str]]
OriginSet = FrozenSet[Origin]
# field -> the origins reaching it at a program point.
State = Dict[str, OriginSet]

# A condition: (guard name, is it the NEGATED sense?). Negation is first-class because
# the else / WHEN OTHER branch carries no guard of its own - its condition is precisely
# the negation of the branches before it, and that negation is usually the business rule
# worth reading ("when the transaction is none of D/W/I").
Cond = Tuple[str, bool]
# Readable form. The fixpoint carries the same sets packed into ints - see
# `_Lineage._intern_conditions` - and `_conds` unpacks one back to this.
CondSet = FrozenSet[Cond]

_WORD = re.compile(r"[A-Z][A-Z0-9-]*")
# Verbs whose data effect is opaque (the value semantics are not modeled) but whose
# *dependency* is plain from the operands - which is all lineage needs.
_DEP_ONLY = {"STRING", "UNSTRING", "INSPECT"}
_UNKNOWN = "<unknown>"
# `IN-FILE_atEnd` / `IN-FILE_notAtEnd`: the READ lowering's synthetic end-of-stream guards.
_ATEND = re.compile(r"^(.*?)_(not)?[Aa]t[Ee]nd$")


# --------------------------------------------------------------------------- #
# operand extraction
# --------------------------------------------------------------------------- #

def _operands(expr: str, known: Set[str]) -> List[str]:
    """Field names referenced by an expression (literals/keywords dropped)."""
    out = []
    for w in _WORD.findall((expr or "").upper()):
        if w in known and w not in out:
            out.append(w)
    return out


_STRING_RE = re.compile(r"\bINTO\s+([A-Z0-9-]+)", re.I)
_UNSTRING_INTO = re.compile(r"\bINTO\b(.*?)(?:\bWITH\b|\bTALLYING\b|\bON\b|$)", re.I | re.S)
_INSPECT_TALLY = re.compile(r"\bTALLYING\s+([A-Z0-9-]+)", re.I)


def _dep_only_flow(verb: str, text: str, known: Set[str]) -> Tuple[List[str], List[str]]:
    """``(receivers, sources)`` for STRING / UNSTRING / INSPECT.

    Lineage needs only *which fields feed which* - not the concatenation/split/count
    semantics - so the operands are enough to keep the chain intact.
    """
    up = (text or "").upper()
    if verb == "STRING":            # STRING a b ... INTO c
        m = _STRING_RE.search(up)
        recv = [m.group(1)] if m and m.group(1) in known else []
        head = up[:m.start()] if m else up
        return recv, [w for w in _operands(head, known) if w not in recv]
    if verb == "UNSTRING":          # UNSTRING src ... INTO a b c
        m = _UNSTRING_INTO.search(up)
        recv = _operands(m.group(1), known) if m else []
        head = up[:m.start()] if m else up
        return recv, [w for w in _operands(head, known) if w not in recv]
    if verb == "INSPECT":           # INSPECT x TALLYING n ... / REPLACING ...
        m = _INSPECT_TALLY.search(up)
        recv = [m.group(1)] if m and m.group(1) in known else []
        src = _operands(up, known)
        subject = src[0] if src else None
        if subject and "REPLACING" in up and subject not in recv:
            recv = recv + [subject]     # REPLACING rewrites the subject in place
        return recv, [w for w in src if w not in recv]
    return [], []


# --------------------------------------------------------------------------- #
# condition rendering
# --------------------------------------------------------------------------- #

def _cond_text(name: str, tree: Optional[dict], negated: bool) -> Optional[str]:
    """A guard as COBOL-ish source text, or None if its test is not recoverable.

    The negated sense is rendered as ``NOT (...)`` rather than by flipping the operator.
    Inverting ``=`` to ``NOT =`` is safe, but inverting an ordering test is not always
    the identity a reader assumes once COBOL's figurative constants and class tests are
    in play - and this table exists to be trusted, not to look tidy. The same reasoning
    leaves ``NOT (F NOT AT END)`` un-simplified: clumsy to read, impossible to misread.
    """
    body: Optional[str] = None
    if isinstance(tree, dict):
        op = tree.get("op")
        if op == "rel":
            left, rel, right = tree.get("left"), tree.get("rel"), tree.get("right")
            if left is not None and rel is not None and right is not None:
                body = f"{left} {rel} {right}"
        elif op == "cond-name":
            body = tree.get("name") or None
        # {op:'raw'} and anything new fall through: say nothing rather than guess.
    elif tree is None:
        # A file's end-of-stream guard is synthesized by the READ lowering, so it has no
        # expression tree - but its meaning is not in doubt and it is not a mystery to be
        # flagged. `_atEnd` is the AT END arm, `_notAtEnd` the NOT AT END one.
        m = _ATEND.match(name)
        if m:
            body = f"{m.group(1)} {'NOT AT END' if m.group(2) else 'AT END'}"
    if not body:
        return None
    return f"NOT ({body})" if negated else body


# --------------------------------------------------------------------------- #
# the analysis
# --------------------------------------------------------------------------- #

class _Lineage:
    def __init__(self, machine: Machine):
        self.m = machine
        self.config = machine.config
        self.actions = machine.semantics.get("actions", {})
        self.provenance = machine.provenance
        self.data = machine.data or {}
        self.known: Set[str] = {n.upper() for n, it in self.data.items()
                                if isinstance(it, dict)
                                and it.get("kind") != "condition-name"}
        self.ordered = machine.paragraph_order
        self.sections = getattr(machine, "sections", {}) or {}
        self.files = getattr(machine, "files", {}) or {}
        self.dv = _iface._DataView(self.data)
        self.cursors = _iface._cursor_tables(self.provenance)
        self.flags: List[str] = []
        # Primitive provenance facts, recorded during the final (row-emitting) pass so a
        # backward query can answer "where did THIS item's value come from" for an item
        # that never crosses the boundary itself - a dynamic CALL target being the case
        # that matters. Recorded HERE, from the fixpoint's own steps, rather than
        # re-derived elsewhere: a second traversal would drift from this one, and the
        # drift would be invisible (a plausible chain that is not the real one).
        self.fills: List[dict] = []       # field <- an external input event
        self.flow: List[dict] = []        # target <- source operands, at one write site
        self.dynamic_sites: List[dict] = []   # unresolved dynamic targets + their origins
        raw: Dict[str, dict] = {}
        for name, _region, st in _iface._iter_states(self.config):
            raw[name] = st
        self.states, self.origin_state = self._split(raw)
        # (owner-exit node -> the edges that leaving/replaced it), filled by _successors
        # and read by _edge_conditions so a return edge keeps the condition of the branch
        # it stands in for.
        self.fold_src: Dict[str, List[str]] = {}
        self.fold_dst: Dict[str, List[str]] = {}
        self.succs = self._successors()
        self.guards = machine.semantics.get("guards", {}) or {}
        self.guard_line: Dict[str, int] = {}
        self.edge_cond = self._edge_conditions()
        self.entries = self._entries()
        self.cond_list, self.edge_bits = self._intern_conditions()
        # Two passes over the same graph. MUST (meet = intersection) is what we report:
        # a condition that holds on EVERY path to the event, so stating it is always
        # true. MAY (meet = union) is only used to decide whether an empty MUST is
        # honest - see `_conditions_of`.
        self.must = self._cond_flow(lambda a, b: a & b)
        self.may = self._cond_flow(lambda a, b: a | b)

    # -- split states at PERFORM boundaries ---------------------------------
    #
    # A state's `entry` is a folded run of straight-line actions, and a PERFORM can sit
    # in the MIDDLE of it: ``[ACCEPT, MOVE, perform_X, WRITE]``. Analyzing that as one
    # node would run the WRITE with pre-call origins. Split each run into a chain so the
    # call happens between the actions either side of it - the same shape the runnable
    # emitter builds with its invoke nodes.

    def _split(self, raw: Dict[str, dict]) -> Tuple[Dict[str, dict], Dict[str, str]]:
        out: Dict[str, dict] = {}
        origin: Dict[str, str] = {}      # split node -> the real state it came from
        for name, st in raw.items():
            entry = list(st.get("entry", []) or [])
            performs = [i for i, a in enumerate(entry) if a.startswith("perform_")]
            control = {k: v for k, v in st.items() if k != "entry"}
            if not performs:
                out[name] = dict(st)
                origin[name] = name
                continue
            segs: List[List[str]] = []
            cur: List[str] = []
            for a in entry:
                if a.startswith("perform_"):
                    segs.append(cur)
                    segs.append([a])     # the call, alone
                    cur = []
                else:
                    cur.append(a)
            segs.append(cur)
            segs = [s for s in segs if s]
            ids = [name] + [f"{name}__L{i}" for i in range(1, len(segs))]
            for i, seg in enumerate(segs):
                nid = ids[i]
                origin[nid] = name
                if i + 1 < len(segs):
                    out[nid] = {"entry": seg,
                                "always": [{"target": ids[i + 1]}]}
                else:
                    out[nid] = {"entry": seg, **control}
            if segs and not segs[-1][0].startswith("perform_"):
                continue
            # a trailing PERFORM: the control edges ride a final empty node
            tail = f"{name}__Lend"
            out[ids[-1]] = {"entry": segs[-1], "always": [{"target": tail}]}
            out[tail] = dict(control)
            origin[tail] = name
        return out, origin

    def _flag(self, msg: str) -> None:
        if msg not in self.flags:
            self.flags.append(msg)

    # -- path conditions ----------------------------------------------------
    #
    # "Where did this value come from?" is only half a business rule. The other half is
    # "under what condition?" - DAILYPOST changes the balance *when the transaction is a
    # deposit*, and the when-clause IS the rule. These two passes recover it.

    def _entries(self) -> List[str]:
        initial = self.config.get("initial")
        entries = [initial] if initial else []
        if self.config.get("type") == "parallel":
            entries = [r.get("initial") for r in self.config["states"].values()
                       if r.get("initial")]
        return [e for e in entries if e in self.states]

    def _branches(self, st: dict) -> List[list]:
        """The state's transition lists. Each list is one first-match-wins group: the
        `always` list is one, and each event key under `on` is its own."""
        out: List[list] = []
        al = st.get("always")
        if al:
            out.append(al if isinstance(al, list) else [al])
        for v in (st.get("on") or {}).values():
            out.append(v if isinstance(v, list) else [v])
        return out

    def _edge_conditions(self) -> Dict[Tuple[str, str], CondSet]:
        """``(src, target)`` -> the conditions under which that edge is taken.

        A transition list is FIRST-MATCH-WINS, which is exactly how COBOL's IF/ELSE and
        EVALUATE/WHEN lower here: branch *i* is taken when its own guard holds AND every
        guard before it failed. So the condition of an unguarded trailing branch (the
        ELSE, the WHEN OTHER, the loop body) is the conjunction of those negations -
        recovered exactly, not guessed.

        Two edges from one state to one target mean the target is reached under a
        DISJUNCTION, which this conjunctive lattice cannot express; they meet, dropping
        to what both agree on. Anything not recorded here defaults to no condition, which
        under-claims - the safe direction for a table that gets read as fact.
        """
        raw: Dict[Tuple[str, str], CondSet] = {}
        for name, st in self.states.items():
            for group in self._branches(st):
                seen: List[str] = []
                for item in group:
                    if isinstance(item, str):
                        tgt, guard, meta = item, None, {}
                    elif isinstance(item, dict):
                        tgt = item.get("target")
                        guard = item.get("guard")
                        meta = item.get("meta") or {}
                    else:
                        continue
                    cond: CondSet = frozenset((g, True) for g in seen)
                    if isinstance(guard, str) and guard:
                        cond = cond | {(guard, False)}
                        if guard not in self.guard_line and meta.get("cobolLine"):
                            self.guard_line[guard] = meta["cobolLine"]
                        seen.append(guard)
                    if not tgt:
                        continue
                    key = (name, tgt)
                    raw[key] = cond if key not in raw else (raw[key] & cond)
        out = {k: v for k, v in raw.items() if k[1] in self.states}
        # A PERFORM's return edge is a synthetic edge standing in for the real ones that
        # ran off the end of the paragraph - so it inherits their condition. Without
        # this, `IF X ... END-IF` at a paragraph's tail loses the `NOT X` on the way out,
        # the negation never cancels its positive downstream, and the merge below the
        # call looks conditional when it is not.
        for s2, leaving in self.fold_src.items():
            acc: Optional[CondSet] = None
            for lt in leaving:
                c = raw.get((s2, lt), frozenset())
                acc = c if acc is None else (acc & c)
            for cont in self.fold_dst.get(s2, []):
                out[(s2, cont)] = acc if acc is not None else frozenset()
        return out

    def _intern_conditions(self) -> Tuple[List[Cond], Dict[Tuple[str, str], int]]:
        """Pack every condition set into an integer, one bit per ``(guard, polarity)``.

        The fixpoint below meets and joins these sets once per graph edge per worklist
        step, and on a large program a single set holds thousands of conditions. As
        frozensets of tuples, one union re-hashed every element of both operands; as
        integers, ``&`` and ``|`` ARE meet and join, at a word per 64 conditions.
        Measured on a 4,330-state program: 2.11s of set algebra became 0.045s, with the
        step count and the answer both unchanged.

        Bits are handed out in SORTED order, never iteration order, so the packing
        cannot make the emitted view depend on PYTHONHASHSEED.
        """
        cond_list = sorted({c for cs in self.edge_cond.values() for c in cs})
        bit = {c: 1 << i for i, c in enumerate(cond_list)}
        edge_bits: Dict[Tuple[str, str], int] = {}
        for key, conds in self.edge_cond.items():
            mask = 0
            for c in conds:
                mask |= bit[c]
            edge_bits[key] = mask
        return cond_list, edge_bits

    def _conds(self, bits: Optional[int]) -> CondSet:
        """A packed bitmask back to the condition set it stands for.

        Costs one step per condition actually present, not one per bit in the universe,
        so decoding a row is no dearer than the frozenset it replaced.
        """
        out: List[Cond] = []
        while bits:
            low = bits & -bits                      # lowest set bit
            out.append(self.cond_list[low.bit_length() - 1])
            bits ^= low
        return frozenset(out)

    def _cond_flow(self, join) -> Dict[str, Optional[int]]:
        """Forward fixpoint of path conditions, meeting at merge points with ``join``.

        Sets are the bitmasks built by ``_intern_conditions``; ``join`` is ``&`` for
        MUST and ``|`` for MAY. ``None`` means *never reached from any entry*, which is
        a different answer from ``0`` (*reached under no condition*) and stays distinct.

        The worklist is a QUEUE, and that is the whole difference between linear and
        quadratic. Popping the most recently pushed state dives down one branch, carrying
        a set that later merges will cut back, and then re-walks everything downstream of
        it once per revision - measured at 1.9 MILLION pops on a 2400-state program,
        against 4,810 for the same program in queue order, which is barely two per state.
        Breadth-first reaches a state after most of its predecessors have settled, so it
        usually settles on the first visit. The fixpoint is unique and order-independent,
        so this is purely about how much work is wasted getting there. A state already
        queued is not queued again, for the same reason.

        The iteration bound is a PROOF, not a guess, which is what makes tripping it
        meaningful. A state's set is assigned once and thereafter only shrinks (under
        intersection) or only grows (under union), so it can change at most once per
        condition plus once for that first assignment; only a change re-queues it; so
        the queue can be popped at most ``states x (conditions + 1)`` times plus the
        entries. The previous bound, ``max(50_000, states * 500)``, grew LINEARLY in the
        state count while the work grew quadratically, so a large program tripped it and
        silently emitted a half-computed ``conditions`` column behind a generic flag.
        """
        IN: Dict[str, Optional[int]] = {s: None for s in self.states}
        for e in self.entries:
            IN[e] = 0
        work = deque(self.entries)
        queued = set(self.entries)
        steps = 0
        limit = len(self.entries) + len(self.states) * (len(self.cond_list) + 1)
        while work:
            steps += 1
            if steps > limit:
                # Unreachable unless the lattice above stops being monotone. Kept as a
                # guard against a future change silently turning this into a hang.
                self._flag("condition fixpoint exceeded its proven iteration bound; "
                           "'conditions' may be incomplete - this is a defect in the "
                           "analysis, please report this program")
                break
            s = work.popleft()
            queued.discard(s)
            base = IN[s]
            if base is None:
                continue
            for t in self.succs.get(s, []):
                if t not in self.states:
                    continue
                out = base | self.edge_bits.get((s, t), 0)
                cur = IN[t]
                new = out if cur is None else join(cur, out)
                if new != cur:
                    IN[t] = new
                    if t not in queued:
                        work.append(t)
                        queued.add(t)
        return IN

    def _reached(self, state: str) -> bool:
        return self.must.get(state) is not None

    def _conditions_of(self, state: str) -> Tuple[List[dict], bool]:
        """``(conditions, partial)`` for a program point.

        The conditions are the MUST set, which is always sound: every one of them holds
        whenever this point is reached. ``partial`` warns that they are not the WHOLE
        condition - that something else also governs this point which a conjunction
        cannot express.

        The test is which guards appear in MAY but not MUST, and in what polarity:

        * both polarities => it says nothing about this point. Either the branches
          reconverged (after ``IF A ... ELSE ...`` every later state has both `A` and
          `NOT A` behind it, and the event really is unconditional), or it is a guard
          inside a loop whose earlier iterations went the other way - which is history,
          not a condition on this arrival.
        * one polarity only => something really does constrain this point that MUST could
          not keep. ``IF A: PERFORM W`` plus ``IF B: PERFORM W`` reaches W under ``A OR B``:
          no single guard is necessary, yet W plainly does not always happen. `B` shows
          up positive-only, and that is the tell.

        Known limit, and the reason ``partial``'s ABSENCE is not a guarantee: the same
        loop-history effect that correctly cancels a reconverged branch will also cancel
        a genuine disjunction *inside* a loop, so a two-IF disjunction in a loop body
        reports its loop guard and stays silent about the rest. The conditions listed
        stay true; the claim "these are all of them" is best-effort. Stated in the
        output's own note rather than left for a reader to discover.
        """
        must = self._conds(self.must.get(state))
        may = self._conds(self.may.get(state))
        partial = any(not ((g, True) in may and (g, False) in may)
                      for g, _ in (may - must))
        return self._conds_json(must), partial

    def _conds_json(self, conds: CondSet) -> List[dict]:
        out = []
        for name, negated in sorted(conds):
            tree = self.guards.get(name)
            d: dict = {"guard": name, "negated": negated}
            text = _cond_text(name, tree, negated)
            if text is None:
                # ALTER switches, GO TO DEPENDING ON and friends: the branch is real and
                # its existence is a fact, but no expression was recovered for it. Name
                # it and say so - never invent the test.
                d["unrecoverable"] = True
            else:
                d["expr"] = text
                d["kind"] = "control" if _is_control_guard(name, tree) else "business"
            if name in self.guard_line:
                d["line"] = self.guard_line[name]
            out.append(d)
        return out

    # -- control-flow graph (PERFORM followed as call + return) --------------
    def _perform_of(self, st: dict) -> Optional[str]:
        for a in st.get("entry", []) or []:
            if a.startswith("perform_"):
                return a[len("perform_"):]
        return None

    def _edges(self, st: dict) -> List[str]:
        out = [t["target"] for t in (st.get("always", []) or []) if t.get("target")]
        on = st.get("on") or {}
        for v in on.values():
            for item in (v if isinstance(v, list) else [v]):
                if isinstance(item, str):
                    out.append(item)
                elif isinstance(item, dict) and item.get("target"):
                    out.append(item["target"])
        return out

    def _owns(self, node: str, owner: Set[str]) -> bool:
        """Is this (possibly split) node part of the performed paragraph's extent?"""
        return _para_of(self.origin_state.get(node, node)) in owner

    def _successors(self) -> Dict[str, List[str]]:
        """Successor map. A PERFORM node enters its target; every node that would leave
        the performed paragraph's extent returns to the call site's continuation.

        Returns merge across call sites (context-insensitive) - sound for provenance: it
        can name an extra origin, never miss one. Flagged when it actually happens.
        """
        succ: Dict[str, List[str]] = {}
        returns: Dict[str, List[str]] = {}   # owner-exit node -> continuations
        multi: Dict[str, int] = {}
        for name, st in self.states.items():
            target = self._perform_of(st)
            if not target:
                succ[name] = [t for t in self._edges(st) if t in self.states]
                continue
            owner, init = _target_owner(target, self.ordered, self.sections)
            cont = next(iter(self._edges(st)), None)
            if owner is None or init not in self.states:
                self._flag(f"PERFORM {target}: target unresolved; the call is not "
                           f"followed - origins produced inside it are not traced")
                succ[name] = [t for t in self._edges(st) if t in self.states]
                continue
            succ[name] = [init]                      # enter the call
            multi[target] = multi.get(target, 0) + 1
            if cont is None:
                continue
            for s2, st2 in self.states.items():      # wire the returns
                if not self._owns(s2, owner) or self._perform_of(st2):
                    continue
                edges = self._edges(st2)
                # Only the edges that LEAVE the performed range are returns. The ones
                # that stay are ordinary control flow inside the paragraph and must
                # survive: a node can do both at once - `IF X ... END-IF` as a
                # paragraph's last statement branches inward on X and falls out of the
                # range otherwise. Replacing the whole list (as this once did) deleted
                # the inward branch, and with it every event inside the IF.
                stay = [t for t in edges
                        if t in self.states and self._owns(t, owner)]
                leaves = [t for t in edges
                          if t not in self.states or not self._owns(t, owner)]
                if leaves or not edges:              # falls out of the range, or ends
                    if s2 not in returns:
                        returns[s2] = list(stay)
                        self.fold_src[s2] = list(leaves)
                    if cont not in returns[s2]:
                        returns[s2].append(cont)
                        self.fold_dst.setdefault(s2, []).append(cont)
        for s, conts in returns.items():
            succ[s] = list(conts)                    # in-range edges + the return
        for target, n in multi.items():
            if n > 1:
                self._flag(f"{target} is PERFORMed from {n} sites; it is analyzed with "
                           f"the merged incoming state (context-insensitive), so an "
                           f"origin from one call site may appear at an event reached "
                           f"only via another - over-approximate, verify")
        return succ

    # -- transfer -----------------------------------------------------------
    def _apply(self, name: str, st: dict, incoming: State,
               rows: Optional[List[dict]]) -> State:
        """Run a state's entry actions over the origin map. When ``rows`` is given,
        emit a row for every field of every event the state performs."""
        cur: State = dict(incoming)
        for aname in st.get("entry", []) or []:
            prov = self.provenance.get(aname, {})
            cobol = prov.get("cobol", "")
            line = prov.get("line", 0)
            spec = self.actions.get(aname)
            hits = _iface._classify(aname, cobol, spec, self.dv, self.files,
                                    self.cursors)
            got = [h for h in hits if h["direction"] == "get"]
            made = [h for h in hits if h["direction"] == "create"]

            # 1. an INPUT event fills its fields: they originate HERE.
            for h in got:
                ev = _iface._event("get", h["etype"], h["endpoint"])
                for f in h["fields"]:
                    cur[f.upper()] = frozenset({(ev, False, None)})
                if rows is not None:
                    for f in h["fields"]:
                        rows.append(self._row(name, h, "input", f, cur, aname, line))
                        self.fills.append({
                            "field": f.upper(), "event": ev, "endpoint": h["endpoint"],
                            "endpointType": h["etype"], "verb": h["verb"],
                            "state": name, "line": line, "cobol": cobol,
                            # host variable -> COLUMN. A host-variable name is
                            # program-local; the column is the database's, and it is the
                            # column a reader has to go and look at.
                            "columns": h.get("columns") or {},
                        })

            # 2. data movement: the target inherits its operands' origins.
            if spec and spec.get("assignments"):
                for a in spec["assignments"]:
                    tgt = (a.get("target") or "").upper()
                    if not tgt or ":" in tgt:
                        if ":" in tgt:
                            self._flag(f"{tgt}: reference-modified store is not modeled; "
                                       f"lineage through it is unknown")
                            cur[tgt.split("(")[0]] = frozenset({(_UNKNOWN, True, None)})
                        continue
                    base = tgt.split("(")[0]
                    srcs = _operands(a.get("expr", ""), self.known)
                    acc: Set[Origin] = set()
                    for s in srcs:
                        acc |= set(cur.get(s, frozenset()))
                    if got:      # e.g. SELECT ... INTO: already set by the event above
                        continue
                    cur[base] = frozenset(acc)
                    if rows is not None:
                        self.flow.append({
                            "target": base, "sources": list(srcs), "state": name,
                            "line": line, "cobol": cobol, "action": aname,
                        })

            # 3. opaque-but-traceable verbs: dependency only, value not modeled.
            if spec and spec.get("kind") == "effect":
                verb = (spec.get("verb") or "").upper()
                if verb in _DEP_ONLY:
                    recv, srcs = _dep_only_flow(verb, cobol, self.known)
                    if recv:
                        acc = set()
                        for s in srcs:
                            acc |= set(cur.get(s, frozenset()))
                        for r in recv:
                            cur[r] = frozenset(acc)

            # 4. CALL ... USING is BY REFERENCE: the callee may rewrite an argument.
            for h in made:
                if h["etype"] == "program" and h["fields"]:
                    ev = _iface._event("create", h["etype"], h["endpoint"])
                    for f in h["fields"]:
                        fu = f.upper()
                        if fu not in self.known:
                            continue
                        cur[fu] = frozenset(set(cur.get(fu, frozenset()))
                                            | {(ev, True, h["endpoint"])})

            # 5. an OUTPUT event emits fields: report what reaches it.
            if rows is not None:
                for h in made:
                    for f in h["fields"]:
                        rows.append(self._row(name, h, "output", f, cur, aname, line))
                    # 6. an UNRESOLVED dynamic target. The "endpoint" here is the DATA
                    # ITEM whose run-time value names the real resource, so the question
                    # the manifest cannot answer - which program does this call? - turns
                    # into one this analysis can: where does that item's value come from?
                    # Snapshot the origins reaching it AT THE CALL, which is why this is
                    # recorded here and not from the state's entry map: an assignment
                    # earlier in the same state changes the answer.
                    if h.get("dynamic"):
                        self.dynamic_sites.append({
                            "item": str(h["endpoint"]).upper(),
                            "endpointType": h["etype"], "verb": h["verb"],
                            "state": name, "line": line, "cobol": cobol,
                            "candidates": list(h.get("candidates") or []),
                            "origins": self._origins_of(str(h["endpoint"]), cur),
                        })
        return cur

    def _origins_of(self, field: str, cur: State) -> List[Origin]:
        """Origins reaching a field. A GROUP item has no assignments of its own - it is
        the union of its elementary children (moving a group moves all of them)."""
        fu = field.upper()
        if fu in cur:
            return sorted(cur[fu])
        leaves = [l for l in self.dv.leaves(fu) if l != fu]
        if leaves:
            acc: Set[Origin] = set()
            for l in leaves:
                acc |= set(cur.get(l, frozenset()))
            return sorted(acc)
        return []

    def _changed_by(self, field: str) -> List[dict]:
        """Assignments writing this field; for a group, those writing any child."""
        fu = field.upper()
        out = list(self.changers.get(fu, []))
        if not out:
            for l in self.dv.leaves(fu):
                for e in self.changers.get(l, []):
                    if e not in out:
                        out.append(e)
        return out

    def _row(self, state: str, hit: dict, direction: str, field: str,
             cur: State, aname: str, line: int) -> dict:
        fu = field.upper()
        item = self.data.get(fu) or {}
        typ = (item.get("type") or {})
        origins = self._origins_of(fu, cur)
        changed = self._changed_by(fu)
        row = {
            # `program` is on every row, not just at the top of the file: rows from many
            # programs get concatenated to answer "what touches this state?", and a
            # top-level field does not survive that.
            "program": self.m.program_id,
            "event": _iface._event(hit["direction"], hit["etype"], hit["endpoint"]),
            "direction": direction,
            "endpoint": hit["endpoint"],
            "endpointType": hit["etype"],
            "verb": hit["verb"],
            "state": state,
            "line": line,
            "field": fu,
            "pic": typ.get("pic") or typ.get("category"),
            "section": item.get("section"),
            "changedByProgram": bool(changed),
            "origins": [
                {"event": e, **({"maybe": True, "resolvedBy": r} if m else {})}
                for (e, m, r) in origins if e != _UNKNOWN
            ],
        }
        # The cross-program identity keys. `field` alone is a program-LOCAL name: A's
        # WS-BALANCE and B's CUST-BAL may be the same state, or unrelated. What proves
        # sameness is a shared declaration - the copybook the field came from, or the
        # file whose record it belongs to. Emitted only when the code actually proves
        # it; a field declared inline has neither, and is honestly unresolvable.
        if item.get("member"):
            row["member"] = item["member"]
        if item.get("file"):
            row["file"] = item["file"]
        # Under what condition does this event happen at all? For an output row that is
        # "when is this field written out", for an input row "when is it read" - and
        # that when-clause is the business rule the row is otherwise missing.
        conds, partial = self._conditions_of(state)
        if conds:
            row["conditions"] = conds
        if partial:
            row["conditionsPartial"] = True
            row["conditionsNote"] = (
                "reached under differing conditions on different paths; no single "
                "condition holds on all of them, so the full condition is a disjunction "
                "this table does not state - read the machine for the exact branches"
            )
        if changed:
            row["changedBy"] = changed
        if any(e == _UNKNOWN for e, _m, _r in origins):
            row["unknown"] = True
            row["unknownReason"] = ("value passes through a construct whose data effect "
                                    "is not modeled; origin cannot be traced")
        if direction == "output" and not row["origins"] and not row.get("unknown"):
            row["note"] = "no external origin reaches this field (internally set)"
        return row

    # -- the fixpoint -------------------------------------------------------
    def _seed(self) -> State:
        """The origin map at program entry.

        LINKAGE items arrive already filled *by the caller* - that is the whole point of
        a parameter - so they start out originating from the caller. Everything else
        starts with no external origin (its VALUE clause is internal).
        """
        seed: State = {}
        caller = _iface._event("get", "caller", "CALLER")
        for n, it in self.data.items():
            if isinstance(it, dict) and it.get("section") == "LINKAGE" \
                    and it.get("kind") != "condition-name":
                seed[n.upper()] = frozenset({(caller, False, None)})
        return seed

    def _changers(self) -> Dict[str, List[dict]]:
        """field -> the assignments in THIS program that write it.

        An input event's own fill (ACCEPT / SELECT INTO) is NOT a change by this
        program - the value came from outside; the program only received it.

        Each write carries the conditions it happens under, which is what turns "this
        program writes the balance" into "this program writes the balance WHEN the
        transaction is a deposit" - the difference between a dependency and a rule.
        """
        out: Dict[str, List[dict]] = {}
        for name, st in self.states.items():
            entry = self._write_entry(name)
            for aname in st.get("entry", []) or []:
                spec = self.actions.get(aname)
                prov = self.provenance.get(aname, {})
                if spec and spec.get("kind") == "input":
                    continue
                if spec and spec.get("assignments"):
                    for a in spec["assignments"]:
                        t = (a.get("target") or "").upper().split("(")[0]
                        if not t:
                            continue
                        e = {"action": aname, "line": prov.get("line", 0), **entry}
                        if e not in out.setdefault(t, []):
                            out[t].append(e)
                if spec and spec.get("kind") == "effect":
                    verb = (spec.get("verb") or "").upper()
                    if verb in _DEP_ONLY:
                        recv, _ = _dep_only_flow(verb, prov.get("cobol", ""), self.known)
                        for t in recv:
                            e = {"action": aname, "line": prov.get("line", 0), **entry}
                            if e not in out.setdefault(t, []):
                                out[t].append(e)
        return out

    def _write_entry(self, state: str) -> dict:
        """The condition keys for a write site. A site that no path reaches is marked
        rather than annotated: it has no path condition BECAUSE it has no path, and an
        empty `conditions` there would read as "this always happens"."""
        if not self._reached(state):
            return {"unreachable": True}
        conds, partial = self._conditions_of(state)
        e: dict = {}
        if conds:
            e["conditions"] = conds
        if partial:
            e["conditionsPartial"] = True
        return e

    def run(self) -> dict:
        self.changers = self._changers()
        entries = self.entries

        preds: Dict[str, List[str]] = {s: [] for s in self.states}
        for s, ts in self.succs.items():
            for t in ts:
                if t in preds:
                    preds[t].append(s)

        seed = self._seed()
        # None = not yet computed, which is NOT the same as "computed and empty" - the
        # distinction is what lets an all-empty prefix still propagate to successors.
        IN: Dict[str, Optional[State]] = {s: None for s in self.states}
        OUT: Dict[str, Optional[State]] = {s: None for s in self.states}
        work = list(entries)
        steps = 0
        # The lattice is finite (origins over fields), so this terminates; the bound is
        # a backstop against a graph shape we did not anticipate, never normal control.
        limit = max(50_000, len(self.states) * 500)
        while work:
            steps += 1
            if steps > limit:
                self._flag("lineage fixpoint hit its iteration bound; the result may be "
                           "incomplete - please report this program")
                break
            s = work.pop()
            merged: State = dict(seed) if s in entries else {}
            for p in preds.get(s, []):
                if OUT[p] is None:
                    continue
                for f, o in OUT[p].items():
                    merged[f] = merged.get(f, frozenset()) | o
            if IN[s] is not None and merged == IN[s]:
                continue                      # input unchanged - nothing to redo
            IN[s] = merged
            new_out = self._apply(s, self.states[s], merged, None)
            if OUT[s] is None or new_out != OUT[s]:
                OUT[s] = new_out
                work.extend(t for t in self.succs.get(s, []) if t in self.states)

        rows: List[dict] = []
        for s in self.states:
            if IN[s] is None:                 # unreachable from any entry
                continue
            self._apply(s, self.states[s], IN[s], rows)

        return {
            "format": "cobol-xstate-lineage",
            "program": self.m.program_id,
            "source": self.m.source_name,
            "note": (
                "One row per (external event, field). For an INPUT event the fields are "
                "the ones that event FILLS; for an OUTPUT event they are the ones that "
                "FILL it. 'changedByProgram' means this program assigns the field; "
                "'origins' are the external events whose data reaches it here "
                "(flow-sensitive). A linkage-sourced value shows GET.CALLER.CALLER as "
                "an origin. 'maybe' origins name the program that would resolve them. "
                "'conditions' are the guards that hold on EVERY path to this event - a "
                "conjunction, always true when it fires; each also appears on the write "
                "sites in 'changedBy'. They are necessary, not necessarily sufficient: "
                "'conditionsPartial' marks a point known to be governed by more than the "
                "conjunction listed, but its absence is best-effort, not a guarantee "
                "(a disjunction inside a loop body can evade it). 'conditions' "
                "are NOT attached to 'origins': an origin can reach a field through a "
                "chain of assignments, so its real condition is the conjunction along "
                "that whole chain, and reporting any one link's condition would look "
                "like the answer while not being it. Nothing is invented - see 'flags'."
            ),
            "rows": rows,
            "flags": self.flags,
        }


def build_lineage(machine: Machine) -> dict:
    """Field lineage across the external boundary. Pure read over the emitted machine."""
    return _Lineage(machine).run()
