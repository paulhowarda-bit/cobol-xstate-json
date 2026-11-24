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
from typing import Deque, Dict, List, Optional, Sequence, Set, Tuple

# endpoint kinds
_FILE, _DB2, _PROGRAM, _CONSOLE, _TERMINAL, _CALLER, _CONDITION, _IMS, _RESPONSE = (
    "file", "db2", "program", "console", "terminal", "caller", "condition", "ims",
    "response")
_QUEUE, _SYSTEM, _TRANSACTION = "queue", "system", "transaction"
# A Db2 STORED PROCEDURE is its own endpoint kind: its operands are the procedure's
# PARAMETERS (linkage, not table columns), so classifying `EXEC SQL CALL` as a table
# would send downstream tooling looking for Column nodes that cannot exist.
_DB2_PROC = "db2_proc"
# Dynamic SQL (PREPARE / EXECUTE [IMMEDIATE]) is likewise its own kind, not "db2": the
# statement text is a run-time value, so no table or column mapping can exist - and a
# consumer that cannot tell this apart from static SQL counts every one of its host
# variables as a mapping failure instead of an inherent unknown.
_DYNAMIC_SQL = "dynamic_sql"
# The statement name right after the verb; the text arrives as `EXEC SQL PREPARE S1 ...`.
_SQL_PREPARED_NAME = re.compile(
    r"\s*(?:EXEC\s+SQL\s+)?(?:PREPARE|EXECUTE)\s+(?!IMMEDIATE\b)([A-Z0-9_#@$-]+)")

# `GET [CURRENT | STACKED] DIAGNOSTICS`; what follows the match is its assignment list.
_SQL_GET_DIAGNOSTICS = re.compile(
    r"\s*(?:EXEC\s+SQL\s+)?GET\s+(?:(?:CURRENT|STACKED)\s+)?DIAGNOSTICS\b")
_CICS_RESOURCE = re.compile(
    r"\b(?:PROGRAM|FILE|DATASET|MAP|MAPSET|QUEUE|QNAME|TSQUEUE|TDQUEUE)\s*\(\s*'?"
    r"([A-Z0-9_.$#@-]+)\s*'?\s*\)", re.I)
# CICS commands that share a verb with the file commands and touch no file: WRITE
# OPERATOR writes to the console, and DELETE with one of these operands removes a
# channel container, a named counter, a timer, an event or a BTS activity.
_CICS_OPERATOR = re.compile(r"\bOPERATOR\b")
_CICS_DELETE_NOT_A_FILE = re.compile(
    r"\b(?:ACTIVITY|CONTAINER|COUNTER|DCOUNTER|EVENT|TIMER)\s*\(")
_CICS_TEXT = re.compile(r"\bTEXT\s*\(\s*([A-Z0-9][A-Z0-9-]*)\s*\)")
_CICS_REPLY = re.compile(r"\bREPLY\s*\(\s*([A-Z0-9][A-Z0-9-]*)\s*\)")
# A RECEIVE with no MAP reads what was typed at the terminal (the transaction id and
# whatever follows it) - unless CONVID/SESSION make it a conversation with a partner
# program. Its options, for the terminal input it publishes (upstream ledger item 141).
_CICS_MAP_OPERAND = re.compile(r"\bMAP\s*\(")
_CICS_CONVERSATION = re.compile(r"\b(?:CONVID|SESSION)\s*\(")
_CICS_BUFFER = re.compile(r"\bBUFFER\b")
_CICS_LENGTH = re.compile(r"\bF?LENGTH\s*\(\s*([A-Z0-9][A-Z0-9-]*)\s*\)")
_CICS_COMMAREA = re.compile(r"\bCOMMAREA\s*\(\s*([A-Z0-9_.$#@-]+)\s*\)", re.I)
_CICS_OPT = re.compile(r"\b(INTO|FROM|RIDFLD|TRANSID|QUEUE|ABCODE|SET)\s*\(\s*'?"
                       r"([A-Z0-9_.$#@-]+)\s*'?\s*\)", re.I)
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
# `SELECT ... FROM FINAL TABLE (UPDATE t SET ...)` - a data-change table reference.
# FINAL / OLD / NEW TABLE is grammar that says WHICH VERSION of the rows the SELECT sees;
# the table is the target of the statement inside the parentheses. Reading the word
# after FROM published a Db2 table called FINAL, which the estate was then asked for.
# A head whose inner statement is not one of the four is never captured as the table.
_DATA_CHANGE_HEAD = r"(?:FINAL|OLD|NEW)\s+TABLE\s*\("
_FROM_TABLE = (r"\bFROM\s+(?:" + _DATA_CHANGE_HEAD
               + r"\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|MERGE\s+INTO)\s+"
               + r"|(?!" + _DATA_CHANGE_HEAD + r"))")
_SQL_FROM = re.compile(_FROM_TABLE + _QUALIFIED, re.I)
_SQL_DATA_CHANGE = re.compile(
    r"\bFROM\s+" + _DATA_CHANGE_HEAD + r"\s*(INSERT|UPDATE|DELETE|MERGE)\b", re.I)
_SQL_INTO_TABLE = re.compile(r"\bINSERT\s+INTO\s+" + _QUALIFIED, re.I)
_SQL_MERGE_INTO = re.compile(r"\bMERGE\s+INTO\s+" + _QUALIFIED, re.I)
_SQL_UPDATE = re.compile(r"\bUPDATE\s+" + _QUALIFIED, re.I)
# `:GFAC . AC-ACC-N` is a QUALIFIED host variable - the field AC-ACC-N inside group
# GFAC - and the elementary name after the period is the one the data dictionary holds.
# Capturing only the leading word named the GROUP, so every slot of a VALUES list
# written that way came back as one repeated group name that matched no column. The
# tail is optional and must end in a word, which is what keeps the period that
# terminates a COBOL sentence (`... :WS-ID END-EXEC.`) from being read as a qualifier.
_SQL_HOSTVAR = re.compile(r":\s*([A-Z0-9-]+)(?:\s*\.\s*([A-Z0-9-]+))?", re.I)
_SQL_CALL = re.compile(r"\bCALL\s+(?:[A-Z0-9_$#@-]+\s*\.\s*)?([A-Z0-9_$#@-]+)", re.I)
_DECLARE_CURSOR = re.compile(
    r"\bDECLARE\s+([A-Z0-9_-]+)\s+CURSOR\b.*?" + _FROM_TABLE + r"([A-Z0-9_.$#@-]+)",
    re.I | re.S)
_CALL_USING = re.compile(r"\bUSING\b(.*?)(?:\bRETURNING\b|$)", re.I | re.S)
_CALL_RETURNING = re.compile(r"\bRETURNING\s+([A-Z0-9-]+)", re.I)
# The three CALL provenance spellings statechart._call_action produces, and the program
# each one names. Mutually exclusive by construction (a quote is not in the identifier
# class, and only one spelling carries the literal "(DYNAMIC)"), so the order they are
# tried in is defensive rather than load-bearing.
#   CALL (dynamic) WS-PGM ...                    -> the data item; the target is unknown
#   CALL WS-SUBPGM -> resolved 'POSTLOG' (...)   -> the literal it was proved to be
#   CALL 'DSNTIAC' ...                           -> the literal itself
# The resolved literal is an OPTIONAL group: the `via` capture beside it must keep
# matching even if that spelling ever loses its quoted target.
_CALL_DYNAMIC = re.compile(r"CALL\s+\(DYNAMIC\)\s+([A-Z0-9-]+)", re.I)
_CALL_RESOLVED = re.compile(
    r"CALL\s+([A-Z0-9-]+)\s+->\s+RESOLVED\b(?:\s+'([^']+)')?", re.I)
_CALL_LITERAL = re.compile(r"CALL\s+'([^']+)'", re.I)
# The target-status keys a hit carries onto its event and its endpoint, in output order.
_DYNAMIC_KEYS = ("dynamic", "via", "candidates", "rejectedCandidates", "evidence",
                 "hasVariableAssignment", "internal", "endpointUnresolved")
# Why an endpoint is not the resource's own name, as a token a consumer can branch on
# rather than a spelling it has to recognise. `no-operand`: the statement names no
# resource this tool could read, so the endpoint is a placeholder (`<file>`, `<queue>`
# ...). `record`: a WRITE/REWRITE/RELEASE names a record whose FD is not in sight.
_UNRESOLVED_NO_OPERAND = "no-operand"
_UNRESOLVED_RECORD = "record"
_PLACEHOLDERS = frozenset(("<program>", "<file>", "<queue>", "<transid>", "<table>",
                           "<procedure>"))
_ACCEPT_SYSTEM = re.compile(r"\bFROM\s+(DATE|DAY|DAY-OF-WEEK|TIME)\b", re.I)
# The other two ways a program reads the clock. CICS ASKTIME stores it in its ABSTIME
# operand; a Db2 SET / VALUES INTO stores a CURRENT DATE / TIME / TIMESTAMP register.
# Both are the input `ACCEPT ... FROM TIME` is, so they get its endpoint.
_CICS_ABSTIME = re.compile(r"\bABSTIME\s*\(\s*([A-Z0-9][A-Z0-9-]*)\s*\)")
_CICS_GETMAIN_SET = re.compile(r"\bSET\s*\(\s*ADDRESS\s+OF\s+([A-Z0-9][A-Z0-9-]*)\s*\)")
_SQL_CLOCK = re.compile(r"\bCURRENT(?:\s+|_)(DATE|TIME|TIMESTAMP)\b")
_CLOCK_ENDPOINT = {"DATE": "SYSTEM-DATE", "TIME": "SYSTEM-TIME", "TIMESTAMP": "SYSTEM-TIME"}
# `SET :A = (SELECT ...)` assigns from a table, the read `SELECT ... INTO :A` is.
_SQL_FULLSELECT = re.compile(r"\(\s*(?:SELECT|VALUES|WITH)\b")
_SQL_SELECT = re.compile(r"\bSELECT\b")
# FORMATTIME writes the ABSTIME value, formatted, into each of these options; DATESEP
# and TIMESEP are the separators it formats with (a literal, or a field it reads).
_FORMATTIME_OUT = re.compile(
    r"\b(?:DATE|DATEFORM|DATESTRING|DAYCOUNT|DAYOFMONTH|DAYOFWEEK|DDMMYY|DDMMYYYY|"
    r"FULLDATE|MILLISECONDS|MMDDYY|MMDDYYYY|MONTHOFYEAR|TIME|YEAR|YYDDD|YYDDMM|YYMMDD|"
    r"YYYYDDD|YYYYDDMM|YYYYMMDD)\s*\(\s*([A-Z0-9][A-Z0-9-]*)\s*\)")
