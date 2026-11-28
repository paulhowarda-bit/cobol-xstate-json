# Items 173-176: performance

All four items are done, and no output changed: every view and both retrieval reports are byte-identical to before, so you need no golden re-record on your side. Items 173 and 176 are as specified. Item 174 differs in mechanism and item 175 in approach; both are explained below.

| repo | commit | what |
|---|---|---|
| mainframe-common | `dc69c7d` | 173 `--indent none`; 174 the alias map read once per process; 175 the DECLARE scan skips lines with no `EXEC` |
| cobol-xstate-json | `4d8835e` | 175 one children index per machine; 176 the alias union in `lineage._put`; fixture `examples/redefloop.cbl`; MANUAL |

Take the two repos together. cobol-xstate-json `4d8835e` runs on the previous mainframe-common, but items 173 and 174 live only in `dc69c7d`.

**Verification.**
- **mainframe-common:** 633 passed, 3 skipped. The parse-bundle ratchet is unchanged.
- **cobol-xstate-json:** 1,176 passed, 1 skipped. The gate passes all six checks: both seeds, the parse-bundle round trip, and the reports at `--jobs 1` and `--jobs 8`.
  - The only golden change is 11 added digests for the new fixture. No existing digest moved.
- **Other repos against `dc69c7d`:** jcl-dependencies 400 passed, eztrieve-dependencies 181, asm-dependencies 250, cics-dependencies 303.
- **The new tests bite.** The `--indent none` and map-cache tests fail on the previous code. For the scan, two deliberate breaks of the filter each fail its equivalence test.

---

## 173. `--indent none`

As specified. `--indent none` (in any case) writes compact JSON; the default and every number mean what they did. It applies to every front-end that uses the shared output arguments: cobol-xstate, jcl-dependencies, eztrieve-dependencies, asm-dependencies and cics-dependencies. `cobol-parser` keeps its own numeric `--indent`, because it writes one parse bundle per run and is not in your profile. On the API, `write_views(..., indent=None)` does the same.

But see "The bigger saving" at the end: for the tracer, the files do not need to be written at all.

## 174. The alias map, read once per process

`read_synonym_map` now keeps the normalised (upper-cased) mapping for one file per process. Its key is the resolved path, size and `st_mtime_ns`, so a rewritten file is read again. A refused file is never kept. The function now returns the mapping already upper-cased; before, it returned the file's spelling.

**Difference from the ledger: no keyword on `SynonymLookup.__init__`.** Every front-end builds the lookup twice per run, not once:

1. `cliargs.synonym_lookup` builds one.
2. The CLI passes `lookup.mapping` to `analyze(synonyms=...)`, and `build_machine` builds a second `SynonymLookup` over it.

jcl, eztrieve and asm do the same through their own `api`. A keyword passed by `synonym_lookup` would have removed only the first copy. Instead, `SynonymLookup` recognises the mapping `read_synonym_map` handed out and uses it as it is, so neither build copies it. A mapping from anywhere else is still copied and upper-cased as before.

Measured on a generated 546 KB map of 10,500 entries:
- **Before:** reading it took 4.6 ms and each of the two copies 2.3 ms, about 9 ms per member.
- **After:** one `stat` per member.

The shared mapping is never mutated. Your three tests are in `test_synonyms.py`, with a fourth showing that a mapping not read from a file is still copied.

## 175. Where the time goes

**Point 1, `storage.children_index`.** It was built seven times per run: once for each of the six `_DataView`s (bundle interface, business, lineage, reactive split, reactive interface, reactive rewriter), and once more for lineage layouts. Each build was over the same unchanged dictionary. `Machine.children()` now builds it once, and every one of those callers uses it. On our 11,000-line benchmark that is 94 ms down to 13 ms.

One rebuild remains. The dynamic-calls view lays out each record its file-field sources read, and each layout still builds its own index. It is not in your profile; tell us if it shows up.

