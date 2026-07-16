"""Stage 5b - lower the faithful machine to a REACTIVE (event-driven) XState v5 module.

This is a sibling of ``emitter.py`` (``--target js``). Where that target keeps boundary I/O
synchronous (a ``READ`` is a no-op the golden-master driver fills in, a ``SELECT`` runs and
control falls straight through), the reactive target rewrites the ~5-15% of states that cross
the program boundary into an *event-driven* shape, per ``docs/reactive-target.md``:

  * **inbound get**  (READ / SQL SELECT|FETCH / ACCEPT / CICS RECEIVE / DLI GU|GN)
        -> the state *waits* ``on: { <GET-EVENT> }``; the record arrives as an event and a
           generated ``recv_*`` action assigns its fields into context (PUSH model).
  * **response get** (branch on SQLCODE / SQLSTATE / EIBRESP)
        -> the guarded ``always`` edges move behind ``on: { <RESPONSE-EVENT> }``; ``recv_*``
           assigns the response items into context so the *existing* context guards branch.
  * **outbound create** (WRITE / SQL INSERT|UPDATE|DELETE / DISPLAY / CICS SEND / CALL ...)
        -> the write becomes a ``publish_*`` fire-and-forget effect; control does not await.

Everything NON-perimeter - the decimal ops, the condition guards, the data dictionary, the
internal ``always`` control flow - is reused verbatim from ``emitter.py``. The reactive
machine is a boundary rewrite of the validated IR, not a re-derivation from source.

PERFORM is FLATTENED, not invoked: a queue delivers events to the root actor and XState
does not forward them into invoked children, so a wait buried in a child actor could never
be reached. Every callee is inlined into the one machine and call/return is modeled with a
return-address context field (see ``_flatten``). Recursion is refused rather than emitted
wrong.

Scope of the current slice (see the doc): single-region machines. ``type: parallel``
(CICS HANDLE regions) is refused; create verbs beyond the publish shape are written for
but not proven. Anything unhandled is flagged, never faked.
"""

from __future__ import annotations

import copy
import json
from typing import Dict, List, Optional, Tuple

from . import interface as _iface
from .emitter import (
    RUNTIME_IMPORT, _HELPERS, _build_guards, _build_ops, _collect_referenced,
    _emit_guard, _field_table, _invoke_transform, _js_context, _js_str,
    _negated_externals, _strip_meta,
)
from .statechart import Machine


def _event_slug(event: str) -> str:
    """A JS-identifier-safe token for an event name (``GET.DB2.CUSTOMER`` -> ``GET_DB2_CUSTOMER``)."""
    return "".join(c if c.isalnum() else "_" for c in event)


# Endpoint types whose GET is an actual data crossing (a record/row to receive), as opposed
# to a RESPONSE (SQLCODE/EIBRESP) or a CONDITION (a HANDLEd exception, deferred here).
_DATA_GET_TYPES = {"db2", "file", "console", "terminal", "ims"}

# Verbs that classify as a `get` but do NOT deliver a record: they declare or position a
# channel. Lowering one to an `on` wait would make the machine block for an event that
# never comes - and worse, swallow the first real record. (OPEN INPUT classifies as
# get/file; see interface._classify.)
_NON_WAIT_GET_VERBS = ("OPEN", "STARTBR", "RESETBR", "ENDBR")


def _is_wait_hit(hit: dict) -> bool:
    """True if this boundary crossing is an inbound *record delivery* - the thing PUSH
    replaces with an `on` wait."""
    return (hit["direction"] == "get"
            and hit["etype"] in _DATA_GET_TYPES
            and not hit["verb"].upper().startswith(_NON_WAIT_GET_VERBS))


def _is_read_verb(verb: str) -> bool:
    """A sequential record read, i.e. one whose stream can end (AT END)."""
    v = (verb or "").upper()
    return v.startswith(("READ", "RETURN")) or v in ("FETCH",)


def _events_by_state(iface: dict) -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = {}
    for ev in iface.get("events", []):
        out.setdefault(ev["state"], []).append(ev)
    return out


