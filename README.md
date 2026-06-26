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
  → statechart  recursively compile each paragraph's full statement tree to faithful
                guarded states/loops/handlers; constant-propagate dynamic CALL;
                provenance for every name; flag runtime-data constructs
                                            (statechart.py, analysis.py, naming.py)
```

### What maps to what

The goal is to capture **all** the program logic — which is why the target is a Harel
statechart (XState), not a UML-subset flattening. Each paragraph's *entire* statement
tree is compiled recursively; the only thing collapsed is a run of genuinely
straight-line statements (the reduction principle). Conditional and order-bearing
constructs become real structure, so nothing is folded away.

| COBOL | XState v5 |
|---|---|
| Paragraph / section | an entry state; its body compiles to sub-states |
| Straight-line run of `MOVE`/`ADD`/`OPEN`/… | one state's `entry` action-name list |
| `IF … ELSE … END-IF` (incl. nested) | guarded `always` split to Then/Else sub-states converging on the continuation |
| `EVALUATE … WHEN … WHEN OTHER` | guarded `always` per WHEN; each branch returns to the continuation |
| `READ … AT END` / `INVALID KEY` | a guarded handler branch — the conditional flag-set is **conditional**, not folded |
| `PERFORM p UNTIL/VARYING/TIMES`, inline `PERFORM` | a **loop** state (exit guard + body that loops back); `TEST AFTER` ⇒ do-while |
| `PERFORM p` (simple) | call-return `entry` action `perform_p`; `p` is compiled as its own region |
| `GO TO p` | exit `always` edge to `p` (no return); suppresses fall-through |
| Fall-through / end of paragraph | eventless `always` edge to the next paragraph (or the shared `final`) |
| `STOP RUN` / `GOBACK` / `EXIT PROGRAM` | `type: 'final'` |
| `GO TO … DEPENDING ON` | guarded fan-out (`depending_eq_1…n`) + out-of-range edge + flag |
| dynamic `CALL ident` | resolved to a literal where constant-provable, else flagged |
| `ALTER … TO PROCEED TO` | context-driven guard switch on the altered exit + flag |

### Resolving the "un-mappable" — drawn but flagged

Most constructs that a naive pass would drop are actually *mappable*; the real
question is whether a **static** parse can pin the behavior. This tool draws the shape
and flags what rides on runtime data, rather than skipping it:

- **Dynamic `CALL ident`** — [analysis.py](src/cobol_xstate/analysis.py) runs
  constant propagation: a `VALUE 'POSTLOG'` clause or `MOVE 'POSTLOG' TO ident` with no
  conflicting assignment resolves the target (`call_POSTLOG`, no flag). If a non-literal
  assignment can also reach the call, it stays flagged — genuinely runtime.
- **`ALTER … TO PROCEED TO`** — the altered one-line `GO TO` becomes a guard set over
  its candidate targets, the initial target is seeded into `context`, and the `ALTER`
  statement becomes the `set_alt_…` action that flips it. Drawn faithfully, then flagged
  as runtime-switched (verify the active target).
- **`GO TO`** — an unconditional exit transition (no return); it suppresses the
  fall-through edge, since it *is* how the paragraph exits.

A flag now means "the shape is drawn, but its behavior depends on runtime data — verify
against the source," not "skipped."

## Honest limitations

This is a **heuristic control-flow recovery**, not a conformant COBOL parser. It is
deliberately explicit about the gap (the skill's core principle — don't pretend):

- **No copybook preprocessor** (`COPY`/`REPLACE`), **no embedded-language extraction**
  (`EXEC SQL`/`CICS`/`DLI`). A production parser needs both (see `parsing-cobol.md`);
  add them before trusting this on real source with copybooks.
- **PERFORM is a call-return action, not a synthesized return edge.** `perform_p` runs
  the separately-compiled paragraph `p` and continues; the literal jump-and-return pair
  isn't drawn (it needs a call stack XState doesn't have). `p`'s full logic is still
  captured as its own region. `GO TO` (no return) *is* drawn as a transition.
- **No data-division semantics yet** — `context` holds only recovered control flags
  (e.g. ALTER switches); USAGE/PICTURE/sign are not modeled. Guard/action *meaning*
  must be filled in against the COBOL (the `setup()` stubs).
- **Step semantics:** one record cycle = one macrostep, STATEMATE next-step sensing
  (a flag set this cycle is sensed next cycle). Same-cycle cross-region dependencies
  should be reviewed.

When in doubt, the tool flags rather than guesses. Treat every `flags` entry as a spot
that needs a human against the original source.

## Development

```bash
PYTHONPATH=src python -m pytest -q     # 31 tests: normalizer, lexer, parser, analysis, statechart
```

Layout:

```
src/cobol_xstate/   normalizer · lexer · model · parser · analysis · naming · statechart · cli
examples/           custrpt.cbl  (canonical batch loop)
                    banktran.cbl (EVALUATE dispatch + dynamic CALL resolved by constant propagation)
                    altswitch.cbl (ALTER first-time-switch idiom + an unresolvable dynamic CALL)
tests/              one module per pipeline stage (31 tests)
```

## License

MIT.