_FORMATTIME_SEP = re.compile(r"\b(?:DATESEP|TIMESEP)\s*\(\s*([A-Z0-9][A-Z0-9-]*)\s*\)")
_WORD = re.compile(r"[A-Z0-9][A-Z0-9-]*")
_STR_LIT = re.compile(r"'[^']*'|\"[^\"]*\"")

# The shared literal mask (semantics is a leaf; no cycle): SQL string constants and
# DISPLAY message literals carry English and colons, and every keyword scan below that
# runs over raw statement text must not see into them.
from .semantics import mask_literals  # noqa: E402
# Every table a statement names. Lives with the parser, which needs it for a cursor
# DECLAREd outside the procedure division and must not import this modelling module.
from cobol_parser.parser import _sql_table_refs  # noqa: E402
from .storage import children_index  # noqa: E402


def _parse_call_args(cobol: str) -> List[str]:
    """The data-item names passed on a ``CALL ... USING a b c`` (for event fields)."""
    m = _CALL_USING.search(cobol or "")
    if not m:
        return []
    out = []
    toks = re.split(r"[,\s]+", m.group(1).strip())
    for i, tok in enumerate(toks):
        u = tok.upper()
        # `USING X OF R` passes X: OF/IN and the qualifying record are not arguments
        # (ledger item 155). `ADDRESS OF X` still passes X - the callee can write it
        # through the pointer - as `CallStmt.arguments` says; `LENGTH OF X` does not.
        if u in ("OF", "IN") or (i and toks[i - 1].upper() in ("OF", "IN")
                                 and not (i > 1 and toks[i - 2].upper() == "ADDRESS")):
            continue
        if u and u not in ("BY", "REFERENCE", "CONTENT", "VALUE", "ON", "NOT",
                           "EXCEPTION", "OVERFLOW", "ADDRESS", "LENGTH") and not u.startswith(("'", '"')):
            out.append(u)
    return out


def _name_suffix(name: str) -> str:
    """The endpoint encoded in an action name like ``read_TRAN-FILE`` / ``call_POSTLOG``.

    A LAST resort. An action name is allocated by ``naming.NameRegistry``, which keys on
    (kind, COBOL text) and appends ``_2``, ``_3``, ... to keep two STATEMENTS apart - so
    the name is not purely the endpoint, and reading one out of it can recover a spelling
    the estate never had. Prefer whatever recorded the endpoint directly: ``io["file"]``
    for a file, :func:`_call_endpoint` for a CALL.
    """
    return name.split("_", 1)[1] if "_" in name else name


def _call_endpoint(cobol: str, name: str) -> str:
    """The program a CALL names, read from the COBOL rather than from the action name.

    Two CALLs to one program whose ``USING`` operands differ are two statements, so the
    registry allocates ``call_MQINQ`` and ``call_MQINQ_2`` - correctly, they have distinct
    provenance. Splitting the second name for an endpoint yielded ``MQINQ_2``: a program
    that exists nowhere, which was then classified (the MQI verb list holds ``MQINQ``, not
    ``MQINQ_2``), listed in the artifact manifest, REQUESTED from the estate, and published
    as a ``CREATE.PROGRAM.MQINQ_2`` event. Two identical CALLs never split that way, so the
    perimeter disagreed with itself over which spelling a program had.

    The COBOL provenance label names the target in all three spellings, so read it there.
    Deliberately matched against the original text, not an uppercased copy: ``CALL 'mqinq'``
    registers as ``call_mqinq`` and has always reported ``mqinq``, and this is a fix for a
    phantom name, not an occasion to start re-casing real ones.
    """
    for pattern, group in ((_CALL_DYNAMIC, 1), (_CALL_RESOLVED, 2), (_CALL_LITERAL, 1)):
        hit = pattern.match(cobol or "")
        if hit and hit.group(group):
            return hit.group(group)
    # A spelling this function does not know. Degrade to the old derivation rather than
    # to nothing: a suffixed name is wrong, but no name at all loses the dependency.
    return _name_suffix(name)


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


def _queue(rs: dict, endpoint: str, opts: dict) -> str:
    """A TS/TD queue's name: QUEUE(), or QNAME() - the 16-character TS queue name."""
    return _resource(rs, "QUEUE", _resource(rs, "QNAME",
                                            endpoint or opts.get("QUEUE", "<queue>")))


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
            for k in ("candidates", "rejectedCandidates"):
                if r.get(k):
                    hit[k] = r[k]
        elif r.get("via"):
            hit["via"] = r["via"]
        break
    return hit


def _with_mapset(hit: dict, rs: dict) -> dict:
    """A ``MAP`` named with a ``MAPSET``: the endpoint stays the map - the screen - and
    the hit says which mapset holds it, because the mapset, not the map, is what a BMS
    source member and a CSD MAPSET definition are named. A mapset that is itself an
    unresolved data item is not named: the endpoint already says the name is dynamic."""
    mapset = rs.get("MAPSET")
    if "MAP" in rs and mapset and mapset.get("name") and not mapset.get("dynamic"):
        hit["mapset"] = mapset["name"]
    return hit


# --------------------------------------------------------------------------- #
# data-dictionary helpers (record <-> file, group -> elementary fields)
# --------------------------------------------------------------------------- #

class _DataView:
    def __init__(self, data: Optional[dict],
                 fd_records: Optional[Dict[str, List[str]]] = None):
        self.data = data or {}
        # record -> file from the FDs' DATA RECORD clauses: the only route back to the
        # file when the record's 01 is not in the visible source. A record two FDs both
        # claim maps to None - naming either one would be a guess.
        self.fd_files = set(fd_records or {})
        self.data_record_file: Dict[str, Optional[str]] = {}
        for fd, recs in sorted((fd_records or {}).items()):
            for rec in recs:
                known = self.data_record_file.get(rec, fd)
                self.data_record_file[rec] = fd if known == fd else None
        self.children: Dict[str, List[str]] = {}
        # file -> its top-level record names, indexed in the SAME pass as children.
        # Answering this by re-filtering the whole data dictionary per I/O statement
        # was O(statements x data items) on copybook-heavy programs.
        self.records: Dict[str, List[str]] = {}
        for name, it in self.data.items():
            if not isinstance(it, dict):
                continue
            if not it.get("parent") and it.get("file") \
                    and it.get("kind") != "condition-name":
                self.records.setdefault(it["file"], []).append(name)
        # Every group's members - each declaration of a name declared under several
        # groups included (ledger item 154) - from the one index `storage` lays records
        # out from, so a record's fields and its byte layout cannot disagree.
        for parent, pairs in children_index(self.data).items():
            if parent:
                self.children[parent] = [n for n, _ in pairs]
        # `leaves` is a pure function of the data dictionary, which is fixed for this
        # object's lifetime - but it is called once per classification, and the lineage
        # fixpoint classifies every state once per VISIT. The deque made each walk
        # linear; what costs is the call COUNT, so a wide COMMAREA or FD record was
        # re-expanded on every visit. Per-INSTANCE, never module-level: two _DataViews
        # over different programs must not share.
        self._leaves_memo: Dict[str, List[str]] = {}
        self._record_fields_memo: Dict[str, List[str]] = {}

    def file_of(self, name: str) -> Optional[str]:
        it = self.data.get((name or "").upper())
        return ((it.get("file") if isinstance(it, dict) else None)
                or self.data_record_file.get((name or "").upper()))

    def is_file(self, name: str) -> bool:
        """An FD/SD this DATA DIVISION declares, whether or not its records are visible."""
        return name in self.records or name in self.fd_files

    def records_of(self, file_name: str) -> List[str]:
        return self.records.get(file_name, [])

    def leaves(self, name: str) -> List[str]:
        """Elementary (leaf) fields under `name`, in dictionary order; the record's
        actual field layout, for field-level interface fidelity.

        Every leaf is returned - a record's fields are its interface, and a wide COMMAREA
        or FD layout that ran past a fixed cap (this once stopped at 64) silently lost the
        rest with no sign they existed. The deque made the walk linear, so listing all of
        them is cheap; a `seen` guard keeps a malformed or REDEFINES-tangled layout that
        points a child back at an ancestor from looping instead of relying on that cap.
        """
        out: List[str] = []
        root = (name or "").upper()
        # A deque: the previous list `pop(0)` was O(n) per step and `kids + stack`
        # reallocated the whole frontier on every expansion - quadratic on a wide
        # COMMAREA/FD record with hundreds of subordinate fields.
        cached = self._leaves_memo.get(root)
        if cached is not None:
            # The SAME list object, not a copy: every caller here treats it as read-only
            # (comprehensions and set unions), and copying per call would give back the
            # allocation this exists to remove. Byte-stability is what proves it - see
            # tools/gate.py.
            return cached
        stack: Deque[str] = deque([root])
        seen: set = set()
        while stack:
            cur = stack.popleft()
            if cur in seen:
                continue
            seen.add(cur)
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
        self._leaves_memo[root] = out
        return out

    def record_fields(self, record: str) -> List[str]:
        """`record` plus its elementary fields (the data crossing with the record)."""
        cached = self._record_fields_memo.get(record)
        if cached is not None:
            return cached
        leaves = self.leaves(record)
        if not leaves or leaves == [record]:
            out = [record] if record in self.data or record else []
        else:
            out = [record] + leaves
        self._record_fields_memo[record] = out
        return out


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #

def _display_fields(cobol: str, data: dict) -> List[str]:
    """Identifiers among DISPLAY operands (literals dropped, UPON clause skipped)."""
    body = re.sub(r"^\s*DISPLAY\b", "", cobol or "", flags=re.I)
    # Mask FIRST - the mask sat one line below the split, so a literal containing UPON
    # ('REPORT UPON REQUEST') cut the operand list inside itself, naming the phantom
    # field REPORT and dropping every real operand after the literal.
    body = _STR_LIT.sub(" ", body)
    body = re.split(r"\bUPON\b", body, flags=re.I)[0]
    # `DISPLAY X OF R` displays X: the qualifier is not an operand (ledger item 155).
    body = re.sub(r"\b(?:OF|IN)\s+[A-Z0-9][A-Z0-9-]*", " ", body, flags=re.I)
    out = []
    for w in _WORD.findall(body.upper()):
        if w in ("NO", "WITH", "ADVANCING"):
            continue
        if w in data and w not in out:
            out.append(w)
    return out


def _data_targets(spec: Optional[dict]) -> List[str]:
    """The assignment targets of a SELECT/FETCH that receive COLUMN DATA.

    Every INTO target except the statement's null indicators. Db2 writes an indicator
    too, so it IS an assignment (statechart._exec_action keeps it, or the variable would
    look never-written and a program branching on it would read its seed) - but what it
    receives is null status, not a column's value. Weighed against the select list it
    makes the counts disagree and refuses a correlation the statement proves; reported
    among the event's fields it becomes a field the tracer then cannot map to any
    column. One rule, so those two cannot come to disagree about which is which.

    A spec with no `indicatorVars` (none in the statement, or a parse bundle written
    before VERSION 4) answers exactly as the plain target list always did.
    """
    targets = [a["target"] for a in (spec or {}).get("assignments", [])
               if isinstance(a, dict) and "target" in a]
    indicators = {str(v).upper() for v in ((spec or {}).get("indicatorVars") or ())}
    return [t for t in targets if str(t).upper() not in indicators]


def _depth0(text: str) -> str:
    """``text`` with everything inside parentheses blanked (the outermost parens kept),
    same length: a top-level comma, ``=`` or ``INTO`` found in it slices the original."""
    out, depth = [], 0
    for ch in text:
        if ch == ")":
            depth -= 1
        out.append(ch if depth <= 0 else " ")
        if ch == "(":
            depth += 1
    return "".join(out)


def _top_split(text: str) -> List[str]:
    """Split at the top-level commas."""
    flat, parts, start = _depth0(text), [], 0
    for i, ch in enumerate(flat):
        if ch == ",":
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return parts


def _first_host_var(text: str) -> Optional[str]:
    """The host variable an assignment target names; an indicator after it is not one."""
    found = _sql_host_vars(text)
    return found[0] if found else None


def _sql_assignments(mup: str, verb: str) -> List[Tuple[str, str]]:
    """``(host variable, expression)`` for each target a Db2 SET or VALUES INTO assigns,
    over the MASKED statement text.

    ``SET :A = e1, :B = e2``, ``SET (:A, :B) = (e1, e2)`` and ``VALUES (e1, e2) INTO :A,
    :B`` pair each target with its own expression; when the counts do not pair (a row
    fullselect), each target gets the whole right-hand side. A target that is not a host
    variable (``SET CURRENT SQLID = :X``) assigns no field of the program."""
    body = re.sub(r"^\s*EXEC\s+SQL\s+", "", mup)
    body = re.sub(r"\s*END-EXEC\b.*$", "", body, flags=re.S)
    pairs: List[Tuple[str, str]] = []

    def assign(lhs: str, rhs: str) -> None:
        lhs = lhs.strip()
        if lhs.startswith("(") and re.fullmatch(r"\(\s*\)", _depth0(lhs)):
            lhs = lhs[1:-1]
        targets = [t for t in (_first_host_var(e) for e in _top_split(lhs)) if t]
        rhs = rhs.strip()
        exprs = [rhs]
        if (len(targets) > 1 and re.fullmatch(r"\(\s*\)", _depth0(rhs))
                and not _SQL_FULLSELECT.match(rhs)):
            exprs = _top_split(rhs[1:-1])
        for i, t in enumerate(targets):
            pairs.append((t, exprs[i] if len(exprs) == len(targets) else rhs))

    if verb == "SET":
        pieces: List[str] = []
        for piece in _top_split(re.sub(r"^SET\s+", "", body)):
            if "=" in _depth0(piece) or not pieces:
                pieces.append(piece)
            else:  # a comma inside an expression that is not in parens
                pieces[-1] += "," + piece
        for piece in pieces:
            eq = _depth0(piece).find("=")
            if eq >= 0:
                assign(piece[:eq], piece[eq + 1:])
    elif verb == "VALUES":
        into = None
        for m in re.finditer(r"\bINTO\b", _depth0(body)):
            into = m
        if into:
            assign(body[into.end():], re.sub(r"^VALUES\s+", "", body[:into.start()]))
    return pairs


def _exec_verb(up: str, spec: Optional[dict]) -> str:
    verb = (spec or {}).get("verb", "")
    if not verb:
        toks = up.replace("EXEC", "", 1).split()
        # skip the language word (SQL/CICS/DLI) to reach the verb
        verb = toks[1] if len(toks) > 1 and toks[0] in ("SQL", "CICS", "DLI") else (
            toks[0] if toks else "")
    return verb.upper()


def _exec_writes(name: str, cobol: str, spec: Optional[dict]) -> List[Tuple[str, List[str]]]:
    """``(field, the fields its value is computed from)`` for each field an EXEC
    statement writes INSIDE the program - the lineage view's `flow` for statements that
    have no `assignments`.

    CICS FORMATTIME formats its ABSTIME operand (and the separators it is given as
    fields) into every output option. A Db2 SET / VALUES INTO assigns each target host
    variable from the host variables in its expression. A target filled from the clock
    alone (`_classify_exec` gives it a system read) has no write of its own here; one
    filled from the clock AND other host variables has both."""
    up = (cobol or "").upper()
    verb = _exec_verb(up, spec)
    mup = mask_literals(up)
    if ("EXEC CICS" in up or name.startswith("exec_cics")) and verb == "FORMATTIME":
        src = _CICS_ABSTIME.findall(mup) + _FORMATTIME_SEP.findall(mup)
        return [(t, list(src)) for t in _dedup(_FORMATTIME_OUT.findall(mup))]
    if ("EXEC SQL" in up or name.startswith("exec_sql")) and verb in ("SET", "VALUES"):
        out = []
        for t, expr in _sql_assignments(mup, verb):
            srcs = _sql_host_vars(expr)
            if _SQL_SELECT.search(expr):
                continue  # filled by the table read `_classify_exec` publishes
            if srcs or not _SQL_CLOCK.search(expr):
                out.append((t, srcs))
        return out
    return []


def _sql_host_vars(text: str) -> List[str]:
    out = []
    for base, qualified in _SQL_HOSTVAR.findall(text or ""):
        u = (qualified or base).upper()
        if u not in out:
            out.append(u)
    return out


def _qualify(columns: Optional[List[dict]], table: str) -> Optional[List[dict]]:
    """Stamp the table onto each mapping: `BAL` alone is not an identity, `CUST.BAL` is."""
    if not columns:
        return None
    return [{"table": table, **c} for c in columns]


def _dedup(names: List[str]) -> List[str]:
    """Order-preserving unique. A variable named twice in one statement is ONE field,
    not two: `SET LAST_ID = :WS-ID WHERE ID = :WS-ID` mentions it in each clause, and
    the statement-wide scan keeps both - which reported the field twice and traced its
    lineage twice with it."""
    out: List[str] = []
    for n in names:
        if n not in out:
            out.append(n)
    return out


def _dml_split(host_vars: List[str], where_vars: Set[str],
               columns: Optional[List[dict]]) -> Tuple[List[str], List[str]]:
    """A DML statement's host variables, split into the data it WRITES and the
    parameters that choose the ROW.

    `UPDATE t SET c = :h WHERE k = :f` sends both to Db2, but only :h reaches a column.
    Reported among the FIELDS, :f reads as a write whose column mapping went missing -
    which is precisely what an unmapped-field warning means downstream, and there were
    thousands of them. As a PARAMETER it is neither dropped nor mistaken for a write:
    the SELECT half of this module has said it that way all along (`params`, below), and
    this brings the CREATE half into line with it.

    A variable used on BOTH sides (`SET LAST_ID = :WS-ID WHERE ID = :WS-ID`) stays a
    field: the write is the stronger claim, and demoting it would throw away a mapping
    the source proves. With no `whereVars` on the spec nothing is split at all - an
    unrecovered clause must not become a guess about which half is which.
    """
    mapped = {str(c.get("hostVar", "")).upper() for c in (columns or ())}
    fields = _dedup([h for h in host_vars if h not in where_vars or h in mapped])
    params = _dedup([h for h in host_vars
                     if h in where_vars and h not in mapped and h not in fields])
    return fields, params


def _read_tables(hits: List[dict], tables: Sequence[str], verb: str) -> List[dict]:
    """One `get` per further table a statement names, after its primary hits.

    The primary endpoint is the first table after FROM (or the INSERT/UPDATE/DELETE
    target), and until this every joined, comma-listed or subquery table was absent
    from every view. Each further table is READ - no fields and no columns, because
    which of its columns reach which host variable is a separate question. A table
    already an endpoint of the statement is not repeated (a qualified cursor table
    counts by its last part, the way `_sql_table_refs` names it).

    `secondaryTable` says the table is read inside Db2 and no row of it arrives as this
    hit's own delivery: without it a DELETE with a subquery read as a record delivery,
    and the reactive view waited for an event that never comes."""
    seen = {str(h["endpoint"]).split(".")[-1] for h in hits}
    for table in tables:
        if table not in seen:
            seen.add(table)
            hit = _hit("get", _DB2, table, verb, [])
            hit["secondaryTable"] = True
            hits.append(hit)
    return hits


def _note(hit: dict, note: Optional[str], unresolved: Optional[str] = None) -> dict:
    """Attach the why-no-mapping note to a hit, so the EVENT carries it.

    ``unresolved`` is the same answer as a stable token (`cursor-unidentified`,
    `cursor-declare-missing`, `count-mismatch`, `insert-no-column-list`). A consumer
    branches on that; the prose is for the person reading the flag, and was never a
    contract to match on.

    A cursor DECLAREd FOR a PREPAREd statement deliberately carries NO token: it is not
    a failed recovery but an inherent unknown, and it says so with prose plus
    ``endpointType: "dynamic_sql"`` and ``dynamic: true`` - the same shape the
    PREPARE end of the same statement already uses.
    """
    if note:
        hit["columnNote"] = note
    if unresolved:
        hit["columnsUnresolved"] = unresolved
    return hit