# --------------------------------------------------------------------------- #
# Pass 1: flatten PERFORM into ONE machine
# --------------------------------------------------------------------------- #
#
# The synchronous target lowers `PERFORM p` to an `invoke` of p's own actor machine
# (emitter._invoke_transform). That cannot work here: queue events are delivered to the
# ROOT actor, and XState does not forward them into invoked children - every nesting
# level would need explicit per-event `sendTo` plumbing, and the deployed service would
# have to understand actor nesting.
#
# So we run the same transform to get the call structure, then INLINE every actor body
# into the single machine and model call/return with a **return-address context field**
# (the mechanism already proven for ALTER switches in statechart._compute_alter_targets:
# a synthetic typed field, a real assignment at the call site, real guards on return):
#
#     call site   entry: [set_ret_X_at_<site>]   always -> X's first state
#     X__RET      always: [ {guard: ret_X_at_<site>, target: <site's continuation>}, ... ]
#
# Context is then genuinely shared - which is *more* faithful to COBOL WORKING-STORAGE
# than the js target's invoke input/output copy-in/copy-out.
#
# Note on `STOP RUN` inside a performed paragraph: the js target reroutes it to the
# actor's return (documented limitation - it resumes the caller). Here the namespaced
# `type: final` state ends the flat machine, which is what COBOL actually does. The
# reactive machine is the more faithful of the two on that path.


def _ns(actor: str, state_id: str) -> str:
    """The id a state of ``actor``'s body takes in the flat machine."""
    return f"{actor}__RET" if state_id == "__RET__" else f"{actor}__{state_id}"


def _retarget(node: dict, actor: str) -> dict:
    """Rewrite every target inside one actor-body state into the actor's namespace."""
    for t in node.get("always", []) or []:
        if t.get("target"):
            t["target"] = _ns(actor, t["target"])
    inv = node.get("invoke")
    if inv and (inv.get("onDone") or {}).get("target"):
        inv["onDone"]["target"] = _ns(actor, inv["onDone"]["target"])
    on = node.get("on")
    if isinstance(on, dict):
        for ev, v in list(on.items()):
            items = v if isinstance(v, list) else [v]
            for item in items:
                if isinstance(item, dict) and item.get("target"):
                    item["target"] = _ns(actor, item["target"])
    return node


def _actor_call_graph(actor_configs: Dict[str, dict]) -> Dict[str, set]:
    """actor target -> the actor targets it invokes."""
    graph: Dict[str, set] = {}
    for name, cfg in actor_configs.items():
        deps = set()
        for st in cfg.get("states", {}).values():
            src = (st.get("invoke") or {}).get("src") or ""
            if src.startswith("actor:"):
                deps.add(src[len("actor:"):])
        graph[name[len("actor:"):]] = deps
    return graph


def _find_cycle(graph: Dict[str, set]) -> Optional[List[str]]:
    """A call cycle (A -> B -> A) if one exists - flattening cannot represent it."""
    colour: Dict[str, int] = {}
    stack: List[str] = []

    def visit(n: str) -> Optional[List[str]]:
        colour[n] = 1
        stack.append(n)
        for m in sorted(graph.get(n, ())):
            if m not in graph:
                continue
            if colour.get(m) == 1:                    # back edge
                return stack[stack.index(m):] + [m]
            if colour.get(m, 0) == 0:
                got = visit(m)
                if got:
                    return got
        stack.pop()
        colour[n] = 2
        return None

    for n in sorted(graph):
        if colour.get(n, 0) == 0:
            got = visit(n)
            if got:
                return got
    return None


class _Flattened:
    """The artifacts of inlining: what to inject into FIELDS / context / ops / guards."""

    def __init__(self) -> None:
        self.ret_fields: Dict[str, str] = {}    # actor target -> RET context key
        self.set_ops: Dict[str, Tuple[str, str]] = {}   # action -> (ret key, site id)
        self.guards: Dict[str, dict] = {}       # guard name -> condition tree
        self.inline: Dict[str, dict] = {}       # actor target -> provenance record
        self.flags: List[str] = []


