"""Stage 6 (overlay) - the external interface / perimeter of the recovered machine.

The emitted statechart is overwhelmingly *internal* control flow: eventless ``always``
transitions guarded by IF/EVALUATE conditions, and actions that just mutate working
storage. A small subset crosses the program boundary to talk to external actors - a
file, a Db2 table, a CICS program/terminal, the console, or the caller.

This module does NOT change the machine or invent anything. It reads the already-emitted
machine + provenance and *classifies* which states participate in an external interface,
in which direction:

  * **get**    - the state receives an external event / reads external data
                 (file READ, SQL SELECT/FETCH, ACCEPT, CICS RECEIVE, an error/exception
                 condition the program HANDLEs, end-of-file).
  * **create** - the state produces an external event / writes external data
                 (file WRITE/REWRITE/DELETE, SQL INSERT/UPDATE/DELETE, DISPLAY, CICS
                 SEND, CALL / CICS LINK / XCTL, CICS RETURN to the caller).

Every classification is traced to the same source line as the action it came from.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# endpoint kinds
_FILE, _DB2, _PROGRAM, _CONSOLE, _TERMINAL, _CALLER, _CONDITION, _IMS = (
    "file", "db2", "program", "console", "terminal", "caller", "condition", "ims")

_CICS_RESOURCE = re.compile(
    r"\b(?:PROGRAM|FILE|DATASET|MAP|MAPSET|QUEUE|TSQUEUE|TDQUEUE)\s*\(\s*'?"
    r"([A-Z0-9_.$#@-]+)'?\s*\)", re.I)
_SQL_FROM = re.compile(r"\bFROM\s+([A-Z0-9_.$#@-]+)", re.I)
_SQL_INTO_TABLE = re.compile(r"\bINSERT\s+INTO\s+([A-Z0-9_.$#@-]+)", re.I)
_SQL_UPDATE = re.compile(r"\bUPDATE\s+([A-Z0-9_.$#@-]+)", re.I)
_CALL_USING = re.compile(r"\bUSING\b(.*?)(?:\bRETURNING\b|$)", re.I | re.S)


def _parse_call_args(cobol: str) -> List[str]:
    """The data-item names passed on a ``CALL ... USING a b c`` (for event fields)."""
    m = _CALL_USING.search(cobol or "")
    if not m:
        return []
    out = []
    for tok in re.split(r"[,\s]+", m.group(1).strip()):
        u = tok.upper()
        if u and u not in ("BY", "REFERENCE", "CONTENT", "VALUE"):
            out.append(u)
    return out


def _name_suffix(name: str) -> str:
    """The endpoint encoded in an action name like ``read_TRAN-FILE`` / ``call_POSTLOG``."""
    return name.split("_", 1)[1] if "_" in name else name


def _event(direction: str, etype: str, endpoint: str) -> str:
    return f"{'GET' if direction == 'get' else 'CREATE'}.{etype.upper()}.{endpoint}"


def _classify_exec(name: str, cobol: str, spec: Optional[dict]
                   ) -> Optional[Tuple[str, str, str, str, List[str]]]:
    """Classify an EXEC SQL / CICS / DLI action -> (direction, etype, endpoint, verb, fields)."""
    up = cobol.upper()
    verb = (spec or {}).get("verb", "")
    if not verb:
        toks = up.replace("EXEC", "", 1).split()
        # skip the language word (SQL/CICS/DLI) to reach the verb
        verb = toks[1] if len(toks) > 1 and toks[0] in ("SQL", "CICS", "DLI") else (
            toks[0] if toks else "")
    verb = verb.upper()
    fields = [a["target"] for a in (spec or {}).get("assignments", [])
              if isinstance(a, dict) and "target" in a]

    is_sql = "EXEC SQL" in up or name.startswith("exec_sql")
    is_cics = "EXEC CICS" in up or name.startswith("exec_cics")

    if is_sql:
        if verb in ("SELECT", "FETCH"):
            m = _SQL_FROM.search(up)
            return ("get", _DB2, m.group(1) if m else "<cursor>", verb, fields)
        if verb == "INSERT":
            m = _SQL_INTO_TABLE.search(up)
            return ("create", _DB2, m.group(1) if m else "<table>", verb, [])
        if verb == "UPDATE":
            m = _SQL_UPDATE.search(up)
            return ("create", _DB2, m.group(1) if m else "<table>", verb, [])
        if verb == "DELETE":
            m = _SQL_FROM.search(up)
            return ("create", _DB2, m.group(1) if m else "<table>", verb, [])
        return None  # OPEN/CLOSE cursor, COMMIT, WHENEVER, etc. - not a data crossing

    if is_cics:
        res = _CICS_RESOURCE.search(up)
        endpoint = res.group(1) if res else ""
        if verb in ("LINK", "XCTL"):
            return ("create", _PROGRAM, endpoint or "<program>", "CICS " + verb, [])
        if verb == "SEND":
            return ("create", _TERMINAL, endpoint or "terminal", "CICS SEND", [])
        if verb == "RECEIVE":
            return ("get", _TERMINAL, endpoint or "terminal", "CICS RECEIVE", [])
        if verb == "READ":
            return ("get", _FILE, endpoint or "<file>", "CICS READ", [])
        if verb in ("WRITE", "REWRITE", "DELETE"):
            return ("create", _FILE, endpoint or "<file>", "CICS " + verb, [])
        if verb == "RETURN":
            return ("create", _CALLER, "CALLER", "CICS RETURN", [])
        return None  # HANDLE (-> handler region), ADDRESS, ASKTIME, etc.

    # DLI / IMS
    if verb in ("GU", "GN", "GNP", "GHU", "GHN"):
        return ("get", _IMS, "IMS-DB", "DLI " + verb, [])
    if verb in ("ISRT", "REPL", "DLET"):
        return ("create", _IMS, "IMS-DB", "DLI " + verb, [])
    return None


def _classify(name: str, cobol: str, spec: Optional[dict]
              ) -> Optional[Tuple[str, str, str, str, List[str]]]:
    """Classify one entry action -> (direction, etype, endpoint, verb, fields) or None."""
    up = (cobol or "").upper().strip()
    verb = up.split()[0] if up else ""

    if verb == "EXEC" or name.startswith(("exec_sql", "exec_cics", "exec_dli")):
        return _classify_exec(name, up, spec)
    if verb == "READ":
        return ("get", _FILE, _name_suffix(name), "READ", [])
    if verb == "WRITE":
        return ("create", _FILE, _name_suffix(name), "WRITE", [])
    if verb == "REWRITE":
        return ("create", _FILE, _name_suffix(name), "REWRITE", [])
    if verb == "DELETE":
        return ("create", _FILE, _name_suffix(name), "DELETE", [])
    if verb == "START":
        return ("get", _FILE, _name_suffix(name), "START", [])
    if verb == "DISPLAY":
        return ("create", _CONSOLE, "SYSOUT", "DISPLAY", [])
    if verb == "ACCEPT":
        return ("get", _CONSOLE, "SYSIN", "ACCEPT", [])
    if verb == "CALL":
        return ("create", _PROGRAM, _name_suffix(name), "CALL", _parse_call_args(cobol))
    return None  # OPEN / CLOSE / internal MOVE/COMPUTE/etc. - not an external crossing


def _classify_condition_event(event_name: str
                              ) -> Optional[Tuple[str, str, str, str, List[str]]]:
    """A handler-region watch event (``IO.ERROR.CUST-FILE`` / ``CICS.NOTFND``) is an
    external condition the program *gets* (an error/exception raised by an external actor)."""
    parts = event_name.split(".")
    if parts[0] == "IO":                      # IO.<TRIGGER>.<FILE>
        endpoint = parts[-1] if len(parts) > 2 else (parts[1] if len(parts) > 1 else "IO")
        return ("get", _CONDITION, endpoint, "->".join(parts[:2]), [])
    if parts[0] == "CICS":                     # CICS.<CONDITION>
        return ("get", _CONDITION, parts[-1], "CICS " + parts[-1], [])
    return None


def _iter_states(config: dict):
    """Yield ``(state_name, region, state_dict)`` for every state, tracking its region.

    In a ``type: parallel`` machine the top-level states ARE the concurrent regions
    (e.g. PROGRAM / HANDLERS); otherwise the whole program is one region.
    """
    program = config.get("id", "PROGRAM")
    parallel = config.get("type") == "parallel"

    def rec(states, region):
        for name, st in (states or {}).items():
            yield name, region, st
            yield from rec(st.get("states"), region)

    root = config.get("states", {})
    if parallel:
        for rname, rst in root.items():
            yield rname, rname, rst
            yield from rec(rst.get("states"), rname)
    else:
        for name, st in root.items():
            yield name, program, st
            yield from rec(st.get("states"), program)


def _linkage_records(data: Optional[dict]) -> List[str]:
    """Top-level (01/77) items in the LINKAGE SECTION - the program's parameter records
    (COMMAREA / passed parameters), independent of any USING list."""
    out = []
    for name, item in (data or {}).items():
        if not isinstance(item, dict) or item.get("section") != "LINKAGE":
            continue
        lvl = item.get("level")
        if lvl in (1, 77, "01", "77") and "parent" not in item:
            out.append(name)
    return out


def build_interface(config: dict, semantics: dict, provenance: dict,
                    data: Optional[dict] = None, using: Optional[List[str]] = None,
                    returning: Optional[str] = None) -> dict:
    """Return the external-interface overlay: events, per-state get/create, endpoints,
    and the program's own parameter interface.

    Pure read over the emitted machine - attributes each boundary crossing to the state
    that hosts it, the direction, the external endpoint, and the source line. The program
    entry's LINKAGE / ``PROCEDURE DIVISION USING`` / ``RETURNING`` (its COMMAREA /
    parameters) is the perimeter at the entry point and is surfaced under ``parameters``.
    """
    actions = (semantics or {}).get("actions", {})
    events: List[dict] = []
    perimeter: Dict[str, dict] = {}
    endpoints: Dict[str, dict] = {}

    def add(state: str, region: str, direction: str, etype: str, endpoint: str,
            verb: str, fields: List[str], line: int, cobol: str) -> None:
        ev = _event(direction, etype, endpoint)
        events.append({
            "event": ev, "direction": direction, "endpointType": etype,
            "endpoint": endpoint, "verb": verb, "fields": fields,
            "state": state, "region": region, "line": line, "cobol": cobol,
        })
        slot = perimeter.setdefault(
            state, {"region": region, "gets": [], "creates": []})
        bucket = slot["gets"] if direction == "get" else slot["creates"]
        if ev not in bucket:
            bucket.append(ev)
        ep = endpoints.setdefault(endpoint, {"type": etype, "directions": []})
        if direction not in ep["directions"]:
            ep["directions"].append(direction)

    for state, region, st in _iter_states(config):
        for aname in st.get("entry", []) or []:
            prov = provenance.get(aname, {})
            cobol = prov.get("cobol", "")
            hit = _classify(aname, cobol, actions.get(aname))
            if hit:
                direction, etype, endpoint, verb, fields = hit
                add(state, region, direction, etype, endpoint, verb, fields,
                    prov.get("line", 0), cobol)
        for event_name in (st.get("on", {}) or {}):
            hit = _classify_condition_event(event_name)
            if hit:
                direction, etype, endpoint, verb, fields = hit
                add(state, region, direction, etype, endpoint, verb, fields, 0, event_name)

    # The program's OWN parameter interface (LINKAGE / PROCEDURE DIVISION USING /
    # RETURNING / COMMAREA) is the perimeter at the entry point: the caller passes these
    # in (get) and receives RETURNING / by-reference updates back (create).
    program = config.get("id", "PROGRAM")
    entry = config.get("initial") or "__ENTRY__"
    using = [u.upper() for u in (using or [])]
    linkage = _linkage_records(data)
    commarea = [n for n in linkage if n.upper() == "DFHCOMMAREA"]
    # Parameters received from the caller: the USING list, else the COMMAREA/LINKAGE record.
    inbound = using or commarea or linkage
    if inbound:
        add(entry, program, "get", _CALLER, "CALLER", "PROCEDURE DIVISION USING",
            list(inbound), 0, "PROCEDURE DIVISION USING " + " ".join(inbound))
        # USING is BY REFERENCE by default, so the caller also sees updates -> create.
        add(entry, program, "create", _CALLER, "CALLER", "USING (by reference)",
            list(inbound), 0, "USING (by reference) " + " ".join(inbound))
    if returning:
        add(entry, program, "create", _CALLER, "CALLER", "PROCEDURE DIVISION RETURNING",
            [returning.upper()], 0, "RETURNING " + returning.upper())

    return {
        "endpoints": [
            {"endpoint": k, "type": v["type"], "directions": sorted(v["directions"])}
            for k, v in sorted(endpoints.items())
        ],
        "events": events,
        "perimeterStates": perimeter,
        "parameters": {
            "using": using,
            "returning": returning.upper() if returning else None,
            "linkage": linkage,
            "commarea": bool(commarea),
        },
    }
