# Unmapped Field Lineage — v52 Trace Analysis

## Summary

Tracer run v52 (2026-08-25, seed FBMMAAIO, depth unlimited, stopped at ~4,770 programs)
produced **11,660 unmapped field warnings** out of 116,636 total field-column link attempts
(**90.0% mapped, 10.0% unmapped**). 1,667 programs produced field lineage output.

These unmapped fields represent places where `lineage.json` reports a host variable flowing
to/from a DB2 table, but the `bundle.json` interface events don't provide a matching
`columns[]` entry with both `hostVar` and `column` keys — so the graph loader can't create a
READS_COLUMN or WRITES_COLUMN edge in Neo4j.

This document details the root causes and provides actionable fix instructions for the
cobol-xstate-json and cobol_parser packages.

---

## Status (2026-08-26) — all five addressed in this repo, verified on master

Unlike the v50 report, this one describes **current master**: every cited file length
matched (parser 1877, interface 1022, statechart 1847, lineage 939, conventions 165) and
every cited site read as quoted. Nothing here was a stale-build artefact.

| # | What shipped | Where |
|---|---|---|
| 2 | `where_vars` on `ExecStmt` (depth-scoped WHERE scan) → `params` on **every** Db2 verb, matching what `SELECT` already did. A `DELETE` writes nothing, so all its host variables are parameters. Duplicate host variables in one statement are now reported once. | `parser._exec_where_vars`, `interface._dml_split` |
| 3 | `:GROUP . FIELD` resolves to the **elementary field name** through one shared rule used by all four colon scanners (statement-wide, WHERE, INTO, VALUES/SET slot). A qualified `SET`/`VALUES` slot now keeps its column mapping too. | `parser._host_var_at`, `interface._SQL_HOSTVAR` |
| 1 | `expression` + `derivedFrom` on derived entries — **provenance, never identity**: `derived` stays and no `column` key is ever added. A `FETCH` takes its derivation from the cursor's `DECLARE`. | `parser._derivation_of`, `interface._cursor_derivations` |
| 4 (slice) | `columnsUnresolved` — a stable token (`cursor-unidentified`, `cursor-declare-missing`, `count-mismatch`, `insert-no-column-list`) so a consumer skips the **event** rather than reporting each field. | `interface._fetch_columns`, `statechart` |
| 5 (slice) | The conventions fallback now reaches a column-list-less `INSERT` — the UNKNOWN-column-list class. A count mismatch is still never convention-resolved. | `statechart._correlate_inserts` |

Two deliberate departures from the proposals above:

- **Issue 1, rule 3 (`CASE WHEN … THEN COL1 ELSE COL2 END`)** does *not* list its
  columns. Which branch supplied the value is a run-time fact, so naming both would
  claim two dependencies where the source proves at most one. It reports
  `expression: "CASE"` with an empty `derivedFrom`. Arithmetic expressions (`A + B`)
  *do* list both, since both are read.
- **Issue 2 uses Option A** (`params`), not Option B (`role: "filter"`). `lineage.py`
  never reads `params`, so the rows disappear with no lineage change and no graph-loader
  change — and `SELECT` had reported its WHERE variables this way all along, which makes
  the CREATE side's behaviour the inconsistency rather than a new policy.

Also corrected from the analysis above: the conventions fallback did **not** trigger
"only for FETCHes" — `_correlate_selects` existed too. `INSERT` was the one pass missing
it.

**Verification**: 706 tests (94 in mainframe-common), `tools/gate.py` 6/6 byte-stable
after a reviewed re-record, `byteproof --check goldens/parse.sha256` green, and all 95
Node-backed tests ran under real XState. New fixtures `examples/sqlqual.cbl` and
`examples/sqlderiv.cbl`. `parse_bundle.VERSION` 2 → 3 (both new `ExecStmt` fields in one
bump).

**Still open, and out of scope here** — all three need the estate or another repo:

- The graph loader's side of Issue 1: consuming `derivedFrom` and creating the
  `DERIVED_INTO` edge is a `mainframe-tracer` change. This repo ships the data.
- Issue 4's fetch pipeline (making absent SQL-include copybooks arrive) and Issue 5's
  DCLGEN supply.
