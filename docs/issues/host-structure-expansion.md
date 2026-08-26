# Host Structure Variables Not Expanded in SQL INTO/VALUES

*Reported 2026-08-26 by the tracer team, from the v55 run (FBMMAAIO seed, 983 programs
with field lineage, 73,489 / 98,685 mapped = 74.5%). Filed against `cobol_parser` as
`issue-host-structure-expansion.md`.*

## Summary

When a COBOL program uses a **group-level variable** (01 or 05 level with subordinates)
as a host variable in embedded SQL, the parser captured the group name verbatim instead
of expanding it to its elementary constituents. Db2's precompiler performs that expansion
at precompile time — the parser must do the same to produce correct column↔field
correlation.

The report calls this the single largest source of unmapped fields in production tracing:
**~4,500+ of 24,055 unmapped field warnings** directly, by making the column count
disagree with the host-variable count, which refuses the correlation *and* suppresses
convention recovery on events that would otherwise resolve.

```cobol
EXEC SQL FETCH POSN-UPDT-CURSOR
    INTO :BSTI-TRNF-INIT
END-EXEC
```

Db2 rewrites `:BSTI-TRNF-INIT` into every elementary item of the group, in declaration
order. Against a cursor declaring 23 columns, the unexpanded form produced
`cursor POSN-UPDT-CURSOR selects 23 column(s) but this FETCH has 1 host variable(s)` and
mapped nothing.

### Why these programs are written that way

The report traces it to a DBA choice: **~36.7% (5,685 of 15,482) of the indexed DCLGEN
copybooks were generated without the `NAMES(prefix-)` option**, so their fields are
direct hyphen-for-underscore translations of the column names with no prefix
(`TRNF_NBR` → `TRNF-NBR`). Programs including those DCLGENs reference the group in SQL
rather than listing fifty unprefixed fields — standard COBOL/Db2 practice.

### Top affected structures (from the tracer log)

| Structure | Unmapped | Table | DCLGEN |
|---|---|---|---|
| `BSTI-TRNF-INIT` | 377 | `T_BSTI_TRNF_INIT` | BSTI |
| `DVDE-DEPT-THR` | 102 | `W_DVDE_DEPT_THR` | DVDE |
| `BSPD-POSN-DLVR` | 95 | `T_BSPD_POSN_DLVR` | BSPD |
| `BSTH-TRNF-HIST` | 92 | `T_BSTH_TRNF_HIST` | BSTH |
| `BSID-ID-LOG` | 75 | `T_BSID_ID_LOG` | BSID |
| `MFOE-ORD-ENTRY` | 67 | `T_MFOE_ORD_ENTRY` | MFOE |
| `BSFE-TRNF-FEE` | 59 | `T_BSFE_TRNF_FEE` | BSFE |
| `ACCL-CHG-LOG` | 38 | `T_ACCL_CHG_LOG` | ACCL |
| `BSMO-MEMO` | 35 | `T_BSMO_MEMO` | BSMO |

---

## Status (2026-08-26) — shipped, and one thing the report did not ask for

Verified against master before changing anything: `_exec_into_vars` returned
`['BSTI-TRNF-INIT']` for a group INTO, `values_list` returned the group name for a
`VALUES` slot, and the count-mismatch note fired exactly as described. Nothing here was
a stale-build artefact.

| What shipped | Where |
|---|---|
| `elementary_subordinates(items, name)` — Db2's expansion rules over the flat data-division item list | `cobol-parser/src/cobol_parser/data_division.py` |
| The expansion applied at **all four** colon-collection points of `parse_exec`, not the two the report named | `cobol-parser/src/cobol_parser/parser.py` |
| A null indicator (`:D:I` and `:D INDICATOR :I`) is one slot with its variable, in `INTO` clauses and in `VALUES`/`SET` slots | `parser._indicator_at`, `_scan_host_vars`, `_host_var_slot` |
| `expanded_structures` / `indicator_vars` on `ExecStmt`, surfaced as `expandedStructures` / `indicatorVars` on the event | `model.py`, `statechart._with_columns`, `interface._classify` |
| `parse_bundle.VERSION` 3 → 4 | `cobol-parser/src/cobol_parser/parse_bundle.py` |

