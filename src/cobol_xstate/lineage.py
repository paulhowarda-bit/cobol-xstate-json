"""Stage 6 (projection) - field lineage across the program's external boundary.

Answers, for every external event and every field crossing it: *which event is
responsible for this field's state?* An input event's fields are filled BY that event;
an output event's fields are traced BACK to the event(s) whose data ultimately reached
them. The result is one row per ``(external event, field)``:

    event  direction  field     changedByProgram  changedBy        origins
    WRITE  output     OUT-FEE   true              COMPUTE @line 25  [GET.CALLER.CALLER,
                                                                    GET.CONSOLE.SYSIN]

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
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from . import interface as _iface
from .emitter import _para_of, _target_owner
from .statechart import Machine

# An origin: (event name, maybe?, program that would resolve a `maybe`).
Origin = Tuple[str, bool, Optional[str]]
OriginSet = FrozenSet[Origin]
# field -> the origins reaching it at a program point.
State = Dict[str, OriginSet]

_WORD = re.compile(r"[A-Z][A-Z0-9-]*")
# Verbs whose data effect is opaque (the value semantics are not modeled) but whose
# *dependency* is plain from the operands - which is all lineage needs.
_DEP_ONLY = {"STRING", "UNSTRING", "INSPECT"}
_UNKNOWN = "<unknown>"


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
        raw: Dict[str, dict] = {}
        for name, _region, st in _iface._iter_states(self.config):
            raw[name] = st
        self.states, self.origin_state = self._split(raw)
        self.succs = self._successors()

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
                leaves = [t for t in edges
                          if t not in self.states or not self._owns(t, owner)]
                if leaves or not edges:              # falls out of the range, or ends
                    returns.setdefault(s2, [])
                    if cont not in returns[s2]:
                        returns[s2].append(cont)
        for s, conts in returns.items():
            succ[s] = list(conts)                    # the return replaces fall-through
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
        """
        out: Dict[str, List[dict]] = {}
        for name, st in self.states.items():
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
                        e = {"action": aname, "line": prov.get("line", 0)}
                        if e not in out.setdefault(t, []):
                            out[t].append(e)
                if spec and spec.get("kind") == "effect":
                    verb = (spec.get("verb") or "").upper()
                    if verb in _DEP_ONLY:
                        recv, _ = _dep_only_flow(verb, prov.get("cobol", ""), self.known)
                        for t in recv:
                            e = {"action": aname, "line": prov.get("line", 0)}
                            if e not in out.setdefault(t, []):
                                out[t].append(e)
        return out

    def run(self) -> dict:
        self.changers = self._changers()
        initial = self.config.get("initial")
        entries = [initial] if initial else []
        if self.config.get("type") == "parallel":
            entries = [r.get("initial") for r in self.config["states"].values()
                       if r.get("initial")]
        entries = [e for e in entries if e in self.states]

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
                "Nothing is invented - see 'flags'."
            ),
            "rows": rows,
            "flags": self.flags,
        }


def build_lineage(machine: Machine) -> dict:
    """Field lineage across the external boundary. Pure read over the emitted machine."""
    return _Lineage(machine).run()
