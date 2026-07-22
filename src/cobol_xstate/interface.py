"""Stage 6 (overlay) - the external interface / perimeter of the recovered machine.

The emitted statechart is overwhelmingly *internal* control flow: eventless ``always``
transitions guarded by IF/EVALUATE conditions, and actions that just mutate working
storage. A small subset crosses the program boundary to talk to external actors - a
file, a Db2 table, a CICS program/terminal/queue, the console, or the caller.

This module does NOT change the machine or invent anything. It reads the already-emitted
machine + provenance + semantics and *classifies* which states participate in an external
interface, in which direction:

  * **get**    - the state receives an external event / reads external data
                 (file READ, SQL SELECT/FETCH, ACCEPT, CICS RECEIVE/READQ, an
                 error/exception condition the program HANDLEs, end-of-file, a
                 response code such as SQLCODE or a FILE STATUS field).
  * **create** - the state produces an external event / writes external data
                 (file WRITE/REWRITE/DELETE, SQL INSERT/UPDATE/DELETE, DISPLAY, CICS
                 SEND/WRITEQ, CALL / CICS LINK / XCTL, CICS RETURN to the caller).

Field-level fidelity: every event carries ``fields`` - the data items crossing the
boundary in the event's direction (READ INTO target or the FD record's elementary
fields, ACCEPT/DISPLAY operands, SQL host variables, COMMAREA, CALL arguments) - and,
where data flows the other way in the same command, ``params`` (SQL WHERE host
variables, CICS RIDFLD keys, CALL RETURNING receivers).

Every classification is traced to the same source line as the action it came from.
"""

from __future__ import annotations

import re
from collections import deque
from typing import Deque, Dict, List, Optional

# endpoint kinds
_FILE, _DB2, _PROGRAM, _CONSOLE, _TERMINAL, _CALLER, _CONDITION, _IMS, _RESPONSE = (
    "file", "db2", "program", "console", "terminal", "caller", "condition", "ims",
    "response")
_QUEUE, _SYSTEM, _TRANSACTION = "queue", "system", "transaction"

_CICS_RESOURCE = re.compile(
    r"\b(?:PROGRAM|FILE|DATASET|MAP|MAPSET|QUEUE|TSQUEUE|TDQUEUE)\s*\(\s*'?"
    r"([A-Z0-9_.$#@-]+)'?\s*\)", re.I)
_CICS_COMMAREA = re.compile(r"\bCOMMAREA\s*\(\s*([A-Z0-9_.$#@-]+)\s*\)", re.I)
_CICS_OPT = re.compile(r"\b(INTO|FROM|RIDFLD|TRANSID|QUEUE|ABCODE|SET)\s*\(\s*'?"
                       r"([A-Z0-9_.$#@-]+)'?\s*\)", re.I)
# Data items that carry an external subsystem's response/return status - branching on
# them is the program reacting to a response event (DB2 SQLCODE, VSAM/CICS file status).
_RESPONSE_ITEMS = {"SQLCODE", "SQLSTATE", "SQLERRD", "EIBRESP", "EIBRESP2"}
# EIB fields the transaction READS as inputs (was I called with a COMMAREA? which PF
# key? which transaction?) - branching on them consumes CICS-supplied input.
_EIB_INPUTS = {"EIBCALEN", "EIBAID", "EIBTRNID", "EIBDATE", "EIBTIME", "EIBCPOSN"}
# A table name may be schema-qualified, and the lexer splits `.` into its own token, so
# the text reads `FROM SCHEMA . ACCOUNT`. Allow the optional qualifier and capture the
# TABLE - matching only the first word would name the schema as the endpoint, and two
# programs reading the same table would then look like they read different ones.
_QUALIFIED = r"(?:[A-Z0-9_$#@-]+\s*\.\s*)?([A-Z0-9_$#@-]+)"
_SQL_FROM = re.compile(r"\bFROM\s+" + _QUALIFIED, re.I)
_SQL_INTO_TABLE = re.compile(r"\bINSERT\s+INTO\s+" + _QUALIFIED, re.I)
_SQL_UPDATE = re.compile(r"\bUPDATE\s+" + _QUALIFIED, re.I)
_SQL_HOSTVAR = re.compile(r":\s*([A-Z0-9-]+)", re.I)
_DECLARE_CURSOR = re.compile(
    r"\bDECLARE\s+([A-Z0-9_-]+)\s+CURSOR\b.*?\bFROM\s+([A-Z0-9_.$#@-]+)", re.I | re.S)
