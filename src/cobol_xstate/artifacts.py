"""Stage 6 (projection) - the related-artifact manifest for one program.

The other views answer *what the program does* and *where each field came from*. This one
answers a flatter, logistical question a migration planner asks first:

    > For this COBOL program, what other things on the estate does it touch?

- an ``EXEC SQL`` names a **Db2 table**;
- a ``SELECT ... ASSIGN`` (or a CICS ``FILE(...)``) names a **file/dataset** - a control
  (CNTL) file it reads, a batch file it writes, the output of an unload;
- a ``CALL`` / CICS ``LINK`` / ``XCTL`` names an **external program**;
- a CICS ``READQ`` / ``WRITEQ`` names a **queue**; a ``SEND MAP`` a **terminal map**;
  DLI a **segment**; ``RETURN`` / ``USING`` the **caller**.

Every one of those is already recovered as an external *endpoint* by ``build_interface``.
This module does not re-parse anything - it **re-projects** that endpoint inventory into an
artifact-centric list, one row per related artifact, aggregating the events that touch it
(verbs, source lines, read/write direction) and attaching the file-control metadata
(ddname, organization, key) the interface already carries.

The honest part - and the whole reason a naive ``grep`` for these names is dangerous - is
that most of these names are **program-local**, and the artifact that turns them into a
system-global identity lives *outside the program*. That is the thesis of
``docs/mainframe-artifacts.md``, and this manifest wears it on every row:

    CUST-FILE   is a name inside THIS program.  The ddname (CUSTIN) is a binding in JCL.
                The dataset (PROD.CUSTOMER.MASTER) is the identity - and it is in the JCL,
                not here.

So each file/queue/program row records what it *is* here, and ``resolvedBy`` / ``needs``
name the artifact you must read next to make it joinable across programs. A Db2 table name
is already catalog-global (``identity: "global"``); a ddname is not (``"program-local"``).
Nothing is invented: where even the ddname is missing (a file used with no ``SELECT``, or a
CICS file whose real dataset is in the CSD), the row says so rather than guessing.

Response registers (SQLCODE, EIB, FILE STATUS), handled conditions (NOTFND, end-of-file),
and system intrinsics (DATE/TIME) are *not* related artifacts - they are the program
reacting to a subsystem, not a second thing it touches - so they are dropped from
``artifacts`` and listed under ``excluded`` with the reason, so the omission is visible.
"""

from __future__ import annotations

from typing import Dict, List

from .interface import build_interface
from .statechart import Machine

# endpoint-type -> how this manifest classifies and resolves it. The `resolver` column is
# the artifact you must read NEXT to turn a program-local name into a system-global one;
# it is the operational form of the resolver table in docs/mainframe-artifacts.md.
#   kind      - the artifact category surfaced to the reader
#   identity  - "global": the name is already an estate-wide identity (a Db2 table, a
#               load-module name); "program-local": it needs an external binding to join
#   priority  - sort order (lower first), so the manifest reads tables-then-files-then-...
_ARTIFACT = "artifact"
_CLASS: Dict[str, dict] = {
    "db2":     {"kind": "db2-table", "identity": "global",        "priority": 0,
                "resolver": None,
                "needs": "Db2 DDL / DCLGEN to resolve columns, types, and keys "
                         "(the table name itself is catalog-global)"},
    "file":    {"kind": "file", "identity": "program-local",      "priority": 1,
                "resolver": "JCL DD statement",
                "needs": "the JCL //<ddname> DD DSN=... to resolve the dataset name "
                         "(DSN); the ddname alone is a program-to-JCL binding, not the "
                         "identity"},
    "program": {"kind": "program", "identity": "global",          "priority": 2,
                "resolver": "binder / link-edit control (static) or run-time config "
                            "(dynamic CALL)",
                "needs": "the link-edit control to confirm which module a static CALL "
                         "binds; a dynamic CALL target is resolved at run time"},
    "queue":   {"kind": "queue", "identity": "program-local",     "priority": 3,
                "resolver": "CICS CSD (TDQUEUE / TSMODEL) or MQ QALIAS",
                "needs": "the CICS CSD or MQ definitions to resolve an alias/model to the "
                         "real queue"},
    "transaction": {"kind": "cics-transaction", "identity": "global", "priority": 4,
                "resolver": "CICS CSD (TRANSACTION -> PROGRAM)",
                "needs": "the CICS CSD to resolve the transaction id to the program it "
                         "starts"},
    "terminal": {"kind": "terminal-map", "identity": "program-local", "priority": 5,
                "resolver": "BMS mapset (DFHMSD/DFHMDI/DFHMDF)",
                "needs": "the BMS mapset to resolve the map's field names and lengths"},
    "ims":     {"kind": "ims-segment", "identity": "program-local", "priority": 6,
                "resolver": "IMS PSB/PCB + DBD",
                "needs": "the PSB/PCB and DBD to resolve the segment and access intent"},
    "caller":  {"kind": "caller", "identity": "program-local",    "priority": 7,
                "resolver": "JCL / binder (batch) or CICS CSD (online)",
                "needs": "the job step or transaction definition to identify who invokes "
                         "this program and passes its parameters/COMMAREA"},
    "console": {"kind": "spool", "identity": "global",            "priority": 8,
                "resolver": None,
                "needs": None},
}