def _flatten(config: dict, ordered: List[str], sections: Dict[str, List[str]],
             taken: set) -> _Flattened:
    """Inline every PERFORMed paragraph into ``config``'s single flat state map.

    ``taken`` is the set of names already used by the program's own data, so a synthetic
    RET field can never shadow a real COBOL item.
    """
    out = _Flattened()
    states = config.get("states") or {}
    initial = config.get("initial")
    if not states or not initial:
        return out

    main_states, actor_configs = _invoke_transform(states, initial, ordered, sections)
    config["states"] = main_states
    if not actor_configs:
        return out          # PERFORM-free program: _invoke_transform is an identity

    cycle = _find_cycle(_actor_call_graph(actor_configs))
    if cycle:
        raise NotImplementedError(
            "reactive target: recursive PERFORM cycle "
            + " -> ".join(cycle)
            + " cannot be flattened - one return-address field per paragraph would be "
              "overwritten by the re-entrant call, producing a machine that returns to "
              "the wrong place. Use --target js (its actors are separate copies), or "
              "remove the recursion."
        )

    flat: Dict[str, dict] = dict(main_states)

    # 1. namespace every actor body into the one map (a paragraph can appear both
    #    standalone and inside a THRU range - the copies must stay disjoint).
    for name, cfg in sorted(actor_configs.items()):
        target = name[len("actor:"):]
        ret_key = f"RET-{target}"
        n = 2
        while ret_key in taken:                       # never shadow real COBOL data
            ret_key, n = f"RET-{target}-{n}", n + 1
        taken.add(ret_key)
        out.ret_fields[target] = ret_key
        origin: Dict[str, str] = {}
        for sid, st in cfg.get("states", {}).items():
            nsid = _ns(target, sid)
            flat[nsid] = _retarget(copy.deepcopy(st), target)
            origin[nsid] = sid
        out.inline[target] = {
            "prefix": f"{target}__",
            "initial": _ns(target, cfg["initial"]),
            "return": _ns(target, "__RET__"),
            "retField": ret_key,
            "states": origin,
            "sites": {},
        }

    # 2. every invoke site (in main OR inside another inlined body) becomes
    #    "record where to come back to, then jump into the callee".
    dispatch: Dict[str, List[Tuple[str, str]]] = {}
    for sid, st in list(flat.items()):
        src = (st.get("invoke") or {}).get("src") or ""
        if not src.startswith("actor:"):
            continue
        target = src[len("actor:"):]
        cont = (st["invoke"].get("onDone") or {}).get("target")
        if target not in out.inline:
            # owner resolved but body unbuildable - never silently drop the call
            out.flags.append(f"{sid}: PERFORM {target} could not be inlined; the call is "
                             f"skipped in this machine - verify")
            flat[sid] = {"always": [{"target": cont}]} if cont else {}
            continue
        ret_key = out.ret_fields[target]
        set_action = f"set_ret_{target}_at_{sid}"
        out.set_ops[set_action] = (ret_key, sid)
        dispatch.setdefault(target, []).append((sid, cont))
        out.inline[target]["sites"][sid] = cont
        flat[sid] = {"entry": [set_action],
                     "always": [{"target": out.inline[target]["initial"]}]}

    # 3. each inlined body's return becomes a guarded dispatch back to its call sites.
    #    Every edge is guarded: the set-actions enumerate exactly these site literals, so
    #    a non-match is impossible absent a bug - and stalling is more honest than
    #    jumping somewhere plausible but wrong.
    for target, sites in dispatch.items():
        ret_key = out.ret_fields[target]
        edges = []
        for site, cont in sites:
            g = f"ret_{target}_at_{site}"
            out.guards[g] = {"op": "rel", "left": ret_key, "rel": "=",
                             "right": f"'{site}'"}
            edges.append({"guard": g, "target": cont})
        flat[out.inline[target]["return"]] = {"always": edges}

    config["states"] = flat
    return out


def _read_action(name: str, provenance: dict, actions: dict, dv, files, cursors) -> bool:
    """True if entry action ``name`` is the synchronous inbound read/fetch for a data get -
    the thing PUSH replaces with an ``on`` wait. Non-read entry actions (MOVE, COMPUTE,
    and channel verbs like OPEN) stay."""
    prov = provenance.get(name, {})
    hits = _iface._classify(name, prov.get("cobol", ""), actions.get(name),
                            dv, files, cursors)
    return any(_is_wait_hit(h) for h in hits)