_CALL_USING = re.compile(r"\bUSING\b(.*?)(?:\bRETURNING\b|$)", re.I | re.S)
_CALL_RETURNING = re.compile(r"\bRETURNING\s+([A-Z0-9-]+)", re.I)
# The two dynamic-CALL provenance spellings statechart._call_action produces:
# unresolved keeps the identifier and says so; resolved names the identifier it came via.
_CALL_DYNAMIC = re.compile(r"CALL\s+\(DYNAMIC\)\s", re.I)
_CALL_RESOLVED = re.compile(r"CALL\s+([A-Z0-9-]+)\s+->\s+RESOLVED\b", re.I)
_ACCEPT_SYSTEM = re.compile(r"\bFROM\s+(DATE|DAY|DAY-OF-WEEK|TIME)\b", re.I)
_WORD = re.compile(r"[A-Z0-9][A-Z0-9-]*")
_STR_LIT = re.compile(r"'[^']*'|\"[^\"]*\"")


def _parse_call_args(cobol: str) -> List[str]:
    """The data-item names passed on a ``CALL ... USING a b c`` (for event fields)."""
    m = _CALL_USING.search(cobol or "")
    if not m:
        return []
    out = []
    for tok in re.split(r"[,\s]+", m.group(1).strip()):
        u = tok.upper()
        if u and u not in ("BY", "REFERENCE", "CONTENT", "VALUE", "ON", "NOT",
                           "EXCEPTION", "OVERFLOW") and not u.startswith(("'", '"')):
            out.append(u)
    return out


def _name_suffix(name: str) -> str:
    """The endpoint encoded in an action name like ``read_TRAN-FILE`` / ``call_POSTLOG``."""
    return name.split("_", 1)[1] if "_" in name else name


def _event(direction: str, etype: str, endpoint: str) -> str:
    return f"{'GET' if direction == 'get' else 'CREATE'}.{etype.upper()}.{endpoint}"


def _hit(direction, etype, endpoint, verb, fields, params=None, columns=None):
    d = {"direction": direction, "etype": etype, "endpoint": endpoint,
         "verb": verb, "fields": fields}
    if params:
        d["params"] = params
    if columns:
        d["columns"] = columns
    return d


def _resource(rs: dict, opt: str, fallback: str) -> str:
    """The endpoint name for a CICS resource operand, preferring the statechart's
    resolved name (spec["resources"], see statechart._exec_resources) over the raw
    text: a PROGRAM/TRANSID/QUEUE/FILE/MAP(data-name) operand resolved by constant
    propagation names the RESOURCE, where the raw text names the data item."""
    r = rs.get(opt)
    return (r.get("name") or fallback) if r else fallback


def _mark_dynamic(hit: dict, rs: dict, *opts: str) -> dict:
    """Attach the dynamic-target status of the first present resource operand to the
    hit: `dynamic` marks an unresolved runtime name (with `candidates` when several
    literals reach it), `via` the data item a resolved one came through."""
    for o in opts:
        r = rs.get(o)
        if not r:
            continue
        if r.get("dynamic"):
            hit["dynamic"] = True
            if r.get("candidates"):
                hit["candidates"] = r["candidates"]
        elif r.get("via"):
            hit["via"] = r["via"]
        break
    return hit


# --------------------------------------------------------------------------- #
# data-dictionary helpers (record <-> file, group -> elementary fields)
# --------------------------------------------------------------------------- #

class _DataView:
    def __init__(self, data: Optional[dict]):
        self.data = data or {}
        self.children: Dict[str, List[str]] = {}
        # file -> its top-level record names, indexed in the SAME pass as children.
        # Answering this by re-filtering the whole data dictionary per I/O statement
        # was O(statements x data items) on copybook-heavy programs.
        self.records: Dict[str, List[str]] = {}
        for name, it in self.data.items():
            if not isinstance(it, dict):
                continue
            parent = it.get("parent")
            if parent:
                self.children.setdefault(parent.upper(), []).append(name)
            elif it.get("file") and it.get("kind") != "condition-name":
                self.records.setdefault(it["file"], []).append(name)

    def file_of(self, name: str) -> Optional[str]:
        it = self.data.get((name or "").upper())
        return it.get("file") if isinstance(it, dict) else None

    def records_of(self, file_name: str) -> List[str]:
        return self.records.get(file_name, [])

    def leaves(self, name: str, limit: int = 64) -> List[str]:
        """Elementary (leaf) fields under `name`, in dictionary order; the record's
        actual field layout, for field-level interface fidelity."""
        out: List[str] = []
        root = (name or "").upper()
        # A deque: the previous list `pop(0)` was O(n) per step and `kids + stack`
        # reallocated the whole frontier on every expansion - quadratic on a wide
        # COMMAREA/FD record with hundreds of subordinate fields.
        stack: Deque[str] = deque([root])
        while stack and len(out) < limit:
            cur = stack.popleft()
            kids = [k for k in self.children.get(cur, [])
                    if isinstance(self.data.get(k), dict)
                    and self.data[k].get("kind") != "condition-name"]
            if not kids:
                if cur in self.data and cur != root:
                    out.append(cur)
                elif cur == root and not self.children.get(cur):
                    out.append(cur)
            else:
                stack.extendleft(reversed(kids))
        return out

    def record_fields(self, record: str) -> List[str]:
        """`record` plus its elementary fields (the data crossing with the record)."""
        leaves = self.leaves(record)
        if not leaves or leaves == [record]:
            return [record] if record in self.data or record else []
        return [record] + leaves


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #

def _display_fields(cobol: str, data: dict) -> List[str]:
    """Identifiers among DISPLAY operands (literals dropped, UPON clause skipped)."""
    body = re.sub(r"^\s*DISPLAY\b", "", cobol or "", flags=re.I)
    body = re.split(r"\bUPON\b", body, flags=re.I)[0]
    body = _STR_LIT.sub(" ", body)
    out = []
    for w in _WORD.findall(body.upper()):
        if w in ("NO", "WITH", "ADVANCING"):
            continue
        if w in data and w not in out:
            out.append(w)
    return out


def _sql_host_vars(text: str) -> List[str]:
    out = []
    for v in _SQL_HOSTVAR.findall(text or ""):
        u = v.upper()
        if u not in out:
            out.append(u)
    return out


def _qualify(columns: Optional[List[dict]], table: str) -> Optional[List[dict]]:
    """Stamp the table onto each mapping: `BAL` alone is not an identity, `CUST.BAL` is."""
    if not columns:
        return None
    return [{"table": table, **c} for c in columns]


def _fetch_columns(up: str, into_fields: List[str],
                   cursor_cols: Dict[str, List[str]]) -> Optional[List[dict]]:
    """Correlate a FETCH's host variables against its cursor's select list.

    Same count gate as the parser's: the columns come from one statement and the host
    variables from another, so only equal lengths prove the correspondence.
    """
    m = re.match(r".*?\bFETCH\s+(?:NEXT\s+|PRIOR\s+|FIRST\s+|FROM\s+)*([A-Z0-9_-]+)", up)
    if not m:
        return None
    cols = cursor_cols.get(m.group(1).upper())
    if not cols or len(cols) != len(into_fields):
        return None
    return [{"column": c, "hostVar": h} for c, h in zip(cols, into_fields)
            if c is not None]


