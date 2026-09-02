# Integrating mfdep Conventions into cobol-xstate-json

This document describes the `mfdep` naming conventions system and how cobol-xstate-json should use it to resolve unmapped fields in lineage output.

## The Problem

The graph loader maps lineage fields to DB2 columns using the `columns[]` array on interface events. When `columns[]` is empty (cursor DECLARE not visible, count mismatch, dynamic SQL, etc.), fields are logged as "unmapped".

Many of these fields CAN be resolved using the naming conventions already indexed in `mfdep.db`. For example, `NP-ID-POSN-A` trivially resolves to column `ID_POSN_A` on table `T_DMNP_NCMM_POSN` via the DCLGEN prefix system.

## How Mainframe COBOL Naming Works

Every DB2 table has a **DCLGEN copybook** that declares host variables with a consistent prefix:

```
Table:    T_DMAA_ACC_ANAL
DCLGEN:   DMAA (copybook member name)
Prefix:   AA
Variable: AA-FUND-A    -> column FUND_A
Variable: AA-ACCT-NBR  -> column ACCT_NBR
```

The prefix is declared in the DCLGEN via `NAMES(AA)`. The entity code is the first 4 chars after `T_` (e.g., `DMAA`). The family is the first 2 chars of the entity (e.g., `MM`).

COPY REPLACING creates additional prefixes:
```cobol
COPY FB01MMAA REPLACING ==:AA:== BY ==AAR==.
```

This creates variables like `AAR-FUND-A` that map to the same columns as `AA-FUND-A`.

## The mfdep Conventions API

