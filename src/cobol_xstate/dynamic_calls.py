"""True dynamic calls: which artifact names the target, and how the name gets here.

A dynamic ``CALL identifier`` whose target this program proves constant is not a dynamic
call in any way that matters - ``analysis.py`` resolves it and the callee becomes an
ordinary dependency, fetched like any other. What is left over after that are the **true**
dynamic calls: the target is genuinely determined at run time, and no amount of reading
this program will name it.

Until now the tool said exactly that and stopped:

    program WS-SUBPGM: dynamic target - WS-SUBPGM is a data item whose run-time value
    names the program; not resolvable from this program alone

That is honest and nearly useless. It tells a migration team that an edge exists without
telling them where to go and find it. But the question *is* answerable - just not as
"which program does this call". Turn it around:

    > This program cannot tell you WHICH program it calls.
    > It can tell you exactly WHERE THE NAME COMES FROM.

``CALL WS-SUBPGM`` is preceded by ``MOVE CTL-PGM-NAME TO WS-SUBPGM``, and ``CTL-PGM-NAME``
is a field of the record read from ``CTL-FILE``, whose ddname is ``CTLDD``, which the JCL
binds to ``PROD.PARM.CNTL``. That dataset is the artifact holding the real answer, and its
``CTL-PGM-NAME`` column is where the answer is written. **Go read that dataset and you
have the call graph** - not by static analysis, but by looking at the one artifact the
program itself is looking at.

So each row here answers three things:

1. **Is it truly dynamic?** Resolved targets are not here at all; a row's presence is the
   claim that constant propagation failed, and ``why`` says what would have made it work.
2. **Which artifact supplies the name?** Traced by a backward walk over the same
   reaching-origins fixpoint the lineage view uses, from the call site to the external
   events that fill the item - so it is flow-sensitive and reports only sources that
   actually reach the call.
3. **How does the name get from that artifact to the call?** The retrieval verb, the field
   it lands in, and every assignment between there and the CALL, in source order.

What this deliberately does NOT do is guess the target. A control file's *contents* are
run-time data; naming the artifact is a fact, and enumerating what it might contain is a
fiction. The row points at the evidence and stops.

**Honest outcomes other than "a file feeds it".** Each is a different answer and gets
said differently, because each sends the reader somewhere different:

* **The caller supplies it** (the item is LINKAGE, or reached from a COMMAREA). The value
  is not determined in this program at all - the enumeration lives in *this program's
  callers*, and the row says so rather than reporting no source.
* **No external origin reaches it.** The item is only ever set from literals here, but
  more than one reaches the call - so the target is one of a known set. Those literals are
  reported as ``candidates``, which is a genuinely different (and much better) situation
  than an unknown artifact.
* **The chain is broken.** A REDEFINES alias, a reference-modified store, an unresolved
  subscript or a paragraph that would not parse ends the trace. Reported as
  ``chainBroken`` with the reason - "we cannot trace this" and "nothing feeds this" are
  opposite answers and must never be printed the same way.
"""

from __future__ import annotations

from collections import deque
from typing import Dict, List, Optional, Tuple

from .lineage import _UNKNOWN, _Lineage
from .statechart import Machine
from .storage import field_position

# endpoint type -> what a run-time value of this item actually names.
_NAMES = {
    "program": "program",
    "transaction": "CICS transaction",
    "queue": "queue",
    "file": "file",
    "map": "BMS map",
}
# The caller event, which is not an artifact on the estate but a fact about who invokes
# this program - a different answer, and a different place to go looking.
_CALLER = "GET.CALLER.CALLER"


def _index(rows: List[dict], key: str) -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = {}
    for row in rows:
        out.setdefault(str(row[key]).upper(), []).append(row)
    return out


def _chain(item: str, event: str, flow_by_target: Dict[str, List[dict]],
           fills_by_field: Dict[str, List[dict]]) -> Optional[Tuple[dict, List[dict]]]:
    """Shortest assignment path from ``item`` back to a field filled by ``event``.

    Breadth-first so the reported chain is the shortest one that explains the value; a
    longer path through the same source would be true as well but harder to read, and
    reporting every path would bury the answer."""
    queue: deque = deque([(item.upper(), [])])
    seen = {item.upper()}
    while queue:
        field, path = queue.popleft()
        for fill in fills_by_field.get(field, []):
            if fill["event"] == event:
                return fill, list(reversed(path))     # report source -> call order
        for edge in flow_by_target.get(field, []):
            for src in edge["sources"]:
                su = str(src).upper()
                if su in seen:
                    continue
                seen.add(su)
                queue.append((su, path + [{
                    "from": su, "to": field, "cobol": edge["cobol"],
                    "line": edge["line"],
                }]))
    return None