def _classify_exec(name: str, cobol: str, spec: Optional[dict], dv: _DataView,
                   cursors: Dict[str, str],
                   cursor_cols: Optional[Dict[str, List[str]]] = None) -> List[dict]:
    """Classify an EXEC SQL / CICS / DLI action -> list of boundary hits."""
    up = cobol.upper()
    verb = (spec or {}).get("verb", "")
    if not verb:
        toks = up.replace("EXEC", "", 1).split()
        # skip the language word (SQL/CICS/DLI) to reach the verb
        verb = toks[1] if len(toks) > 1 and toks[0] in ("SQL", "CICS", "DLI") else (
            toks[0] if toks else "")
    verb = verb.upper()
    into_fields = [a["target"] for a in (spec or {}).get("assignments", [])
                   if isinstance(a, dict) and "target" in a]
    host_vars = [h.lstrip(":").upper()
                 for h in ((spec or {}).get("hostVars") or _sql_host_vars(up))]

    is_sql = "EXEC SQL" in up or name.startswith("exec_sql")
    is_cics = "EXEC CICS" in up or name.startswith("exec_cics")

    # Which column fills which host variable - the cross-program state identity. The
    # parser proves it for SELECT/UPDATE; a FETCH's columns live on its cursor's DECLARE.
    columns = (spec or {}).get("columns") or None

    if is_sql:
        if verb in ("SELECT", "FETCH"):
            endpoint = None
            if verb == "FETCH":
                fm = re.match(r".*?\bFETCH\s+(?:NEXT\s+|PRIOR\s+|FIRST\s+|FROM\s+)*"
                              r"([A-Z0-9_-]+)", up)
                if fm:
                    endpoint = cursors.get(fm.group(1)) or f"<cursor {fm.group(1)}>"
                if columns is None:
                    columns = _fetch_columns(up, into_fields, cursor_cols or {})
            if endpoint is None:
                m = _SQL_FROM.search(up)
                endpoint = m.group(1) if m else "<cursor>"
            params = [h for h in host_vars if h not in into_fields]
            return [_hit("get", _DB2, endpoint, verb, into_fields, params,
                         _qualify(columns, endpoint))]
        if verb == "INSERT":
            m = _SQL_INTO_TABLE.search(up)
            ep = m.group(1) if m else "<table>"
            return [_hit("create", _DB2, ep, verb, host_vars,
                         columns=_qualify(columns, ep))]
        if verb == "UPDATE":
            m = _SQL_UPDATE.search(up)
            ep = m.group(1) if m else "<table>"
            return [_hit("create", _DB2, ep, verb, host_vars,
                         columns=_qualify(columns, ep))]
        if verb == "DELETE":
            m = _SQL_FROM.search(up)
            return [_hit("create", _DB2, m.group(1) if m else "<table>", verb, host_vars)]
        if verb in ("PREPARE", "EXECUTE"):
            # Dynamic SQL: the statement text is a run-time value, so the operation and
            # table(s) are not statically knowable. One endpoint, both directions (the
            # statement could read or write), marked dynamic; the fields are the host
            # variables that carry/parameterize the statement.
            v = verb + (" IMMEDIATE" if "IMMEDIATE" in up else "")
            hits = [_hit("get", _DB2, "<dynamic-sql>", v, host_vars),
                    _hit("create", _DB2, "<dynamic-sql>", v, host_vars)]
            for h in hits:
                h["dynamic"] = True
            return hits
        return []  # OPEN/CLOSE cursor, DECLARE, COMMIT, WHENEVER - not a data crossing

    if is_cics:
        res = _CICS_RESOURCE.search(up)
        endpoint = res.group(1) if res else ""
        opts = {k.upper(): v.upper() for k, v in _CICS_OPT.findall(up)}
        ca = _CICS_COMMAREA.search(up)
        commarea = [ca.group(1).upper()] if ca else []
        into = [opts["INTO"]] if "INTO" in opts else []
        from_ = [opts["FROM"]] if "FROM" in opts else []
        ridfld = [opts["RIDFLD"]] if "RIDFLD" in opts else []
        # Resolved resource-name operands (statechart._exec_resources): a
        # PROGRAM/TRANSID/QUEUE/FILE/MAP(data-name) operand resolved by constant
        # propagation names the RESOURCE where the raw text names the data item.
        # Unresolved ones stay the identifier, marked dynamic, so downstream views
        # never present a working-storage name as a real resource identity.
        rs = (spec or {}).get("resources") or {}
        if verb in ("LINK", "XCTL"):
            return [_mark_dynamic(
                _hit("create", _PROGRAM, _resource(rs, "PROGRAM", endpoint or "<program>"),
                     "CICS " + verb, commarea), rs, "PROGRAM")]
        if verb == "RETURN":
            v = "CICS RETURN"
            if "TRANSID" in opts:
                v += f" TRANSID({_resource(rs, 'TRANSID', opts['TRANSID'])})"
            return [_hit("create", _CALLER, "CALLER", v, commarea)]
        if verb == "SEND":
            ep = _resource(rs, "MAP", _resource(rs, "MAPSET", endpoint or "terminal"))
            return [_mark_dynamic(_hit("create", _TERMINAL, ep, "CICS SEND", from_),
                                  rs, "MAP", "MAPSET")]
        if verb == "RECEIVE":
            ep = _resource(rs, "MAP", _resource(rs, "MAPSET", endpoint or "terminal"))
            return [_mark_dynamic(_hit("get", _TERMINAL, ep, "CICS RECEIVE", into),
                                  rs, "MAP", "MAPSET")]
        if verb in ("READ", "READNEXT", "READPREV"):
            f = _resource(rs, "FILE", _resource(rs, "DATASET", endpoint or "<file>"))
            return [_mark_dynamic(
                _hit("get", _FILE, f, "CICS " + verb,
                     into or dv.record_fields(f), ridfld), rs, "FILE", "DATASET")]
        if verb in ("STARTBR", "RESETBR"):
            f = _resource(rs, "FILE", _resource(rs, "DATASET", endpoint or "<file>"))
            return [_mark_dynamic(_hit("get", _FILE, f, "CICS " + verb, [], ridfld),
                                  rs, "FILE", "DATASET")]
        if verb == "ENDBR":
            return []
        if verb in ("WRITE", "REWRITE", "DELETE"):
            f = _resource(rs, "FILE", _resource(rs, "DATASET", endpoint or "<file>"))
            return [_mark_dynamic(
                _hit("create", _FILE, f, "CICS " + verb, from_, ridfld),
                rs, "FILE", "DATASET")]
        if verb in ("READQ",):
            q = _resource(rs, "QUEUE", endpoint or opts.get("QUEUE", "<queue>"))
            qtype = "TD" if re.search(r"\bREADQ\s+TD\b", up) else "TS"
            return [_mark_dynamic(_hit("get", _QUEUE, q, f"CICS READQ {qtype}", into),
                                  rs, "QUEUE")]
        if verb in ("WRITEQ",):
            q = _resource(rs, "QUEUE", endpoint or opts.get("QUEUE", "<queue>"))
            qtype = "TD" if re.search(r"\bWRITEQ\s+TD\b", up) else "TS"
            return [_mark_dynamic(_hit("create", _QUEUE, q, f"CICS WRITEQ {qtype}", from_),
                                  rs, "QUEUE")]
        if verb == "DELETEQ":
            q = _resource(rs, "QUEUE", endpoint or opts.get("QUEUE", "<queue>"))
            return [_mark_dynamic(_hit("create", _QUEUE, q, "CICS DELETEQ", []),
                                  rs, "QUEUE")]
        if verb == "START":
            t = _resource(rs, "TRANSID", opts.get("TRANSID", "<transid>"))
            return [_mark_dynamic(_hit("create", _TRANSACTION, t, "CICS START", from_),
                                  rs, "TRANSID")]
        if verb == "RETRIEVE":
            return [_hit("get", _TRANSACTION, "RETRIEVE", "CICS RETRIEVE", into)]
        if verb == "ABEND":
            code = opts.get("ABCODE", "ABEND")
            return [_hit("create", _CONDITION, code, "CICS ABEND", [])]
        return []  # HANDLE (-> handler region), ADDRESS, ASKTIME, etc.

    # DLI / IMS
    if verb in ("GU", "GN", "GNP", "GHU", "GHN"):
        return [_hit("get", _IMS, "IMS-DB", "DLI " + verb, [])]
    if verb in ("ISRT", "REPL", "DLET"):
        return [_hit("create", _IMS, "IMS-DB", "DLI " + verb, [])]
    return []