- Re-running v52 to measure the actual drop in the 11,660 count.

---

## Root Cause Breakdown

| # | Root Cause | Unmapped Fields | Impact |
|---|------------|-----------------|--------|
| 1 | Aggregate/expression source columns lost | ~6,338 (GET) | Source column inside SUM/MAX/COALESCE/CASE discarded |
| 2 | WHERE clause parameters reported as lineage | ~3,500 (mixed) | UPDATE WHERE vars, DELETE WHERE vars in `fields` |
| 3 | Qualified host vars (`:GROUP.FIELD`) | ~1,800 (CREATE) | Parser captures group name only, loses `.FIELD` |
| 4 | Unresolved cursor FETCHes | 259 events / 7,444 fields | DECLARE in absent copybook, no select-list available |
| 5 | INSERT without column list / no DCLGEN | ~1,132 events | No DECLARE TABLE visible for positional correlation |

---

## Issue 1: Aggregate/Expression Source Column Lost

### What Happens

When a SELECT or cursor DECLARE contains an expression like `SUM(SPOKE_DOL_A)`, `COUNT(*)`,
`MAX(FIELD)`, `COALESCE(COL, 0)`, or a literal like `'Y'` / `1`, the parser's `_column_of()`
method returns `None` for that select-list slot. The `None` propagates through `_correlate()`
which produces `{"hostVar": "WS-TOTAL", "derived": True}` — an entry with no `column` key.

The graph loader at `graph_loader.py:522` requires all three of `hostVar`, `table`, AND
`column` to build the host_var_map:

```python
if hv and tbl and col:
    host_var_map[hv] = (tbl, col)
```

Since `derived: True` entries have no `column`, they never enter the map, and the
corresponding lineage row triggers the "Unmapped field" warning.

### Distribution (8,982 column entries marked derived)

| SQL Pattern | Count | Source Info Available? |
|-------------|-------|------------------------|
| `SUM(COL)` | 1,677 | Yes — `COL` is extractable |
| `COALESCE(COL, default)` / `VALUE(COL, 0)` | 1,667 | Yes — first arg is usually a column |
| `SELECT 'Y'` / `SELECT 1` (existence check) | 792 | No — truly no source column |
| `COUNT(*)` | 475 | No — aggregates all rows |
| `MAX(COL)` / `MIN(COL)` / `AVG(COL)` | 166 | Yes — `COL` is extractable |
| FETCH from cursor with aggregate in DECLARE | 3,658 | Yes — at DECLARE site |
| Other inline expressions | 482 | Varies |

### Root Location

**File**: `packages/mainframe-common/cobol_parser/parser.py`
**Method**: `StmtParser._column_of()` (line 1359)

```python
@staticmethod
def _column_of(item: List[Token]) -> Optional[str]:
    """The column one select-list item names, or None if it is *derived*."""
    for i, t in enumerate(item):
        if t.kind == "word" and t.up == "AS":
            item = item[:i]
            break
    item = [t for t in item if not (t.kind == "punct" and t.text in "()")] or item
    if len(item) == 1 and item[0].kind == "word":
        return item[0].up
    if (len(item) == 3 and item[0].kind == "word"        # BAL
            and item[1].kind == "period" and item[2].kind == "word"):
        return item[2].up                                # T.BAL -> BAL
    return None                                          # <- everything else is lost
```

After stripping parens, `SUM(SPOKE_DOL_A)` becomes `[SUM, SPOKE_DOL_A]` — 2 tokens, neither
the 1-token nor 3-token pattern matches, so `None` is returned. The actual source column
`SPOKE_DOL_A` is right there in the token list.

### Also Involved

**File**: `packages/mainframe-common/cobol_parser/parser.py`
**Method**: `StmtParser._correlate()` (line 1631)

```python
mapped = [{"column": c, "hostVar": h} if c is not None
          else {"hostVar": h, "derived": True}
          for c, h in zip(columns, into_vars)]
```

And for cursor FETCHes:

**File**: `packages/cobol-xstate-json/src/cobol_xstate/interface.py`
**Method**: `_fetch_columns()` (line 255)