# Endpoint types that are NOT a second thing the program touches - a subsystem's reply, a
# handled exception, or a machine clock - listed under `excluded` so the drop is honest.
_NON_ARTIFACT = {
    "response":  "a response register (SQLCODE / EIB / FILE STATUS), i.e. this program "
                 "reacting to a subsystem, not a related artifact",
    "condition": "a handled condition / exception (NOTFND, end-of-file, an I/O error), "
                 "not a related artifact",
    "system":    "a system intrinsic (DATE / TIME), not a related artifact",
}

# CICS file verbs mean the dataset binding is in the CSD, not JCL. Detected from the verb
# text so a CICS-read file's `resolvedBy` points at the right artifact.
_CICS_FILE_RESOLVER = "CICS CSD (DEFINE FILE ... DSNAME=...)"

# kind -> sort order, so the manifest reads tables-then-files-then-programs, stably.
# Copybooks are a compile-time dependency, listed after the runtime endpoints.
_CLASS_PRIORITY = {v["kind"]: v["priority"] for v in _CLASS.values()}
_CLASS_PRIORITY["copybook"] = 9

_COPYBOOK_RESOLVER = "copybook library + SYSLIB concatenation order"
_COPYBOOK_NEEDS = (
    "the SYSLIB the compile saw: a member name is unique only within a library, so which "
    "layout this is depends on concatenation order; REPLACING (when present) also renames "
    "the fields per program")


def _io(directions: List[str]) -> str:
    g, c = "get" in directions, "create" in directions
    if g and c:
        return "read-write"
    return "read" if g else "write"


def _dedup(seq: List) -> List:
    """Order-preserving de-duplication (verbs/lines read better in first-seen order).

    Set-guarded: the membership test against a growing list was O(n^2), and the line
    list for a heavily-used endpoint runs to thousands of entries."""
    out: List = []
    seen = set()
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _member_counts(items) -> Dict[str, int]:
    """member (uppercased) -> how many entries in `items` came from that copybook."""
    out: Dict[str, int] = {}
    for it in (items or {}).values():
        if isinstance(it, dict) and it.get("member"):
            m = str(it["member"]).upper()
            out[m] = out.get(m, 0) + 1
    return out


