"""Stage 7 - retrieve every artifact the program depends on.

``artifacts.build_artifacts`` answers *which* other things on the estate a program
touches. This stage answers the next question a migration or impact analysis asks:

    > Now go and GET them - all of them, not just the copybooks.

It walks the artifact manifest and calls a caller-supplied ``fetcher(name, ...)`` for
every row that names something actually retrievable: the called COBOL programs, the
copybooks, the assembler modules, the control (CNTL/PARM) members, the Db2 DDL/DCLGEN
for each table, the BMS mapsets, the JCL/PROC. A fetched COBOL program can then be
parsed in turn and *its* dependencies fetched, so a single call walks the whole
dependency closure to whatever depth the caller allows.

The honest part - the reason this is a projection over the manifest rather than a
``grep`` for identifiers - is that **not every artifact row names something fetchable**,
and pretending otherwise produces a directory full of wrong files:

* ``OUT-FILE`` is a name inside ONE program. There is no member called ``OUT-FILE``
  anywhere on the estate; the retrievable identity is the ddname's DSN, which lives in
  JCL. So a file row is requested by its **dataset** (when ``--bind-jcl`` resolved one),
  else its **ddname**, and if neither is known the row is *skipped with the reason*.
* A row marked ``dynamic`` names a DATA ITEM, not an artifact. Fetching ``WS-FBSPREST``
  would at best return nothing and at worst return an unrelated member that happens to
  share the name. Skipped, with the reason.
* ``CALLER``, ``SYSOUT``, ``<dynamic-sql>`` are not members of anything. Skipped.

Every skip is recorded with its reason, so the report distinguishes *"we asked and the
estate does not have it"* (a real gap) from *"this name was never fetchable"* (a
modelling fact) - the same distinction the rest of the tool insists on.

The fetcher is the caller's: this module never invents a retrieval mechanism, a search
path, or a naming convention. It passes the artifact name and a ``type`` hint (the
estate's own vocabulary - "cobol", "copybook", "ddl", ...) and accepts whatever the
service returns, via ``preprocessor.normalize_fetched``.
"""

from __future__ import annotations

import os
import re
from typing import Callable, Dict, List, Optional, Set

from .preprocessor import CopybookResolver, normalize_fetched

# artifact kind -> (type hint passed to the fetcher, file extension used when saving).
# The hint is the estate's vocabulary, not ours: a service that auto-detects can ignore
# it (the fetcher is called without it first if it does not accept the keyword).
_KIND_TYPE: Dict[str, tuple] = {
    "program":          ("cobol", ".cbl"),
    "copybook":         ("copybook", ".cpy"),
    "db2-table":        ("ddl", ".sql"),
    "file":             ("cntl", ".txt"),
    "terminal-map":     ("bms", ".bms"),
    "cics-transaction": ("csd", ".txt"),
    "queue":            ("csd", ".txt"),
}

# Rows that never name a retrievable artifact, and why. Kept explicit so the report
# says WHY rather than silently shortening the list.
_NEVER_FETCHABLE = {
    "caller": "the caller is whoever invokes this program (a JCL step or a CICS "
              "transaction), not a member that can be retrieved by this name",
    "spool":  "SYSOUT/spool is a runtime destination, not a stored artifact",
}

# A member name that could plausibly be requested from a library. Anything with a
# space, a wildcard, or the tool's own placeholder brackets is a description, not a name.
_MEMBER_NAME = re.compile(r"^[A-Z0-9$#@][A-Z0-9$#@._-]{0,43}$", re.I)

_COBOL_MARKERS = re.compile(
    r"\b(IDENTIFICATION\s+DIVISION|PROGRAM-ID|PROCEDURE\s+DIVISION)\b", re.I)


def _request_name(row: dict) -> tuple:
    """(name_to_request, reason_if_not_fetchable) for one artifact row.

    A file's retrievable identity is its DATASET, not the program-local file name -
    the whole thesis of docs/mainframe-artifacts.md, applied to retrieval."""
    kind = row.get("kind", "")
    name = str(row.get("artifact", ""))

    if kind in _NEVER_FETCHABLE:
        return None, _NEVER_FETCHABLE[kind]
    if row.get("dynamic"):
        return None, (f"{name} is a data item whose run-time value names the artifact, "
                      f"not the artifact name itself - fetching it would retrieve the "
                      f"wrong member or nothing")
    if kind == "file":
        # dataset (resolved by --bind-jcl) is the real identity; the ddname is the
        # next-best request; the program-local file name is not fetchable at all.
        ds = row.get("dataset")
        if ds:
            return str(ds), None
        dd = row.get("ddname")
        if dd:
            return str(dd), None
        return None, (f"{name} is a program-local file name with no ddname or dataset "
                      f"known here; bind the JCL (--bind-jcl) to resolve the DSN before "
                      f"this can be fetched")
    if kind not in _KIND_TYPE:
        return None, f"artifact kind '{kind}' has no known retrieval type"
    if not _MEMBER_NAME.match(name):
        return None, f"'{name}' is not a member name that can be requested"
    return name, None


def build_fetch_plan(manifest: dict) -> List[dict]:
    """One request row per artifact in ``manifest``, in manifest order: what would be
    fetched, under which name and type, or why the row is not fetchable. Pure - makes
    no calls, so a caller can review (or print) the plan before hitting a service."""
    plan: List[dict] = []
    for row in manifest.get("artifacts", []) or []:
        kind = row.get("kind", "")
        name, reason = _request_name(row)
        entry = {
            "artifact": row.get("artifact"),
            "kind": kind,
            "dependency": row.get("dependency"),
        }
        if name is None:
            entry.update({"status": "skipped", "reason": reason})
        else:
            entry.update({"status": "planned", "request": name,
                          "type": _KIND_TYPE.get(kind, (None, ""))[0]})
            if name != row.get("artifact"):
                # Say WHY the request name differs from the row name (a file requested
                # by its DSN), so the report is auditable.
                entry["requestedAs"] = ("dataset" if row.get("dataset") == name
                                        else "ddname")
        plan.append(entry)
    return plan