```python
return [{"column": c, "hostVar": h} if c is not None
        else {"hostVar": h, "derived": True}
        for c, h in zip(cols, into_fields)], None
```

### Concrete Examples

#### Example A: `SUM(column)` — source column lost

```sql
-- Program: FBBP497 | Event: GET.DB2.T_MMJT_JRNL_TXN
EXEC SQL SELECT SUM(SPOKE_DOL_A)
    INTO :W-TOTAL-SPOKE-XFER
    FROM T_MMJT_JRNL_TXN
    WHERE TRK_N = :JT-TRK-N
      AND MULTI_CO_N = :WS-MULTI-CO-N
    GROUP BY TRK_N
END-EXEC
```

**Current output**: `{"hostVar": "W-TOTAL-SPOKE-XFER", "derived": True}`
**Desired output**: `{"hostVar": "W-TOTAL-SPOKE-XFER", "derived": True, "derivedFrom": ["SPOKE_DOL_A"], "expression": "SUM"}`

#### Example B: `COALESCE(column, literal)`

```sql
-- Program: FBC4272 | Event: GET.DB2.T_MMAR_ACC_ANAL_HST
EXEC SQL SELECT VALUE(SUM(TRX_RUN_COLL_BAL_A), 0)
    INTO :W-TOTAL-HST-TXN-AMT
    FROM T_MMAR_ACC_ANAL_HST
    WHERE ...
END-EXEC
```

**Current output**: `{"hostVar": "W-TOTAL-HST-TXN-AMT", "derived": True}`
**Desired output**: `{"hostVar": "W-TOTAL-HST-TXN-AMT", "derived": True, "derivedFrom": ["TRX_RUN_COLL_BAL_A"], "expression": "VALUE(SUM(...))"}`

#### Example C: Literal existence check (no source column)

```sql
-- Program: FBMMAAIO | Event: GET.DB2.T_MMTC_TRX_CTL
EXEC SQL SELECT 'Y'
    INTO :W-MMTC-EXISTS-SW
    FROM T_MMTC_TRX_CTL
    WHERE PGM_NM = :FBMMAAIO-CALLING-PGM
      AND PHS_N = 'ID'
END-EXEC
```

**Current output**: `{"hostVar": "W-MMTC-EXISTS-SW", "derived": True}`
**Desired output**: `{"hostVar": "W-MMTC-EXISTS-SW", "derived": True, "derivedFrom": [], "expression": "literal"}`

This case truly has no source column — the output is fine. But it should be distinguishable
from the SUM/MAX cases that DO have a source column.

#### Example D: FETCH from cursor with SUM in DECLARE

```sql
-- Program: FBC2124
-- DECLARE: SELECT SUM(ADJ_A), FBSI_BRCH_C, FBSI_BASE_C FROM RTOA_ADJUSTMENT ...
-- FETCH:
EXEC SQL FETCH BOLA_CURSOR
    INTO :OA-FBSI-BRCH-C, :OA-FBSI-BASE-C, :WS-SUM-OA-ADJ-A
END-EXEC
```

The DECLARE's select_list is `["FBSI_BRCH_C", "FBSI_BASE_C", None]` — the SUM slot is `None`.
When correlated with the FETCH's INTO vars, the third slot (`WS-SUM-OA-ADJ-A`) gets
`derived: True` without knowing its source is `ADJ_A`.

### Proposed Fix

Enhance `_column_of()` to extract the innermost column from aggregate functions:

```python
@staticmethod
def _column_of(item: List[Token]) -> Tuple[Optional[str], Optional[str]]:
    """Returns (column_name, expression_type) or (None, expression_type).

    For SUM(COL)  -> ("COL", "SUM")
    For COUNT(*)  -> (None, "COUNT")
    For 'Y'       -> (None, "literal")
    For COL       -> ("COL", None)
    """
```

Then propagate the expression type and source columns into the `columns[]` entries:

```python
# In _correlate():
{"column": c, "hostVar": h}                     # simple column
{"hostVar": h, "derived": True,
 "derivedFrom": [c], "expression": expr}        # aggregate with extractable source
{"hostVar": h, "derived": True,
 "derivedFrom": [], "expression": "literal"}    # no source column at all
```