def _dead_ends(item: str, flow_by_target: Dict[str, List[dict]]) -> List[str]:
    """Walking the assignment chain back from ``item``, the items it bottoms out at -
    those nothing in this program ever writes.

    An item that is never assigned AND has no external origin is a much sharper finding
    than "unresolvable": the value at the call is whatever the item was initialised to.
    Naming the dead end tells a reader where the chain actually stops, instead of leaving
    them to walk it themselves."""
    ends: List[str] = []
    seen = {item.upper()}
    queue: deque = deque([item.upper()])
    while queue:
        field = queue.popleft()
        edges = flow_by_target.get(field, [])
        if not edges and field != item.upper():
            ends.append(field)
            continue
        for edge in edges:
            for src in edge["sources"]:
                su = str(src).upper()
                if su not in seen:
                    seen.add(su)
                    queue.append(su)
    return ends


def _column_of(fill: dict) -> Optional[str]:
    """The Db2 COLUMN a host variable was selected into, when the source proves it."""
    for col in (fill.get("columns") or []):
        if isinstance(col, dict) and \
                str(col.get("hostVar", "")).upper() == str(fill["field"]).upper():
            return col.get("column")
    return None


def _artifact_row(manifest: Optional[dict], name: str) -> dict:
    """The manifest's own row for this endpoint - ddname, dataset, kind - so the reader
    gets the *retrievable* identity of the artifact and not just its program-local name.
    A file's dataset is only present when the JCL was bound (``--bind-jcl``); saying so
    is the difference between an address and a hint."""
    for row in ((manifest or {}).get("artifacts") or []):
        if str(row.get("artifact", "")).upper() == name.upper():
            out = {}
            for key in ("kind", "ddname", "dataset", "io", "identity", "organization",
                        "recordKey", "boundBy"):
                if row.get(key):
                    out[key] = row[key]
            return out
    return {}


def _recipe(fill: dict, endpoint: str, data: dict) -> dict:
    """What to actually RUN or READ to get the real target list out of the artifact.

    Naming the artifact and the field is where this view used to stop, and it leaves the
    last mile to the reader. For Db2 the exact query is derivable from the SQL we already
    parsed, and for a file the field's byte position is derivable from the record layout -
    so derive them. Both are refused rather than guessed when the inputs do not support
    them (see `storage.record_layout`)."""
    column = _column_of(fill)
    if column:
        return {
            "kind": "sql",
            "run": f"SELECT DISTINCT {column} FROM {endpoint}",
            "table": endpoint,
            "column": column,
            "note": ("every distinct value this column holds is a possible target of "
                     "the call - the live table is the authoritative call graph"),
        }
    position = field_position(data, fill["field"])
    out = {
        "kind": "file-field",
        "field": fill["field"],
        "record": position.get("record"),
        "layout": position.get("layout"),
        "note": ("each record's value in this field is a possible target - read the "
                 "dataset and take the distinct values of this field"),
    }
    for key in ("offset", "length", "readAt", "recordLength"):
        if position.get(key):
            out[key] = position[key]
    if not position.get("provable") and position.get("reason"):
        out["positionWithheld"] = position["reason"]
    return out


