"""Classify a CALL / CICS LINK / XCTL target into exactly one category.

Every called program a COBOL program names must be listed AND classified: a modernization
planner needs to know which callees are real dependencies to trace, which are provided by a
subsystem (no application source to chase), and which the tool has simply not identified yet.
This module is the single source of truth for that classification, shared by the artifact
manifest (`artifacts.py`) and the dependency-fetch report (`fetch.py`) so the two never
disagree.

Categories, decided by the highest-confidence signal available:

  ``internal-nested``    the target is a program CONTAINED in this source (a nested
                         ``PROGRAM-ID``); a CALL to it is internal, not an external
                         dependency. Signal: the parser's `Program.nested_programs`.
  ``ibm-runtime``        the target is a standard IBM subsystem entry point - an MQ MQI
                         verb, a Db2 language-interface / message module, a Language
                         Environment ``CEE*`` service, or a CICS ``DFH*`` module -
                         corroborated by the program's own context. Provided by the
                         runtime; there is no application source.
  ``unresolved``         NONE of the above could be positively established. This is an
                         HONEST default: the tool never guesses a provider it cannot prove
                         (the project rule is *flag, never guess*). The fetch stage then
                         PROBES the estate - COBOL or assembler source refines it to
                         ``cobol-program`` / ``assembler-program``; nothing found leaves it
                         ``unresolved`` (e.g. ``ABENDL``, a site utility not yet figured
                         out).

The IBM vocabularies below are the standard, CLOSED API surfaces (the same kind of
reference table as `artifact_service.EXT_FOR_TYPE` / `_TYPE_SYNONYMS`), NOT a
site-maintained list - so recognising one is reading the code, not guessing.
"""

from __future__ import annotations

from typing import Dict, Iterable

# The category vocabulary, named once here so every stage spells them identically.
CATEGORY_INTERNAL = "internal-nested"
CATEGORY_IBM = "ibm-runtime"
CATEGORY_UNRESOLVED = "unresolved"
# Refinements the fetch stage assigns to a formerly-`unresolved` target once the estate
# answers - kept here so the fetch report and any summary name them the same way.
CATEGORY_COBOL = "cobol-program"
CATEGORY_ASM = "assembler-program"

# Categories with NO application source to retrieve, so the fetch stage must not chase them
# (mirrors `fetch._NEVER_FETCHABLE`, but keyed on classification, not endpoint kind).
NON_FETCHABLE = frozenset({CATEGORY_INTERNAL, CATEGORY_IBM})

# --- Standard, closed IBM subsystem entry points (reference knowledge, not a guess) ------

# IBM MQ - the MQI verbs (a fixed API surface).
_MQI_VERBS = frozenset({
    "MQCONN", "MQCONNX", "MQDISC", "MQOPEN", "MQCLOSE", "MQGET", "MQPUT", "MQPUT1",
    "MQINQ", "MQSET", "MQCMIT", "MQBACK", "MQBEGIN", "MQSUB", "MQSUBRQ", "MQCB",
    "MQCTL", "MQSTAT", "MQCRTMH", "MQDLTMH", "MQSETMH", "MQINQMP", "MQSETMP",
    "MQDLTMP", "MQMHBUF", "MQBUFMH",
})

# IBM Db2 - the language-interface / message-formatting modules a precompiled program links.
_DB2_MODULES = frozenset({
    "DSNTIAR", "DSNTIAC", "DSNHLI", "DSNALI", "DSNRLI", "DSNELI", "DSNCLI",
})

# IBM Language Environment - the reserved CEE* callable-service namespace (a prefix, because
# CEE is reserved to LE: no application program may use it).
_LE_PREFIX = "CEE"

# IBM CICS - the reserved DFH* namespace, for exactly the reason CEE is a prefix and the
# MQI is a list: IBM reserves DFH, so a DFH-named module is CICS-supplied by construction
# and no application program may take the name. That is what makes a prefix a reading of
# the code rather than a guess, and it is also why this cannot be an enumerated list -
# CICS ships hundreds of them (DFHEI1 and DFHECI, the command-level stubs, plus DFHPC,
# DFHNCTR and the rest) and any list would go stale in exactly the way that leaves a
# caller looking like an unresolved application dependency. What the classification rests
# on is the RESERVATION, not on knowing what each module does.
#
# The cost, stated plainly because it is the same one CEE already carries: a site that
# breaks the reservation and names its own module DFHxxx now goes unfetched, where before
# it would have been probed and found. That trade is deliberate - the reservation is a
# documented IBM rule, and a site breaking it is a site defect, not a modelling case.
_CICS_PREFIX = "DFH"


def _copies_mq(copybooks: Iterable[dict]) -> bool:
    """True if the program COPYs an IBM MQ copybook (``CMQ*``) - the members that define
    the MQ structures (MQMD, MQGMO, ...) an MQI call passes. Corroborates MQ-verb calls."""
    for cb in copybooks or ():
        if str((cb or {}).get("member", "")).upper().startswith("CMQ"):
            return True
    return False


def classify_call_target(name: str, *, internal_programs: Iterable[str] = (),
                         copybooks: Iterable[dict] = (),
                         uses_sql: bool = False) -> Dict[str, str]:
    """Classify one CALL/LINK/XCTL target.

    Returns ``{"category": ..., "reason": ...}`` (plus ``"subsystem"`` for ``ibm-runtime``).
    Pure and total - every name lands in exactly one category, defaulting to ``unresolved``.
    The result is the STATIC classification; `fetch` may refine an ``unresolved`` target to
    ``cobol-program`` / ``assembler-program`` once the estate answers."""
    up = (name or "").upper()

    if up in {str(p).upper() for p in (internal_programs or ())}:
        return {"category": CATEGORY_INTERNAL,
                "reason": "a program contained in this source (nested PROGRAM-ID); this "
                          "CALL is internal, not an external dependency"}

    if up in _MQI_VERBS:
        reason = "IBM MQ MQI call - runtime library, no application source"
        if _copies_mq(copybooks):
            reason += " (program COPYs an MQ copybook)"
        return {"category": CATEGORY_IBM, "subsystem": "ibm-mq", "reason": reason}

    if up in _DB2_MODULES and uses_sql:
        return {"category": CATEGORY_IBM, "subsystem": "ibm-db2",
                "reason": "IBM Db2 language-interface / message module - precompiler "
                          "runtime, no application source"}

    if up.startswith(_LE_PREFIX):
        return {"category": CATEGORY_IBM, "subsystem": "ibm-le",
                "reason": "IBM Language Environment callable service (CEE*) - runtime "
                          "library, no application source"}

    if up.startswith(_CICS_PREFIX):
        return {"category": CATEGORY_IBM, "subsystem": "ibm-cics",
                "reason": "IBM CICS-supplied module (DFH* is reserved to CICS) - runtime "
                          "library, no application source"}

    return {"category": CATEGORY_UNRESOLVED,
            "reason": "not a contained program and not a recognised IBM runtime API; "
                      "resolution (COBOL/assembler source on the estate, or genuinely "
                      "unresolved) is decided by fetching it"}