The `derivedFrom` array enables the graph loader to create edges:
- `(:Column (table: T, column: SPOKE_DOL_A)) -[:DERIVED_INTO]-> (:Field (name: W-TOTAL-SPOKE-XFER))`

This preserves the data lineage chain: the field IS derived, but we know WHERE the data came
from.

### Aggregate Recognition Rules

For `_column_of()` — after stripping parens and AS:

1. **Single aggregate function**: tokens like `[SUM, COL]` or `[MAX, COL]` or `[COUNT, *]` →
   if first token is an aggregate keyword (`SUM`, `COUNT`, `MAX`, `MIN`, `AVG`, `VALUE`,
   `COALESCE`), extract subsequent column-shaped tokens.

2. **Nested**: `VALUE(SUM(COL), 0)` → strip outer function, recurse. The innermost named
   column is the source.

3. **Multi-column expressions**: `A + B`, `CASE WHEN ... THEN COL1 ELSE COL2 END` → extract
   ALL column-shaped tokens as `derivedFrom: ["COL1", "COL2"]`.

4. **Literals only**: `'Y'`, `1`, `CURRENT DATE` → `derivedFrom: []`, `expression: "literal"`.

5. **Star**: `COUNT(*)` → `derivedFrom: []`, `expression: "COUNT(*)"`.

---

## Issue 2: WHERE Clause Parameters Reported as Lineage Fields

### What Happens

For UPDATE and DELETE statements, `interface.py:_classify_exec()` passes ALL host variables as
`fields` — both SET-clause variables (which DO write to columns) and WHERE-clause variables
(which are filter parameters, not column writes).

```python
# interface.py line 415 (UPDATE handler):
return [_note(_hit("create", _DB2, ep, verb, host_vars,
                   columns=_qualify(columns, ep)), note)]
```

The `host_vars` list is built from ALL `:VAR` references in the SQL text, regardless of their
position (SET vs WHERE). The `columns[]` array IS correctly populated only for SET-clause vars
(via `_exec_update_sets`), but `fields` gets everything.

The lineage emitter then creates a lineage row for every field in the event, including
WHERE-clause parameters. The graph loader tries to map them all, and the WHERE params fail
because they have no column mapping.

### Scale

- **11,660 total unmapped warnings**
- **5,322 are from CREATE events** (INSERT/UPDATE/DELETE)
- Of these, a significant portion are WHERE-clause parameters that shouldn't generate
  column-lineage at all

### Concrete Example

```sql
-- Program: FBB4527 | Event: CREATE.DB2.T_MMMC_MIR_CTL | verb: UPDATE
EXEC SQL UPDATE T_MMMC_MIR_CTL
    SET USAGE_IND = :LS-PARM-USAGE-IND,
        LAST_UPDATED_ID = :LS-PARM-JOB-NAME,
        LAST_UPDATED_TS = CURRENT TIMESTAMP
    WHERE TABLE_NAME = :LS-PARM-TABLE-NAME
      AND MULTI_CO_N = :WS-MULTI-CO-N
END-EXEC
```

**Current state**:
- `fields`: `["LS-PARM-USAGE-IND", "LS-PARM-JOB-NAME", "LS-PARM-TABLE-NAME", "WS-MULTI-CO-N"]`
- `columns`: `[{hostVar: "LS-PARM-USAGE-IND", column: "USAGE_IND"}, {hostVar: "LS-PARM-JOB-NAME", column: "LAST_UPDATED_ID"}]`
- Lineage emits 4 rows with direction "output" for all fields
- Graph loader maps 2, warns about 2

**Problem**: `LS-PARM-TABLE-NAME` and `WS-MULTI-CO-N` are WHERE-clause filters. They don't
write to any column — they select which row gets updated. They should not appear in lineage as
output fields.

### Root Locations

1. **Field list construction** — `interface.py:_classify_exec()` (line ~380):
   ```python
   host_vars = [h.lstrip(":").upper()
                for h in ((spec or {}).get("hostVars") or _sql_host_vars(mup))]
   ```
   This gets ALL host vars from the SQL text indiscriminately.