def _copybook_rows(machine: Machine, flags: List[str]) -> List[dict]:
    """One row per distinct COPY / EXEC SQL INCLUDE member the program depends on."""
    data_by_member = _member_counts(getattr(machine, "data", None))
    prov_by_member = _member_counts(getattr(machine, "provenance", None))

    # Dedup by member, keeping source order; OR the replacing flag; a `missing` status
    # wins over `expanded` (the same name copied twice resolves the same way, but if any
    # site failed to find it that is the fact to surface).
    merged: Dict[str, dict] = {}
    for cb in getattr(machine, "copybooks", None) or []:
        key = str(cb.get("member", "")).upper()
        if not key:
            continue
        cur = merged.get(key)
        if cur is None:
            merged[key] = dict(cb)
        else:
            cur["replacing"] = cur.get("replacing") or cb.get("replacing")
            cur["source"] = cur.get("source") or cb.get("source")
            if cb.get("status") == "missing":
                cur["status"] = "missing"

    rows: List[dict] = []
    for key, cb in merged.items():
        status = cb.get("status", "expanded")
        row: dict = {
            _ARTIFACT: key,
            "kind": "copybook",
            "dependency": "compile-time",
            "via": cb.get("via", "COPY"),
            "status": status,
            "identity": "program-local",
        }
        if cb.get("replacing"):
            row["replacing"] = True
        if cb.get("source"):
            # WHICH library actually supplied this member - the SYSLIB ambiguity the
            # `needs` text warns about, answered for this run.
            row["source"] = cb["source"]
        contributes = {}
        if data_by_member.get(key):
            contributes["dataItems"] = data_by_member[key]
        if prov_by_member.get(key):
            contributes["paragraphs"] = prov_by_member[key]
        if contributes:
            row["contributes"] = contributes
        if status == "missing":
            # The single most useful copybook fact: a dependency the model could NOT see.
            row["resolvedBy"] = None
            row["needs"] = (f"copybook {key} was not found on the search path, so the data "
                            f"items / logic it defines are ABSENT from this model - supply "
                            f"the library that holds it")
            flags.append(f"copybook {key}: not found on the search path - the layout/logic "
                         f"it defines is missing from every view of this program")
        else:
            row["resolvedBy"] = _COPYBOOK_RESOLVER
            row["needs"] = _COPYBOOK_NEEDS
        rows.append(row)
    return rows