# Positioning keywords Db2 allows between FETCH and its cursor name. Kept in step with
# parser._FETCH_POS, which is the primary reader - this text fallback runs only for an
# action with no spec (an overlay built over a rewritten config, as reactive's is).
_FETCH_CURSOR = re.compile(
    r"\bFETCH\s+(?:(?:NEXT|PRIOR|FIRST|LAST|CURRENT|BEFORE|AFTER|ABSOLUTE|RELATIVE"
    r"|ROWSET|FROM|INSENSITIVE|SENSITIVE|STARTING|AT|CONTINUE)\s+|\d+\s+"
    r"|:[A-Z0-9_-]+\s+)*([A-Z0-9_-]+)")


def _fetch_cursor_name(spec: Optional[dict], up: str) -> Optional[str]:
    """The cursor a FETCH reads: the parser's token-resolved name where there is one.

    `FETCH NEXT ROWSET FROM CSR1` used to bind the cursor name "ROWSET", which matches
    no DECLARE - costing the FETCH both its column mapping and its real table endpoint.
    """
    name = (spec or {}).get("cursor")
    if name:
        return name.upper()
    m = _FETCH_CURSOR.search(up)
    if not m or m.group(1) in ("INTO", "FOR"):
        return None
    return m.group(1).upper()


def _fetch_columns(cursor: Optional[str], into_fields: List[str],
                   cursor_cols: Dict[str, List[str]],
                   cursor_derivations: Optional[Dict[str, List[Optional[dict]]]] = None
                   ) -> Tuple[Optional[List[dict]], Optional[str], Optional[str]]:
    """Correlate a FETCH's host variables against its cursor's select list.

    Same count gate as the parser's: the columns come from one statement and the host
    variables from another, so only equal lengths prove the correspondence. Returns
    ``(columns, note, reason)`` - the note says which half of the join was missing, so a
    FETCH whose cursor is declared in an absent copybook reads differently from one
    whose DECLARE is right there but disagrees on arity.

    The REASON says the same thing as a stable token. Prose is for the human reading the
    flag; a consumer that has to branch on it either matches on wording that was never a
    contract, or - as the v52 trace did - reports every field of the event separately as
    an unmapped field, when the whole EVENT is one known unknown.
    """
    if not cursor:
        return None, ("the cursor this FETCH reads could not be identified, so its "
                      "columns cannot be recovered"), "cursor-unidentified"
    cols = cursor_cols.get(cursor)
    if not cols:
        # No cause is asserted, because this branch holds at least three: a copybook
        # that did not arrive, an `EXEC SQL INCLUDE` member that did not resolve, and
        # `ALLOCATE c CURSOR FOR RESULT SET` (a called procedure's result set, which
        # has no DECLARE anywhere by construction). Naming one of them was not merely
        # vague - for a cursor DECLAREd in the same source it was FALSE, and it sent
        # the reader after a copybook that had never failed to arrive.
        return None, (f"no DECLARE for cursor {cursor} was found in the expanded "
                      f"source, so its columns are unknown"), "cursor-declare-missing"
    if len(cols) != len(into_fields):
        return None, (f"cursor {cursor} selects {len(cols)} column(s) but this FETCH "
                      f"has {len(into_fields)} host variable(s), host structures already "
                      f"expanded and null indicators already attached: not correlatable "
                      f"- a group whose data division entry did not arrive cannot be "
                      f"expanded - verify by hand"), "count-mismatch"
    # A derived slot gets an explicit `derived` entry, same rule as parser._correlate:
    # "an aggregate fills this by construction" must stay distinguishable from "the
    # recovery failed here", or a consumer cannot skip one without hiding the other.
    # A cursor splits the evidence over two statements, so the DERIVATION lives on the
    # DECLARE - `DECLARE c CURSOR FOR SELECT SUM(ADJ_A), ...` is where the source
    # column is written, and the FETCH that fills the slot never sees it.
    derivs = (cursor_derivations or {}).get(cursor) or []
    out: List[dict] = []
    for i, (c, h) in enumerate(zip(cols, into_fields)):
        if c is not None:
            out.append({"column": c, "hostVar": h})
            continue
        entry = {"hostVar": h, "derived": True}
        d = derivs[i] if i < len(derivs) else None
        if d:
            entry.update(d)
        out.append(entry)
    return out, None, None