### The report named two edit sites; there are four

`_exec_into_vars` and the INSERT `VALUES` path are not the only places the parser
collects host variables. The **statement-wide colon scan** (`parse_exec`, the list that
becomes `ExecStmt.host_vars`) is what supplies `fields` on INSERT/UPDATE events and
`params` on GET events. Expanding only the two the report named does not under-deliver —
it actively regresses:

- `FETCH ... INTO :GROUP` — `into_fields` becomes the elementary items while `host_vars`
  still says `GROUP`, and `params = [h for h in host_vars if h not in into_fields]` emits
  the group name as a phantom **parameter** of the very event whose fields it just became.
- `INSERT ... VALUES (:GROUP, …)` — `columns[]` correctly names the elementary items,
  but `fields` (built from `host_vars` through `_dml_split`) still says `GROUP`. A column
  mapping whose host variable appears in no field list is *exactly* an unmapped field
  downstream — the same warning, arrived at from the other end.

One more site outside the parser needed the same treatment: `statechart` did not put
`hostVars` on a SELECT/FETCH action spec, so `interface` fell back to its regex over the
raw text — which still reads the source spelling. The parser's list is now carried there
as it already was for the DML verbs.

### Not in the report's fix list, but in its own evidence: null indicators

The report's ACTB008 example — *"cursor ACTA-VOLUNTARY-CURSOR selects 17 column(s) but
this FETCH has 18 host variable(s)"*, where the 18th is `WS-NULL-IND-01` — is **not** a
host-structure problem. `:DATA:IND` is one Db2 slot: host variable `DATA` with null
indicator `IND`. The parser counted two, so a single nullable column was enough to refuse
a correlation the statement proves. Expansion alone would not have fixed that family.

Both are now handled, in one rule (`_scan_host_vars`), so the statement-wide scan, the
`INTO` scan and the slot rule cannot come to disagree about what a host variable is.

An indicator is reported apart (`indicatorVars`), never among `fields`, `params` or
`columns` — and **still assigned by the statement**. Db2 writes the indicator; it is how
the program learns the column was NULL, and programs branch on exactly that. Dropping
the assignment along with the field entry would have left a variable the emitted machine
never writes and the lineage shows no origin for: a lost write, which is a worse answer
than the refusal this change replaced. It carries `<external: SQL null indicator>` to say
what it receives is null status, not a column's value.

### What the refusal still is, and must stay

A group whose data-division entry never arrived (its copybook was not fetched) **cannot**
be expanded, and inventing an expansion would be a guess. Such a name is kept exactly as
the source spells it, the counts still disagree, and the event still carries
`columnsUnresolved: "count-mismatch"` with a flag. `examples/sqlhost.cbl` paragraph
`5000-ABSENT` pins that. The count-mismatch notes were reworded to say so — the old
wording blamed indicator variables and host structures, which are no longer the cause.

Three tests inverted deliberately, from "refuses" to "maps, and never by convention":
`test_indicator_variable_refuses_to_correlate`,
`test_a_qualified_value_with_an_indicator_is_still_refused`, and
`test_bug_doc_reproduction_indicator_refusal_stands`. The last one is from
`docs/issues/conventions-indicator-variable-bug.md`, whose actual hazard — the
conventions overruling the parser and resolving `WS-NAME`/`WS-BAL` to the unrelated
`T_APWS_WKFL_STEP` — is still asserted, now alongside the correct source-proven mapping.
Two conventions tests that used an indicator as their count-mismatch *vehicle* were given
a genuine one (one column, two host variables) so they still guard what they claim to.