def _split_multi_gets(states: dict, machine: Machine) -> None:
    """Give every inbound record read its own state.

    A folded entry run like ``[ACCEPT A, ACCEPT B]`` is one state with two gets, and a
    state can only wait for one event at a time - the rewriter would lower the first and
    flag the rest. Split the run so each read gets its own wait. States with 0 or 1 gets
    are left exactly as they are (which is what keeps PERFORM-free machines byte-stable).
    """
    dv = _iface._DataView(machine.data)
    files = getattr(machine, "files", {}) or {}
    cursors = _iface._cursor_tables(machine.provenance)
    actions = machine.semantics.get("actions", {})

    def is_get(a: str) -> bool:
        return _read_action(a, machine.provenance, actions, dv, files, cursors)

    for name in list(states):
        st = states[name]
        entry = st.get("entry", []) or []
        if sum(1 for a in entry if is_get(a)) < 2:
            continue
        # segment the run so each segment ends on a get (the last may be trailing ops)
        segs: List[List[str]] = []
        cur: List[str] = []
        for a in entry:
            cur.append(a)
            if is_get(a):
                segs.append(cur)
                cur = []
        if cur:
            segs.append(cur)
        control = {k: v for k, v in st.items() if k != "entry"}
        ids = [name] + [f"{name}__g{i}" for i in range(1, len(segs))]
        for i, seg in enumerate(segs):
            if i + 1 < len(segs):
                states[ids[i]] = {"entry": seg, "always": [{"target": ids[i + 1]}]}
            else:                       # the last segment carries the original control
                states[ids[i]] = {"entry": seg, **control}