2. **The hit construction for UPDATE/DELETE** — `interface.py` lines 415, 420:
   ```python
   # UPDATE
   return [_note(_hit("create", _DB2, ep, verb, host_vars, columns=...), note)]
   # DELETE
   return [_note(_hit("create", _DB2, ep, verb, host_vars), note)]
   ```
   The `host_vars` passed as `fields` includes WHERE params.

3. **Lineage emission** — `lineage.py` line 608:
   ```python
   for f in h["fields"]:
       rows.append(self._row(name, h, "input"/"output", f, ...))
   ```
   Emits a lineage row for every field.

### Proposed Fix

Separate WHERE-clause host variables from SET/VALUES host variables:

**Option A** (minimal change): Add a `params` field to the hit (already done for SELECT), and
exclude params from lineage emission:
```python
# For UPDATE: fields = SET vars, params = WHERE vars
params = [h for h in host_vars if h not in set_vars]
set_vars = [col["hostVar"] for col in columns] if columns else host_vars
return [_hit("create", _DB2, ep, verb, set_vars, params=params, columns=...)]
```

**Option B** (richer): Mark WHERE params in lineage with a distinct role:
```python
# lineage row:
{"field": "WS-MULTI-CO-N", "direction": "output", "role": "filter",
 "endpoint": "T_MMMC_MIR_CTL", "event": "CREATE.DB2.T_MMMC_MIR_CTL"}
```

The graph loader already handles `params` on GET events (it doesn't create lineage edges for
them). For CREATE events, the same separation is needed.

### Affected Host Variables (top contributors)

| Variable | Count | Why it's a WHERE param |
|----------|-------|------------------------|
| `WS-MULTI-CO-N` | 343 | Company number filter in every multi-company UPDATE/DELETE |
| `WS-DUMMY` | 393 | Often `SELECT INTO :WS-DUMMY` existence check, also WHERE |
| `MFER-ERROR` | 46 | Group-level var used in WHERE of T_MFER_ERROR updates |
| `WS-COUNT` | 107 | Aggregate result or WHERE filter |
| `WS-EXISTS` | 76 | Existence-check variable |

---

## Issue 3: Qualified Host Variables (`:GROUP . FIELD`)

### What Happens

COBOL allows referencing fields with qualified notation: `:GFAC . AC-ACC-N` means "field
AC-ACC-N within group GFAC." The parser's host variable extraction regex captures only the
group name:

**File**: `packages/cobol-xstate-json/src/cobol_xstate/interface.py` line 70:
```python
_SQL_HOSTVAR = re.compile(r":\s*([A-Z0-9-]+)", re.I)
```

For `: GFAC . AC-ACC-N`, this captures `GFAC` and stops (space and dot aren't in
`[A-Z0-9-]`).

**File**: `packages/mainframe-common/cobol_parser/parser.py` line 1261:
```python
for idx, t in enumerate(toks):
    if t.kind == "punct" and t.text == ":" and idx + 1 < len(toks) \
            and toks[idx + 1].kind == "word":
        host_vars.append(":" + toks[idx + 1].text.upper())
```

The lexer tokenises `: GFAC . AC-ACC-N` as:
- `punct ':'` → `word 'GFAC'` → `period '.'` → `word 'AC-ACC-N'`

The parser takes only the word immediately after `':'`, which is `GFAC` (the group name). The
actual field `AC-ACC-N` after the period is ignored.

### Scale

- **22,949 qualified host variable references** across all v52 bundles
- **Top group names**: CAAM-ACC-MSTR (1,977), RGIN-INST (1,253), RTJH-REQ-HDR (910),
  RGRE-RED-ANNC (704), CACU-CUSTOMER (547), RTAC-ACCOUNT (528)
- **635 events** have empty or partial columns because of this (225 empty + 410 partial)
- The remaining 3,062 events with qualified vars still get correct columns because the
  INSERT/UPDATE has an explicit column list that provides the mapping independently

### Concrete Example