# One OPEN can carry several mode clauses: `OPEN INPUT F1 OUTPUT F2`. The file-name run
# must therefore STOP at the next mode keyword - a plain `[A-Z0-9-]+` swallowed it, so
# that statement parsed as one INPUT clause over three "files", inventing an endpoint
# literally named OUTPUT and classifying the output file as read. The inner
# `(?![A-Z0-9-])` keeps a file genuinely named OUTPUT-FILE or I-O-AREA from being read
# as the keyword, since a plain \b would match before the hyphen.
_OPEN_MODE = r"(?:INPUT|OUTPUT|I-O|EXTEND)"
_OPEN_MODES = re.compile(
    rf"\b({_OPEN_MODE})(?![A-Z0-9-])"
    rf"((?:\s+(?!{_OPEN_MODE}(?![A-Z0-9-]))[A-Z0-9-]+)+)", re.I)


def _classify(name: str, cobol: str, spec: Optional[dict], dv: _DataView,
              files: Dict[str, dict], cursors: Dict[str, str],
              cursor_cols: Optional[Dict[str, List[str]]] = None) -> List[dict]:
    """Classify one entry action -> list of boundary hits (empty if internal).

    ``cursor_cols`` is optional: only the interface build needs a FETCH correlated to its
    cursor's columns, so the other callers (lineage, business, reactive) pass nothing and
    are unaffected.
    """
    up = (cobol or "").upper().strip()
    verb = up.split()[0] if up else ""

    if verb == "EXEC" or name.startswith(("exec_sql", "exec_cics", "exec_dli")):
        return _classify_exec(name, up, spec, dv, cursors, cursor_cols)

    io = spec if (spec or {}).get("kind") == "io" else {}

    if verb in ("READ", "START"):
        f = io.get("file") or _name_suffix(name)
        if io.get("into"):
            fields = [io["into"]]
        else:  # no INTO: the data lands in the FD record - list its field layout
            fields = [x for r in dv.records_of(f) for x in dv.record_fields(r)]
        return [_hit("get", _FILE, f, verb, fields)]
    if verb in ("WRITE", "REWRITE", "DELETE"):
        rec = io.get("file") or _name_suffix(name)
        f = dv.file_of(rec) or (rec if rec in files else rec)
        fields = dv.record_fields(rec) if dv.file_of(rec) else ([rec] if rec else [])
        if io.get("from"):
            fields = fields + [io["from"]]
        return [_hit("create", _FILE, f, verb, fields)]
    if verb == "RETURN":  # sort OUTPUT PROCEDURE: RETURN sort-file [INTO x]
        f = io.get("file") or _name_suffix(name)
        fields = [io["into"]] if io.get("into") else []
        return [_hit("get", _FILE, f, "RETURN (sort)", fields)]
    if verb == "RELEASE":  # sort INPUT PROCEDURE: RELEASE rec [FROM x]
        m = re.match(r"RELEASE\s+([A-Z0-9-]+)(?:\s+FROM\s+([A-Z0-9-]+))?", up)
        rec = m.group(1) if m else _name_suffix(name)
        fields = [rec] + ([m.group(2)] if m and m.group(2) else [])
        return [_hit("create", _FILE, dv.file_of(rec) or rec, "RELEASE (sort)", fields)]
    if verb in ("SORT", "MERGE"):
        hits = []
        um = re.search(r"\bUSING((?:\s+[A-Z0-9-]+)+?)(?=\s+(?:GIVING|OUTPUT|$))",
                       up + " ")
        gm = re.search(r"\bGIVING((?:\s+[A-Z0-9-]+)+)", up)
        for m2, direction in ((um, "get"), (gm, "create")):
            if m2:
                for f in m2.group(1).split():
                    hits.append(_hit(direction, _FILE, f, f"{verb} {'USING' if direction == 'get' else 'GIVING'}",
                                     [x for r in dv.records_of(f) for x in dv.record_fields(r)]))
        return hits
    if verb == "OPEN":
        # OPEN INPUT/OUTPUT/I-O/EXTEND f...: declares the channel and its direction
        # (a file that is only ever OPENed still appears on the perimeter).
        hits = []
        for mode, names in _OPEN_MODES.findall(up):
            for f in names.split():
                if mode.upper() == "INPUT":
                    hits.append(_hit("get", _FILE, f, "OPEN INPUT", []))
                elif mode.upper() in ("OUTPUT", "EXTEND"):
                    hits.append(_hit("create", _FILE, f, f"OPEN {mode.upper()}", []))
                else:  # I-O
                    hits.append(_hit("get", _FILE, f, "OPEN I-O", []))
                    hits.append(_hit("create", _FILE, f, "OPEN I-O", []))
        return hits
    if verb == "DISPLAY":
        return [_hit("create", _CONSOLE, "SYSOUT", "DISPLAY",
                     _display_fields(cobol, dv.data))]
    if verb == "ACCEPT":
        fields = [a["target"] for a in (spec or {}).get("assignments", [])
                  if isinstance(a, dict) and "target" in a]
        sysm = _ACCEPT_SYSTEM.search(up)
        if sysm:
            return [_hit("get", _SYSTEM, "SYSTEM-" + sysm.group(1).upper(),
                         f"ACCEPT FROM {sysm.group(1).upper()}", fields)]
        return [_hit("get", _CONSOLE, "SYSIN", "ACCEPT", fields)]
    if verb == "CALL":
        params = []
        rm = _CALL_RETURNING.search(up)
        if rm:
            params = [rm.group(1).upper()]
        hit = _hit("create", _PROGRAM, _name_suffix(name), "CALL",
                   _parse_call_args(cobol), params)
        # The statechart's provenance label spells out the dynamic-target status
        # (see statechart._call_action): carry it so downstream views don't present
        # an unresolved identifier as a load-module name.
        dm = _CALL_DYNAMIC.match(up)
        if dm:
            hit["dynamic"] = True
        else:
            vm = _CALL_RESOLVED.match(up)
            if vm:
                hit["via"] = vm.group(1)
        return [hit]
    return []  # CLOSE / internal MOVE/COMPUTE/etc. - not an external crossing


