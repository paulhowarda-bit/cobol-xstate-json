# cobol-xstate-json Issues — From FBMMAAIO_v50 Trace

*(Assembled from photos of the work-machine document, 2026-08-25.)*

## Triage (2026-08-25, against master)

Most of the trace's field-mapping issues were fixed on master **before** v50 ran — the
work machine's pinned build predates the 2026-08-14 SQL column-correlation work.
**Repointing the tracer at current master resolves issues 1, 2b, 2d and 6 outright.**

| # | Issue | Status | What the tracer needs to do |
|---|---|---|---|
| 1 | Cursor SELECT column resolution | **Already fixed on master** (2026-08-14): DECLARE↔FETCH correlated positionally, incl. cursors declared in copybooks (whole-stream scan); a non-correlatable FETCH carries a `columnNote` saying why | Rebuild against current master |
| 2a | Synonym/view tables | **Supported via `--synonym-map`** — catalog knowledge as input, exactly as this doc suggests | Supply the synonym map on every run |
| 2b | INSERT without column list | **Already fixed on master**: VALUES slots zipped against the table's DECLARE TABLE / DCLGEN order (works through `--synonym-map` too) | Rebuild; ensure DCLGENs are retrievable |
| 2c | Dynamic SQL classification | **Fixed now**: `endpointType: "dynamic_sql"` (events `GET/CREATE.DYNAMIC_SQL.<dynamic-sql>`, artifacts kind `db2-dynamic-sql`), following the `db2_proc` precedent | Treat `dynamic_sql` as inherently unmappable, not as failures |
| 2d | Aggregate control variables | **Already fixed on master**: a `COUNT(*)`/expression slot maps as `{"hostVar": ..., "derived": true}` + a note | Filter `derived: true` column entries |
| 3 | Verbs/literals as program endpoints | **Not a cobol-xstate-json problem** (determined 2026-08-25 after this doc was written) — the false names do not originate in this tool's output. Consistent with what the code showed: the suspect `_name_suffix` fallback is unreachable from master's CALL provenance spellings | Fix on the tracer side |
| 4 | Timeouts (600s) | **Open — needs work-machine profiling**: run the biggest offenders (FBVS0141, FBC0555, DCOMS250) with `--timing` and send the per-stage numbers | Interim: scale the timeout with line count |
| 5 | Exit codes / truncated stderr | **Fixed now**: a companion-view crash after the bundle is written is a WARNING, exit stays 0 (the MXBNKSUM false negative); the internal-error line now carries the actual exception type + message | Re-classify: non-zero now really means "no usable output" |
| 6 | Stored proc CALL | **Already fixed on master**: `endpointType: "db2_proc"`, params noted as procedure parameters, never table columns | Rebuild against current master |

Discovered 2026-08-25. Trace stats: 15,998 programs OK, 696 failed, 5.8h runtime.

These issues all originate in `cobol-xstate-json` output and must be fixed there. The
tracer consumes the extractor's output as-is — it does not post-process or compensate
for extraction defects.

---

## 1. Cursor SELECT — No Column Resolution

**Impact:** 59,129 unmapped field warnings (42% of all unmapped fields across 8,226 programs)
**Severity:** High

### Problem

When a program uses a cursor:

```cobol
EXEC SQL DECLARE MMAA_CURSOR CURSOR FOR
    SELECT FUND_A, ACCOUNT_N, BALANCE_A
    FROM T_MMAA_ACC_ANAL
    WHERE ...
END-EXEC.

EXEC SQL FETCH MMAA_CURSOR
    INTO :AA-FUND-A, :AA-ACCOUNT-N, :AA-BALANCE-A
END-EXEC.
```

cobol-xstate emits a lineage event `GET.DB2.<cursor MMAA_CURSOR>` with host variables
(`AA-FUND-A`, `AA-ACCOUNT-N`, `AA-BALANCE-A`) as fields. The `columns[]` array in
`interface.events[]` is **empty** — there is no mapping from host variable to SELECT column.

### Expected Output

Each FETCH INTO host variable should map positionally to the corresponding SELECT column:

```json
{
  "event": "GET.DB2.<cursor MMAA_CURSOR>",
  "endpoint": "<cursor MMAA_CURSOR>",
  "columns": [
    {"table": "T_MMAA_ACC_ANAL", "column": "FUND_A", "hostVar": "AA-FUND-A"},
    {"table": "T_MMAA_ACC_ANAL", "column": "ACCOUNT_N", "hostVar": "AA-ACCOUNT-N"},
    {"table": "T_MMAA_ACC_ANAL", "column": "BALANCE_A", "hostVar": "AA-BALANCE-A"}
  ]
}
```

### What Needs to Change

1. Parse DECLARE CURSOR statements — store the SELECT column list keyed by cursor name
2. On FETCH INTO — look up the cursor's SELECT columns and correlate by position (1st host var = 1st column, etc.)
3. Populate `columns[]` with `{table, column, hostVar}` triples

### Scale

59,129 field instances across thousands of programs. Cursors are the primary data access
pattern in batch COBOL — this single issue accounts for nearly half of all unmapped fields.

---

## 2. Direct Table Events — Missing Column Mappings

**Impact:** ~81,000 unmapped field warnings
**Severity:** Medium

### Problem

Some direct SQL statements (non-cursor SELECT INTO, INSERT, UPDATE) produce events with
the correct endpoint (table name) but `columns[]` is empty or incomplete. The downstream
graph loader sees a field connected to a table but cannot determine which column.

### Sub-problems

#### 2a. Synonym/View Tables Not Resolved

Tables like `RTAC_ACCOUNT` (472 unmapped), `V_SMIX_ACTIVE` (2,383 unmapped),
`CAAM_ACC_MSTR` (760 unmapped) are DB2 synonyms or views. cobol-xstate identifies the
synonym name from the SQL but does not resolve it to the base table DDL for column lookup.

**Expected:** When populating `columns[]`, resolve synonyms to base table names using the
mfdep conventions module (which already provides `synonym_to_real_table()`). Accept a
synonym map as input if needed.

#### 2b. INSERT Without Column List

Pattern: `EXEC SQL INSERT INTO T_MFER_ERROR VALUES (:MFER-ERROR)`. With no explicit
column list in the SQL, cobol-xstate cannot determine which column the host variable
maps to.

**Expected:** When an INSERT has no column list, look up the table's DCLGEN to get the
full column order and map host variables positionally.

#### 2c. Dynamic SQL

522 unmapped fields come from `<dynamic-sql>` endpoints where the SQL text is
constructed at runtime.

**Expected:** Emit `endpointType: "dynamic_sql"` (not `"db2"`) so consumers can
distinguish these from static SQL. Column mapping is inherently impossible here — just
classify correctly.

#### 2d. Control Variables as Fields

Fields like `WS-DUMMY`, `WS-ROW-TEST`, `WS-2000-COUNT-SQL` appear in lineage because
they're used in SQL statements (`SELECT COUNT(*) INTO :WS-2000-COUNT-SQL`). These are
control variables, not semantic data fields.

**Expected:** Either suppress lineage rows for host variables used only with aggregate
functions (COUNT, MAX, MIN, SUM), or mark them with a flag (e.g., `"aggregate": true`)
so consumers can filter them.

---

## 3. COBOL Verbs/Literals Emitted as Program Endpoints

**Impact:** ~87 false program lookups per trace, each wasting fetch + enqueue capacity
**Severity:** Medium

### Problem

The `interface.endpoints[]` array contains entries with `type: "program"` for names that
are clearly not programs:

**COBOL reserved words:** `MOVE`, `CALL`, `PERFORM`, `INITIALIZE`, `SET`, `ADD`,
`DISPLAY`, `EXEC`, `COPY`

**Numeric literals/addresses:** `00082270`, `00016180`, `00032320`, `03020001`, `05`,
`10`, `00004510`, `01060000`, `321500`

**Paragraph labels:** `3100-EXIT`, `9010-EXIT`, `6110-EXIT`, `3810-EXIT`, `SP-03`

**JCL symbolics / placeholders:** `&PROGNAME`, `<PROGRAM>`

### Root Cause