```sql
-- Program: FBB5191 | Event: CREATE.DB2.GFAC_ACC | verb: INSERT
EXEC SQL INSERT INTO GFAC_ACC (MULTI_CO_N, ACC_N, CSDN_C, ...)
    VALUES (:GFAC . AC-MULTI-CO-N,
            :GFAC . AC-ACC-N,
            :GFAC . AC-CSDN-C, ...)
END-EXEC
```

**Current extraction**: `fields: ["GFAC", "GFAC", "GFAC", ...]` (group name repeated)
**Desired extraction**: `fields: ["GFAC.AC-MULTI-CO-N", "GFAC.AC-ACC-N", "GFAC.AC-CSDN-C", ...]`
or equivalently `["AC-MULTI-CO-N", "AC-ACC-N", "AC-CSDN-C", ...]`

### Proposed Fix

**In the parser** (`parser.py` line 1261): After capturing the word following `':'`, check if
the next tokens are `period` + `word`. If so, join them:

```python
for idx, t in enumerate(toks):
    if t.kind == "punct" and t.text == ":" and idx + 1 < len(toks) \
            and toks[idx + 1].kind == "word":
        base = toks[idx + 1].text.upper()
        # Check for qualified: : GROUP . FIELD
        if (idx + 3 < len(toks)
                and toks[idx + 2].kind == "period"
                and toks[idx + 3].kind == "word"):
            qualified = base + "." + toks[idx + 3].text.upper()
            host_vars.append(":" + qualified)
        else:
            host_vars.append(":" + base)
```

**In the interface regex** (`interface.py` line 70):
```python
# Match both :VAR and :GROUP.FIELD (with optional spaces around dot)
_SQL_HOSTVAR = re.compile(r":\s*([A-Z0-9-]+(?:\s*\.\s*[A-Z0-9-]+)?)", re.I)
```

**Representation decision**: The qualified name can be stored as:
- Full: `"GFAC.AC-ACC-N"` (preserves provenance)
- Field-only: `"AC-ACC-N"` (matches the elementary field name in data division)
- Both: `{"hostVar": "AC-ACC-N", "qualifier": "GFAC"}`

Recommendation: Store the **elementary field name** (`AC-ACC-N`) as `hostVar` since that's
what appears in the data division and what naming conventions can resolve. Keep the qualifier
available for disambiguation if needed.

### Column Mapping Impact

Once the field name is correctly extracted as `AC-ACC-N` (instead of `GFAC`), the existing
INSERT column-list correlation can map it:
- The INSERT has `MULTI_CO_N` at position 1, and VALUES has `:GFAC.AC-MULTI-CO-N` at position 1
- Currently: hostVar = `"GFAC"` (wrong) → can't match to column `MULTI_CO_N`
- After fix: hostVar = `"AC-MULTI-CO-N"` (correct) → naming conventions resolve to `MULTI_CO_N`

---

## Issue 4: Unresolved Cursor FETCHes (DECLARE in Absent Copybook)

### What Happens

A FETCH statement references a cursor whose DECLARE lives in a copybook that wasn't fetched as
a dependency. Without the DECLARE, there's no select-list to correlate against the FETCH's
INTO variables.

### Scale

- **259 events** with `<cursor X>` as endpoint
- **7,444 fields** across those events
- **Top case**: RTJH-REQ-HDR appears in 720 `<cursor>` events (60 cursor variants × 12
  programs using the same copybook)

### Concrete Example

```sql
-- Program: FBBRTJH | Event: GET.DB2.<cursor ACCT-DATE-DESC>
EXEC SQL FETCH ACCT-DATE-DESC INTO :RTJH-REQ-HDR END-EXEC
```

The cursor `ACCT-DATE-DESC` is declared in a copybook (likely `FBBRTJHD` or similar) that
wasn't fetched. Without the DECLARE, we don't know:
1. Which table the cursor reads from
2. Which columns are in the select list

### Why This Happens

The artifact fetcher (`mf_fetch`) retrieves the primary source file and its COPY members. But
the cursor DECLARE may live in:
- A data-division DCLGEN copybook that IS fetched (these work fine)
- A SQL-specific include that's NOT listed as a COPY dependency

### Proposed Fix