def _classify_condition_event(event_name: str) -> Optional[dict]:
    """A handler-region watch event (``IO.ERROR.CUST-FILE`` / ``CICS.NOTFND``) is an
    external condition the program *gets* (an error/exception raised by an external actor)."""
    parts = event_name.split(".")
    if parts[0] == "IO":                      # IO.<TRIGGER>.<FILE>
        endpoint = parts[-1] if len(parts) > 2 else (parts[1] if len(parts) > 1 else "IO")
        return _hit("get", _CONDITION, endpoint, "->".join(parts[:2]), [])
    if parts[0] == "CICS":                     # CICS.<CONDITION>
        return _hit("get", _CONDITION, parts[-1], "CICS " + parts[-1], [])
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


def _classify_dataflow(spec: Optional[dict], linkage: set) -> List[dict]:
    """Any assignment touching a LINKAGE item (or RETURN-CODE) is a boundary crossing:
    reading a linkage field is receiving the caller's request (get); writing one - by
    MOVE, COMPUTE, ADD, SET, ... - is producing the caller-visible response (create).
    RETURN-CODE is a special register the caller/JCL sees. Plain WS-to-WS stays internal."""
    hits: List[dict] = []
    verb = (spec or {}).get("verb", "?")
    for a in (spec or {}).get("assignments", []):
        if not isinstance(a, dict):
            continue
        target = (a.get("target") or "").upper()
        expr = (a.get("expr") or "").upper()
        if target in linkage:
            hits.append(_hit("create", _CALLER, "CALLER",
                             f"{verb} -> linkage (send response)", [target]))
        elif target == "RETURN-CODE":
            hits.append(_hit("create", _CALLER, "CALLER",
                             f"{verb} -> RETURN-CODE (caller-visible)", ["RETURN-CODE"]))
        for w in _WORD.findall(expr):
            if w in linkage:
                hits.append(_hit("get", _CALLER, "CALLER",
                                 f"{verb} <- linkage (receive request)", [w]))
    return hits


def _guard_items(node, wanted: Dict[str, str]) -> List[str]:
    """All items from `wanted` (name -> kind) that a guard tree references."""
    found: List[str] = []

    def rec(n):
        if isinstance(n, str):
            for w in _WORD.findall(n.upper()):
                if w in wanted and w not in found:
                    found.append(w)
        elif isinstance(n, dict):
            for v in n.values():
                rec(v)
        elif isinstance(n, list):
            for v in n:
                rec(v)

    rec(node)
    return found


def _perimeter_kind(gets: List[str], creates: List[str]) -> str:
    if gets and creates:
        return "input-output"
    return "input" if gets else "output"