def _source(site: dict, event: str, maybe: bool, resolver: Optional[str],
            flow_by_target, fills_by_field, manifest, data: dict) -> dict:
    """One artifact that supplies the call's target, and the route the name takes."""
    if maybe and resolver and event.startswith("CREATE.PROGRAM."):
        # The item is passed BY REFERENCE to another program, which may write it - so
        # the name this program then calls is decided THERE. Reported as its own kind:
        # it is the mirror of the `caller` case (the answer is in a neighbouring program)
        # but pointing downstream, and it used to be misreported as an untraceable
        # group-level move, which threw the one useful fact away.
        return {
            "artifact": resolver,
            "kind": "called-program",
            "maybe": True,
            "event": event,
            "how": (f"{site['item']} is passed BY REFERENCE to {resolver}, which may "
                    f"write it before this call runs - so the target is decided inside "
                    f"{resolver}. Analyse {resolver} to enumerate the targets"),
            "maybeNote": (f"BY REFERENCE means {resolver} CAN write the argument, not "
                          f"that it does - whether this source actually applies is a "
                          f"question about {resolver}, not about this program"),
        }
    if event == _CALLER:
        src = {
            "artifact": "CALLER",
            "kind": "caller",
            "supplies": "the value arrives in this program's parameter list / COMMAREA",
            "how": ("the name is chosen by whoever calls this program, so it is not "
                    "determined here at all - enumerate this program's CALLERs (and the "
                    "value each passes) to enumerate the targets"),
        }
        found = _chain(site["item"], event, flow_by_target, fills_by_field)
        if found:
            _fill, steps = found
            if steps:
                src["chain"] = steps
        return src

    if event == _UNKNOWN:
        return {
            "artifact": None,
            "kind": "untraceable",
            "chainBroken": True,
            "how": ("the value passes through a construct whose data effect is not "
                    "modeled (a REDEFINES alias, a reference-modified store, an "
                    "unresolved subscript, or a paragraph that would not parse), so the "
                    "trace ends here - this is 'we cannot follow it', NOT 'nothing "
                    "feeds it'"),
        }

    found = _chain(site["item"], event, flow_by_target, fills_by_field)
    if found is None:
        # The origin reaches the call but no path explains it - a group/child aliasing
        # step the chain walk does not reproduce. Report the artifact (which IS proven
        # by the fixpoint) and be explicit that the route is not reconstructed.
        return {
            "artifact": event.split(".")[-1], "kind": "unknown",
            "event": event,
            "how": ("this source reaches the call, but the assignment path was not "
                    "reconstructed (typically a group-level move); the artifact is "
                    "proven, the route is not"),
        }

    fill, steps = found
    endpoint = fill["endpoint"]
    how = {
        # HOW the data leaves the artifact: the verb, the statement, and the field the
        # value lands in - which is what to go and read inside the artifact.
        "verb": fill["verb"],
        "statement": fill["cobol"],
        "line": fill["line"],
        "field": fill["field"],
    }
    column = _column_of(fill)
    if column:
        # For Db2 the host variable is a program-local name and useless to a reader;
        # the COLUMN is the database's own, and is the thing to go and select.
        how["column"] = column
        how["readAt"] = f"{endpoint}.{column}"
    src = {
        "artifact": endpoint,
        "kind": (_artifact_row(manifest, endpoint).get("kind")
                 or fill["endpointType"]),
        "event": fill["event"],
        "how": how,
        # ...and what to run or read to turn the artifact into the actual target list.
        "extract": _recipe(fill, endpoint, data),
    }
    src.update({k: v for k, v in _artifact_row(manifest, endpoint).items()
                if k != "kind"})
    if steps:
        # ...and how it travels from that field to the call, in source order.
        src["chain"] = steps
    if maybe:
        src["maybe"] = True
        src["maybeNote"] = (
            f"passed BY REFERENCE to {resolver}, which may rewrite it - whether this "
            f"source actually applies depends on that program")
    return src


def annotate_artifacts(manifest: dict, report: dict) -> dict:
    """Attach the "go read this" answer to the manifest rows that currently only say
    they are unresolvable.

    The artifact manifest is where a reader looks first, and its dynamic rows end at
    "not resolvable from this program alone". That is where the pointer is worth the
    most - and carrying it here also means the fetch plan inherits it, since the plan
    is built from these rows."""
    by_item = {r["item"]: r for r in report.get("dynamicCalls", [])}
    for row in (manifest.get("artifacts") or []):
        if not row.get("dynamic"):
            continue
        found = by_item.get(str(row.get("artifact", "")).upper())
        if not found:
            continue
        named: List[dict] = []
        for src in found.get("sources", []):
            if not src.get("artifact"):
                continue
            entry = {"artifact": src["artifact"], "kind": src.get("kind")}
            how = src.get("how")
            if isinstance(how, dict):
                entry["read"] = how.get("readAt") or how.get("field")
                entry["verb"] = how.get("verb")
            for key in ("ddname", "dataset"):
                if src.get(key):
                    entry[key] = src[key]
            named.append(entry)
        if named:
            row["namedBy"] = named
            # Replace the "a reaching-definition trace is needed" text rather than
            # appending to it: that trace has now been done, and leaving the old
            # sentence in front of its own answer reads as though it had not.
            row["needs"] = (
                f"{row.get('artifact')} is a data item, not a "
                f"{row.get('kind')} name - but its run-time value comes from "
                + ", ".join(
                    n["artifact"] + (f" ({n['dataset']})" if n.get("dataset") else "")
                    + (f", field {n['read']}" if n.get("read") else "")
                    for n in named)
                + ". Read that artifact to enumerate the real targets; the "
                  "dynamic-calls view carries the full chain from it to this call.")
        elif found.get("candidates"):
            row["namedBy"] = []
            row["candidates"] = found["candidates"]
    return manifest