**Short-term**: The existing `sql_cursors` scan (whole-stream cursor detection in `Machine`)
should be enhanced to look for cursor DECLAREs across ALL fetched members, not just procedure
division. This is partially done already — verify that the COPY members are actually being
scanned.

**Medium-term**: When a cursor cannot be resolved, emit a `cursorMissing` flag on the event.
The graph loader can then skip these cleanly rather than warning about each field individually.

**Long-term**: Add a pre-pass that identifies required DCLGEN copybooks from the SQL statements
and fetches them explicitly before the main extraction pass. The cursor name often encodes the
table (e.g., `ACCT-DATE-DESC` → table RTAC_ACCOUNT or similar).

---

## Issue 5: INSERT Without Column List / No DCLGEN

### What Happens

An INSERT like `INSERT INTO T VALUES (:h1, :h2, :h3)` states no columns — the slots map by
position to the table's declared column order. Resolving this requires the table's DCLGEN
(DECLARE TABLE statement), which shows the column order.

The `_correlate_inserts()` function in `statechart.py` (line 1587) looks for `declared_tables`
— tables whose DCLGEN was included in the source. When absent, it emits a note and leaves
columns empty.

### Scale

- **1,132 CREATE.DB2 events** with completely empty `columns[]`
- **174 are INSERT...SELECT** (values from a query, not host vars — no direct lineage possible)
- Remaining are INSERT/UPDATE/DELETE where the column list is absent and no DCLGEN was found

### Top Tables Affected

These tables frequently appear without their DCLGEN:
- `RS_RESTART` / `T_MFRS_RESTART` / `T_MMRS_RESTART` (checkpoint/restart tables)
- Various operational tables

### Proposed Fix

**Supply DCLGENs**: The DCLGEN copybooks exist on the network share. The extractor's
prefetch/artifact stage should include them. Common patterns:
- Table `T_XXXX_YYYY` → DCLGEN member name is typically `DXXXX` or `DCL_XXXX_YYYY`
- The `mfdep.db` source index knows where these live

**Naming convention fallback**: For INSERT without column list, if the host variables follow
the table prefix convention (e.g., `AA-FUND-A` → `FUND_A` for `T_MMAA_ACC_ANAL`), the
conventions resolver should attempt position-independent matching. This already partially works
via `_conventions_recover()` (statechart.py:1440) but only triggers for FETCHes, not INSERTs.

---

## Interaction Between Issues

These issues compound. A single program can hit multiple issues simultaneously:

1. A FETCH from a cursor declared in an absent copybook (Issue 4) means no select-list is
   available
2. Even when the select-list IS available, aggregates produce `None` entries (Issue 1)
3. Fields that DO appear in lineage may be WHERE params rather than actual data flow (Issue 2)
4. Qualified host vars show up as the group name instead of the elementary field (Issue 3)
5. Without a DCLGEN, INSERT positional matching fails entirely (Issue 5)

The graph loader only sees the final combined effect: a lineage row with no column mapping.

---

## Priority Ordering

Ranked by impact × feasibility:

### P0: Issue 2 — WHERE params in lineage (high impact, clean fix)
- **Impact**: Eliminates ~3,500 false unmapped warnings immediately (WS-MULTI-CO-N alone is 343)
- **Risk**: Low — only changes what gets emitted, not how parsing works
- **Scope**: `interface.py` (split host_vars into fields vs params for CREATE events) +
  `lineage.py` (skip params or mark as filter)

### P1: Issue 3 — Qualified host vars (high impact, localised fix)
- **Impact**: Recovers correct field names for 22,949 host var references; ~635 events gain
  correct columns
- **Risk**: Low — the fix is in two regexes/lookups
- **Scope**: `parser.py` line 1261 + `interface.py` line 70

### P2: Issue 1 — Aggregate source columns (high value, moderate effort)
- **Impact**: Recovers source column identity for ~3,500 derived fields (SUM/MAX/COALESCE with
  extractable columns)
- **Risk**: Medium — requires careful parsing of nested expressions
- **Scope**: `parser.py:_column_of()` + `_correlate()` + `interface.py:_fetch_columns()` +
  consumers must handle new schema

