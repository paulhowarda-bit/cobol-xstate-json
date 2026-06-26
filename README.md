# cobol-xstate

Parse IBM Enterprise COBOL and recover its behavior as an **XState v5 JSON Harel
statechart** — a *rewrite contract* for mainframe modernization.

Because a Harel/STATEMATE statechart holds more than control flow — typed data items,
actions as assignments, conditions as expressions — the recovery captures **all** the
program logic: the paragraph control flow *and* the data layer (`PIC`/`USAGE`/sign
types, `MOVE`/`COMPUTE` as `target := expr`, conditions as Boolean trees). The guiding
rule is **no invented logic**: every state, guard, action, and expression is a faithful
translation of source that traces back via a provenance table, and what genuinely rides
on runtime data is *flagged*, never smoothed over.

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
cobol-xstate prog.cbl -I copybooks -I shared/cpy   # copybook search paths for COPY
```

### Output

The default output is a JSON **bundle**:

```jsonc
{
  "format": "xstate-v5-config",
  "metadata": { "program": "...", "disclaimer": "..." },
  "machine":  { "id": "...", "initial": "...", "context": { /* typed initial values */ }, "states": { ... } },
  "data":     { "WS-TOTAL": { "type": { "category": "numeric", "usage": "DISPLAY", "digits": 13, "scale": 2, "signed": false }, ... } },
  "semantics": {
    "actions": { "ADD_CUST-AMT_TO_WS-TOTAL": { "kind": "arith", "assignments": [ { "target": "WS-TOTAL", "expr": "WS-TOTAL + CUST-AMT" } ] } },
    "guards":  { "UNTIL_WS-EOF_eq_Y": { "op": "rel", "left": "WS-EOF", "rel": "=", "right": "'Y'" } }
  },
  "provenance": { "<name>": { "kind": "state|guard|action", "cobol": "...", "line": N } },
  "flags":    [ { "paragraph": "...", "line": N, "message": "..." } ],
  "notes":    [ "..." ]
}
```

`machine` is a bare XState v5 `createMachine` **config**. The logic that a Harel/
STATEMATE statechart holds beyond control flow travels alongside it:

- **`data`** — the typed data dictionary recovered from the DATA DIVISION: every item's
  `PIC`, `USAGE` (DISPLAY / COMP / COMP-3 packed / …), digit count, decimal `scale`, and
  `signed` flag, plus 88-level condition-names resolved to `(parent == values)`. This is
  the type information that governs COBOL's fixed-point decimal arithmetic.
- **`semantics.actions`** — each action's actual operation: `MOVE`/`ADD`/`COMPUTE`/… as
  `target := expression` assignments (with `rounded`/`onSizeError` annotations), not just
  a name. `semantics.guards` — each guard's Boolean expression tree (relational / class /
  sign / 88-level / AND-OR-NOT).
- **`machine.context`** — seeded with each elementary item's start-of-run value (its
  `VALUE` clause, else the typed default).

Nothing is invented — every action/guard expression is a faithful translation of the
COBOL the `provenance` entry points to. The one thing the bare config can't embed is the
*decimal evaluator*: feed the machine to XState with a `setup({ guards, actions })` block
that implements these expressions over a **decimal** type (COMP-3/zoned/binary per `data`),
not binary float. `--machine-only` emits just the config.

## How it works (the pipeline)

```
raw source
  → normalizer  fixed/free format, column-7 comment/continuation/debug, *> comments,
                continuation-literal stitching, Area-A detection            (normalizer.py)
  → preprocess  COPY / REPLACING / EXEC SQL INCLUDE expansion via a configurable
                copybook resolver (search paths, exts, missing policy)   (preprocessor.py)
  → lexer       words / numbers / string literals / period / operators,
                each carrying its source line                              (lexer.py)
  → parser      DATA DIVISION → typed data dictionary (PIC/USAGE/sign, 88-levels);
                EXEC SQL/CICS/DLI extracted (host vars, LINK/XCTL/RETURN/HANDLE);
                PROCEDURE DIVISION → sections/paragraphs (Area-A headers) +
                a control-flow statement AST (IF / EVALUATE / PERFORM / GO TO /
                I-O handlers / CALL / ALTER / terminators)
                                  (parser.py, model.py, data_division.py)
  → statechart  recursively compile each paragraph's full statement tree to faithful
                guarded states/loops/handlers; translate MOVE/COMPUTE/… to
                target := expr and conditions to Boolean trees; type the context;
                constant-propagate dynamic CALL; provenance + flags
                          (statechart.py, semantics.py, analysis.py, naming.py)
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