class _Rewriter:
    """Applies the boundary rewrite to a flat single-region ``states`` dict, in place.

    Accumulates the generated ``recv_*`` / ``publish_*`` names and an inbound/outbound event
    manifest, plus a list of un-handled perimeter situations to flag."""

    def __init__(self, provenance: dict, actions: dict, dv=None, files=None,
                 cursors=None):
        self.provenance = provenance
        self.actions = actions
        self.dv = dv if dv is not None else _iface._DataView(None)
        self.files = files or {}
        self.cursors = cursors or {}
        self.recv: Dict[str, List[str]] = {}      # recv action name -> fields to assign
        self.recv_end: Dict[str, str] = {}        # END recv action name -> endpoint
        self.publish: List[str] = []              # publish effect names
        self.inbound: List[str] = []              # event names the machine waits on
        self.outbound: List[str] = []             # event names the machine publishes
        self.flags: List[str] = []
        self._recv_by_shape: Dict[Tuple[str, tuple], str] = {}

    def _add_inbound(self, event: str) -> None:
        if event not in self.inbound:
            self.inbound.append(event)

    def _recv_action(self, event: str, fields: List[str]) -> str:
        """The action that assigns an arriving event's fields into context.

        Keyed by (event, fields): two waits on the same event carrying *different* field
        lists (READ INTO x here, INTO y there) are different assignments and must not
        collide on one name.
        """
        shape = (event, tuple(fields))
        if shape in self._recv_by_shape:
            return self._recv_by_shape[shape]
        name = "recv_" + _event_slug(event)
        n = 2
        while name in self.recv:                  # same event, different fields
            name, n = f"recv_{_event_slug(event)}_{n}", n + 1
        self.recv[name] = list(fields)
        self._recv_by_shape[shape] = name
        return name

    def rewrite(self, states: dict) -> None:
        ev_by_state = self._pending
        for name in list(states.keys()):
            evs = ev_by_state.get(name)
            if evs:
                self._rewrite_state(states, name, evs)

    def run(self, states: dict, events_by_state: Dict[str, List[dict]]) -> None:
        self._pending = events_by_state
        self.rewrite(states)

    def _rewrite_state(self, states: dict, name: str, evs: List[dict]) -> None:
        st = states[name]
        # A wait must be an actual record delivery. OPEN INPUT classifies as get/file:
        # lowering it would block for an event that never arrives, and swallow the
        # first real record when one did.
        data_gets = [e for e in evs
                     if _is_wait_hit({"direction": e["direction"],
                                      "etype": e["endpointType"],
                                      "verb": e["verb"]})]
        responses = [e for e in evs if e["endpointType"] == "response"]
        creates = [e for e in evs if e["direction"] == "create"]
        # A channel verb (OPEN/STARTBR) is a get that delivers no record: correctly left
        # as an effect, so it is handled - not "unhandled".
        channel = [e for e in evs if e["direction"] == "get"
                   and e["verb"].upper().startswith(_NON_WAIT_GET_VERBS)]
        others = [e for e in evs if e not in data_gets and e not in responses
                  and e not in creates and e not in channel]
        if others:
            self.flags.append(f"{name}: unhandled perimeter event(s) "
                              f"{[e['event'] for e in others]} - left synchronous")

        # Outbound creates: fire-and-forget publish effects on entry; keep `always`.
        for e in creates:
            self._make_publish(st, e)

        # Inbound: a state waits for at most one primary get (data row, else response). A
        # state with several inbound gets at once is beyond the slice - flag and take the first.
        primary = data_gets[0] if data_gets else (responses[0] if responses else None)
        extra = (data_gets[1:] + responses) if data_gets else responses[1:]
        if extra:
            self.flags.append(f"{name}: multiple inbound gets "
                              f"{[e['event'] for e in extra]} - only "
                              f"{primary['event'] if primary else None} lowered")
        if primary is not None:
            if primary in data_gets:
                self._make_data_wait(states, name, primary)
            else:
                self._make_response_wait(states, name, primary)

    def _make_publish(self, st: dict, ev: dict) -> None:
        """Replace the create's synchronous write action with a fire-and-forget publish
        effect; the state's `always` transition is left intact (control does not await)."""
        pub = "publish_" + _event_slug(ev["event"])
        entry = st.get("entry", []) or []
        replaced = [a for a in entry
                    if not self._is_create_action(a, ev)]
        if pub not in replaced:
            replaced.append(pub)
        st["entry"] = replaced
        if pub not in self.publish:
            self.publish.append(pub)
        if ev["event"] not in self.outbound:
            self.outbound.append(ev["event"])

    def _is_create_action(self, name: str, ev: dict) -> bool:
        prov = self.provenance.get(name, {})
        hits = _iface._classify(name, prov.get("cobol", ""), self.actions.get(name),
                                self.dv, self.files, self.cursors)
        return any(h["direction"] == "create" for h in hits)

    def _make_data_wait(self, states: dict, name: str, ev: dict) -> None:
        st = states[name]
        # drop the synchronous read; keep other entry actions (MOVE, etc.)
        st["entry"] = [a for a in (st.get("entry", []) or [])
                       if not _read_action(a, self.provenance, self.actions,
                                           self.dv, self.files, self.cursors)]
        if not st["entry"]:
            st.pop("entry", None)
        recv = self._recv_action(ev["event"], ev.get("fields", []))
        target = self._detach_outgoing(states, name)
        st["on"] = {ev["event"]: {"actions": [recv], "target": target}}
        self._add_inbound(ev["event"])

        # A sequential read's stream can END. COBOL learns that through AT END, which the
        # faithful machine already compiled into a guarded edge on an external `atEnd`
        # flag. Under push there is no return code to inspect - end-of-stream has to
        # arrive as its own event, whose recv raises exactly that flag. It shares the
        # GET handler's target, so the parked AT END edges then branch as they always did.
        if ev["endpointType"] == "file" and _is_read_verb(ev.get("verb", "")):
            end_event = f"END.FILE.{ev['endpoint']}"
            end_recv = "recv_" + _event_slug(end_event)
            self.recv_end[end_recv] = ev["endpoint"]
            st["on"][end_event] = {"actions": [end_recv], "target": target}
            self._add_inbound(end_event)

    def _make_response_wait(self, states: dict, name: str, ev: dict) -> None:
        st = states[name]
        recv = self._recv_action(ev["event"], ev.get("fields", []))
        target = self._detach_outgoing(states, name, force_ready=True)
        st["on"] = {ev["event"]: {"actions": [recv], "target": target}}
        self._add_inbound(ev["event"])

    def _detach_outgoing(self, states: dict, name: str, force_ready: bool = False) -> str:
        """Remove state ``name``'s ``always`` edges and return the target the inbound-event
        handler should point at. A single unconditional edge collapses to its own target;
        anything guarded (or ``force_ready``) is parked in a synthetic ``<name>__ready`` state
        so the original guard branching still runs after the record/response is assigned."""
        st = states[name]
        edges = st.get("always", []) or []
        st.pop("always", None)
        if not force_ready and len(edges) == 1 and "guard" not in edges[0]:
            return edges[0]["target"]
        ready = f"{name}__ready"
        states[ready] = {"always": edges}
        return ready


