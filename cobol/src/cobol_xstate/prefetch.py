"""Attribution of stage-1 retrieval onto the modelling side's artifact manifest.

The retrieval itself - :func:`cobol_parse.prefetch.prefetch_cobol`, the lexical COPY
closure that runs BEFORE the parse - moved to the parse front-end distribution and is
re-exported here so existing imports keep working. What REMAINS here is the half that
needs the modelling engine's output: marking manifest rows whose resolution the
prefetch paid for.
"""

from __future__ import annotations

from typing import Dict, Tuple

from cobol_parse.prefetch import prefetch_cobol  # noqa: F401  (re-export)


def attribute_resolution(manifest: dict, program, store: Dict[str, Tuple[str, str]]
                         ) -> dict:
    """Mark the manifest rows that owe their resolution to stage 1.

    A dynamic ``CALL`` row carries ``via`` - the data item the target was proved through.
    When that item was declared in a member this run prefetched, the row exists *because*
    prefetch ran, and says so. Without this the improvement is invisible: the row simply
    looks like it was always resolvable, and no reader can tell that a member arriving
    from the estate is what turned an unresolved runtime target into a named program."""
    if not store:
        return manifest
    origins = {}
    for item in getattr(program, "data_items", None) or []:
        name = getattr(item, "name", None)
        origin = getattr(item, "origin", None)
        if name and origin:
            origins.setdefault(str(name).upper(), str(origin).upper())
    if not origins:
        return manifest
    for row in manifest.get("artifacts", []) or []:
        via = row.get("via")
        if not via:
            continue
        member = origins.get(str(via).upper())
        if member and member in store:
            row["resolvedBy"] = {
                "stage": "prefetch", "member": member,
                "note": (f"{via} is declared in {member}, retrieved before the parse; "
                         f"without it this target would still be an unresolved "
                         f"runtime name"),
            }
    return manifest
