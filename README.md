# cobol-xstate

Parse IBM Enterprise COBOL and emit its control flow as an **XState v5 JSON Harel
statechart** — a *rewrite contract* for mainframe modernization.

The recovered statechart asserts what the legacy program provably does, against which
a rewrite can be validated (golden-master / equivalence testing). The guiding rule is
**no invented logic**: states, guards, and actions are names that trace back to the
COBOL source via a provenance table, and constructs a static pass cannot resolve are
*flagged*, never smoothed over.

> Built with the `ibm-cobol` skill. The mapping follows its
> `references/cobol-to-statecharts.md` (COBOL → XState v5) and
> `references/harel-statecharts.md` (statechart semantics); the parser follows
> `references/parsing-cobol.md` (the normalize → preprocess → parse pipeline).

## Install

```bash
python -m pip install -e .
# or run without installing:
PYTHONPATH=src python -m cobol_xstate.cli <file.cbl>
```

Pure standard library — no runtime dependencies. Python ≥ 3.9. `pytest` for tests.

## Usage

```bash
cobol-xstate examples/custrpt.cbl                 # full bundle to stdout
cobol-xstate examples/banktran.cbl --summary      # + human summary & flags on stderr
cobol-xstate prog.cbl -o prog.machine.json        # write to a file
cobol-xstate prog.cbl --machine-only              # bare XState config only
cobol-xstate - < prog.cbl                          # read from stdin
cobol-xstate prog.cbl --format free                # force free-format source
```

### Output

The default output is a JSON **bundle**:

```jsonc
{
  "format": "xstate-v5-config",
  "metadata": { "program": "...", "disclaimer": "..." },
  "machine":  { "id": "...", "initial": "...", "context": {}, "states": { ... } },
  "provenance": { "<name>": { "kind": "state|guard|action", "cobol": "...", "line": N } },
  "flags":    [ { "paragraph": "...", "line": N, "message": "..." } ],
  "notes":    [ "..." ]
}
```

`machine` is a bare XState v5 `createMachine` **config** (serializable data — no
function bodies). Feed it to XState with a `setup({ guards, actions })` block whose
stubs you implement *deliberately against the COBOL* named in `provenance` — never
from a generated guess. `--machine-only` emits just that config.

## How it works (the pipeline)

```
raw source
  → normalizer  fixed/free format, column-7 comment/continuation/debug, *> comments,
                continuation-literal stitching, Area-A detection            (normalizer.py)
  → lexer       words / numbers / string literals / period / operators,
                each carrying its source line                              (lexer.py)
  → parser      PROCEDURE DIVISION → sections/paragraphs (Area-A headers) +
                a control-flow statement AST (IF / EVALUATE / PERFORM / GO TO /
                I-O handlers / CALL / ALTER / terminators)            (parser.py, model.py)
  → statechart  one state per paragraph; transfers → `always` transitions;
                guards from conditions; flags for the un-modelable;
                a provenance table for every name              (statechart.py, naming.py)
```

### What maps to what

| COBOL | XState v5 |
|---|---|
| Paragraph / section | a state (OR-state sibling) |
| Straight-line `MOVE`/`ADD`/`OPEN`/… | folded into the state's `entry` action-name list |
| `IF` / `EVALUATE` branch that transfers | guarded `always` edge (`guard` from the condition) |
| `PERFORM p` (all forms) | `always` edge to `p`, tagged call-return |
| `GO TO p` | `always` edge to `p` (no return) |
| Fall-through to next paragraph | eventless `always` edge |
| `READ … AT END` / `INVALID KEY` | guarded `always` edge on the I/O condition |
| `STOP RUN` / `GOBACK` / `EXIT PROGRAM` | `type: 'final'` |
| `ALTER`, `GO TO … DEPENDING ON`, dynamic `CALL`, `NEXT SENTENCE`, `DECLARATIVES` | **flagged**, not modeled |

## Honest limitations

This is a **heuristic control-flow recovery**, not a conformant COBOL parser. It is
deliberately explicit about the gap (the skill's core principle — don't pretend):

- **No copybook preprocessor** (`COPY`/`REPLACE`), **no embedded-language extraction**
  (`EXEC SQL`/`CICS`/`DLI`). A production parser needs both (see `parsing-cobol.md`);
  add them before trusting this on real source with copybooks.
- **PERFORM return edges are not inferred.** A `PERFORM` becomes a forward edge tagged
  "add explicit return edge"; the chart is a **review skeleton**, and `always` edges
  are document-ordered. Review ordering and returns before treating it as executable.
- **No data-division semantics** — `context` is emitted empty; USAGE/PICTURE/sign are
  not modeled. Guard/action *meaning* must be filled in against the COBOL.
- **Step semantics:** one record cycle = one macrostep, STATEMATE next-step sensing
  (a flag set this cycle is sensed next cycle). Same-cycle cross-region dependencies
  should be reviewed.

When in doubt, the tool flags rather than guesses. Treat every `flags` entry as a spot
that needs a human against the original source.

## Development

```bash
PYTHONPATH=src python -m pytest -q     # 26 tests: normalizer, lexer, parser, statechart
```

Layout:

```
src/cobol_xstate/   normalizer · lexer · model · parser · naming · statechart · cli
examples/           custrpt.cbl (canonical batch loop) · banktran.cbl (dispatch + flags)
tests/              one module per pipeline stage
```

## License

MIT.
