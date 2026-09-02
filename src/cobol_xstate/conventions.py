"""mfdep naming-convention resolution for unmapped SQL columns.

Every DB2 table on the estate has a DCLGEN copybook declaring host variables under a
consistent prefix (``NAMES(AA)`` -> ``AA-FUND-A`` fills column ``FUND_A`` on
``T_DMAA_ACC_ANAL``), and COPY REPLACING mints variant prefixes onto the same columns.
mfdep indexes those conventions; when the *statement* evidence for a column<->host-
variable mapping is missing (cursor DECLARE not visible, count mismatch), the naming
convention can still recover it. See ``docs/mfdep-conventions-integration.md``.

This module is the only place ``mfdep`` is touched. The package is ASSUMED PRESENT -
it is part of the runtime environment, always installed beside this tool - so there is
no degraded conventions-less mode: the import happens lazily on the first failed
correlation that needs recovering (a program with nothing to recover never imports
it), and if it is needed and missing the run dies on the ImportError rather than
silently emitting unmapped columns (exactly the silent-degradation failure the v50
trace was). Tests and the byte-stability goldens pass an explicit
``conventions=None`` to ``build_machine`` - a determinism pin, so their outputs never
depend on the day's ``mfdep.db`` contents, not an absence fallback.

Two division-of-labour rules. A convention-recovered mapping is a HEURISTIC, not a
proof - every entry it produces is marked ``viaConventions`` and the call sites flag
it, per the no-invented-logic rule: recovered-by-convention must never read as
recovered-from-the-source. And classifying the slots is MFDEP'S JOB, not this
module's: indicator variables and derived columns are whatever
``resolve_field_variants`` says they are - its verdict is taken verbatim, and a
variable it declines to place stays an explicit ``unresolved`` entry. No second-
guessing here.
"""

from __future__ import annotations

from typing import FrozenSet, List, Optional, Tuple


def base_table(name: str) -> str:
    """``OWNER.T`` -> ``T``: mfdep's DCLGEN index speaks unqualified table names, so
    every comparison against it drops the schema qualifier first."""
    return name.rsplit(".", 1)[-1]


def load() -> "Conventions":
    """The mfdep conventions API, wrapped. mfdep is assumed present; if it is not,
    the ImportError propagates - a loud failure beats silently unmapped columns.

    Deliberately uncached: the import system already memoizes the module, and a fresh
    wrapper per build keeps one run's failure (``disabled_reason``) from silently
    muting every later run in the same process.
    """
    import mfdep.conventions as api  # deliberate lazy import - see module docstring
    return Conventions(api)


class Conventions:
    """Column resolution over the mfdep conventions API (``resolve_field_variants``,
    ``infer_table_from_prefix``).

    Any exception out of mfdep disables the instance for the rest of the run
    (``disabled_reason`` records why) rather than crashing the build or resolving
    half a statement: the model must degrade to exactly the conventions-less output,
    plus one flag saying the lookup failed.
    """

    def __init__(self, api) -> None:
        self._api = api
        self.disabled_reason: Optional[str] = None

    def _call(self, fname: str, *args):
        if self.disabled_reason is not None:
            return None
        fn = getattr(self._api, fname, None)
        if fn is None:
            self.disabled_reason = f"mfdep.conventions has no {fname}()"
            return None
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001 - any mfdep failure disables, never crashes
            self.disabled_reason = f"{fname}() raised {type(exc).__name__}: {exc}"
            return None

    def resolve_field(self, field: str, table: str = "",
                      program_tables: FrozenSet[str] = frozenset()
                      ) -> Optional[dict]:
        """One host variable -> ``{"column", "table"}`` by naming convention, or None.

        The table evidence must AGREE, not merely exist - the generic-prefix hazard
        (``WS-`` is the estate's working-storage prefix on nearly every program, AND
        somebody's DCLGEN prefix) means a lone prefix->table hit can be flatly wrong
        (docs/issues/conventions-indicator-variable-bug.md). So:

        * ``table`` known (the statement's own FROM / the cursor's DECLARE): that is
          the ground truth. The prefix must VALIDATE against it (the table is among
          the prefix's candidates, or is what mfdep itself inferred) - a prefix whose
          tables contradict the statement resolves nothing.
        * ``table`` unknown: a unique candidate, or an ambiguous prefix narrowed to
          exactly one of the tables this program provably references (the doc's
          collision disambiguation), or mfdep's own single inference - and then the
          program's references get a veto: a resolution naming a table this program
          never touches is the WS- hazard again. An EMPTY reference set vetoes
          nothing (a pure reader whose only table mention was the missing DECLARE).

        Anything still ambiguous or contradicted stays unresolved - a guessed table
        is wrong lineage, which is worse than none.
        """
        field = (field or "").upper()
        r = self._call("resolve_field_variants", field, table or "")
        if not isinstance(r, dict):
            return None
        column = str(r.get("db2_column") or "").upper()
        prefix = str(r.get("prefix") or "")
        if not column:
            return None
        raw = self._call("infer_table_from_prefix", prefix) if prefix else None
        candidates = (sorted({base_table(str(t).upper()) for t in raw})
                      if isinstance(raw, (list, tuple)) else [])
        inferred = base_table(str(r.get("table") or "").upper())
        ptables = {base_table(t.upper()) for t in program_tables}
        table = base_table((table or "").upper())
        resolved: Optional[str] = None
        if table:
            if table in candidates or (inferred and table == inferred):
                resolved = table
        else:
            if len(candidates) == 1:
                resolved = candidates[0]
            elif len(candidates) > 1:
                hits = sorted(set(candidates) & ptables)
                if len(hits) == 1:
                    resolved = hits[0]
            else:
                resolved = inferred or None
            if resolved and ptables and resolved not in ptables:
                resolved = None
        if not resolved:
            return None
        return {"column": column, "table": resolved}

    def resolve_columns(self, into_fields: List[str], table: str,
                        program_tables: FrozenSet[str] = frozenset()
                        ) -> Tuple[Optional[List[dict]], int]:
        """A whole INTO list -> ``columns[]`` entries, or ``(None, 0)``.

        Each resolved entry is marked ``viaConventions``; a variable the conventions
        cannot place keeps an explicit ``unresolved`` entry, so "recovered by
        convention" and "the recovery failed on this field" stay distinguishable per
        slot (the same rule the parser's ``derived`` entries follow). Whether a slot
        is an indicator variable or a derived column is MFDEP'S determination - every
        variable goes to it and its verdict is taken verbatim, never re-classified
        here. A list with no resolved entry at all - or a lookup that failed partway
        - is no list.
        """
        entries: List[dict] = []
        resolved = 0
        for var in into_fields:
            hit = self.resolve_field(var, table=table, program_tables=program_tables)
            if self.disabled_reason is not None:
                return None, 0
            if hit:
                entries.append({"column": hit["column"], "hostVar": var,
                                "table": hit["table"], "viaConventions": True})
                resolved += 1
            else:
                entries.append({"hostVar": var, "unresolved": True})
        if not resolved:
            return None, 0
        return entries, resolved
