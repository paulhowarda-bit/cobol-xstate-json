# Bug: Conventions fallback overrides indicator-variable refusal

## Summary

When the parser correctly refuses to correlate a SELECT/FETCH because of a column-count mismatch (caused by DB2 indicator variables), the mfdep conventions fallback kicks in and resolves the host variables anyway — to potentially wrong tables. The conventions fallback should not fire when the failure reason is a count mismatch.

## Reproduction

`examples/sqlcols.cbl`, paragraph `5000-INDICATOR`:

```sql
SELECT NAME, BAL
INTO :WS-NAME, :WS-BAL:IND-BAL
FROM CUSTOMER
```

DB2 syntax: `:WS-BAL:IND-BAL` means host variable `WS-BAL` with null indicator `IND-BAL`. So there are 2 columns and 2 real host variables — the parser sees 3 tokens in INTO and correctly refuses (2 columns vs 3 host variables).

With conventions active, `_correlate_selects` → `_conventions_recover` fires on this site. `resolve_columns(["WS-NAME", "WS-BAL", "IND-BAL"], ...)` resolves 2 of 3 (IND-BAL returns None because `IND` isn't a known DCLGEN prefix) — but the 2 it resolves map to `T_APWS_WKFL_STEP`, which is unrelated to the actual `CUSTOMER` table. The result is **wrong lineage injected by convention** where the parser had correctly said "I can't map this".

## Root Cause

`_conventions_recover` in `statechart.py` does not distinguish WHY the correlation failed. It fires on every failed site equally:
- "no DECLARE for cursor found" → conventions fallback is appropriate — but ONLY when the
  column list is genuinely unknown. A cursor DECLAREd **FOR a PREPAREd statement** has no
  select list until run time, so there is nothing for a naming convention to be a fallback
  FOR; it is now classified as `dynamic_sql` and `continue`s before this point. That path
  fired: a dynamic FETCH acquired `T_MMAA_ACC_ANAL.FUND_A` / `.ACCT_NBR`, both
  `viaConventions: true`, for a select list that does not exist at build time.
- "N columns vs M host variables" → conventions fallback is NOT appropriate (the count mismatch signals indicator variables or host structures that break the 1:1 column↔variable assumption)

## Fix

In `_conventions_recover` (or at the call sites in `_correlate_fetches` / `_correlate_selects`), skip the conventions fallback when the failure reason indicates a count mismatch. The `why`/`note` string already carries this info — it contains text like `"2 column(s) vs 3 host variable(s)"`.

Proposed guard:

```python
def _conventions_recover(ctx, spec, verb, para, line, into, table, program_tables, why):
    # Count mismatch means indicator variables or host structures —
    # the positional assumption is broken, conventions cannot help.
    if "vs" in why and "host variable(s)" in why:
        return False
    ...
```

Or more robustly, change the call sites to pass a structured reason rather than a string, and only invoke the fallback for specific failure classes:

```python
class _CorrelationFailure(enum.Enum):
    NO_DECLARE = "no_declare"          # cursor DECLARE not visible -> conventions OK
    COUNT_MISMATCH = "count_mismatch"  # indicator vars -> conventions NOT OK
    DYNAMIC_SQL = "dynamic_sql"        # PREPARE/EXECUTE -> conventions unlikely to help
    SELECT_STAR = "select_star"        # columns unknown -> conventions could help
```

## Affected Tests

- `test_indicator_variable_refuses_to_correlate` — asserts no mapping for `IND-BAL`; currently fails because conventions injects one
- `test_count_star_is_not_select_star` — the FETCH in `sqlgaps.cbl` (cursor C9) gets convention-resolved, changing the flags text

## Secondary Issue: WS-prefixed variables resolve to wrong table

`WS-NAME` and `WS-BAL` resolve via conventions to `T_APWS_WKFL_STEP` — a completely unrelated table. This happens because `WS` is a DCLGEN prefix for that table. In practice, `WS-` is overwhelmingly used as a generic working-storage prefix, not a DCLGEN prefix.

The conventions system should either:
1. Deprioritize `WS` as a prefix (it's too generic/ambiguous), or
2. Not resolve when the inferred table doesn't match any table the program actually references (the `program_tables` disambiguation should reject it — but only if `program_tables` is correctly populated for this site)

Check whether `program_tables` is being passed correctly at the SELECT correlation sites — if the program references `CUSTOMER` but not `T_APWS_WKFL_STEP`, the disambiguation should reject the WS-based resolution.