### Already fixed before the report arrived

The report's FBVS0126 row — *"group name repeated in fields list"* for `GFAC` / `WIAA` —
is the **qualified** reference `:GFAC . AC-ACC-N`, which commit `80060ce` resolved to the
elementary field name (`docs/issues/unmapped-fields-v52.md`, issue 3); `interface._dedup`
also collapses a name repeated within one statement. That is a different construct from
the unqualified group this issue is about, and it should be re-checked against v55 rather
than counted here.

### One question back to the reporting team

v55 reports **74.5% mapped** against v52's **90.0%**. The five v52 fixes all shipped on
2026-08-26 and should have moved that the other way. The runs differ in size (983 vs
1,667 programs with field lineage) and the `params` split removed whole rows from the
denominator, so the two percentages are probably not comparable — worth confirming
before reading it as a regression.

---

## Expansion rules implemented

Db2's, not ours:

1. **Only elementary items.** A nested group is recursed into and never named itself.
2. **Declaration order**, depth-first.
3. **`FILLER` skipped** — not a nameable host variable.
4. **`REDEFINES` skipped, with its subordinates** — it occupies its original's storage,
   and Db2 expands the original only.
5. **Unlimited depth.**
6. **`OCCURS` expands once**, not N times (Db2 takes the first element).

Two rules the report did not list, both found while verifying:

7. **Level 88 is stepped over, not stopped at.** A condition name is not storage; an
   elementary item declared after one still belongs to the group.
8. **Levels 66 and 77 END the group's run.** Both are always top-level, but `77 > 5`, so
   a bare `level > group.level` walk reads a standalone `77` following a group as one of
   its members and hands the statement a host variable the source never put there.

The walk is positional over the flat item list — not `data_by_name` (first-wins on
duplicate names: a DCLGEN copied twice would expand to the wrong copy) and not the
`parent` chain, which carries a **pre-existing bug** found while verifying: `parse_data_
division`'s level stack pops on `level >=`, so a level-77 item following a group is
assigned that group as its `parent`. It does not affect this change, but it does pollute
`interface._DataView.leaves()` for COMMAREA/FD record field lists. Separate fix.

---

## Out of scope, stated rather than silently skipped

- **`interface._sql_host_vars`**, the regex over raw statement text, still reads the
  source spelling. It fires only when an action has **no spec** — the reactive target's
  rewritten-config overlay — so the tracer's path is fully covered; bringing the overlay
  into line (reusing `_DataView.leaves()` with FILLER/REDEFINES filtering) is a follow-on.
- **Re-running the trace** to measure the actual drop in the 24,055 count needs the
  estate and stays with the reporting team.
- **Supplying the missing copybooks** so a group can be expanded at all is the fetch
  pipeline's problem (issue 4 of the v52 report), unchanged here.

---

## Verification

- **821 tests**: 715 in this repo (1 skipped — mfdep absent), 106 in mainframe-common
  (3 skipped). The Node-backed emitter/reactive/golden-master tests **ran** under real
  XState — only the mfdep skip remains.
- New `tests/test_host_structures.py` in `cobol-parser` (12 tests): the expansion rules
  one by one, and the statement parser end to end.
- New fixture `examples/sqlhost.cbl` + 8 tests in `tests/test_sql_fixtures.py`.
- `python tools/gate.py` — **6/6 byte-stable** after a reviewed re-record, including the
  parse-bundle round trip, which is what proves VERSION 4 rehydrates byte-identically.
- `python ../mainframe-common/tools/byteproof.py --check goldens/parse.sha256` — green
  after re-record (3 digests; the two SQL-free examples moved on the version bump alone).
- Every re-recorded golden was diffed against a HEAD worktree before recording. Eight
  examples changed by gaining `hostVars` on their input specs and nothing else;
  `sqlcols` and `sqlqual` changed exactly where their indicator paragraphs are.