**Point 2, `parser._scan_sql_declarations`.** It did not tokenise text the lexer had already tokenised, so reusing tokens was not the fix. It tokenised the whole program, every division, to find a handful of `EXEC SQL` blocks: 22-26% of the parse on our benchmarks, even with no SQL in them.

The lexer works line by line, carrying nothing from one line to the next. A line whose text holds no `EXEC` can therefore hold neither the word that opens a block nor the one that closes it. The scan now does not tokenise such a line when it starts outside a block. It still reads every line inside a block, and a line that starts just after an `EXEC` waiting for its `SQL`.

The scan's share of the parse is now about 0. The cursors and tables it finds are the same, which is tested against a scan of every token on eight edge shapes and on the examples:
- `EXEC` and `SQL` on different lines;
- `EXEC EXEC SQL`;
- two blocks on one line;
- `EXEC CICS` before `EXEC SQL`;
- `EXEC SQL` inside a literal;
- a missing `END-EXEC`;
- lower case;
- `EXEC` as the last word.

**Point 3** is item 176.

## 176. The REDEFINES alias union

Your diff is applied as written, and the fixture you asked for is in the gate. In `examples/redefloop.cbl`, one alternative holds a REDEFINES of its own, so the aliases are all-to-all. The group MOVE is placed field by field from three different origins, inside a `PERFORM VARYING`. On it, 24 of 36 alias writes take the new "already holds every origin" path.

We could not reproduce your members, so we measured on generated programs, with every output file identical before and after:
- **Wide origin sets** (one record filled from 40 files, a 60-field all-to-all REDEFINES): lineage is about 1.5x faster, matching your 1.5-1.6x.
- **Origin sets of one or two:** no measurable change, as expected, since the saving grows with origin-set size times alias pairs.

---

## The bigger saving: do not write the views at all

Item 173 makes the text cheaper. The tracer can skip the text altogether. It embeds this package and runs `cobol_xstate.cli.run`, which writes every view as a file; it then reads the bundle, lineage, artifacts, dynamic-calls and dependents straight back with `orjson`. Every view is already a Python object on the `Analysis` that `api.analyze` returns. MANUAL section 4, "Reading the views in-process", now documents this as a contract:

| view | in-process |
|---|---|
| bundle | `analysis.machine.bundle()` |
| business, lineage, reactive, artifacts, dynamic-calls, dependents | `analysis.business()`, `.lineage()`, `.reactive()`, `.artifacts()`, `.dynamic_calls()`, `.dependents()` |
| retrieval reports | `analysis.prefetch.report()`, `analysis.fetch` |

- **Same document.** Each object is plain JSON data, and `json.loads` of its file equals it. We checked all 660 views and reports of every example.
- **Read-only.** The views are memoized and shared with `write_views`, so copy one before changing it.
- **Two refusals to handle.** `business()` can raise `ViewBudgetExceeded`, and `reactive()` can raise `ReactiveLoweringError`. `write_views` isolates both, so a direct caller must handle them itself.
- **Files only for what you keep.** `write_views(analysis, dest, targets=(...), indent=None)` computes and writes only the views named.

This removes both halves of item 173's cost: our encoding and your decoding. Your backlog row `XSTATE-VIEWS-JSON-ROUNDTRIP` is where this lands. The change is on your side, in `program_processor._run_cobol_xstate_sync`: call `api.analyze` and take the objects, rather than `cli.run` and reading the files back.

**One thing `cli.run` does that you would take over:** turning its flags into `analyze` arguments. That means `synonym_lookup(args)`, then `synonyms=lookup.mapping` and `synonym_resolver=lookup.resolver`, plus the same for dependents. If you call `analyze` directly, call `read_synonym_map` yourself, and the item 174 cache still applies.

## After merging

Re-profile a pilot's workers as you did for item 175. You should see:
- `cliargs.synonym_lookup` and `SynonymLookup.__init__` gone from the profile;
- `storage.children_index` at about a seventh of its share;
- `_scan_sql_declarations` near zero;
- `lineage._put` lower on REDEFINES-heavy members;
- if you take the in-process route, `api.write_views` gone from it too.