### P3: Issue 4 — Cursor DECLARE discovery (moderate impact, moderate effort)
- **Impact**: 7,444 fields across 259 events
- **Risk**: Medium — requires changes to artifact fetching strategy
- **Scope**: Fetch pipeline + `statechart.py` cursor correlation

### P4: Issue 5 — Missing DCLGENs (moderate impact, infrastructure change)
- **Impact**: 1,132 events gain column mappings
- **Risk**: Medium — requires identifying and fetching additional artifacts
- **Scope**: Artifact resolver + prefetch logic

---

## Testing Strategy

For each fix, the tracer's existing test infrastructure provides verification:

1. **Unit tests**: Add test cases in cobol-xstate-json for each SQL pattern (e.g.,
   `SELECT SUM(X) INTO :Y`)
2. **Regression**: Re-run on FBMMAAIO seed (depth 1-2) and compare unmapped counts
3. **Integration**: Full v52 trace and compare field lineage coverage percentages

### Key Programs for Testing

| Program | Issue Demonstrated | Current Coverage |
|---------|--------------------|------------------|
| FBBP497 | SUM aggregate (Issue 1) | 115/116 (99%) |
| FBB4527 | WHERE params in UPDATE (Issue 2) | 2/4 (50%) |
| FBB5191 | Qualified host vars (Issue 3) | 0% (all GFAC group) |
| MXBESPU2 | Multiple issues combined | 0/14 (0%) |
| FBC7303 | Derived + WHERE params | varies |
| FBBRTJH | Missing cursor DECLARE (Issue 4) | 0% |

---

## Schema Changes Required

If Issue 1 is implemented, the `columns[]` entry schema expands:

```json
// Current (unchanged for simple columns):
{"column": "FUND_A", "hostVar": "AA-FUND-A", "table": "T_MMAA_ACC_ANAL"}

// New for derived-with-source:
{"hostVar": "W-TOTAL-SPOKE-XFER", "derived": true,
 "derivedFrom": ["SPOKE_DOL_A"], "expression": "SUM",
 "table": "T_MMJT_JRNL_TXN"}

// New for derived-no-source (literal/COUNT(*)):
{"hostVar": "W-MMTC-EXISTS-SW", "derived": true,
 "derivedFrom": [], "expression": "literal",
 "table": "T_MMTC_TRX_CTL"}
```

The graph loader in mainframe-tracer will be updated to handle `derivedFrom` by creating edges
to the source column(s):

```
(:Column (table: T, column: SPOKE_DOL_A)) -[:DERIVED_INTO (expression: "SUM")]-> (:Field (name: W-TOTAL-SPOKE-XFER))
```

---

## File Reference

All file paths relative to repo root (`brokerage-event-finder/`):

| File | Lines | Role |
|------|-------|------|
| `packages/mainframe-common/cobol_parser/parser.py` | 1877 | Token-level SQL parsing, host var extraction, column correlation |
| `packages/cobol-xstate-json/src/cobol_xstate/interface.py` | 1022 | Event classification, `_sql_host_vars` regex, `_fetch_columns` |
| `packages/cobol-xstate-json/src/cobol_xstate/statechart.py` | 1847 | Build-time correlation passes (`_correlate_fetches`, `_correlate_inserts`, `_conventions_recover`) |
| `packages/cobol-xstate-json/src/cobol_xstate/lineage.py` | 939 | Lineage row emission from interface events |
| `packages/cobol-xstate-json/src/cobol_xstate/conventions.py` | 165 | Mfdep naming convention adapter |
| `packages/mainframe-tracer/src/tracer_agent/graph_loader.py` | ~550 | Consumer: host_var_map building (line 516-523), lineage loading (line 272-329) |

---

## v52 Run Metadata

- **Date**: 2026-08-25 14:48-16:00
- **Seed**: FBMMAAIO
- **Programs processed**: 1,718 OK, 41 failed
- **Programs with field lineage**: 1,667
- **Total field-column links**: 116,636
- **Mapped**: 104,976 (90.0%)
- **Unmapped**: 11,660 (10.0%)
- **Run terminated by**: STOP sentinel (graceful)
- **Output location**: `/data/brokerage-event-finder/packages/mainframe-tracer/output/FBMMAAIO_v52/`