class _Lowered:
    """One reactive lowering, from which both artifacts are serialized.

    The drawable JSON and the runnable module are two encodings of the SAME machine, so
    they are computed once here - never derived from each other, and never twice.
    """

    def __init__(self, config, fields, iface, rw, flat, ops, guard_fns,
                 external_guards, effect_actions, manifest):
        self.config = config
        self.fields = fields
        self.iface = iface
        self.rw = rw
        self.flat = flat
        self.ops = ops
        self.guard_fns = guard_fns
        self.external_guards = external_guards
        self.effect_actions = effect_actions
        self.manifest = manifest


def _lower(machine: Machine) -> _Lowered:
    """Apply the reactive lowering to ``machine``: flatten PERFORM, split multi-reads,
    rewrite the boundary, and build every table the artifacts need."""
    fields = _field_table(machine)
    config = _strip_meta(copy.deepcopy(machine.config))

    if config.get("type") == "parallel":
        raise NotImplementedError(
            "reactive target: type:parallel (CICS handler regions) not yet lowered")

    # 1. PERFORM -> one flat machine (see _flatten). Must precede the interface build:
    #    interface events are keyed by STATE NAME, and flattening renames states.
    flat = _flatten(config, machine.paragraph_order,
                    getattr(machine, "sections", {}) or {},
                    taken={n.upper() for n in (machine.data or {})})
    for ret_key in flat.ret_fields.values():
        fields[ret_key] = {"category": "alphanumeric"}   # no len: storeStr must not pad
    config["context"] = _js_context(config, fields)
    for ret_key in flat.ret_fields.values():
        config["context"][ret_key] = ""

    # 2. a state reading two records must wait twice - split before the events are keyed.
    _split_multi_gets(config.get("states", {}), machine)

    # 3. the perimeter, over the FLATTENED states.
    iface = _iface.build_interface(
        config, machine.semantics, machine.provenance,
        data=machine.data, using=machine.using, returning=machine.returning,
        files=getattr(machine, "files", {}) or {})
    config = _strip_meta(config)          # build_interface re-annotates meta onto nodes
    ev_by_state = _events_by_state(iface)

    # 4. the boundary rewrite.
    rw = _Rewriter(machine.provenance, machine.semantics.get("actions", {}),
                   dv=_iface._DataView(machine.data),
                   files=getattr(machine, "files", {}) or {},
                   cursors=_iface._cursor_tables(machine.provenance))
    rw.flags.extend(flat.flags)
    rw.run(config.get("states", {}), ev_by_state)

    # After flattening, a surviving perform_ marker means its target could not be
    # resolved (not that PERFORM is unsupported) - flag each one, individually.
    for sname, st in config.get("states", {}).items():
        for a in (st.get("entry", []) or []):
            if a.startswith("perform_"):
                rw.flags.append(f"{sname}: {a} - PERFORM target unresolved, so it is a "
                                f"NO-OP in this machine; verify")

    ref_actions, ref_guards = _collect_referenced({"main": config, "actors": {}})
    ops, sem_effects = _build_ops(machine, fields)
    for act, (ret_key, site) in flat.set_ops.items():          # return-address writes
        ops[act] = "{ return { %s: %s }; }" % (_js_str(ret_key), _js_str(site))
    guard_fns, external_guards = _build_guards(machine, ref_guards, fields)
    for g, tree in flat.guards.items():                        # return-address reads
        js = _emit_guard(tree, fields)
        if js is not None:
            guard_fns[g] = js
    # a return-dispatch guard is REAL and evaluable - it must never be left external
    external_guards = [g for g in external_guards if g not in guard_fns]
    # Effects: referenced actions that are neither a data op nor a generated recv/publish.
    generated = set(rw.recv) | set(rw.recv_end) | set(rw.publish)
    effect_actions = sorted((ref_actions | set(sem_effects)) - set(ops) - generated)

    # The deployment contract: which events to feed in, which to expect out, the caveats.
    manifest = {
        "inbound": rw.inbound,
        "outbound": rw.outbound,
        "endpoints": iface["endpoints"],
        "ordering": ("push model: the event source must deliver records in order; "
                     "the machine processes one event at a time but does not reorder"),
        # Where each inlined paragraph's states came from, so a state id in this flat
        # machine can be traced back to the paragraph (and thence to the COBOL).
        "inline": flat.inline,
        "flags": rw.flags,
    }
    return _Lowered(config, fields, iface, rw, flat, ops, guard_fns,
                    external_guards, effect_actions, manifest)