In `interface.py`, the `_call_endpoint()` function has a fallback path: when none of the
three regex patterns (`_CALL_DYNAMIC`, `_CALL_RESOLVED`, `_CALL_LITERAL`) match the
provenance text, it falls back to `_name_suffix(name)` which splits the action name and
returns the suffix. If the statechart parser mis-identifies a MOVE statement, a
paragraph reference, or a numeric literal as a CALL action, the verb/paragraph/number
becomes a program endpoint.

### Expected Fix

1. Validate that emitted program endpoint names are syntactically valid COBOL program
   names before adding to `interface.endpoints[]`:
   - Must start with a letter (A-Z) or national char ($, #, @)
   - Must not be purely numeric
   - Must not contain hyphens (that's paragraph names)
   - Must not be a COBOL reserved word
   - Must not contain `<`, `>`, or `&`
2. When the fallback path produces an invalid name, either:
   - Drop the endpoint entirely, or
   - Mark it as `"internal": true` so consumers skip it

### Programs Where This Occurs (sample from v50)

These false endpoints were discovered from various programs traced through table
dependency paths. The tracer then tried to fetch MOVE, CALL, 00082270, etc. as programs
and failed.

---

## 4. cobol-xstate Timeouts (600s)

**Impact:** 83 programs excluded from graph
**Severity:** Medium

### Problem

These programs exceed the 600-second timeout. They are legitimate COBOL programs that
fetch successfully but cobol-xstate cannot complete extraction within 10 minutes.

### Affected Programs (full list)

**FBVS\* (12):** FBVS0140, FBVS0141, FBVS0143, FBVS1005, FBVS1006, FBVS141L, FBVS0638,
FBVS1340, FBVS1343, FBVS1414, FBVS1463, FBVS3315

**DCOMS\* (6):** DCOMS150, DCOMS220, DCOMS230, DCOMS250, DCOMS260, DCOMS500, DCOMS670

**FBC\* (12):** FBC0117, FBC0314, FBC0404A, FBC0414, FBC0510, FBC0512, FBC0555, FBC1363,
FBC2610, FBC3203, FBC3404, FBC4093, FBC4703

**MXC\* (3):** MXCBMSX, MXCBTXX, MXCBWSX

**FBB\* (10):** FBB0112, FBB1153, FBB230D, FBB580D, FBB7001A, FBB7004A, FBB7114N,
FBB7440, FBB8769

**DCLP\* (2):** DCLP0304, DCLP0312

**Misc:** B3CB301, ACTB100, ALSC045, BTA118C, BM160D, BM230D, BM500D, COMCALCB,
COMCALCO, DCAMC160, DCAMC170, DCDSNAMD, DCDSNAD0, DCFCB020, DCSPMAMB, FBAI2000,
FBAI3000, FBAI3001, FBB230D, FBB580D, FBB5113, FBB5182, FBB5650, FBBU120, FBBU121,
FBC0174D, FBCALC10, FBCALC40, FBCM315, FBCP011, FBCP131, FBCP160, FBCPP0R, FBCTGRC,
FBSB447, FBSB471, FBSP363, FBVS3315, MXCBTXX

### Suggested Investigation

Profile cobol-xstate on the largest timeouts (`FBVS0141` — VisionStation, many cursors;
`FBC0555` — mutual fund; `DCOMS250` — order management). Likely hot paths:
- Cursor/SQL resolution with many DECLARE CURSOR + FETCH combinations
- Deep PERFORM graph traversal
- Large copybook expansion chains

### Possible Approaches

1. **Performance optimization** of the hot paths
2. **Partial output** — emit whatever has been extracted when timeout occurs (parse +
   partial machine + partial interface), so the program isn't completely lost
3. **Incremental timeout** — allow more time for larger programs (e.g., scale timeout
   with line count)

---

## 5. cobol-xstate Parse Errors (Non-Zero Exit)

**Impact:** 60 programs excluded from graph
**Severity:** Medium

### Problem

cobol-xstate returns a non-zero exit code despite detecting source format and (in some
cases) producing valid output files. The error reported in stderr is truncated and
unhelpful — it only shows `detected source format = fixed (97%: column 7 is a valid
indicator on all N lines...)` without the actual failure reason.

### Known False Negative

**MXBNKSUM** (736 lines) — produced valid `MXBNKSUM.json` (bundle) and
`MXBNKSUM.lineage.json` (6 entries), yet exited non-zero. The tracer classifies this as
`parse_failed` and discards the output. This is a valid extraction that should be
reported as success.

### Affected Programs (full list, 60 total)

ALSC070, B30B010, BTA176C, BTI176C, DCCOB792, DCDR040, DCROB000 (2985 lines), DCROB0002
(2137 lines), DCROB0082 (2525 lines), DCROB0085 (1975 lines), FBAC040, FBB0250,
FBB0250X, FBB0303, FBB0660, FBB1071, FBB1487, FBB1765M, FBB2457, FBB2470B, FBB2470X,
FBB2657, FBB2701, FBB4096, FBB4255, FBB4255C, FBB4920, FBB5316, FBB5316D, FBB5372,
FBB5376, FBB5380, FBB5382, FBB6104, FBB6506, FBB6603, FBB7000, FBB7140, FBB7438,
FBB7464, FBB8632, FBBA707, FBBC039, FBBC174, FBBW435, FBBW439, FBBP725, FBDRV20,
FBBUSEC1, FBC3520 (4426 lines), FBF15780 (980 lines), FBPDREM1, FBSB900, FBSPY007
(2800 lines), FBVS5LSW, MXBNKSUM, MXBDFAC, MXC942, MXC2942, XTU942, ZTU942

### Expected Fix

1. **Exit code integrity:** If valid bundle.json is produced, exit 0 (or a distinct
   "warnings" exit code like 2). Non-zero should mean "no usable output."
2. **Full error reporting:** When exiting non-zero, stderr should contain the actual
   parser error (not just the format detection preamble). The current truncation at the
   format-detection line hides what actually failed.
3. **Investigate the FBB\* cluster** (~40 programs): Likely a common COBOL structural
   pattern (nested COPY REPLACING, unusual PROCEDURE DIVISION layout, or EXEC CICS
   inline) that trips the parser.
4. **Investigate the "942" cluster** (ZTU942, XTU942, MXC2942, MXC942): These are all
   manual SI (standing instruction) processing programs — likely share a structural
   pattern.

---

## 6. Stored Procedure CALL Parameters Misclassified

**Impact:** ~30 fields across 3 endpoints
**Severity:** Low

### Problem

When a program calls a DB2 stored procedure via
`EXEC SQL CALL PCBEN171(:IN-RECORD, :IN-MESSAGE, :OUT-CODE)`, cobol-xstate emits:

```json
{
  "event": "CREATE.DB2.PCBEN171",
  "endpoint": "PCBEN171",
  "endpointType": "db2",
  "columns": []
}
```

The call parameters (`IN-RECORD`, `IN-MESSAGE`, `OUT-CODE`) appear as lineage fields
mapped to a "table" called `PCBEN171`. But these are procedure parameters, not table
columns.

### Expected Fix

Classify `EXEC SQL CALL` differently:
- Set `endpointType: "db2_proc"` (not `"db2"`)
- Map fields to procedure parameters, not table columns
- This lets consumers create appropriate graph relationships (CALLS_PROC with parameter
  edges, not READS_COLUMN/WRITES_COLUMN)

**Affected endpoints:** PCBEN171, DCOBS110, DCOMS230

---

## Priority Order

| # | Issue | Impact | Effort Estimate |
|---|-------|--------|-----------------|
| 1 | Cursor SELECT column resolution | 59,129 fields | Medium-High (DECLARE+FETCH correlation) |
| 2 | False program names in endpoints | 87 lookups + cascade | Low (validation in `_call_endpoint` fallback) |
| 3 | Parse errors / exit code integrity | 60 programs lost | Low-Medium (fix exit logic + investigate FBB* pattern) |
| 4 | Timeouts on large programs | 83 programs lost | High (performance profiling) |
| 5 | Synonym/view column resolution | ~4,000 fields | Medium (integrate synonym map) |
| 6 | INSERT without column list | ~46 fields | Low (DCLGEN positional mapping) |
| 7 | Stored proc classification | 30 fields | Low (endpointType change) |