def _classify_exec(name: str, cobol: str, spec: Optional[dict], dv: _DataView,
                   cursors: Dict[str, str],
                   cursor_cols: Optional[Dict[str, List[str]]] = None,
                   cursor_derivations: Optional[Dict[str, List[Optional[dict]]]] = None,
                   cursor_refs: Optional[Dict[str, List[str]]] = None
                   ) -> List[dict]:
    """Classify an EXEC SQL / CICS / DLI action -> list of boundary hits.

    ``cursor_refs`` is cursor -> every table its DECLARE names (`_cursor_table_refs`):
    a FETCH's text names only the cursor, so its further tables come from there."""
    up = cobol.upper()
    verb = _exec_verb(up, spec)
    into_fields = _data_targets(spec)
    # Masked for the keyword scans below: an SQL string constant can contain FROM
    # (`SELECT 'FROM AUDIT', COL1 ...` named the phantom Db2 table AUDIT - which the
    # artifact manifest then listed and the fetch stage requested from the estate),
    # and a time literal's colons read as host-variable markers ('12:30:45' -> the
    # phantom host vars 30 and 45).
    mup = mask_literals(up)
    host_vars = [h.lstrip(":").upper()
                 for h in ((spec or {}).get("hostVars") or _sql_host_vars(mup))]
    # The DML row selector, told apart from the data written (parser._exec_where_vars).
    where_vars = {h.lstrip(":").upper()
                  for h in ((spec or {}).get("whereVars") or ())}

    is_sql = "EXEC SQL" in up or name.startswith("exec_sql")
    is_cics = "EXEC CICS" in up or name.startswith("exec_cics")

    # Which column fills which host variable - the cross-program state identity. The
    # parser proves it for SELECT/UPDATE/INSERT; a FETCH's columns live on its cursor's
    # DECLARE (joined at build time, statechart._correlate_fetches). The NOTE says why a
    # mapping is absent - carried onto the hit so the EVENT distinguishes "nothing to
    # map here" from "the recovery failed", which downstream tooling cannot tell apart
    # from an absent key.
    columns = (spec or {}).get("columns") or None
    note = (spec or {}).get("columnNote")
    unresolved = (spec or {}).get("columnsUnresolved")

    if is_sql:
        if verb in ("SELECT", "FETCH"):
            endpoint = None
            tables = _sql_table_refs(mup) if verb == "SELECT" else []
            if verb == "FETCH":
                cname = _fetch_cursor_name(spec, up)
                if (spec or {}).get("cursorDynamic"):
                    # One direction only - a FETCH reads - but the same endpoint KIND
                    # and the same `dynamic` marking the PREPARE/EXECUTE arm below
                    # uses. The `<cursor X>` db2 endpoint this replaces was
                    # table-typed for something that has no statically knowable table,
                    # and never will have one.
                    # `columns` only when the statement's text is built from literals
                    # (ledger item 153): candidates, every entry marked `dynamic`.
                    hit = _hit("get", _DYNAMIC_SQL, "<dynamic-sql>", verb, into_fields,
                               _dedup([h for h in host_vars if h not in into_fields]),
                               columns)
                    hit["dynamic"] = True
                    if cname:
                        hit["cursor"] = cname
                    stmt = (spec or {}).get("preparedStatement")
                    if stmt:
                        hit["preparedStatement"] = stmt
                    return [_note(hit, note)]
                if cname:
                    endpoint = cursors.get(cname) or f"<cursor {cname}>"
                    tables = (cursor_refs or {}).get(cname) or []
                if columns is None and note is None:
                    # Spec-less overlay (reactive's rewritten config): correlate here.
                    columns, note, unresolved = _fetch_columns(
                        cname, into_fields, cursor_cols or {}, cursor_derivations)
            if endpoint is None:
                m = _SQL_FROM.search(mup)
                endpoint = m.group(1) if m else "<cursor>"
            params = _dedup([h for h in host_vars if h not in into_fields])
            # A select-list slot that reads only a clock register (`SELECT CURRENT_DATE
            # INTO :D FROM T`) fills its host variable from the clock, as `SET :D =
            # CURRENT DATE` does - not from a column of T, which has none of that name.
            by_reg: Dict[str, List[str]] = {}
            for c in columns or ():
                reg = str(c.get("register") or "").split(" ")[-1]
                if reg in _CLOCK_ENDPOINT:
                    by_reg.setdefault(reg, []).append(str(c["hostVar"]).upper())
            clocked = {h for fs in by_reg.values() for h in fs}
            if clocked:
                into_fields = [f for f in into_fields if f.upper() not in clocked]
                columns = [c for c in columns
                           if str(c.get("hostVar", "")).upper() not in clocked]
            hits = [_note(_hit("get", _DB2, endpoint, verb, into_fields, params,
                               _qualify(columns, endpoint)), note, unresolved)]
            hits += [_hit("get", _SYSTEM, _CLOCK_ENDPOINT[reg],
                          f"{verb} CURRENT {reg}", fields)
                     for reg, fields in by_reg.items()]
            change = _SQL_DATA_CHANGE.search(mup)
            if change:
                # The inner statement WRITES the table the SELECT reads back: one
                # statement, both directions on the same endpoint. The parser read the
                # inner statement as it would read it alone (`dataChange`): its columns,
                # its row selectors and, when it could not map them, why. A spec-less
                # overlay has none of it and splits nothing, as before.
                # A variable both written and read back (`SELECT ID INTO :X FROM NEW
                # TABLE (INSERT ... VALUES (:X))`) is sent before it is overwritten, so
                # it is the write's field as well as the read's.
                dc = (spec or {}).get("dataChange") or {}
                dcols = dc.get("columns") or None
                mapped = {str(c.get("hostVar", "")).upper() for c in (dcols or ())}
                fields, wparams = _dml_split(
                    _dedup([h for h in host_vars if h not in into_fields or h in mapped]),
                    {h.lstrip(":").upper() for h in dc.get("whereVars") or ()}, dcols)
                hits.append(_note(_hit("create", _DB2, endpoint, change.group(1).upper(),
                                       fields, wparams, _qualify(dcols, endpoint)),
                                  dc.get("columnNote"), dc.get("columnsUnresolved")))
            return _read_tables(hits, tables, verb)
        if verb == "INSERT":
            m = _SQL_INTO_TABLE.search(up)
            ep = m.group(1) if m else "<table>"
            # A plain VALUES insert has no WHERE and splits into nothing; an
            # `INSERT ... SELECT ... WHERE` filters the rows it copies.
            fields, params = _dml_split(host_vars, where_vars, columns)
            return _read_tables([_note(_hit("create", _DB2, ep, verb, fields, params,
                                             columns=_qualify(columns, ep)),
                                       note, unresolved)], _sql_table_refs(mup), verb)
        if verb == "UPDATE":
            m = _SQL_UPDATE.search(up)
            ep = m.group(1) if m else "<table>"
            fields, params = _dml_split(host_vars, where_vars, columns)
            return _read_tables([_note(_hit("create", _DB2, ep, verb, fields, params,
                                             columns=_qualify(columns, ep)),
                                       note, unresolved)], _sql_table_refs(mup), verb)
        if verb == "MERGE":
            # MERGE INTO t USING ... WHEN MATCHED THEN UPDATE ... WHEN NOT MATCHED THEN
            # INSERT: a write to t either way, and a USING subquery's tables are read.
            m = _SQL_MERGE_INTO.search(up)
            ep = m.group(1) if m else "<table>"
            fields, params = _dml_split(host_vars, where_vars, columns)
            return _read_tables([_note(_hit("create", _DB2, ep, verb, fields, params,
                                             columns=_qualify(columns, ep)),
                                       note, unresolved)], _sql_table_refs(mup), verb)
        if verb == "WITH":
            # A leading WITH introduces a query: the SELECT behind its common table
            # expressions reads the tables they name, and its INTO is the delivery.
            # A CTE is not a table (`_sql_table_refs`), so the endpoint is the first
            # base table the statement reads.
            tables = _sql_table_refs(mup)
            ep = tables[0] if tables else "<table>"
            params = _dedup([h for h in host_vars if h not in into_fields])
            return _read_tables([_note(_hit("get", _DB2, ep, verb, into_fields, params),
                                       note, unresolved)], tables, verb)
        if verb == "DELETE":
            m = _SQL_FROM.search(mup)
            # EVERY host variable in a DELETE is a filter. The statement has no SET and
            # no VALUES clause, so it writes no field's value anywhere - it only picks
            # which row goes. That holds structurally, which is why this needs no
            # `whereVars` to be sure of it: a spec-less overlay splits the same way.
            return _read_tables([_hit("create", _DB2, m.group(1) if m else "<table>", verb,
                                      [], params=_dedup(host_vars))],
                                _sql_table_refs(mup), verb)
        if verb in ("PREPARE", "EXECUTE"):
            # Dynamic SQL: the statement text is a run-time value, so the operation and
            # table(s) are not statically knowable. One endpoint, both directions (the
            # statement could read or write), marked dynamic; the fields are the host
            # variables that carry/parameterize the statement.
            v = verb + (" IMMEDIATE" if "IMMEDIATE" in up else "")
            hits = [_hit("get", _DYNAMIC_SQL, "<dynamic-sql>", v, host_vars),
                    _hit("create", _DYNAMIC_SQL, "<dynamic-sql>", v, host_vars)]
            # The statement this PREPAREs or EXECUTEs: `PREPARE S1 FROM ...`,
            # `EXECUTE S1 USING ...`. Same key the dynamic-cursor FETCH carries, so the
            # three ends of one statement join. EXECUTE IMMEDIATE names none.
            m = _SQL_PREPARED_NAME.match(up)
            for h in hits:
                h["dynamic"] = True
                if m:
                    h["preparedStatement"] = m.group(1)
            return hits
        if verb == "CALL":
            # A STORED PROCEDURE, not a table: the operands are the procedure's
            # parameters (linkage), so there are no columns to map - and saying so on
            # the event stops downstream tooling from hunting for them. Both
            # directions, because which parameters are IN and which are OUT is the
            # procedure's signature, which is not in this source.
            # `CALL :X` names it through a host variable: statechart._exec_resources
            # resolved X (PROCEDURE) or kept it as the item, marked dynamic - the same
            # answer a dynamic COBOL CALL publishes. X is the procedure's NAME, not a
            # parameter: as a field, the procedure would fill the item that names it.
            rs = (spec or {}).get("resources") or {}
            proc = _resource(rs, "PROCEDURE", (spec or {}).get("target"))
            params = list(host_vars)
            r = rs.get("PROCEDURE")
            if r and (r.get("via") or r["name"]) in params:
                params.remove(r.get("via") or r["name"])
            if not proc:
                m = _SQL_CALL.search(mup)
                proc = m.group(1).upper() if m else "<procedure>"
            note = ("stored-procedure parameters, not table columns; their IN/OUT "
                    "direction is the procedure's signature, which is not in this "
                    "source")
            return [_mark_dynamic(_note(_hit(d, _DB2_PROC, proc, verb, params), note),
                                  rs, "PROCEDURE")
                    for d in ("get", "create")]
        if verb in ("SET", "VALUES"):
            # `SET :hv = CURRENT TIMESTAMP` reads the clock into :hv, as `ACCEPT ... FROM
            # TIME` does: one system read per register, filling every target whose
            # expression names it. A host variable on the right is an internal write
            # (`_exec_writes`). A target assigned from a fullselect is filled by a read
            # of its first table, as `SELECT ... INTO` is, with the expression's host
            # variables as that read's params; any further table is read as usual.
            hits: List[dict] = []
            by_reg: Dict[str, List[str]] = {}
            by_table: Dict[str, Tuple[List[str], List[str]]] = {}
            for t, expr in _sql_assignments(mup, verb):
                if _SQL_SELECT.search(expr):
                    table = next(iter(_sql_table_refs(expr)), "<table>")
                    fields, params = by_table.setdefault(table, ([], []))
                    fields.append(t)
                    params.extend(h for h in _sql_host_vars(expr) if h not in params)
                    continue
                for reg in _dedup(_SQL_CLOCK.findall(expr)):
                    fields = by_reg.setdefault(reg, [])
                    if t not in fields:
                        fields.append(t)
            for table, (fields, params) in by_table.items():
                hits.append(_hit("get", _DB2, table, verb, _dedup(fields), params))
            for reg, fields in by_reg.items():
                hits.append(_hit("get", _SYSTEM, _CLOCK_ENDPOINT[reg],
                                 f"{verb} CURRENT {reg}", fields))
            return _read_tables(hits, _sql_table_refs(mup), verb)
        diag = _SQL_GET_DIAGNOSTICS.match(mup) if verb == "GET" else None
        if diag:
            # GET DIAGNOSTICS [CONDITION n] :hv = item, ... stores Db2's diagnostics
            # area into each target (ledger item 157): a system read, like SET :hv =
            # CURRENT TIMESTAMP. A host variable after CONDITION picks the condition;
            # it is a param, not a target.
            body = mup[diag.end():]
            cond = re.match(r"\s*CONDITION\s+(:\s*[A-Z0-9][A-Z0-9-]*|\d+)\s*", body)
            params = _sql_host_vars(cond.group(1)) if cond else []
            if cond:
                body = body[cond.end():]
            targets = [t for t, _ in _sql_assignments("SET " + body, "SET")]
            return [_hit("get", _SYSTEM, "DB2-DIAGNOSTICS", "SQL GET DIAGNOSTICS",
                         _dedup(targets), params)] if targets else []
        change = (spec or {}).get("dataChange") if verb == "OPEN" else None
        if change:
            # A cursor over `FROM FINAL TABLE (INSERT ...)`: opening it runs the inner
            # write (statechart._mark_data_change_opens). A DELETE's host variables
            # only pick rows, as in the DELETE arm above; the others split on the
            # inner statement's own columns and row selectors, which a parse bundle
            # older than VERSION 14 does not carry - and then nothing is split.
            hv = list(change.get("hostVars") or [])
            if change["verb"] == "DELETE":
                return [_hit("create", _DB2, change["table"], change["verb"], [],
                             params=hv)]
            dcols = change.get("columns") or None
            fields, wparams = _dml_split(
                hv, {h.lstrip(":").upper() for h in change.get("whereVars") or ()}, dcols)
            return [_note(_hit("create", _DB2, change["table"], change["verb"], fields,
                               wparams, _qualify(dcols, change["table"])),
                          change.get("columnNote"), change.get("columnsUnresolved"))]
        cname = (spec or {}).get("unfetchedCursor") if verb == "OPEN" else None
        refs = ((cursor_refs or {}).get(cname) or []) if cname else []
        table = (cursors.get(cname) or next(iter(refs), None)) if cname else None
        if table:
            # Db2 runs a cursor's query when it is OPENed. One no FETCH of this program
            # reads (statechart._mark_unfetched_opens) - a stored procedure's CURSOR
            # WITH RETURN, handed to its caller - is published here, or none of its
            # tables reached any view. No row arrives at OPEN, so there are no fields
            # or columns and every table is `secondaryTable`, which also keeps the
            # reactive view from waiting on a delivery. A select wrapped whole in
            # parens has no `table`, only `tables`; a cursor over a PREPAREd statement
            # has neither and still publishes nothing.
            hit = _hit("get", _DB2, table, verb, [], _dedup(host_vars))
            hit["cursor"] = cname
            hit["secondaryTable"] = True
            return _read_tables([hit], refs, verb)
        return []  # OPEN/CLOSE cursor, DECLARE, COMMIT, WHENEVER - not a data crossing

    if is_cics:
        # Every command's own hits, plus the options it stores into that its own
        # case does not publish (ledger item 158).
        opts = {k.upper(): v.upper() for k, v in _CICS_OPT.findall(up)}
        return (_classify_cics(up, mup, verb, spec, dv)
                + _cics_receivers(verb, mup, opts))

    # DLI / IMS
    if verb in ("GU", "GN", "GNP", "GHU", "GHN"):
        return [_hit("get", _IMS, "IMS-DB", "DLI " + verb, [])]
    if verb in ("ISRT", "REPL", "DLET"):
        return [_hit("create", _IMS, "IMS-DB", "DLI " + verb, [])]
    return []


