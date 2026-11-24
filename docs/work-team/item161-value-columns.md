# Item 161: a column written from a clock register or a literal

Item 161 is done as specified. An INSERT, UPDATE or MERGE that writes a column from a Db2 clock register, a literal or an expression over columns now says so. A new `valueColumns` list sits beside `columns` on the statement. Before this change, such a column was published as nothing: it looked the same as a column the program never writes.

| repo | commit | what |
|---|---|---|
| mainframe-common | `abf1090` | `ExecStmt.value_columns` from INSERT VALUES, UPDATE SET and both MERGE arms, plus `valueColumns` on a `FINAL TABLE` data change; parse bundle VERSION 17 |
| cobol-xstate-json | `a8d2476` | `statechart._with_columns` copies it onto the action spec as `valueColumns`; new fixture `examples/sqlvalcol.cbl`; MANUAL documents the key |

Take the two repos together. A parse bundle written by `abf1090` is VERSION 17, and an older `cobol-parser` refuses it rather than reading part of it. cobol-xstate-json `a8d2476` reads `value_columns`, which an older parser does not have.

**Verification.** 7 new parser tests in mainframe-common, all failing on the previous code; suite 606 passed, 3 skipped. 2 new tests in cobol-xstate-json; suite 1,152 passed, 1 skipped. Both byte ratchets pass after re-recording (what moved is listed below).

---

## What you get

Your minimum shape gives exactly what you asked for. Here is the INSERT, with the fixture's neutral names (see difference 4):

```json
"columns":      [{"column": "ZZ_ID", "hostVar": "WS-ID"}],
"valueColumns": [{"column": "ZZ_STAT", "expression": "literal", "derivedFrom": []},
                 {"column": "ZZ_UPD_TS", "expression": "register",
                  "register": "CURRENT TIMESTAMP", "derivedFrom": []}]
```

The UPDATE gives the same two entries, and its `columns` is unchanged (empty). Your other test cases:

- `SET C = CURRENT DATE` gives the register `CURRENT DATE`.
- `SET C = C + 1` gives `{"expression": "expression", "derivedFrom": ["C"]}`.
- `SET C = UPPER(:H)` keeps its `derived` entry in `columns` and gets no `valueColumns` row.
- A statement whose columns are all written from host variables has no `valueColumns` key at all.

`columns` keeps its meaning, and `columnNote` keeps its prose. On the INSERT the note still names the same columns; `valueColumns` is the machine-readable form of that note.

---

## Where we differ from the ledger

1. **All three mainframe-common parse digests moved, not only those with such a slot.** Every bundle carries the version number, and every `ExecStmt` now has `value_columns`, empty where nothing applies. The VERSION 16 bump moved all three digests in the same way. We compared the three bundles with the previous code's: they are identical apart from that field.
2. **A column-list-less INSERT is not covered.** Its VALUES slots are matched to the table's `DECLARE TABLE` at build time (`statechart._correlate_inserts`), not in the parser. A literal slot there still shows up only in the build-time note. Covering it would need a per-slot derivation on the statement, which is another parse-bundle field. We left it out because the ledger did not ask for it; say if you want it.
3. **MERGE details your diff did not spell out.** A `USING (VALUES ...)` slot that holds a literal counts as that literal wherever an arm writes it (`SET C = S.X`). A column that the MATCHED UPDATE and the NOT MATCHED INSERT both write from the same value is listed once.
4. **The fixture does not use your minimum shape's table and column names.** They appear in your census of the real estate, and cobol-xstate-json is public. `examples/sqlvalcol.cbl` and the tests use `ZZTAB`, `ZZ_ID`, `ZZ_STAT`, `ZZ_UPD_TS` and similar. The shapes are the same.

---

## What moved in cobol-xstate-json

Three existing bundle digests moved, each only because `valueColumns` was added. We checked each against the previous output with the key removed, and they are identical:

| example | new entries |
|---|---|
| `sqlgaps` | `STAMP` from `CURRENT TIMESTAMP` |
| `sqlmerge` | `AMT` from `S.AMT`, a column of the MERGE's `USING (SELECT ...)` query: `expression`, `derivedFrom: ["AMT"]` |
| `sqlwrite` | `SEQ_N` from `SEQ_N + 1` (an expression); `ORD_AMT` from the literal `0` |

The `sqlmerge` entry shows one case worth knowing. A column copied from another table's column is an `expression` whose `derivedFrom` names that column, without the correlation qualifier. It is not a constant, and nothing labels it as one. Eleven digests were added for the new fixture: 9 views and 2 retrieval reports.

---

## What the tracer has to change

- **Accept parse bundle VERSION 17.** Update `cobol-parser` to `abf1090` or later before reading bundles written by it.
- **Read `valueColumns` from the statement spec,** and from `dataChange` for a `SELECT ... FROM FINAL TABLE (...)`, as your "After merging" section plans. Each entry is `{column, expression, register?, derivedFrom}`. `expression` is `register`, `literal`, a function name such as `UPPER`, `CASE`, or `expression`. Only a `register` entry has the `register` key.
- **Do not count an `expression` entry with a non-empty `derivedFrom` as `system-clock` or `constant`.** Such an entry reads other columns, like `SEQ_N + 1` or the `sqlmerge` copy above.