def _state_index(config: dict) -> Dict[str, dict]:
    """name -> state node, for every state in the (possibly nested/parallel) config.

    Built in ONE walk. Looking each name up with its own recursive descent made
    annotation O(perimeter states x all states) - on a large program that is millions
    of dict visits for something a single pass answers."""
    index: Dict[str, dict] = {}

    def rec(states):
        for n, st in (states or {}).items():
            index.setdefault(n, st)
            rec(st.get("states"))
    rec(config.get("states", {}))
    return index


def _annotate_states(config: dict, perimeter: Dict[str, dict]) -> None:
    """Tag each perimeter state's node in the machine with ``meta.perimeter`` (input /
    output / input-output) and its get/create events, so the boundary is visible on the
    state itself - not only in the separate overlay. Idempotent."""
    index = _state_index(config)
    for name, d in perimeter.items():
        st = index.get(name)
        if st is None:
            continue
        meta = st.setdefault("meta", {})
        meta["perimeter"] = _perimeter_kind(d["gets"], d["creates"])
        meta["gets"] = d["gets"]
        meta["creates"] = d["creates"]


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


def _cursor_tables(provenance: dict) -> Dict[str, str]:
    """cursor-name -> table, from ``DECLARE c CURSOR FOR SELECT ... FROM t`` texts."""
    out: Dict[str, str] = {}
    for prov in (provenance or {}).values():
        m = _DECLARE_CURSOR.search((prov or {}).get("cobol", "") or "")
        if m:
            out[m.group(1).upper()] = m.group(2).upper()
    return out


def _cursor_columns(semantics: dict, provenance: dict) -> Dict[str, List[str]]:
    """cursor-name -> its select list, so a FETCH can be correlated to real columns.

    A cursor splits the information in two: ``DECLARE c CURSOR FOR SELECT ID, BAL FROM t``
    has the columns, ``FETCH c INTO :WS-ID, :WS-BAL`` has the host variables. Neither
    statement alone says which column fills which variable. Kept as its OWN map rather
    than widening ``_cursor_tables`` - three call sites depend on that one's values being
    plain table names.
    """
    out: Dict[str, List[str]] = {}
    actions = (semantics or {}).get("actions", {})
    for name, spec in actions.items():
        if not isinstance(spec, dict) or spec.get("verb") != "DECLARE":
            continue
        cols = spec.get("selectList")
        if not cols:
            continue
        m = _DECLARE_CURSOR.search((provenance.get(name) or {}).get("cobol", "") or "")
        if m:
            out[m.group(1).upper()] = cols
    return out


