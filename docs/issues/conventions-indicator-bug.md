# Bug: Conventions Fallback Resolves Indicator Variables and Suppresses Derived-Column Flags

*(Discovered 2026-08-25, during monorepo merge — mfdep conventions available at test time.)*

## Summary

Two correctness bugs in the conventions fallback path (`statechart.py`):

1. **Indicator variables get false column mappings.** When `_conventions_recover` receives a count-mismatched INTO list (e.g. `INTO :WS-BAL:IND-BAL`), indicator variables like `IND-BAL` reach `conv.resolve_columns()` unfiltered. A real mfdep index resolves `IND-BAL` to column `BAL` — producing wrong lineage.

2. **Derived-column SELECTs lose their flag.** A SELECT like `SELECT ID, COUNT(*) INTO :WS-ID, :WS-N` correlates successfully (2 cols vs 2 vars, one `derived: true`) but also carries a `column_note`. The `select_pending` condition defers it to `select_sites` because `column_note is not None`, which suppresses the informational flag. Then `_correlate_selects` skips it (it already has `columns`). Result: "WS-N has no column identity" flag disappears.

## Reproduction

Run `sqlcols.cbl` and `sqlgaps.cbl` with mfdep conventions available:

```bash
# sqlcols.cbl 5000-INDICATOR: IND-BAL mapped to column BAL (WRONG)
PYTHONPATH=src python -c "
from cobol_xstate.parser import parse_program
from cobol_xstate.statechart import build_machine
import json
m = build_machine(parse_program(open('examples/sqlcols.cbl').read()))
for e in m.bundle()['interface']['events']:
    if '5000' in e.get('state', ''):
        print(json.dumps(e['columns'], indent=2))
"
# Expected: IND-BAL should NOT appear with a column mapping
# Actual: {"column": "BAL", "hostVar": "IND-BAL", "viaConventions": true}

# sqlgaps.cbl 5000-COUNT: "WS-N has no column identity" flag lost
PYTHONPATH=src python -c "
from cobol_xstate.parser import parse_program
from cobol_xstate.statechart import build_machine
m = build_machine(parse_program(open('examples/sqlgaps.cbl').read()))
flags = ' '.join(f['message'] for f in m.flags)
print('WS-N has no column identity' in flags)
"
# Expected: True
# Actual: False
```

## Failing tests (4)

These pass on a standalone checkout without mfdep, but fail when conventions are active:

| Test | Why it fails |
|------|-------------|
| `test_sql_fixtures.py::test_indicator_variable_refuses_to_correlate` | IND-BAL gets mapped to column BAL via conventions |
| `test_sql_fixtures.py::test_count_star_is_not_select_star` | "WS-N has no column identity" flag suppressed |
| `test_conventions.py::test_load_returns_none_without_mfdep` | `load()` returns a Conventions instance, not None |
| `test_conventions.py::test_auto_load_defaults_to_off_without_mfdep` | Auto-load enriches output (not equal to conv=None) |

---

## Required Fix 1: Don't defer already-correlated SELECTs

**File**: `src/cobol_xstate/statechart.py`, line 697-699

The `select_pending` condition defers a SELECT to the conventions post-pass when `column_note is not None`. But a SELECT that already has `columns` (parser correlated it successfully) should NOT be deferred — the note is informational, not a failure.

**Change**: Add `and not st.columns` to the condition:

```python
# Before:
select_pending = (st.lang == "SQL" and st.verb == "SELECT"
                  and st.column_note is not None and bool(st.into_vars)
                  and self.ctx.conventions is not None)

# After:
select_pending = (st.lang == "SQL" and st.verb == "SELECT"
                  and st.column_note is not None and bool(st.into_vars)
                  and self.ctx.conventions is not None
                  and not st.columns)
```

**Why**: If `st.columns` is truthy, the parser proved the correlation. The `column_note` in that case is about derived slots (COUNT(*), SUM()), not a failure that conventions should retry. The flag at line 702 should emit normally.

---

## Required Fix 2: Filter indicator variables before conventions

**File**: `src/cobol_xstate/statechart.py`

The `INTO :WS-BAL:IND-BAL` syntax identifies `IND-BAL` as a null-indicator (second colon-variable in a comma group). The raw SQL text preserves this structure: `INTO : WS-BAL : IND-BAL` — within the comma-separated group, any second `:VAR` is an indicator.

**Add** a helper (near `_conventions_recover`):