def build_artifacts(machine: Machine) -> dict:
    """Return the related-artifact manifest: one row per external artifact this program
    touches, with the resolution chain each program-local name still needs. Pure read
    over the emitted machine - it re-projects ``build_interface``'s endpoint inventory and
    invents nothing."""
    iface = machine.interface()
    endpoints = {e["endpoint"]: e for e in iface["endpoints"]}

    # Aggregate the events per endpoint: which verbs touch it, on which source lines.
    verbs: Dict[str, List[str]] = {}
    lines: Dict[str, List[int]] = {}
    cics_file: Dict[str, bool] = {}
    for ev in iface["events"]:
        ep = ev["endpoint"]
        verbs.setdefault(ep, []).append(ev["verb"])
        if ev.get("line"):
            lines.setdefault(ep, []).append(ev["line"])
        if ev["endpointType"] == "file" and str(ev.get("verb", "")).upper().startswith("CICS"):
            cics_file[ep] = True

    artifacts: List[dict] = []
    excluded: List[dict] = []
    flags: List[str] = []
    # For diagnosing an unresolved dynamic name that is not declared in the visible
    # source: a missing copybook is the usual place its definition (and VALUE) hides.
    declared = {str(k).upper() for k in (getattr(machine, "data", None) or {})}
    missing_cbs = [str(cb.get("member", "")).upper()
                   for cb in (getattr(machine, "copybooks", None) or [])
                   if cb.get("status") == "missing"]
    for name, ep in endpoints.items():
        etype = ep["type"]
        if etype in _NON_ARTIFACT:
            excluded.append({"name": name, "endpointType": etype,
                             "reason": _NON_ARTIFACT[etype]})
            continue
        cls = _CLASS.get(etype)
        if cls is None:                         # an endpoint type we do not classify yet
            excluded.append({"name": name, "endpointType": etype,
                             "reason": "endpoint type not classified by this manifest"})
            continue

        row_verbs = _dedup(verbs.get(name, []))
        is_cics = any(str(v).upper().startswith("CICS") for v in row_verbs)
        row: dict = {
            _ARTIFACT: name,
            "kind": cls["kind"],
            "dependency": "runtime",
            "io": _io(ep.get("directions", [])),
            "verbs": row_verbs,
            "identity": cls["identity"],
        }
        if lines.get(name):
            row["lines"] = _dedup(sorted(lines[name]))

        if ep.get("via"):
            # A dynamic name resolved by constant propagation: the resource name is
            # proven; `via` records the data item it came through.
            row["via"] = ep["via"]

        if ep.get("dynamic"):
            # An UNRESOLVED dynamic name: the "artifact" here is a DATA ITEM whose
            # run-time value names the real resource (program, transaction, queue,
            # file, map) - or, for <dynamic-sql>, a statement string assembled at run
            # time. Presenting it as a resolvable identity would be exactly the false
            # join this manifest exists to prevent, so downgrade it and say what is
            # still needed.
            row["identity"] = "program-local"
            row["dynamic"] = True
            if ep.get("candidates"):
                row["candidates"] = ep["candidates"]
            row["resolvedBy"] = None
            if name == "<dynamic-sql>":
                row["needs"] = ("the SQL statement text is assembled at run time "
                                "(PREPARE / EXECUTE): the operation and table(s) are "
                                "not statically knowable - trace the fields that build "
                                "the statement string")
                flags.append("db2-table <dynamic-sql>: statement text assembled at "
                             "run time - tables/operation unresolvable statically")
            else:
                kind = cls["kind"]
                needs = (f"{name} is a data item, not a {kind} name: the target "
                         f"is its run-time value"
                         + (f" (literals seen in this program: "
                            f"{', '.join(ep['candidates'])})"
                            if ep.get("candidates") else "")
                         + f"; a reaching-definition trace or the run-time "
                           f"configuration is needed to name the real {kind}")
                if name.upper() not in declared and missing_cbs:
                    # The single most actionable diagnosis: the item is not declared in
                    # the visible source, and a copybook that could not be found is
                    # where its definition - typically with the VALUE that names the
                    # real target - lives. Supplying that copybook resolves this row.
                    needs += (f"; NOTE {name} is not declared in the visible source - "
                              f"missing copybook(s) {', '.join(missing_cbs)} may "
                              f"define it and the VALUE that names the target")
                row["needs"] = needs
                flags.append(f"{kind} {name}: dynamic target - {name} is a data item "
                             f"whose run-time value names the {kind}; not resolvable "
                             f"from this program alone")
        elif etype == "file":
            ddname = ep.get("assign")
            if ddname:
                row["ddname"] = ddname
            for key in ("organization", "access", "recordKey", "statusField"):
                if ep.get(key):
                    row[key] = ep[key]
            if ddname:
                row["resolvedBy"] = cls["resolver"]
                row["needs"] = cls["needs"]
            elif cics_file.get(name):
                row["identity"] = "program-local"
                row["resolvedBy"] = _CICS_FILE_RESOLVER
                row["needs"] = ("the CICS CSD (DEFINE FILE ... DSNAME=...) to resolve the "
                                "CICS file name to its dataset")
            else:
                # A file referenced with no SELECT/ASSIGN and no CICS FILE(...): even the
                # ddname is unknown, so there is nothing to join on yet. Say so - a silent
                # row here would read as a resolvable binding that does not exist.
                row["resolvedBy"] = None
                row["needs"] = ("no SELECT ... ASSIGN found for this file, so not even "
                                "the ddname is known here; the FILE-CONTROL entry (or the "
                                "CICS FILE definition) is needed before the dataset can "
                                "be resolved")
                flags.append(f"file {name}: no SELECT/ASSIGN - ddname unknown, dataset "
                             f"unresolvable from this program alone")
        elif etype == "program" and is_cics:
            # CICS LINK/XCTL is not a batch CALL: which module runs is the installed
            # PROGRAM resource in the CSD, not the link-edit of the caller.
            row["resolvedBy"] = "CICS CSD (DEFINE PROGRAM)"
            row["needs"] = ("the CICS CSD (or autoinstall rule) that installs this "
                            "PROGRAM resource; the binder still link-edits the module")
        else:
            if cls["resolver"] is not None:
                row["resolvedBy"] = cls["resolver"]
            if cls["needs"] is not None:
                row["needs"] = cls["needs"]

        artifacts.append(row)

    # Copybooks (COPY / EXEC SQL INCLUDE) - a COMPILE-TIME source dependency, not a
    # runtime endpoint, so they carry no io/verbs. A member name is program-local in the
    # sense that matters here: it is unique only *within a library*, and which library the
    # compile saw is SYSLIB concatenation order - and `REPLACING` renames its fields per
    # program. Both are the false-join hazards docs/mainframe-artifacts.md calls out, so
    # the row names the SYSLIB as the resolver and flags a missing member.
    artifacts.extend(_copybook_rows(machine, flags))

    artifacts.sort(key=lambda r: (_CLASS_PRIORITY.get(r["kind"], 99), r[_ARTIFACT]))
    excluded.sort(key=lambda r: (r["endpointType"], r["name"]))

    # Program-level patterns the manifest can prove structurally (the example programs are
    # named for exactly these): a Db2 read paired with a file write IS an unload; a file
    # read paired with a Db2 write IS a load. Stated only when both halves are present.
    kinds = {(r["kind"], r["io"]) for r in artifacts if "io" in r}  # runtime rows only
    db2_read = any(k == "db2-table" and io in ("read", "read-write") for k, io in kinds)
    db2_write = any(k == "db2-table" and io in ("write", "read-write") for k, io in kinds)
    file_read = any(k == "file" and io in ("read", "read-write") for k, io in kinds)
    file_write = any(k == "file" and io in ("write", "read-write") for k, io in kinds)
    patterns: List[str] = []
    if db2_read and file_write:
        tables = [r[_ARTIFACT] for r in artifacts if r["kind"] == "db2-table"]
        outs = [r[_ARTIFACT] for r in artifacts
                if r["kind"] == "file" and r.get("io") in ("write", "read-write")]
        patterns.append(f"unload: reads Db2 ({', '.join(tables)}) and writes a file "
                        f"({', '.join(outs)})")
    if file_read and db2_write:
        tables = [r[_ARTIFACT] for r in artifacts if r["kind"] == "db2-table"]
        ins = [r[_ARTIFACT] for r in artifacts
               if r["kind"] == "file" and r.get("io") in ("read", "read-write")]
        patterns.append(f"load: reads a file ({', '.join(ins)}) and writes Db2 "
                        f"({', '.join(tables)})")

    return {
        "format": "cobol-xstate-artifacts",
        "program": machine.program_id,
        "source": machine.source_name,
        "note": (
            "One row per artifact this program is related to. 'dependency' is 'runtime' "
            "for the external endpoints it touches when it runs - Db2 tables, files "
            "(datasets), called programs, queues, maps, IMS segments, the caller - and "
            "'compile-time' for the copybooks (COPY / EXEC SQL INCLUDE) it is built from. "
            "'io' (runtime rows) is read/write/read-write. 'identity' is 'global' when the name is "
            "already an estate-wide identity (a Db2 table, a load-module name) and "
            "'program-local' when it is not - a ddname, a CICS file name, a queue alias. "
            "For a program-local artifact, 'resolvedBy'/'needs' name the OTHER artifact "
            "(JCL, the CSD, a DDL, the binder) you must read to make it joinable across "
            "programs: a file's ddname is a binding in JCL, and the dataset name (DSN) - "
            "the real identity - is there, not here. Response registers, handled "
            "conditions, and system intrinsics are not related artifacts and are listed "
            "under 'excluded' with the reason. Nothing is invented; see 'flags'. See "
            "docs/mainframe-artifacts.md for why the middle binding is not the identity."
        ),
        "artifacts": artifacts,
        "patterns": patterns,
        "excluded": excluded,
        "flags": _dedup(flags),
    }