def build_interface(config: dict, semantics: dict, provenance: dict,
                    data: Optional[dict] = None, using: Optional[List[str]] = None,
                    returning: Optional[str] = None,
                    files: Optional[Dict[str, dict]] = None) -> dict:
    """Return the external-interface overlay: events, per-state get/create, endpoints,
    and the program's own parameter interface.

    Pure read over the emitted machine - attributes each boundary crossing to the state
    that hosts it, the direction, the external endpoint, the source line, and the
    fields crossing. The program entry's LINKAGE / ``PROCEDURE DIVISION USING`` /
    ``RETURNING`` (its COMMAREA / parameters) is the perimeter at the entry point and
    is surfaced under ``parameters``.
    """
    actions = (semantics or {}).get("actions", {})
    guards = (semantics or {}).get("guards", {})
    files = files or {}
    dv = _DataView(data)
    cursors = _cursor_tables(provenance)
    cursor_cols = _cursor_columns(semantics, provenance)
    linkage_all = {n.upper() for n, it in (data or {}).items()
                   if isinstance(it, dict) and it.get("section") == "LINKAGE"}
    # Guard-scanned response/input items: SQLCODE-style registers, EIB inputs, and
    # each file's FILE STATUS field (the file subsystem's response register).
    status_fields = {v["statusField"]: k for k, v in files.items()
                     if isinstance(v, dict) and v.get("statusField")}
    # item -> which kind of external read branching on it represents. Depends only on
    # the file/linkage sets, so it is built ONCE here rather than per guarded edge -
    # a COMMAREA-heavy CICS program has hundreds of linkage names and thousands of
    # edges, which made rebuilding it per edge a six-figure cost per program.
    wanted: Dict[str, str] = {r: "response" for r in _RESPONSE_ITEMS}
    wanted.update({e: "eib" for e in _EIB_INPUTS})
    wanted.update({s: "status" for s in status_fields})
    wanted.update({l: "linkage" for l in linkage_all})
    events: List[dict] = []
    perimeter: Dict[str, dict] = {}
    endpoints: Dict[str, dict] = {}

    def add(state: str, region: str, hit: dict, line: int, cobol: str) -> None:
        ev = _event(hit["direction"], hit["etype"], hit["endpoint"])
        entry = {
            "event": ev, "direction": hit["direction"], "endpointType": hit["etype"],
            "endpoint": hit["endpoint"], "verb": hit["verb"], "fields": hit["fields"],
            "state": state, "region": region, "line": line, "cobol": cobol,
        }
        if hit.get("params"):
            entry["params"] = hit["params"]
        if hit.get("columns"):
            # NOTE: this dict is rebuilt key-by-key, so a new key on the hit is dropped
            # unless it is copied here. lineage/business read the hit directly, so
            # forgetting this would make the mapping appear to work in two of three
            # places - the worst kind of bug to chase.
            entry["columns"] = hit["columns"]
        # Dynamic program-target status (CALL identifier / LINK PROGRAM(data-name)):
        # `dynamic` marks an unresolved runtime target, `via` the data item a resolved
        # one came through, `candidates` the literals an ambiguous one may be.
        for k in ("dynamic", "via", "candidates"):
            if hit.get(k):
                entry[k] = hit[k]
        events.append(entry)
        slot = perimeter.setdefault(
            state, {"region": region, "gets": [], "creates": []})
        bucket = slot["gets"] if hit["direction"] == "get" else slot["creates"]
        if ev not in bucket:
            bucket.append(ev)
        ep = endpoints.setdefault(hit["endpoint"], {"type": hit["etype"], "directions": []})
        if hit["direction"] not in ep["directions"]:
            ep["directions"].append(hit["direction"])
        for k in ("dynamic", "via", "candidates"):
            if hit.get(k):
                ep.setdefault(k, hit[k])
        fc = files.get(hit["endpoint"])
        if isinstance(fc, dict):
            for k in ("assign", "organization", "access", "recordKey", "statusField"):
                if fc.get(k):
                    ep.setdefault(k, fc[k])

    for state, region, st in _iter_states(config):
        for aname in st.get("entry", []) or []:
            prov = provenance.get(aname, {})
            cobol = prov.get("cobol", "")
            line = prov.get("line", 0)
            spec = actions.get(aname)
            hits = _classify(aname, cobol, spec, dv, files, cursors, cursor_cols)
            for hit in hits:
                add(state, region, hit, line, cobol)
            if not hits:
                for hit in _classify_dataflow(spec, linkage_all):
                    add(state, region, hit, line, cobol)
        # Branching on an external return item (SQLCODE / EIBRESP / a FILE STATUS
        # field) is the program reacting to a *response event* from that subsystem;
        # branching on an EIB input field consumes CICS-supplied input; branching on
        # a linkage field reads the caller's request.
        for edge in st.get("always", []) or []:
            g = edge.get("guard")
            if not g:
                continue
            tree = guards.get(g)
            for item in _guard_items(tree, wanted):
                kind = wanted[item]
                if kind == "response":
                    sub = "DB2" if item.startswith("SQL") else "CICS"
                    add(state, region,
                        _hit("get", _RESPONSE, sub, f"response ({item})", [item]),
                        0, f"branch on {item}")
                elif kind == "status":
                    add(state, region,
                        _hit("get", _RESPONSE, status_fields[item],
                             f"file status ({item})", [item]),
                        0, f"branch on {item}")
                elif kind == "eib":
                    add(state, region,
                        _hit("get", _RESPONSE, "CICS-EIB", f"EIB input ({item})", [item]),
                        0, f"branch on {item}")
                else:  # linkage
                    add(state, region,
                        _hit("get", _CALLER, "CALLER", f"guard reads linkage ({item})",
                             [item]),
                        0, f"branch on {item}")
        for event_name in (st.get("on", {}) or {}):
            hit = _classify_condition_event(event_name)
            if hit:
                add(state, region, hit, 0, event_name)

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
        add(entry, program,
            _hit("get", _CALLER, "CALLER", "PROCEDURE DIVISION USING", list(inbound)),
            0, "PROCEDURE DIVISION USING " + " ".join(inbound))
        # USING is BY REFERENCE by default, so the caller also sees updates -> create.
        add(entry, program,
            _hit("create", _CALLER, "CALLER", "USING (by reference)", list(inbound)),
            0, "USING (by reference) " + " ".join(inbound))
    if returning:
        add(entry, program,
            _hit("create", _CALLER, "CALLER", "PROCEDURE DIVISION RETURNING",
                 [returning.upper()]),
            0, "RETURNING " + returning.upper())

    # Label each perimeter state input / output / input-output, and tag the state node
    # itself so the boundary is visible on the machine (meta.perimeter), not just here.
    for d in perimeter.values():
        d["perimeter"] = _perimeter_kind(d["gets"], d["creates"])
    _annotate_states(config, perimeter)

    return {
        "endpoints": [
            {"endpoint": k, **{**v, "directions": sorted(v["directions"])}}
            for k, v in sorted(endpoints.items())
        ],
        "events": events,
        "perimeterStates": perimeter,
        "parameters": {
            "using": using,
            "returning": returning.upper() if returning else None,
            "linkage": linkage,
            "commarea": bool(commarea),
            # Each parameter record expanded to its elementary fields (PIC-typed in
            # 'data'), so the caller contract is field-level, not just record names.
            "fields": {rec: dv.leaves(rec) for rec in (using or linkage)},
        },
    }