def _call_fetcher(fetcher: Callable, name: str, type_hint: Optional[str]):
    """Call the fetcher, passing ``type=`` only if it is accepted. An estate client
    that auto-detects (or names the keyword differently) must not be broken by us."""
    if type_hint:
        try:
            return fetcher(name, type=type_hint)
        except TypeError:
            pass          # signature does not take `type` - fall through to name-only
    return fetcher(name)


def _save(dest: str, name: str, kind: str, text: str) -> str:
    ext = _KIND_TYPE.get(kind, (None, ".txt"))[1]
    safe = re.sub(r"[^A-Za-z0-9$#@._-]", "_", name)
    path = os.path.join(dest, safe + ext)
    with open(path, "w", encoding="utf-8", errors="replace") as f:
        f.write(text)
    return path


def fetch_dependencies(manifest: dict, fetcher: Callable, dest: Optional[str] = None,
                       depth: int = 1, copybook_paths: Optional[List[str]] = None,
                       _seen: Optional[Set[str]] = None,
                       _level: int = 0) -> dict:
    """Fetch every retrievable artifact in ``manifest``; recurse into fetched COBOL
    programs up to ``depth`` levels (``depth=1`` = this program's direct dependencies).

    ``fetcher(name, type=...)`` is the caller's retrieval function - the same one the
    copybook resolver takes. ``dest``, when given, is a directory each fetched artifact
    is written into (so a later run can use it as a ``-I`` path).

    Returns a report: one row per artifact with its status (``fetched`` / ``not-found``
    / ``skipped`` / ``error``), where it came from, where it was saved, and the depth it
    was discovered at. Nothing is invented: a name that was never fetchable is reported
    as ``skipped`` with the reason, not quietly dropped."""
    # Import here: parser/statechart import this module's siblings, and only the
    # recursive step needs them.
    from .artifacts import build_artifacts
    from .parser import parse_program
    from .statechart import build_machine

    seen: Set[str] = _seen if _seen is not None else set()
    program = manifest.get("program") or "?"
    seen.add(str(program).upper())

    if dest:
        os.makedirs(dest, exist_ok=True)

    rows: List[dict] = []
    errors: List[dict] = []
    to_recurse: List[tuple] = []      # (program_name, text) fetched at this level

    for entry in build_fetch_plan(manifest):
        row = dict(entry)
        row["depth"] = _level
        row["forProgram"] = program
        if row["status"] == "skipped":
            rows.append(row)
            continue

        name, kind = row["request"], row["kind"]
        key = f"{kind}:{name.upper()}"
        if key in seen:
            row.update({"status": "already-fetched",
                        "reason": f"{name} was already retrieved in this run"})
            rows.append(row)
            continue
        seen.add(key)

        try:
            got = normalize_fetched(_call_fetcher(fetcher, name, row.get("type")), name)
        except Exception as exc:
            row.update({"status": "error", "error": f"{type(exc).__name__}: {exc}"})
            errors.append({"artifact": name, "error": row["error"]})
            rows.append(row)
            continue

        if got is None:
            row["status"] = "not-found"
            rows.append(row)
            continue

        text, source = got
        row.update({"status": "fetched", "source": source, "bytes": len(text)})
        if dest:
            row["savedTo"] = _save(dest, name, kind, text)
        rows.append(row)

        # A fetched COBOL program is itself analyzable - queue it for the next level.
        if kind == "program" and _level + 1 < depth and _COBOL_MARKERS.search(text):
            to_recurse.append((name, text))

    # Recurse: parse each fetched program and fetch ITS dependencies. The copybook
    # resolver gets the same fetcher, so a callee's copybooks resolve too - which is
    # what makes its own dynamic CALL targets resolvable in turn.
    children: List[dict] = []
    for name, text in to_recurse:
        if name.upper() in seen and _level > 0:
            continue
        try:
            resolver = CopybookResolver(paths=list(copybook_paths or []),
                                        fetcher=fetcher)
            sub = build_artifacts(build_machine(
                parse_program(text, resolver=resolver), source_name=name))
        except Exception as exc:      # a callee that will not parse must not stop the walk
            errors.append({"artifact": name,
                           "error": f"fetched but could not be analyzed: "
                                    f"{type(exc).__name__}: {exc}"})
            continue
        children.append(fetch_dependencies(
            sub, fetcher, dest=dest, depth=depth, copybook_paths=copybook_paths,
            _seen=seen, _level=_level + 1))

    for child in children:
        rows.extend(child["artifacts"])
        errors.extend(child["errors"])

    counts: Dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    return {
        "format": "cobol-xstate-fetch",
        "program": program,
        "depth": depth,
        "note": (
            "One row per artifact this program depends on, with the outcome of "
            "retrieving it. 'fetched' carries the source it came from (and savedTo when "
            "a destination was given); 'not-found' means the estate service was asked "
            "and had nothing; 'error' means the request itself failed (a fixable "
            "condition, NOT evidence the artifact is absent); 'skipped' means the row "
            "never named a retrievable artifact and says why - a program-local file "
            "name with no ddname/DSN, a dynamic name that is really a data item, or a "
            "caller/spool destination. Fetched COBOL programs are parsed and their own "
            "dependencies followed, up to 'depth' levels."
        ),
        "counts": counts,
        "artifacts": rows,
        "errors": errors,
    }