def build_dynamic_calls(machine: Machine, artifacts: Optional[dict] = None) -> dict:
    """One row per TRUE dynamic target: what it is, why it is unresolvable, which
    artifact supplies its value, and how the value gets from there to the call.

    ``artifacts`` is the related-artifact manifest, used to give each source its
    retrievable identity (ddname, and the dataset when the JCL was bound). Pure read."""
    lin = _Lineage(machine)
    lin.run()

    flow_by_target = _index(lin.flow, "target")
    fills_by_field = _index(lin.fills, "field")

    rows: List[dict] = []
    for site in lin.dynamic_sites:
        item = site["item"]
        kind = _NAMES.get(site["endpointType"], site["endpointType"])

        row: dict = {
            "program": machine.program_id,
            "item": item,
            "names": kind,
            "verb": site["verb"],
            "line": site["line"],
            "statement": site["cobol"],
            "state": site["state"],
        }

        if item == "<DYNAMIC-SQL>":
            row.update({
                "names": "SQL statement",
                "why": ("the statement text itself is assembled at run time (PREPARE / "
                        "EXECUTE), so the operation and the tables are not statically "
                        "knowable - trace the fields that build the statement string"),
                "sources": [],
            })
            rows.append(row)
            continue

        # The analysis that DECIDED this was unresolvable knows exactly why - that it
        # found two literals, or a variable assignment, or no assignment at all. Those
        # are different situations with different next steps, so report its reason
        # rather than the generic fact of failure.
        unresolved = (machine.unresolved_calls or {}).get(item, {})
        row["why"] = (
            f"{item} is a data item, not a {kind} name: the target is whatever value it "
            f"holds when the {site['verb']} runs, and this program does not fix it - "
            + (unresolved.get("reason") or
               "constant propagation could not prove a single literal reaches here"))

        # Two grades of candidate, kept apart. An 88-level VALUE says the program was
        # WRITTEN to allow that name; a MOVE/VALUE says it actually stores it. Merging
        # them lets a name nothing ever assigns be read with the confidence of one the
        # program demonstrably moves.
        candidates = list(site["candidates"] or unresolved.get("candidates") or [])
        declared_only = unresolved.get("evidence") == "declared-88"
        if candidates and declared_only:
            row["declaredCandidates"] = candidates
            row["declaredCandidatesNote"] = (
                "values an 88-level condition name declares for this item. NOTHING in "
                "the visible source (no SET ... TO TRUE, no MOVE) proves any of them is "
                "ever stored - these are what the program was written to ALLOW, not what "
                "it is known to do, and the set may be neither complete nor reachable")
            candidates = []          # not proven, so never counted as the target set
        elif candidates:
            row["candidates"] = candidates
            row["candidatesNote"] = (
                "literals a MOVE or VALUE clause provably stores into this item - the "
                "set the target is drawn from if nothing external writes it")
        if unresolved.get("hasVariableAssignment"):
            row["variableAssignment"] = True
            row["variableAssignmentNote"] = (
                "a non-literal assignment reaches this item"
                + (", so the literals above are not the full set - the sources below "
                   "are authoritative" if candidates else
                   " - its value is computed, so follow 'sources' (or 'deadEnds') "
                   "rather than looking for literals"))

        sources: List[dict] = []
        for (event, maybe, resolver) in site["origins"]:
            sources.append(_source(site, event, maybe, resolver, flow_by_target,
                                   fills_by_field, artifacts, machine.data or {}))
        row["sources"] = sources

        if not sources:
            # Four different reasons for "no external source", and they are NOT
            # interchangeable: one is a local fix, one is a complete answer, one is a
            # likely defect, and one is a gap in the model. Diagnosing them all as
            # "unresolvable" - or worse, all as "a copybook is missing" - sends most
            # readers looking for something that is not there.
            declared = str(item).upper() in {str(k).upper() for k in
                                             (machine.data or {})}
            base = ("no external source reaches this item: nothing outside the program "
                    "writes it on any path to the call. ")
            if candidates:
                row["sourcesNote"] = base + (
                    "Its value comes from literal assignments inside this program, so "
                    "'candidates' is the FULL set of possible targets - this edge is "
                    "resolvable by inspection even though a single target is not")
            elif not declared:
                # This row rests on an INCOMPLETE MODEL, not on a property of the
                # program. Supply the member and it may resolve to an ordinary named
                # call and vanish from this view entirely - so it is marked provisional
                # rather than presented as a finding of equal standing to the others.
                missing = [str(cb.get("member", "")).upper()
                           for cb in (machine.copybooks or [])
                           if cb.get("status") == "missing"]
                row["provisional"] = True
                row["provisionalNote"] = (
                    f"{item} is not declared in the visible source, so this may not be a "
                    f"dynamic call at all: the declaration - and the VALUE clause that "
                    f"would resolve the target - is most likely in a member that did not "
                    f"resolve"
                    + (f" ({', '.join(missing)})" if missing else "")
                    + ". Supply it and re-run before treating this as genuinely "
                      "run-time; the prefetch report says why it is absent")
                if missing:
                    row["missingMembers"] = missing
                row["sourcesNote"] = base + (
                    f"{item} is not declared in the visible source either - see "
                    f"'provisionalNote'")
            else:
                ends = _dead_ends(item, flow_by_target)
                if ends:
                    row["deadEnds"] = ends
                    row["sourcesNote"] = base + (
                        f"It is assigned only from other items in this program, and "
                        f"following those back the chain ends at "
                        f"{', '.join(ends)} - which nothing in this program ever "
                        f"assigns and no external event fills. The target is therefore "
                        f"whatever that item was initialised to, which is usually a "
                        f"defect rather than a run-time indirection")
                else:
                    row["sourcesNote"] = base + (
                        f"nothing in this program assigns {item} at all, so the target "
                        f"is whatever it was initialised to - usually a defect rather "
                        f"than a run-time indirection")
        rows.append(row)

    return {
        "format": "cobol-xstate-dynamic-calls",
        "program": machine.program_id,
        "source": machine.source_name,
        "note": (
            "One row per TRUE dynamic target - a CALL/LINK/XCTL/START (or dynamic "
            "resource name) whose target this program does NOT determine. Targets that "
            "constant propagation resolved are absent: they are ordinary dependencies "
            "and appear in the artifact manifest as named programs. For each row, "
            "'sources' names the artifact whose data supplies the run-time value and "
            "'how' gives the verb, statement and field it arrives in; 'chain' is the "
            "assignments carrying it from there to the call, in source order. That "
            "artifact is where the real call graph is written down - read it and the "
            "edge resolves. Nothing here guesses the target: an artifact's CONTENTS are "
            "run-time data, so naming the artifact is a fact and enumerating what it "
            "holds would be a fiction. A 'caller' source means the value is passed in, "
            "so the enumeration lives in this program's callers; 'chainBroken' means the "
            "trace hit an unmodeled construct, which is NOT the same as nothing feeding "
            "the item."
        ),
        "counts": {
            "dynamicTargets": len(rows),
            "withAnArtifactSource": sum(
                1 for r in rows if any(s.get("artifact") and s.get("kind") != "caller"
                                       for s in r.get("sources", []))),
            "callerSupplied": sum(
                1 for r in rows if any(s.get("kind") == "caller"
                                       for s in r.get("sources", []))),
            "untraceable": sum(
                1 for r in rows if any(s.get("chainBroken")
                                       for s in r.get("sources", []))),
        },
        "dynamicCalls": rows,
        "flags": lin.flags,
    }
