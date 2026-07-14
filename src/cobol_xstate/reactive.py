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

Scope of the current slice (see the doc): flat single-region machines; SQL SELECT proven
end-to-end. Perimeter states inside a performed paragraph, ``type: parallel`` machines, and
create/other-get verb classes are written for but not yet proven - anything unhandled is
flagged, never faked.
"""

from __future__ import annotations

import copy
import json
from typing import Dict, List, Optional, Tuple

from . import interface as _iface
from .emitter import (
    RUNTIME_IMPORT, _HELPERS, _build_guards, _build_ops, _collect_referenced,
    _field_table, _js_context, _js_str, _strip_meta,
)
from .statechart import Machine


def _event_slug(event: str) -> str:
    """A JS-identifier-safe token for an event name (``GET.DB2.CUSTOMER`` -> ``GET_DB2_CUSTOMER``)."""
    return "".join(c if c.isalnum() else "_" for c in event)


# Endpoint types whose GET is an actual data crossing (a record/row to receive), as opposed
# to a RESPONSE (SQLCODE/EIBRESP) or a CONDITION (a HANDLEd exception, deferred here).
_DATA_GET_TYPES = {"db2", "file", "console", "terminal", "ims"}


def _events_by_state(iface: dict) -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = {}
    for ev in iface.get("events", []):
        out.setdefault(ev["state"], []).append(ev)
    return out


def _read_action(name: str, provenance: dict, actions: dict) -> bool:
    """True if entry action ``name`` is the synchronous inbound read/fetch for a data get -
    the thing PUSH replaces with an ``on`` wait. Non-read entry actions (MOVE, COMPUTE) stay."""
    prov = provenance.get(name, {})
    hit = _iface._classify(name, prov.get("cobol", ""), actions.get(name))
    if not hit:
        return False
    direction, etype, _endpoint, _verb, _fields = hit
    return direction == "get" and etype in _DATA_GET_TYPES


class _Rewriter:
    """Applies the boundary rewrite to a flat single-region ``states`` dict, in place.

    Accumulates the generated ``recv_*`` / ``publish_*`` names and an inbound/outbound event
    manifest, plus a list of un-handled perimeter situations to flag."""

    def __init__(self, provenance: dict, actions: dict):
        self.provenance = provenance
        self.actions = actions
        self.recv: Dict[str, List[str]] = {}      # recv action name -> fields to assign
        self.publish: List[str] = []              # publish effect names
        self.inbound: List[str] = []              # event names the machine waits on
        self.outbound: List[str] = []             # event names the machine publishes
        self.flags: List[str] = []

    def _add_inbound(self, event: str) -> None:
        if event not in self.inbound:
            self.inbound.append(event)

    def _recv_action(self, event: str, fields: List[str]) -> str:
        name = "recv_" + _event_slug(event)
        self.recv[name] = list(fields)
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
        data_gets = [e for e in evs if e["direction"] == "get"
                     and e["endpointType"] in _DATA_GET_TYPES]
        responses = [e for e in evs if e["endpointType"] == "response"]
        creates = [e for e in evs if e["direction"] == "create"]
        others = [e for e in evs if e not in data_gets and e not in responses
                  and e not in creates]
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
        hit = _iface._classify(name, prov.get("cobol", ""), self.actions.get(name))
        return bool(hit) and hit[0] == "create"

    def _make_data_wait(self, states: dict, name: str, ev: dict) -> None:
        st = states[name]
        # drop the synchronous read; keep other entry actions (MOVE, etc.)
        st["entry"] = [a for a in (st.get("entry", []) or [])
                       if not _read_action(a, self.provenance, self.actions)]
        if not st["entry"]:
            st.pop("entry", None)
        recv = self._recv_action(ev["event"], ev.get("fields", []))
        target = self._detach_outgoing(states, name)
        st["on"] = {ev["event"]: {"actions": [recv], "target": target}}
        self._add_inbound(ev["event"])

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


def emit_reactive_module(machine: Machine, runtime_import: str = RUNTIME_IMPORT) -> str:
    """Emit the reactive XState v5 ES module for ``machine``.

    Returns a runnable module: ``setup({actions,guards}).createMachine(...)`` whose boundary
    states wait on / publish the overlay's events. ``recvOps`` (event -> context assignments)
    and a ``manifest`` of inbound/outbound events are exported for the deployment to wire."""
    fields = _field_table(machine)
    config = _strip_meta(copy.deepcopy(machine.config))
    config["context"] = _js_context(config, fields)

    if config.get("type") == "parallel":
        raise NotImplementedError(
            "reactive target: type:parallel (CICS handler regions) not yet lowered")

    iface = _iface.build_interface(
        machine.config, machine.semantics, machine.provenance,
        data=machine.data, using=machine.using, returning=machine.returning)
    ev_by_state = _events_by_state(iface)

    rw = _Rewriter(machine.provenance, machine.semantics.get("actions", {}))
    rw.run(config.get("states", {}), ev_by_state)

    # Flag any perimeter state that lives inside a performed paragraph (an actor in the js
    # target): the slice inlines to stay flat, so a leftover perform_ into a perimeter state
    # is out of scope here.
    if any(a.startswith("perform_")
           for st in config.get("states", {}).values()
           for a in (st.get("entry", []) or [])):
        rw.flags.append("machine contains PERFORM into a paragraph; perimeter states inside "
                        "a performed actor are not lowered in this slice")

    ref_actions, ref_guards = _collect_referenced({"main": config, "actors": {}})
    ops, sem_effects = _build_ops(machine, fields)
    guard_fns, external_guards = _build_guards(machine, ref_guards, fields)
    # Effects: referenced actions that are neither a data op nor a generated recv/publish.
    generated = set(rw.recv) | set(rw.publish)
    effect_actions = sorted((ref_actions | set(sem_effects)) - set(ops) - generated)

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
        out.append(f"  {_js_str(name)}: (context) => ({body}),")
    out.append("};")
    out.append("")

    # recv ops: (context, event) => partial context - assign received fields from the event.
    out.append("export const recvOps = {")
    for name, flds in rw.recv.items():
        assigns = ", ".join(f"{_js_str(f)}: event[{_js_str(f)}]" for f in flds)
        out.append(f"  {_js_str(name)}: (context, event) => ({{ {assigns} }}),")
    out.append("};")
    out.append("")

    out.append("export const guardFns = {")
    for name, body in guard_fns.items():
        out.append(f"  {_js_str(name)}: (context) => {body},")
    out.append("};")
    out.append("")

    out.append("export const externalGuards = " + json.dumps(external_guards) + ";")
    out.append("export const effectActions = " + json.dumps(effect_actions) + ";")
    out.append("export const publishEffects = " + json.dumps(rw.publish) + ";")
    out.append("")

    # The deployment contract: which events to feed in, which to expect out, the flags/caveats.
    manifest = {
        "inbound": rw.inbound,
        "outbound": rw.outbound,
        "endpoints": iface["endpoints"],
        "ordering": ("push model: the event source must deliver records in order; "
                     "the machine processes one event at a time but does not reorder"),
        "flags": rw.flags,
    }
    out.append("export const manifest = " + json.dumps(manifest, indent=2) + ";")
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
    out.append("for (const k of externalGuards) "
               "guards[k] = ({ context }) => Boolean(context.__cobol_external "
               "&& context.__cobol_external[k]);")
    out.append("")

    out.append("export const machineConfig = " + json.dumps(config, indent=2) + ";")
    out.append("export const machine = setup({ actions, guards })"
               ".createMachine(machineConfig);")
    out.append("export default machine;")
    out.append("")
    return "\n".join(out)