def build_reactive_view(machine: Machine) -> dict:
    """The reactive machine as drawable JSON.

    Same shape as the other machine views (``format: xstate-v5-config``), so the renderer
    draws the event-driven machine exactly as it draws the faithful one - this is the
    chart that shows the message contract of the new system: which states wait on which
    inbound event, and where each outbound event is published.
    """
    lo = _lower(machine)
    return {
        "format": "xstate-v5-config",
        "metadata": {
            "program": machine.program_id,
            "source": machine.source_name,
            "generator": "cobol-xstate 0.1.0 (--target reactive)",
            "view": "reactive",
            "disclaimer": (
                "The EVENT-DRIVEN machine: inbound records arrive as events the state "
                "waits `on` (see manifest.inbound), outbound writes are fire-and-forget "
                "`publish_*` effects (manifest.outbound), and response codes "
                "(SQLCODE/EIBRESP) come back as their own events. PERFORM is flattened "
                "into this one machine - a queue delivers to the root, so a wait must "
                "not be buried in a child actor; call/return is modeled with a "
                "RET-<paragraph> return-address field. This is a VIEW of the machine: "
                "run the .reactive.mjs, which carries the same config plus the decimal "
                "ops and guards. ORDERING is a deployment contract (manifest.ordering), "
                "not something the machine enforces."
            ),
        },
        "machine": lo.config,
        "interface": lo.iface,
        "manifest": lo.manifest,
        "flags": lo.rw.flags,
    }


