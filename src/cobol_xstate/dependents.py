"""What depends on this program: the reverse direction, which only a host index holds.

Every other view in this package is a faithful reading of the COBOL its provenance points
at. This one is not a reading of anything: *what calls this program* cannot be derived
from the program's own source, by construction. Which jobs run it, which modules ``CALL``
it, which transaction is defined to start it - those facts live in an estate-wide index
only the host can read.

**The placeholder has been in the manifest all along.** ``artifacts.py`` emits a ``caller``
row whose whole content is that the answer is elsewhere - "the caller is whoever invokes
this program (a JCL step or a CICS transaction), not a member that can be retrieved by
this name" - and ``fetch`` refuses to chase it for exactly that reason. This view is where
that answer lands when a host can supply it, and it stays a separate view rather than
filling the ``caller`` row in, because a row in the manifest carries provenance into the
source and these rows have none.

So the core principle is kept the way it is kept everywhere else here: nothing is invented,
the source of every row is named, and the three answers a lookup can give stay three
answers. ``None`` means nobody was asked - and then nothing is written at all.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from mainframe_artifacts.dependents import output_rows

from . import VIEW_SCHEMA_VERSION
from .statechart import Machine

FORMAT = "cobol-xstate-dependents"

_NOTE = (
    "What the ESTATE says depends on this program - the reverse of every other view in "
    "this bundle, and the half a program's own source cannot contain: which jobs run it, "
    "which modules CALL it, which CICS transaction is defined to start it. Supplied by "
    "the host through --dependents-map or --dependents-resolver and reported as given; "
    "'suppliedBy' says which door answered. This is the answer the artifact manifest's "
    "'caller' row points at: that row says who invokes this program is not a member that "
    "can be retrieved by name, and this is where the index's answer lands. It is a "
    "SEPARATE view on purpose - every row in the manifest traces to the COBOL its "
    "provenance names, and these rows trace to an index instead, so they are never "
    "merged in. 'matchStrength' is the host's own field, never folded into prose, and a "
    "capped answer carries 'truncated' with the true 'total'. 'unanswered' is the honest "
    "half: absent from these lists means nobody said, never that nothing calls the "
    "program."
)


def _provides(machine: Machine) -> List[dict]:
    """The name another artifact can invoke this program BY: its PROGRAM-ID.

    One name, deliberately. A COBOL ``ENTRY`` statement would add a second name a caller
    can bind to - which is precisely the assembler case this contract was written for -
    but this package does not model ``ENTRY`` today, and inventing a name here would be
    exactly the guess the rest of the pipeline refuses to make. When ``ENTRY`` is
    modelled, each declared name joins this list and the view needs no other change.
    """
    key = (machine.program_id or "").strip().upper()
    return [{"name": key, "kind": "program", "provides": "the PROGRAM-ID"}] if key else []


def build_dependents(machine: Machine, lookup) -> Optional[dict]:
    """What depends on this program, or ``None`` when no lookup was supplied.

    ``None`` is the answer that matters: it is what a run that opened no door reports, and
    the CLI then writes no file at all - so a run nobody told anything produces exactly
    the artifacts it always did.
    """
    if lookup is None or not lookup.supplied:
        return None

    names: List[dict] = []
    unanswered: List[dict] = []
    for provided in _provides(machine):
        answer = lookup(provided["name"], provided["kind"])
        if answer is None:
            unanswered.append({
                "name": provided["name"], "provides": provided["provides"],
                "reason": ("the lookup failed earlier in this run and was not asked again"
                           if lookup.disabled_reason else
                           "the lookup does not cover this name"),
            })
            continue
        row: Dict[str, Any] = {
            "name": provided["name"], "kind": provided["kind"],
            "provides": provided["provides"],
            "dependents": output_rows(answer.rows),
            "count": len(answer.rows), "suppliedBy": answer.door,
        }
        if answer.truncated:
            row["truncated"] = True
            if answer.total is not None:
                row["total"] = answer.total
        names.append(row)

    flags = []
    if lookup.disabled_reason:
        flags.append(
            "dependents lookup failed mid-run ({0}); names it did not reach stay "
            "unanswered - fix the lookup and re-run".format(lookup.disabled_reason))
    if lookup.map_warning:
        flags.append(
            "part of the dependents map could not be read ({0}); the entries it did read "
            "answered normally".format(lookup.map_warning))

    return {
        "format": FORMAT,
        "formatVersion": VIEW_SCHEMA_VERSION,
        "program": machine.program_id,
        "source": machine.source_name,
        "note": _NOTE,
        "suppliedBy": lookup.describe(),
        "provides": names,
        "unanswered": unanswered,
        "flags": flags,
    }