_CICS_RESP = re.compile(r"\bRESP2?\s*\(\s*([A-Z0-9][A-Z0-9-]*)\s*\)")
_CICS_ANY_OPT = re.compile(r"\b([A-Z][A-Z0-9]*)\s*\(\s*([A-Z][A-Z0-9-]*)\s*\)")
_CICS_TOLENGTH = re.compile(r"\bTOF?LENGTH\s*\(\s*([A-Z0-9][A-Z0-9-]*)\s*\)")
_CICS_SET_ADDRESS = re.compile(r"\bSET\s*\(\s*ADDRESS\s+OF\s+([A-Z0-9][A-Z0-9-]*)\s*\)")


def _cics_receivers(verb: str, mup: str, opts: dict) -> List[dict]:
    """The fields an EXEC CICS command stores into that its own case does not publish
    (ledger item 158): RESP/RESP2 on any command, every ASSIGN option, GET CONTAINER /
    CONVERSE / WEB RECEIVE INTO. Each is a fill with an endpoint type a consumer can
    judge - CICS put the value there, so none of them is "internally set"."""
    hits = []
    resp = _dedup(_CICS_RESP.findall(mup))
    if resp:
        hits.append(_hit("get", _SYSTEM, "CICS-RESPONSE", f"CICS {verb} RESP", resp))
    if verb == "ASSIGN":
        f = _dedup([v for k, v in _CICS_ANY_OPT.findall(mup) if k not in ("RESP", "RESP2")])
        if f:
            hits.append(_hit("get", _SYSTEM, "CICS-ASSIGN", "CICS ASSIGN", f))
    # A container's data and an HTTP request body arrive from whoever invoked this
    # program - its channel, its web client - so they are the caller's, as a COMMAREA
    # is. Named CONTAINER or "web", they became artifact rows the fetch stage would
    # request from the estate: a CSD transaction CONTAINER, a BMS map "web".
    if verb == "GET" and re.search(r"\bCONTAINER\b", mup) and "INTO" in opts:
        hits.append(_hit("get", _CALLER, "CALLER", "CICS GET CONTAINER", [opts["INTO"]]))
    if verb == "WEB" and re.search(r"\bRECEIVE\b", mup) and "INTO" in opts:
        hits.append(_hit("get", _CALLER, "CALLER", "CICS WEB RECEIVE", [opts["INTO"]]))
    if verb == "CONVERSE" and "INTO" in opts:
        # Mapless terminal input, read the way a mapless RECEIVE's is.
        hit = _hit("get", _TERMINAL, "terminal", "CICS CONVERSE", [opts["INTO"]])
        hit["terminalInput"] = {"buffer": False}
        length = _CICS_TOLENGTH.search(mup)
        if length:
            hit["terminalInput"]["length"] = length.group(1)
        hits.append(hit)
    return hits


def _classify_cics(up: str, mup: str, verb: str, spec: Optional[dict],
                   dv: _DataView) -> List[dict]:
    """The CICS arm of `_classify_exec`: one command's own boundary hits."""
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
        return [_with_mapset(_mark_dynamic(
            _hit("create", _TERMINAL, ep, "CICS SEND", from_), rs, "MAP", "MAPSET"),
            rs)]
    if verb == "RECEIVE":
        ep = _resource(rs, "MAP", _resource(rs, "MAPSET", endpoint or "terminal"))
        hit = _with_mapset(_mark_dynamic(
            _hit("get", _TERMINAL, ep, "CICS RECEIVE", into), rs, "MAP", "MAPSET"), rs)
        if (not _CICS_MAP_OPERAND.search(mup)
                and not _CICS_CONVERSATION.search(mup)):
            # Mapless: the area holds the raw terminal input. Say which area, how
            # it is read and how long, so a consumer holding the transaction
            # definitions can name what arrives in it.
            terminal = {"buffer": bool(_CICS_BUFFER.search(mup))}
            length = _CICS_LENGTH.search(mup)
            if length:
                terminal["length"] = length.group(1)
            if "SET" in opts:
                terminal["set"] = opts["SET"]
            hit["terminalInput"] = terminal
        return [hit]
    if verb == "WRITE" and _CICS_OPERATOR.search(mup):
        # WRITE OPERATOR: a message to the system console operator, not a record to
        # a file. REPLY brings the operator's answer back.
        text, reply = _CICS_TEXT.search(mup), _CICS_REPLY.search(mup)
        hits = [_hit("create", _CONSOLE, "OPERATOR", "CICS WRITE OPERATOR",
                     [text.group(1)] if text else [])]
        if reply:
            hits.append(_hit("get", _CONSOLE, "OPERATOR", "CICS WRITE OPERATOR",
                             [reply.group(1)]))
        return hits
    if verb == "DELETE" and _CICS_DELETE_NOT_A_FILE.search(mup):
        return []  # a container, counter, timer, event or activity - like GET/PUT
    if verb in ("READ", "READNEXT", "READPREV"):
        f = _resource(rs, "FILE", _resource(rs, "DATASET", endpoint or "<file>"))
        # Locate mode, SET(ADDRESS OF X): the record is X (ledger item 158). With
        # neither INTO nor SET, only a declared record of that name is a field - never
        # a phantom field named after the file.
        sa = _CICS_SET_ADDRESS.search(mup)
        fields = into or (dv.record_fields(sa.group(1)) if sa else
                          [x for x in dv.record_fields(f) if x in dv.data])
        return [_mark_dynamic(
            _hit("get", _FILE, f, "CICS " + verb, fields, ridfld),
            rs, "FILE", "DATASET")]
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
        q = _queue(rs, endpoint, opts)
        qtype = "TD" if re.search(r"\bREADQ\s+TD\b", up) else "TS"
        return [_mark_dynamic(_hit("get", _QUEUE, q, f"CICS READQ {qtype}", into),
                              rs, "QUEUE", "QNAME")]
    if verb in ("WRITEQ",):
        q = _queue(rs, endpoint, opts)
        qtype = "TD" if re.search(r"\bWRITEQ\s+TD\b", up) else "TS"
        return [_mark_dynamic(_hit("create", _QUEUE, q, f"CICS WRITEQ {qtype}", from_),
                              rs, "QUEUE", "QNAME")]
    if verb == "DELETEQ":
        q = _queue(rs, endpoint, opts)
        return [_mark_dynamic(_hit("create", _QUEUE, q, "CICS DELETEQ", []),
                              rs, "QUEUE", "QNAME")]
    if verb == "START":
        t = _resource(rs, "TRANSID", opts.get("TRANSID", "<transid>"))
        return [_mark_dynamic(_hit("create", _TRANSACTION, t, "CICS START", from_),
                              rs, "TRANSID")]
    if verb == "RETRIEVE":
        return [_hit("get", _TRANSACTION, "RETRIEVE", "CICS RETRIEVE", into)]
    if verb == "ABEND":
        code = opts.get("ABCODE", "ABEND")
        return [_hit("create", _CONDITION, code, "CICS ABEND", [])]
    if verb == "ASKTIME":
        # Without ABSTIME it refreshes only EIBDATE / EIBTIME, which no field holds.
        m = _CICS_ABSTIME.search(mup)
        return [_hit("get", _SYSTEM, "SYSTEM-TIME", "CICS ASKTIME",
                     [m.group(1)])] if m else []
    if verb == "GETMAIN":
        # GETMAIN SET(ADDRESS OF X): X is fresh storage CICS hands the program,
        # which then initialises it - its origin, not a caller's. SET(ptr) addresses
        # no record yet; the SET ADDRESS OF that later uses ptr says so.
        m = _CICS_GETMAIN_SET.search(mup)
        return [_hit("get", _SYSTEM, "SYSTEM-STORAGE", "CICS GETMAIN",
                     [m.group(1)])] if m else []
    return []  # HANDLE (-> handler region), ADDRESS, FORMATTIME (internal), etc.


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
              cursor_cols: Optional[Dict[str, List[str]]] = None,
              cursor_derivations: Optional[Dict[str, List[Optional[dict]]]] = None,
              cursor_refs: Optional[Dict[str, List[str]]] = None
              ) -> List[dict]:
    """Classify one entry action -> list of boundary hits (empty if internal).

    ``cursor_cols`` is optional: only the interface build needs a FETCH correlated to its
    cursor's columns, so the other callers (lineage, business, reactive) pass nothing and
    are unaffected.
    """
    up = (cobol or "").upper().strip()
    verb = up.split()[0] if up else ""

    if verb == "EXEC" or name.startswith(("exec_sql", "exec_cics", "exec_dli")):
        hits = _classify_exec(name, up, spec, dv, cursors, cursor_cols,
                              cursor_derivations, cursor_refs)
        # What the Db2 precompiler rewrote on the way, stamped on every hit of the
        # statement rather than at each of the SQL branch's return sites: both are facts
        # about the STATEMENT, not about one crossing it makes. `fields` holds elementary
        # items where the source wrote a group name, and the null indicators are in the
        # source but in no field list - saying so here is what keeps the expansion from
        # reading as a discrepancy between the event and the COBOL beside it.
        for key in ("expandedStructures", "indicatorVars"):
            if (spec or {}).get(key):
                for h in hits:
                    h[key] = spec[key]
        for h in hits:
            if h["endpoint"] in _PLACEHOLDERS:
                h["endpointUnresolved"] = _UNRESOLVED_NO_OPERAND
        return hits

    io = spec if (spec or {}).get("kind") == "io" else {}

    if verb in ("READ", "START"):
        f = io.get("file") or _name_suffix(name)
        if io.get("into"):
            # READ f INTO rec == READ f; MOVE fd-record TO rec. Subsequent statements
            # address rec's LEAVES, so the arriving event must carry them - exactly as
            # SQL SELECT ... INTO :a :b carries its host variables. Listing only the group
            # record name left the reactive `recv` with no elementary field to assign (a
            # group item is not a context key), so it was a runtime NO-OP that processed an
            # empty record every cycle.
            fields = dv.record_fields(io["into"])
        else:  # no INTO: the data lands in the FD record - list its field layout
            fields = [x for r in dv.records_of(f) for x in dv.record_fields(r)]
        return [_hit("get", _FILE, f, verb, fields)]
    if verb in ("WRITE", "REWRITE", "DELETE"):
        rec = io.get("file") or _name_suffix(name)
        f = dv.file_of(rec)
        fields = dv.record_fields(rec) if f else ([rec] if rec else [])
        if io.get("from"):
            fields = fields + [io["from"]]
        hit = _hit("create", _FILE, f or rec, verb, fields)
        # WRITE / REWRITE name a RECORD, DELETE names the file. A record no FD in sight
        # declares or lists is published under its own name - so it says it is one.
        if not f and verb != "DELETE" and rec not in files and not dv.is_file(rec):
            hit["endpointUnresolved"] = _UNRESOLVED_RECORD
        return [hit]
    if verb == "RETURN":  # sort OUTPUT PROCEDURE: RETURN sort-file [INTO x]
        f = io.get("file") or _name_suffix(name)
        fields = [io["into"]] if io.get("into") else []
        return [_hit("get", _FILE, f, "RETURN (sort)", fields)]
    if verb == "RELEASE":  # sort INPUT PROCEDURE: RELEASE rec [FROM x]
        m = re.match(r"RELEASE\s+([A-Z0-9-]+)(?:\s+FROM\s+([A-Z0-9-]+))?", up)
        rec = m.group(1) if m else _name_suffix(name)
        fields = [rec] + ([m.group(2)] if m and m.group(2) else [])
        hit = _hit("create", _FILE, dv.file_of(rec) or rec, "RELEASE (sort)", fields)
        if not dv.file_of(rec):
            hit["endpointUnresolved"] = _UNRESOLVED_RECORD
        return [hit]
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
        hit = _hit("create", _PROGRAM, _call_endpoint(cobol, name), "CALL",
                   _parse_call_args(cobol), params)
        # Positional: one entry per USING argument, the data name or null (a literal,
        # OMITTED, LENGTH OF) - CallStmt.arguments, carried on the action spec by
        # statechart._call_action. `fields` names what is passed; this says where.
        if spec and spec.get("arguments") is not None:
            hit["arguments"] = list(spec["arguments"])
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