All functions are importable from `mfdep.conventions` and accept an optional `db_path` keyword (defaults to the project's `mfdep.db`):

```python
from mfdep.conventions import (
    resolve_field_variants,
    cobol_to_db2_column,
    infer_table_from_prefix,
    strip_prefix,
    lookup_prefix,
    lookup_dclgen,
    lookup_entity,
    all_prefixes_for_entity,
    replacing_variants,
    resolve_table_synonym,
)
```

### Key Functions for Field Resolution

#### `resolve_field_variants(field, table="") -> dict`

The primary resolution function. Given a COBOL host variable name and optional table context:

```python
>>> resolve_field_variants("NP-ID-POSN-A")
{
    "original": "NP-ID-POSN-A",
    "core": "ID-POSN-A",          # prefix stripped
    "db2_column": "ID_POSN_A",    # hyphens -> underscores
    "table": "T_DMNP_NCMM_POSN",  # inferred from prefix
    "prefix": "NP",
    "all_prefixed": ["NP-ID-POSN-A"],
    "search_terms": ["ID-POSN-A", "ID_POSN_A", "NP-ID-POSN-A"]
}
```

When a table is provided, disambiguation improves:
```python
>>> resolve_field_variants("NP-ID-POSN-A", "T_DMNP_NCMM_POSN")
# Same result but higher confidence — prefix validated against table's DCLGEN
```

#### `cobol_to_db2_column(field, table="") -> str`

Quick conversion when you just need the column name:
```python
>>> cobol_to_db2_column("AA-FUND-A")
"FUND_A"
>>> cobol_to_db2_column("AA-FUND-A", "T_DMAA_ACC_ANAL")
"FUND_A"  # uses table context for correct prefix stripping
```

#### `infer_table_from_prefix(prefix) -> list[str]`

Given a field prefix, returns candidate table names:
```python
>>> infer_table_from_prefix("AA")
["T_DMAA_ACC_ANAL"]
>>> infer_table_from_prefix("NP")
["T_DMNP_NCMM_POSN", "T_EXNP_NWK_PART"]  # ambiguous — 2 entities share prefix
```

#### `strip_prefix(field) -> str`

Strips the COBOL prefix using the DCLGEN prefix table:
```python
>>> strip_prefix("AA-FUND-A")
"FUND-A"
>>> strip_prefix("NP-ID-POSN-A")
"ID-POSN-A"
```

### Table and Entity Lookups

#### `lookup_dclgen(member) -> dict | None`

Look up a DCLGEN copybook by member name:
```python
>>> lookup_dclgen("DMAA")
{"member": "DMAA", "schema": "DMD1DBO", "table_name": "T_DMAA_ACC_ANAL",
 "names_prefix": "AA", "structure": "DMAA", "entity": "DMAA"}
```

#### `lookup_prefix(prefix) -> list[dict]`

Find all DCLGENs using a prefix:
```python
>>> lookup_prefix("AA")
[{"member": "DMAA", "schema": "DMD1DBO", "table_name": "T_DMAA_ACC_ANAL", ...}]
```

#### `lookup_entity(entity) -> dict | None`

Look up by 4-char entity code:
```python
>>> lookup_entity("DMAA")
{"member": "DMAA", "schema": "DMD1DBO", "table_name": "T_DMAA_ACC_ANAL", ...}
```

#### `all_prefixes_for_entity(entity) -> list[str]`

All known prefixes including REPLACING variants:
```python
>>> all_prefixes_for_entity("DMAA")
["AA", "AAR", "MAAR", ...]
```

#### `replacing_variants(copybook) -> dict`

COPY REPLACING data for a copybook:
```python
>>> replacing_variants("FB01MMAA")
{"copybook": "FB01MMAA", "token": "AA", "values": ["AAR", "MAAR", ...], "programs": 47}
```

### Table Synonym Resolution

#### `resolve_table_synonym(name) -> dict | None`

Resolves table synonyms, views, and work tables to real tables:
```python
>>> resolve_table_synonym("W_DMAA_ACC_ANAL")
{"name": "W_DMAA_ACC_ANAL", "real_table": "T_DMAA_ACC_ANAL",
 "schema": "DMD1DBO", "source": "convention_W_strip", "confidence": "high"}
```

### Prefix Collision Awareness

Some prefixes map to multiple entities (ambiguous). Use `infer_table_from_prefix` and check if it returns multiple candidates. When ambiguous, use additional context (which tables the program references) to disambiguate.

```python
>>> from mfdep.conventions import collision_candidates
>>> collision_candidates("NP")
[{"entity": "DMNP", "prefix": "NP", "table_name": "T_DMNP_NCMM_POSN", "family": "MM"},
 {"entity": "EXNP", "prefix": "NP", "table_name": "T_EXNP_NWK_PART", "family": "SM"}]
```

## Where to Integrate

### 1. Fallback in `_correlate_fetches` (statechart.py)

When `_fetch_columns()` returns an empty correlation (DECLARE not visible or count mismatch), add a conventions-based fallback:

```python
# After _fetch_columns fails:
if not columns and into_fields:
    columns = _resolve_via_conventions(into_fields, cursor_endpoint)
```

The fallback logic:
1. Take the host variable names from the FETCH INTO list
2. For each variable, call `resolve_field_variants(var_name, table)` where `table` is the endpoint table (if known from the cursor)
3. If a table isn't known from the cursor, use `infer_table_from_prefix` on the first variable's prefix
4. Build `columns[]` entries: `{"column": result["db2_column"], "hostVar": var_name, "table": result["table"]}`

### 2. Fallback in `interface.py: _classify_exec`

Same pattern — when the interface builder encounters a FETCH/SELECT with empty columns, attempt conventions-based resolution before emitting the event without column mappings.

### 3. Graph loader last-resort (mainframe-tracer)

The graph loader already has the field name and endpoint at the point it logs "unmapped". A final fallback here catches anything the parser missed:

```python
mapping = host_var_map.get(field_name)
if not mapping:
    # Conventions fallback
    resolved = resolve_field_variants(field_name, endpoint_table)
    if resolved["table"] and resolved["db2_column"]:
        mapping = (resolved["table"], resolved["db2_column"])
```

This is a safety net — ideally resolution happens in cobol-xstate so the interface output is complete.

### 4. Disambiguating collisions

When `infer_table_from_prefix` returns multiple candidates, use the program's known table references (from `table_refs` in the lineage or from the interface events) to pick the correct one:

```python
candidates = infer_table_from_prefix(prefix)
if len(candidates) > 1:
    # Intersect with tables this program actually references
    program_tables = {ev["endpoint"] for ev in interface_events if ev.get("endpointType") == "db2"}
    matches = [t for t in candidates if t in program_tables]
    if len(matches) == 1:
        table = matches[0]
```

### 5. Synonyms for a column-list-less INSERT — NOT wired to mfdep by default

`resolve_table_synonym` (above) is **not** consulted automatically. A synonym's base
table is *catalog* knowledge, and mfdep's answer is convention-derived
(`"source": "convention_W_strip"`) — a heuristic, however confident — so wiring it into
the input seam would let a guess arrive through the door reserved for facts. The seam is
`mainframe_artifacts.synonyms.SynonymLookup`, fed by `--synonym-map FILE` (the shape
`mfdep catalog export-synonym-map` emits; the operator's explicit answer, wins when both
are given) and `--synonym-resolver MODULE:FUNC` (a `(name) -> base | None` callable
asked at the point of need: only a column-list-less INSERT whose table has no visible
DECLARE, and only for a name the map does not hold). A host that wants mfdep's answer
passes it explicitly, owning the choice:

```python
from mfdep.conventions import resolve_table_synonym

def resolve(name):
    hit = resolve_table_synonym(name)
    return hit["real_table"] if hit else None      # None = not a synonym; raise = failed
```

`cobol-xstate prog.cbl --synonym-resolver mymodule:resolve`, or in Python
`analyze(src, synonym_resolver=resolve)`. The resolver is asked BEFORE the conventions
fallback (§1–§4): a synonym it resolves is correlated from the base table's DECLARE and
never reaches `resolve_field_variants`; a name it declines goes on to the conventions
exactly as before. A resolver that raises disables itself for the run and adds one
`synonym resolver failed mid-run` flag — every synonym it did not reach stays an
unresolved INSERT, never "not a synonym".

## Discovery and Export (CLI)

The conventions system also supports discovery of patterns not yet codified:

```bash
# Show convention statistics
mfdep conventions summary

# Look up a specific prefix/entity/field
mfdep conventions lookup-prefix AA
mfdep conventions lookup-entity DMAA
mfdep conventions resolve NP-ID-POSN-A

# Run full discovery analysis
mfdep conventions discover

# Export all convention data to JSON
mfdep conventions export -o ./conventions-data/
```

The export produces:
- `dclgen_lookup.json` — full member-to-table mapping
- `replacing_lookup.json` — all COPY REPLACING tokens and their values
- `prefix_families.json` — entity groupings by family
- `linkage_patterns.json` — IO module calling patterns
- `dataset_patterns.json` — dataset HLQ groupings
- `copybook_cohorts.json` — copybooks always included together

These JSON files can be bundled with cobol-xstate-json as static lookup tables if a runtime dependency on mfdep is undesirable.

## Dependency Options

**Option A: Runtime import** (preferred for accuracy)
```python
from mfdep.conventions import resolve_field_variants, cobol_to_db2_column
```

Both packages are installed in the same environment on C2C.

**Option B: Static JSON bundle** (for standalone operation)
Export conventions data via `mfdep conventions export`, ship the JSON files with cobol-xstate-json, and load them at startup. This avoids the mfdep dependency but requires periodic re-export when new DCLGENs are indexed.

**Option C: Post-processing hook** (graph loader fallback)
Leave cobol-xstate-json unchanged; add the conventions fallback in the graph loader only. This is the simplest change but means the interface JSON output remains incomplete — downstream consumers other than the graph loader still see unmapped fields.

## Expected Impact

Based on the v50 trace (140,605 unmapped fields across 8,226 programs):
- ~42% are cursor-based FETCH (DECLARE not visible) — conventions resolve almost all of these
- ~30% are COPY REPLACING variants — `all_prefixes_for_entity` handles these
- ~15% are direct SELECT with column/variable count mismatch — partial resolution possible
- ~13% are dynamic SQL, stored procs, or truly unresolvable — no conventions fix

Conservative estimate: conventions-based fallback should resolve **60-70% of currently unmapped fields**.