- **Copybooks must be resolvable.** `COPY`/`REPLACING`/`EXEC SQL INCLUDE` are expanded
  via the resolver (`-I DIR`, `--copybook-ext`); a member that can't be found is listed
  in `notes` as **missing** (its data/logic isn't in the model) rather than silently
  dropped. Embedded `EXEC SQL/CICS/DLI` is extracted opaquely — host vars preserved,
  `LINK`/`XCTL`/`RETURN`/`HANDLE` mapped to call/transfer/terminate/flag — but the SQL/
  CICS sub-language itself isn't interpreted.
- **PERFORM call-return: a no-op marker in the JSON contract, a real `invoke` in the
  runnable JS.** In the `--target json` bundle a `PERFORM p` is the flat marker
  `perform_p` (the review contract; the literal jump-and-return pair isn't drawn there).
  The `--target js` module *does* synthesize call-return: each performed paragraph becomes
  an XState actor, the PERFORM site `invoke`s it with the context as input and assigns the
  output back on `onDone`, so WORKING-STORAGE threads through nested calls and the machine
  runs end-to-end under stock `createActor`. `GO TO` into another paragraph can't be told
  apart from fall-through once provenance is stripped, so inside an actor it is modeled as
  a return (flagged in the JSON `flags`).
- **Data semantics are captured but not *evaluated*.** `data` carries the types and
  `semantics` carries the `target := expr` / Boolean-tree logic, but the bare config
  can't embed the decimal evaluator — the `setup({ guards, actions })` stubs must
  implement these over a decimal type (COMP-3/zoned/binary per `data`), not float.
  `OCCURS`/`REDEFINES` are recorded but subscript/alias addressing isn't resolved, and
  conditions beyond relational/class/sign/88/AND-OR-NOT fall back to `{op:'raw'}`
  (nothing dropped — flagged shape).
- **Step semantics:** one record cycle = one macrostep, STATEMATE next-step sensing
  (a flag set this cycle is sensed next cycle). Same-cycle cross-region dependencies
  should be reviewed.

When in doubt, the tool flags rather than guesses. Treat every `flags` entry as a spot
that needs a human against the original source.

## Development

```bash
PYTHONPATH=src python -m pytest -q     # 79 tests: normalizer, lexer, parser, preprocessor, data, semantics, analysis, statechart, emitter, golden-master
```

The emitter (`--target js`) and golden-master tests need Node + a local `xstate`
install (`npm install`); they skip cleanly when those are absent.

Layout:

```
src/cobol_xstate/   normalizer · lexer · model · parser · preprocessor · data_division · semantics · analysis · naming · statechart · emitter · cli
runtime/            cobolRuntime.mjs (fixed-point decimal ops + field-aware store)
                    cobolDriver.mjs  (reference driver: invoke interpreter + file I/O for golden-master)
examples/           custrpt.cbl  (canonical batch loop)
                    banktran.cbl (EVALUATE dispatch + dynamic CALL resolved by constant propagation)
                    altswitch.cbl (ALTER first-time-switch idiom + an unresolvable dynamic CALL)
                    accum.cbl / nestperf.cbl (PERFORM-UNTIL & nested PERFORM call-return)
tests/              one module per pipeline stage (79 tests)
```

## License

MIT.