_ADDRESS_OF_EXPR = re.compile(r"^ADDRESS\s+OF\s+([A-Z0-9-]+)$")
_CICS_SET_PTR = re.compile(r"\bSET\s*\(\s*([A-Z0-9][A-Z0-9-]*)\s*\)")


def address_index(actions: dict, provenance: dict
                  ) -> Tuple[Dict[str, Set[str]], Dict[str, Set[str]]]:
    """``(pointer -> items SET to its address, pointer -> every other write to it)``,
    over the whole program. A pointer is followed only when the first map holds one
    item and the second holds nothing - two addresses, or any other write (a CICS
    ``SET(ptr)``, a MOVE), and where it points is not provable here."""
    addr: Dict[str, Set[str]] = {}
    other: Dict[str, Set[str]] = {}
    for aname, sp in actions.items():
        for a in (sp or {}).get("assignments") or []:
            t = (a.get("target") or "").upper()
            e = re.sub(r"\s+", " ", (a.get("expr") or "").strip().upper())
            am = _ADDRESS_OF_EXPR.match(e)
            if am:
                addr.setdefault(t, set()).add(am.group(1))
            elif t:
                other.setdefault(t, set()).add(f"{sp.get('verb', '?')} {e}")
        if (sp or {}).get("kind") == "exec-cics":
            text = mask_literals(((provenance.get(aname) or {}).get("cobol") or "").upper())
            for pm in _CICS_SET_PTR.finditer(text):
                other.setdefault(pm.group(1), set()).add(
                    f"CICS {sp.get('verb', '?')} SET({pm.group(1)})")
    return addr, other


def own_linkage(data: Optional[dict], semantics: Optional[dict], provenance: dict,
                using: Optional[Sequence[str]]) -> Set[str]:
    """LINKAGE items that are this program's OWN storage, not the caller's: every name
    under a record whose address the program itself sets, every time, to storage that
    is not the caller's - ``SET ADDRESS OF X TO ADDRESS OF Y`` (directly, or through a
    pointer that provably holds only that address) with Y outside LINKAGE, or a CICS
    command handing it storage with ``SET(ADDRESS OF X)`` (GETMAIN, a READ / RECEIVE /
    GET CONTAINER ... SET). A store into such a field changes no byte the caller
    passed, so it is no caller output, and a read of it is no caller input.

    A record in ``PROCEDURE DIVISION USING`` and DFHCOMMAREA arrive FROM the caller,
    so they are never own. A record also given an address that is not provably own -
    an unfollowable pointer, a LINKAGE target, ``EXEC CICS ADDRESS SET`` - stays the
    caller's: it may be, and that is the safe direction for an output contract."""
    data = data or {}
    actions = (semantics or {}).get("actions", {})
    linkage = {n.upper() for n, it in data.items()
               if isinstance(it, dict) and it.get("section") == "LINKAGE"}
    if not linkage:
        return set()
    callers = {u.upper() for u in (using or ())} | {"DFHCOMMAREA"}
    addr, other = address_index(actions, provenance)
    verdicts: Dict[str, Set[bool]] = {}
    for aname, sp in actions.items():
        sp = sp or {}
        if sp.get("kind") == "address":
            ptr = re.sub(r"\s+", " ", (sp.get("pointer") or "").strip().upper())
            if ptr in ("NULL", "NULLS"):
                continue
            m = _ADDRESS_OF_EXPR.match(ptr)
            if m:
                ys = [m.group(1)]
            else:
                p = ptr.split("(")[0].strip()
                ys = [] if other.get(p) else sorted(addr.get(p, ()))
            own = len(ys) == 1 and ys[0] in data and ys[0] not in linkage
            for x in sp.get("addressOf") or []:
                verdicts.setdefault(str(x).upper(), set()).add(own)
        elif sp.get("kind") == "exec-cics":
            own = str(sp.get("verb") or "").upper() != "ADDRESS"
            text = mask_literals(((provenance.get(aname) or {}).get("cobol") or "").upper())
            for sm in _CICS_GETMAIN_SET.finditer(text):
                verdicts.setdefault(sm.group(1), set()).add(own)
    roots = sorted(x for x, v in verdicts.items()
                   if v == {True} and x in linkage and x not in callers)
    children: Dict[str, List[str]] = {}
    for n, it in data.items():
        if isinstance(it, dict) and it.get("parent"):
            children.setdefault(str(it["parent"]).upper(), []).append(n.upper())
    out: Set[str] = set()
    stack = list(roots)
    while stack:
        n = stack.pop()
        if n not in out:
            out.add(n)
            stack.extend(children.get(n, ()))
    return out


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


def seed_cursor_maps(cursors: Dict[str, str],
                     cursor_cols: Dict[str, List[Optional[str]]],
                     cursor_derivs: Dict[str, List[Optional[dict]]],
                     sql_cursors: Optional[Sequence[dict]],
                     cursor_refs: Optional[Dict[str, List[str]]] = None) -> None:
    """Fold the whole-stream cursor-DECLARE scan into the three provenance-built maps.

    `_cursor_tables` / `_cursor_columns` / `_cursor_derivations` read provenance and
    semantics, which hold PROCEDURE-DIVISION statements only. A cursor DECLAREd in the
    DATA DIVISION or a copybook - the common case, a DCLGEN-adjacent member - never
    became an action, so those maps have no entry for it and a FETCH on it mints a
    phantom `<cursor X>` endpoint instead of naming the real table.

    Every view that classifies a boundary crossing builds these same three maps, and
    each one that forgets to seed them disagrees with the interface overlay about what
    a FETCH touches: the bundle event says the table while the lineage row says
    `<cursor X>`, and no (event, endpoint) join between the two views is possible for
    that class. This exists so the next view cannot reintroduce that by omission.

    `setdefault`, not assignment: a DECLARE the procedure division CAN see is the more
    specific evidence and wins. Mutates in place and returns None - the caller owns the
    maps.
    """
    for decl in (sql_cursors or []):
        cname = str(decl.get("cursor", "")).upper()
        if not cname:
            continue
        if decl.get("table"):
            cursors.setdefault(cname, str(decl["table"]).upper())
        if decl.get("selectList"):
            cursor_cols.setdefault(cname, decl["selectList"])
        if decl.get("selectDerivations"):
            cursor_derivs.setdefault(cname, decl["selectDerivations"])
        if cursor_refs is not None and decl.get("tables"):
            cursor_refs.setdefault(cname, decl["tables"])


def _cursor_tables(provenance: dict) -> Dict[str, str]:
    """cursor-name -> table, from ``DECLARE c CURSOR FOR SELECT ... FROM t`` texts."""
    out: Dict[str, str] = {}
    for prov in (provenance or {}).values():
        # Masked: a string constant in the cursor's select list could otherwise supply
        # the FROM this regex binds the cursor's table from.
        m = _DECLARE_CURSOR.search(mask_literals((prov or {}).get("cobol", "") or ""))
        if m:
            out[m.group(1).upper()] = m.group(2).upper()
    return out


def _cursor_table_refs(provenance: dict) -> Dict[str, List[str]]:
    """cursor-name -> EVERY table its DECLARE names, from the same texts as
    `_cursor_tables`. Its own map because three call sites depend on that one's values
    being one plain table name; a cursor over a join reads them all."""
    out: Dict[str, List[str]] = {}
    for prov in (provenance or {}).values():
        text = mask_literals((prov or {}).get("cobol", "") or "").upper()
        m = _DECLARE_CURSOR.search(text)
        if m:
            out[m.group(1)] = _sql_table_refs(text)
    return out