def emit_reactive_module(machine: Machine, runtime_import: str = RUNTIME_IMPORT) -> str:
    """Emit the reactive XState v5 ES module for ``machine``.

    Returns a runnable module: ``setup({actions,guards}).createMachine(...)`` whose boundary
    states wait on / publish the overlay's events. ``recvOps`` (event -> context assignments)
    and a ``manifest`` of inbound/outbound events are exported for the deployment to wire."""
    lo = _lower(machine)
    config, fields, iface, rw, flat = lo.config, lo.fields, lo.iface, lo.rw, lo.flat
    ops, guard_fns = lo.ops, lo.guard_fns
    external_guards, effect_actions = lo.external_guards, lo.effect_actions

    out: List[str] = []
    out.append(f"// Generated by cobol-xstate (REACTIVE target) from {machine.source_name} "
               f"(program {machine.program_id}).")
    out.append("// Event-driven XState v5 machine: boundary I/O is push (inbound gets wait "
               "`on` an event),")
    out.append("// fire-and-forget (outbound creates publish and do not await), and response "
               "codes")
    out.append("// (SQLCODE/EIBRESP) arrive as their own inbound events feeding the existing "
               "guards.")
    out.append("// Derived mechanically from the faithful machine + perimeter overlay; see "
               "docs/reactive-target.md")
    out.append("// and the JSON bundle for provenance/flags. ORDERING: under push the event "
               "SOURCE must")
    out.append("// deliver records in order (see manifest.ordering); the machine does not "
               "enforce it.")
    out.append("import { setup, assign } from 'xstate';")
    out.append(f"import {{ {', '.join(_HELPERS)} }} from {_js_str(runtime_import)};")
    out.append("")

    out.append("export const FIELDS = " + json.dumps(fields, indent=2) + ";")
    out.append("")

    # data ops: (context) => partial context
    out.append("export const ops = {")
    for name, body in ops.items():
        out.append(f"  {_js_str(name)}: (context) => {body},")
    out.append("};")
    out.append("")

    # recv ops: (context, event) => partial context.
    # An arriving field is stored through the SAME PICTURE rules as any internal MOVE -
    # a record does not become exempt from COBOL's data semantics by arriving as an event.
    # A field the publisher omits leaves context untouched (D(undefined) would throw).
    out.append("export const recvOps = {")
    for name, flds in rw.recv.items():
        assigns = []
        for f in flds:
            spec = fields.get(f)
            if spec is None:
                continue                       # a group item is not a context key
            src = f"event[{_js_str(f)}]"
            if spec.get("occurs"):
                val = src                      # a whole table arriving: assign as given
            elif spec.get("category") == "numeric":
                val = f"store(D(String({src})), FIELDS[{_js_str(f)}])"
            else:
                val = f"storeStr({src}, FIELDS[{_js_str(f)}])"
            assigns.append(f"{_js_str(f)}: {src} !== undefined ? {val} "
                           f": context[{_js_str(f)}]")
        body = "{ " + ", ".join(assigns) + " }" if assigns else "{}"
        out.append(f"  {_js_str(name)}: (context, event) => ({body}),")
    # End-of-stream recvs raise the file's at-end flag - the very flag the faithful
    # machine's AT END guards already read. Keys come from the emitted guard list, not
    # from reconstructing the name (the registry can suffix `_2` on a collision).
    for name, endpoint in rw.recv_end.items():
        keys = [g for g in external_guards
                if g == f"{endpoint}_atEnd" or g.startswith(f"{endpoint}_atEnd_")]
        sets = " ".join(f"ext[{_js_str(k)}] = true;" for k in keys)
        out.append(f"  {_js_str(name)}: (context, event) => {{ const ext = "
                   f"Object.assign({{}}, context.__cobol_external); {sets} "
                   f"return {{ __cobol_external: ext }}; }},")
    out.append("};")
    out.append("")

    out.append("export const guardFns = {")
    for name, body in guard_fns.items():
        out.append(f"  {_js_str(name)}: (context) => {body},")
    out.append("};")
    out.append("")

    out.append("export const externalGuards = " + json.dumps(external_guards) + ";")
    out.append("// NOT AT END / NOT INVALID KEY: the negation of the positive condition, so")
    out.append("// they are TRUE until the END event raises it - i.e. the per-record path.")
    out.append("export const negatedExternal = "
               + json.dumps(_negated_externals(external_guards)) + ";")
    out.append("export const effectActions = " + json.dumps(effect_actions) + ";")
    out.append("export const publishEffects = " + json.dumps(rw.publish) + ";")
    out.append("")

    # The deployment contract: which events to feed in, which to expect out, the caveats.
    out.append("export const manifest = " + json.dumps(lo.manifest, indent=2) + ";")
    out.append("")

    out.append("const actions = {};")
    out.append("for (const [k, fn] of Object.entries(ops)) "
               "actions[k] = assign(({ context }) => fn(context));")
    out.append("for (const [k, fn] of Object.entries(recvOps)) "
               "actions[k] = assign(({ context, event }) => fn(context, event));")
    out.append("for (const k of effectActions) actions[k] = function () {};")
    out.append("for (const k of publishEffects) actions[k] = function () {};")
    out.append("const guards = {};")
    out.append("for (const [k, fn] of Object.entries(guardFns)) "
               "guards[k] = ({ context }) => fn(context);")
    out.append("for (const k of externalGuards) {")
    out.append("  const pos = negatedExternal[k];")
    out.append("  guards[k] = pos !== undefined")
    out.append("    ? ({ context }) => !(context.__cobol_external "
               "&& context.__cobol_external[pos])")
    out.append("    : ({ context }) => Boolean(context.__cobol_external "
               "&& context.__cobol_external[k]);")
    out.append("}")
    out.append("")

    out.append("export const machineConfig = " + json.dumps(config, indent=2) + ";")
    out.append("export const machine = setup({ actions, guards })"
               ".createMachine(machineConfig);")
    out.append("export default machine;")
    out.append("")
    return "\n".join(out)