```python
_INTO_CLAUSE = re.compile(
    r"\bINTO\b(.*?)(?:\bFROM\b|\bWHERE\b|\bORDER\b|\bGROUP\b|\bHAVING\b|\bFOR\b"
    r"|\bEND-EXEC\b|$)", re.I | re.S)
_INTO_HOSTVAR = re.compile(r":\s*([A-Z][A-Z0-9-]*)", re.I)


def _indicator_vars(raw: str) -> FrozenSet[str]:
    """Identify indicator host variables from raw SQL text.

    In DB2 embedded SQL, ``INTO :DATA:IND`` (two colon-variables in one comma
    group) marks the second as a null indicator for the first.  Indicators carry
    null-status metadata, not column data — they must never receive a column
    identity mapping.
    """
    m = _INTO_CLAUSE.search(raw.upper() if raw else "")
    if not m:
        return frozenset()
    into_text = m.group(1)
    indicators: set = set()
    for group in into_text.split(","):
        vars_in_group = _INTO_HOSTVAR.findall(group)
        # First var in each comma group is data; subsequent vars are indicators
        for v in vars_in_group[1:]:
            indicators.add(v.upper())
    return frozenset(indicators)
```

**Then modify** both call sites that build `into` for `_conventions_recover`:

In `_correlate_fetches` (line ~1474) and `_correlate_selects` (line ~1510):

```python
# Before:
into = [a["target"] for a in spec.get("assignments", [])
        if isinstance(a, dict) and "target" in a]

# After:
all_into = [a["target"] for a in spec.get("assignments", [])
            if isinstance(a, dict) and "target" in a]
indicators = _indicator_vars(spec.get("raw") or "")
into = [v for v in all_into if v not in indicators]
```

**Tested regex results:**

| Raw SQL | Detected indicators |
|---------|---------------------|
| `INTO : WS-NAME , : WS-BAL : IND-BAL FROM CUSTOMER` | `{IND-BAL}` |
| `INTO : AA-FUND-A : IND-X , : AA-ACCT-NBR END-EXEC` | `{IND-X}` |
| `INTO : WS-ID , : WS-N FROM ACCOUNT` | `{}` (no indicators) |
| `INTO : WS-ID , : WS-BAL END-EXEC` | `{}` (no indicators) |

---

## Required Fix 3: Update test_conventions.py for mfdep-present environments

The two tests `test_load_returns_none_without_mfdep` and `test_auto_load_defaults_to_off_without_mfdep` guard the "mfdep absent" code path. Their invariants are correct for standalone use but cannot hold when mfdep IS importable.

**Options** (pick one):

**(A) Skip when mfdep is available:**
```python
def test_load_returns_none_without_mfdep():
    if load() is not None:
        pytest.skip("mfdep is installed; test only applies without it")
    assert load() is None

def test_auto_load_defaults_to_off_without_mfdep():
    if load() is not None:
        pytest.skip("mfdep is installed; test only applies without it")
    ...
```

**(B) Add companion tests for the mfdep-present path:**
```python
def test_load_returns_conventions_when_mfdep_available():
    result = load()
    if result is None:
        pytest.skip("mfdep not installed")
    assert isinstance(result, Conventions)
    assert result.disabled_reason is None

def test_auto_load_enriches_output_with_mfdep():
    if load() is None:
        pytest.skip("mfdep not installed")
    auto = build_machine(parse_program(...), source_name="convtest.cbl")
    spec = _spec(auto, "FETCH")
    assert spec.get("columns")
    assert any(c.get("viaConventions") for c in spec["columns"])
```

**Recommendation**: Do both (A) and (B). The original tests still verify the no-mfdep path when run standalone; the new tests verify the with-mfdep path in the monorepo.

---

## Required Fix 4: Update test expectations for indicator filtering

After Fix 2, tests that previously expected indicator variables in the `columns` list need updating:

- `test_fetch_count_mismatch_partial_resolution_with_cursor_table` — `IND-X` should no longer appear as `{"hostVar": "IND-X", "unresolved": True}` in the columns list. The columns list should contain only data variables.
- `test_select_count_mismatch_recovered_by_convention` — same: indicator vars excluded.
- The flag message counts change from "N of M" (M = all vars) to "N of M" (M = data vars only), which is more accurate.

---

## Golden update

After all fixes, run `python tools/gate.py --record` to re-record goldens. Output changes are expected for any example that has indicator variables with conventions active. Then verify byte-stability with `python tools/gate.py`.

---

## Impact

These bugs produce **wrong lineage** in production tracing: indicator variables are falsely mapped to columns, creating phantom cross-program links in Neo4j. Every program with indicator-variable SQL that also resolves via conventions currently produces incorrect edges. This is a data correctness issue, not cosmetic.