def _cursor_derivations(semantics: dict) -> Dict[str, List[Optional[dict]]]:
    """cursor-name -> its select list's DERIVATIONS, the companion to _cursor_columns.

    Its own map for the same reason that one is: three call sites depend on the values
    of `_cursor_tables` being plain table names, and a FETCH needs the columns whether
    or not any slot is derived. Keyed off the parser's cursor name only - the text
    fallback in `_cursor_columns` exists for specs that predate it, and a spec that old
    carries no derivations to find.
    """
    out: Dict[str, List[Optional[dict]]] = {}
    for spec in ((semantics or {}).get("actions", {}) or {}).values():
        if not isinstance(spec, dict) or spec.get("verb") != "DECLARE":
            continue
        cursor, derivs = spec.get("cursor"), spec.get("selectDerivations")
        if cursor and derivs:
            out.setdefault(cursor.upper(), derivs)
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
        # The parser names the cursor from the DECLARE's tokens; the text scan is the
        # fallback for a spec that predates it (or an overlay built over rewritten
        # actions), masked so a literal in the select list cannot supply the name.
        cursor = spec.get("cursor")
        if not cursor:
            m = _DECLARE_CURSOR.search(
                mask_literals((provenance.get(name) or {}).get("cobol", "") or ""))
            cursor = m.group(1) if m else None
        if cursor:
            out[cursor.upper()] = cols
    return out


def build_interface(config: dict, semantics: dict, provenance: dict,
                    data: Optional[dict] = None, using: Optional[List[str]] = None,
                    returning: Optional[str] = None,
                    files: Optional[Dict[str, dict]] = None,
                    internal_programs: Optional[set] = None,
                    sql_cursors: Optional[List[dict]] = None,
                    unresolved_calls: Optional[Dict[str, dict]] = None,
                    fd_records: Optional[Dict[str, List[str]]] = None) -> dict:
    """Return the external-interface overlay: events, per-state get/create, endpoints,
    and the program's own parameter interface.

    Pure read over the emitted machine - attributes each boundary crossing to the state
    that hosts it, the direction, the external endpoint, the source line, and the
    fields crossing. The program entry's LINKAGE / ``PROCEDURE DIVISION USING`` /
    ``RETURNING`` (its COMMAREA / parameters) is the perimeter at the entry point and
    is surfaced under ``parameters``.

    ``internal_programs`` names the programs CONTAINED in this source (nested
    ``PROGRAM-ID``): a CALL / LINK / XCTL to one of them is an INTERNAL call, so the
    program endpoint it produces is tagged ``internal`` rather than presented as an
    external module dependency.

    ``sql_cursors`` is the whole-stream cursor-DECLARE scan (Machine.sql_cursors) -
    cursors declared in the DATA DIVISION or a copybook, which the provenance texts
    (procedure-division statements only) never carry. Without it a FETCH on such a
    cursor lost its real table endpoint and read as ``<cursor X>``.

    ``unresolved_calls`` is Machine.unresolved_calls - what constant propagation found
    for each data item naming a dynamic target. A CALL's provenance label says only
    that its target is dynamic, never which literals may reach it, so without this the
    candidates a batch CALL already had were dropped here while a CICS operand over the
    same item kept them.
    """
    actions = (semantics or {}).get("actions", {})
    guards = (semantics or {}).get("guards", {})
    files = files or {}
    internal_programs = {str(p).upper() for p in (internal_programs or ())}
    unresolved_calls = unresolved_calls or {}
    dv = _DataView(data, fd_records)
    cursors = _cursor_tables(provenance)
    cursor_cols = _cursor_columns(semantics, provenance)
    cursor_derivs = _cursor_derivations(semantics)
    cursor_refs = _cursor_table_refs(provenance)
    seed_cursor_maps(cursors, cursor_cols, cursor_derivs, sql_cursors, cursor_refs)
    # LINKAGE whose storage the program addresses itself is not the caller's: reading it
    # is no request, writing it no response.
    own = own_linkage(data, semantics, provenance, using)
    linkage_all = {n.upper() for n, it in (data or {}).items()
                   if isinstance(it, dict) and it.get("section") == "LINKAGE"} - own
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
        if hit.get("columnNote"):
            entry["columnNote"] = hit["columnNote"]
        if hit.get("columnsUnresolved"):
            entry["columnsUnresolved"] = hit["columnsUnresolved"]
        for k in ("expandedStructures", "indicatorVars"):
            if hit.get(k):
                entry[k] = hit[k]
        # WHICH cursor, and which PREPAREd statement, a dynamic FETCH reads. Only the
        # dynamic arm sets these; without them the event says the crossing is
        # dynamic_sql but not what it is dynamic ABOUT, and `<dynamic-sql>` is shared
        # by every such crossing in the program. Deliberately NOT copied onto the
        # ENDPOINT below for that reason - one shared endpoint cannot carry one
        # cursor's name.
        for k in ("cursor", "preparedStatement"):
            if hit.get(k):
                entry[k] = hit[k]
        if hit.get("secondaryTable"):
            entry["secondaryTable"] = True
        if "terminalInput" in hit:
            entry["terminalInput"] = hit["terminalInput"]  # a mapless CICS RECEIVE
        if "mapset" in hit:
            entry["mapset"] = hit["mapset"]             # the mapset a CICS MAP is in
        if "arguments" in hit:
            entry["arguments"] = hit["arguments"]       # a CALL's USING list, by position
        # Dynamic program-target status (CALL identifier / LINK PROGRAM(data-name)):
        # `dynamic` marks an unresolved runtime target, `via` the data item a resolved
        # one came through, `candidates` the literals an ambiguous one may be, with
        # `evidence` and `hasVariableAssignment` saying how far to trust that list.
        for k in _DYNAMIC_KEYS:
            if hit.get(k):
                entry[k] = hit[k]
        events.append(entry)
        slot = perimeter.setdefault(
            state, {"region": region, "gets": [], "creates": []})
        bucket = slot["gets"] if hit["direction"] == "get" else slot["creates"]
        if ev not in bucket:
            bucket.append(ev)
        ep = endpoints.setdefault(hit["endpoint"], {"type": hit["etype"], "directions": []})
        if hit["etype"] == _FILE and ep["type"] == _RESPONSE:
            # A FILE STATUS branch names the file too. Reached before the file's own I/O
            # (a `PERFORM UNTIL status` around a READ NEXT), it published the file as a
            # response endpoint and the file endpoint never appeared at all.
            ep["type"] = _FILE
        if hit["direction"] not in ep["directions"]:
            ep["directions"].append(hit["direction"])
        for k in _DYNAMIC_KEYS:
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
            hits = _classify(aname, cobol, spec, dv, files, cursors, cursor_cols,
                             cursor_derivs, cursor_refs)
            if internal_programs:
                # A CALL / LINK / XCTL to a program contained in this source is internal:
                # tag its program endpoint so downstream views list it as an internal call,
                # not chase it as a missing external module.
                for hit in hits:
                    if (hit.get("etype") == _PROGRAM
                            and str(hit.get("endpoint", "")).upper() in internal_programs):
                        hit["internal"] = True
            for hit in hits:
                # A dynamic target's endpoint is the data item, which is what the
                # record is keyed on. Filled for CALL and CICS alike so the two paths
                # cannot publish different answers to the same question. An 88-level
                # list without its `evidence`, or a list a variable MOVE also reaches
                # without `hasVariableAssignment`, would read as more than it is.
                rec = (unresolved_calls.get(str(hit["endpoint"]).upper())
                       if hit.get("dynamic") else None)
                if rec and rec.get("candidates"):
                    hit["candidates"] = rec["candidates"]
                    hit["evidence"] = rec.get("evidence")
                    hit["hasVariableAssignment"] = rec.get("hasVariableAssignment")
                # Carried with or without admissible candidates: "none could be a name"
                # is a different answer from "none was found".
                if rec and rec.get("rejectedCandidates"):
                    hit["rejectedCandidates"] = rec["rejectedCandidates"]
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
    # Parameters received from the caller: the USING list, else the COMMAREA/LINKAGE
    # records - less those the program addresses itself, which no caller passes.
    params = [n for n in linkage if n.upper() not in own]
    inbound = using or commarea or params
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
            "fields": {rec: dv.leaves(rec) for rec in (using or params)},
        },
    }


# ------------------------------------------------------------------------------------
# the published form: each distinct field list once
# ------------------------------------------------------------------------------------

class FieldSets:
    """A document's interned field lists: each distinct ordered list of field names
    written ONCE, in its top-level ``fieldSets``, and every row that carries one
    pointing at it by index.

    A ``READ`` with no ``INTO`` carries every elementary field of every record its FD
    declares, and a program reads one file at many sites: the same list was written in
    full at every one of them - on the interface event, and in the business view on the
    boundary action and the state's ``meta`` as well - so a document grew with READ
    sites times record width. ``fieldSets[id]`` is the list the row held; `expand_fields`
    and ``business.expand_business_view`` rebuild the full form from it."""

    def __init__(self) -> None:
        # Id 0 is always the empty list. A reader written for the old form typically
        # reads `ev.get("fields") or []`; 0 is the one falsy id, so it must mean what
        # that reader takes it to mean, or the reader silently draws a populated list
        # as no fields. Every other id is a nonzero int and fails such a reader loudly.
        self.lists: List[List[str]] = [[]]
        self._ids: Dict[Tuple[str, ...], int] = {(): 0}

    def id(self, fields: Sequence[str]) -> int:
        key = tuple(fields)
        i = self._ids.get(key)
        if i is None:
            i = self._ids[key] = len(self.lists)
            self.lists.append(list(key))
        return i


def publish_interface(iface: dict, sets: FieldSets) -> dict:
    """The overlay as a document writes it: each event's ``fields`` is its id in
    ``sets``, and the rest is the overlay unchanged. The overlay itself (what
    ``Machine.interface()`` caches and every view reads) keeps its lists."""
    out = dict(iface)
    out["events"] = [{**e, "fields": sets.id(e["fields"])} for e in iface["events"]]
    return out


def expand_fields(doc: dict) -> dict:
    """``doc`` (the bundle, or the reactive view) with each interface event's ``fields``
    id resolved to its list and ``fieldSets`` removed: the form ``formatVersion`` 6
    wrote. A shallow copy - what it does not resolve is shared with ``doc``."""
    sets = doc["fieldSets"]
    out = {k: v for k, v in doc.items() if k != "fieldSets"}
    iface = dict(doc["interface"])
    iface["events"] = [{**e, "fields": list(sets[e["fields"]])} for e in iface["events"]]
    out["interface"] = iface
    return out
