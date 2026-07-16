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

> **[MANUAL.md](MANUAL.md) is the complete reference** — every flag, every output field,
> every COBOL construct understood, flag triage, and troubleshooting. This README is the
> overview.

> Built with the `ibm-cobol` skill. The mapping follows its
> `references/cobol-to-statecharts.md` (COBOL → XState v5) and
> `references/harel-statecharts.md` (statechart semantics); the parser follows
> `references/parsing-cobol.md` (the normalize → preprocess → parse pipeline).

## Install

A normal Python package. Install it, then run it:

```bash
python -m pip install -e .        # editable (development)
python -m pip install .           # regular

cobol-xstate <file.cbl>           # the console script
python -m cobol_xstate <file.cbl> # equivalent, interpreter-explicit
```

Pure standard library — **no runtime dependencies**, no build step. Python ≥ 3.9.
`pytest` for the tests.

## Usage

```bash
cobol-xstate prog.cbl                              # -> 4 JSON views (see below)
cobol-xstate prog.cbl --outdir build/charts        # -> build/charts/... (dir created)
cobol-xstate examples/banktran.cbl --summary       # + human summary & flags on stderr
cobol-xstate prog.cbl -o out/custom.json           # exact path (overrides --outdir/name)
cobol-xstate prog.cbl -o -                          # write the bundle to stdout instead
cobol-xstate prog.cbl --no-business --no-lineage --no-reactive   # bundle only
cobol-xstate prog.cbl --machine-only               # bare XState config only
cobol-xstate - < prog.cbl                          # read from stdin (-> <PROGRAM-ID>.json)
cobol-xstate prog.cbl --format free                # force free-format source
cobol-xstate prog.cbl --target js                  # -> ./prog.mjs (+ cobolRuntime.mjs)
cobol-xstate prog.cbl --target lineage             # the lineage table on its own
cobol-xstate prog.cbl --target business            # -> ./prog.business.json (+ lineage)
cobol-xstate prog.cbl -I copybooks -I shared/cpy   # copybook search paths for COPY
```

A default run writes **four JSON files** — four views of the same program, each answering
a different question:

| File | Answers |
|---|---|
| `prog.json` | **What does it do?** The faithful machine: config + data dictionary + semantics + interface + provenance. Complete, verbose. |
| `prog.business.json` | **Which steps matter?** The business distillation: scaffolding collapsed, only boundary/decision/calculation states. |
| `prog.lineage.json` | **Where did each value come from, and under what condition?** One row per (external event, field): the event each value originates from, plus the guards that govern the write. |
| `prog.reactive.json` | **What replaces it?** The event-driven machine: its `on` waits and `publish_*` effects are the new system's message contract. |

All four are things you **read or draw** (all are renderable `xstate-v5-config`, bar the
lineage table). The **runnable** modules stay behind their own flag: `--target js` for the
decimal-exact reference, `--target reactive` for the deployable module.

Use `--outdir` to choose the directory (created if absent), `-o PATH` for an exact path
(companions follow it), or `-o -` to stream the bundle to stdout. Opt out with
`--no-business` / `--no-lineage` / `--no-reactive`; `--machine-only` emits the bare config
alone. A program the reactive lowering refuses (CICS handler regions, recursive PERFORM)
simply gets no `.reactive.json` and a note — the other three still land.

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
  "interface": {
    "endpoints": [ { "endpoint": "CUST", "type": "db2", "directions": ["get"] }, { "endpoint": "POSTLOG", "type": "program", "directions": ["create"] } ],
    "events":    [ { "event": "GET.DB2.CUST", "direction": "get", "endpointType": "db2", "endpoint": "CUST", "fields": ["CUST-NAME","CUST-BALANCE"], "state": "1000-LOOKUP", "region": "PROGRAM", "line": 42 } ],
    "perimeterStates": { "1000-LOOKUP": { "region": "PROGRAM", "gets": ["GET.DB2.CUST"], "creates": [] } }
  },
  "provenance": { "<name>": { "kind": "state|guard|action", "cobol": "...", "line": N, "member": "COPYBOOK?" } },
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
- **`interface`** — the program's **external perimeter**: an overlay classifying which
  states cross the program boundary and in which direction. `perimeterStates` maps a
  state (and its region / "state machine") to the external events it **gets** (file
  `READ`, SQL `SELECT`/`FETCH`, `ACCEPT`, CICS `RECEIVE`, an error/exception condition it
  `HANDLE`s) and **creates** (file `WRITE`/`REWRITE`, SQL `INSERT`/`UPDATE`/`DELETE`,
  `DISPLAY`, CICS `SEND`, `CALL` / CICS `LINK`/`XCTL`, CICS `RETURN`). `endpoints` lists
  the external actors (Db2 table, file, program, console, terminal, caller) and `events`
  is the per-crossing detail (direction, endpoint, fields, state, source line).
  `parameters` captures the program's **own** entry interface — `PROCEDURE DIVISION USING`
  / `RETURNING`, the `LINKAGE SECTION` records, and whether a CICS `DFHCOMMAREA` is present
  — i.e. the input/output parameters the caller passes across the boundary (surfaced as
  `get`/`create` against the caller, since `USING` is by reference). `CALL … USING`
  arguments appear as the `fields` of the outbound program event. `MOVE`s to/from a
  `LINKAGE` item are recognized as the request/response boundary (receive-request `get` /
  send-response `create`), branches on an external return item (`SQLCODE`, `EIBRESP`) are
  surfaced as a `get` **response** event from that subsystem, and each perimeter state is
  labelled `input` / `output` / `input-output`. The boundary is also tagged **on the
  machine itself** — every perimeter state carries `meta.perimeter` with its gets/creates,
  so a consumer reading the bare `machine` (or the `harel-statechart-render` skill) sees it
  without cross-referencing. This is a pure classification of the emitted machine. See it at
  a glance with `--summary`.

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
| `EVALUATE … WHEN … WHEN OTHER` | guarded `always` per WHEN; each branch returns to the continuation. `EVALUATE a ALSO b … WHEN x ALSO y` → `a = x AND b = y`; `THRU` ranges, abbreviated relations (`WHEN > 5`), and `ANY` handled |
| `SEARCH` / `SEARCH ALL … WHEN … AT END` | each `WHEN` is a guarded branch to its body and `AT END` a guarded branch; the serial index iteration is an opaque effect (flagged) |
| `READ … AT END` / `INVALID KEY` | a guarded handler branch — the conditional flag-set is **conditional**, not folded |
| `PERFORM p UNTIL/VARYING/TIMES`, inline `PERFORM` | a **loop** state (exit guard + body that loops back); `TEST AFTER` ⇒ do-while. `VARYING` initializes (`var := from`) and steps (`var := var + by`) the control variable; nested `AFTER` indices are flagged |
| `PERFORM p` (simple) | call-return `entry` action `perform_p`; `p` is compiled as its own region |
| `PERFORM p THRU q` | call-return into a range actor spanning paragraphs `p..q` (source order), returning after `q` |
| `SORT/MERGE … INPUT/OUTPUT PROCEDURE` | `perform_input` → `sort_file` effect → `perform_output` (the procedures are call-returns, `THRU` ranges included); `USING`/`GIVING` & key order flagged |
| `GO TO p` | exit `always` edge to `p` (no return); suppresses fall-through |
| Fall-through / end of paragraph | eventless `always` edge to the next paragraph (or the shared `final`) |
| `STOP RUN` / `GOBACK` / `EXIT PROGRAM` | `type: 'final'` |
| `GO TO … DEPENDING ON` | guarded fan-out (`depending_eq_1…n`) + out-of-range edge + flag |
| dynamic `CALL ident` | resolved to a literal where constant-provable, else flagged |
| `ALTER … TO PROCEED TO` | context-driven guard switch on the altered exit + flag |
| `DECLARATIVES` USE / CICS `HANDLE CONDITION` | top-level `type:'parallel'`: a `PROGRAM` region + an orthogonal `HANDLERS` region that watches a trigger event (`IO.ERROR.file` / `CICS.cond`) and performs the handler |

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
  dropped. An expanded member's `origin` is threaded through to its tokens, so a
  copybook-defined data item carries a `member` in `data`, and a copybook-defined
  paragraph carries `member` in `provenance` — the diagram traces back to the right file.
  (Statement-level action/guard `member` is not yet threaded.) Embedded `EXEC SQL/CICS/DLI`
  is extracted opaquely — host vars preserved, `LINK`/`XCTL`/`RETURN`/`HANDLE` mapped to
  call/transfer/terminate/flag, and `SELECT`/`FETCH … INTO :host-vars` modeled as real
  (external-sourced) input assignments to those host variables — but the rest of the
  SQL/CICS sub-language is not interpreted.
- **Robust at scale.** A paragraph whose body fails to parse does not abort the program
  (or a batch of thousands): it is recovered as one opaque action and **flagged** (`body
  did not parse …`), so a corpus of millions of lines converts without a hard stop and
  every unrecovered spot is visible in `flags`.
- **DECLARATIVES & CICS HANDLE are an orthogonal handler region, not main-flow code.** A
  `USE AFTER ERROR` procedure or a `HANDLE CONDITION` registration becomes a `type:'parallel'`
  machine: the `PROGRAM` region is the normal flow, and a `HANDLERS` region watches the
  trigger event and performs the handler (threading the shared context). The triggering
  errors are *runtime* events, so those edges are reactive — the autonomous run / golden
  master exercises the `PROGRAM` region, and the handler fires only when its event is sent
  (flagged). A multi-paragraph USE section performs its first body paragraph.
- **PERFORM call-return: a no-op marker in the JSON contract, a real `invoke` in the
  runnable JS.** In the `--target json` bundle a `PERFORM p` is the flat marker
  `perform_p` (the review contract; the literal jump-and-return pair isn't drawn there).
  The `--target js` module *does* synthesize call-return: each performed paragraph becomes
  an XState actor, the PERFORM site `invoke`s it with the context as input and assigns the
  output back on `onDone`, so WORKING-STORAGE threads through nested calls and the machine
  runs end-to-end under stock `createActor`. `PERFORM section-name` owns the section's
  whole extent (header + member paragraphs), `PERFORM p THRU q` owns the source-order
  span (a THRU tail that is a section extends through its members), and `PERFORM n
  TIMES` steps a synthetic typed counter with a real exit guard. `GO TO` into another
  paragraph can't be told apart from fall-through once provenance is stripped, so inside
  an actor it is modeled as a return (flagged in the JSON `flags`).
- **ON-condition handlers are guarded branches, never hoisted.** `CALL ... ON
  EXCEPTION/OVERFLOW`, arithmetic `[NOT] ON SIZE ERROR`, `ACCEPT/DISPLAY ON EXCEPTION`,
  `READ ... [NOT] AT END`, `[NOT] INVALID KEY` and `WRITE ... AT END-OF-PAGE` handler
  imperatives compile to guarded edges keyed to a flagged external guard (the trigger is
  a runtime condition). The NOT-form guards are the *negation* of their positive
  condition, so `NOT AT END` is the per-record path both under stock XState and in the
  reference driver. `EXIT PARAGRAPH`/`EXIT SECTION` jump to the paragraph's/section's
  continuation; `EXIT PERFORM [CYCLE]` breaks/continues the enclosing inline loop.
  ALTER switches and `GO TO ... DEPENDING ON` compile to *real evaluable guards* over a
  synthetic switch field / the index variable (still flagged for review). Stacked
  `WHEN`s share the following body. STRING/UNSTRING/INSPECT remain opaque effects —
  their receiver/tally data changes are NOT modeled, and always flagged.
- **The `interface` overlay is field-level.** Every boundary event carries the `fields`
  crossing in its direction (READ INTO target or the FD record's elementary layout —
  the FD ↔ record association is recovered, so a WRITE of a record resolves to its
  physical file; ACCEPT/DISPLAY operands; SQL host variables, with WHERE inputs under
  `params`; COMMAREA; CALL arguments) plus FILE-CONTROL bindings on file endpoints
  (ASSIGN/ORGANIZATION/RECORD KEY/FILE STATUS). CICS RETURN COMMAREA/TRANSID, XCTL,
  ABEND, READQ/WRITEQ TS/TD, STARTBR/READNEXT and EIB-field reads are all visible;
  branching on SQLCODE *or a FILE STATUS field* emits a response event; any assignment
  crossing LINKAGE (not just MOVE), guards reading linkage fields, and `RETURN-CODE`
  writes classify as caller traffic; `parameters.fields` expands each parameter record
  to its elementary fields.
- **Data semantics are captured but not *evaluated*.** `data` carries the types and
  `semantics` carries the `target := expr` / Boolean-tree logic, but the bare config
  can't embed the decimal evaluator — the `setup({ guards, actions })` stubs must
  implement these over a decimal type (COMP-3/zoned/binary per `data`), not float.
  Single-dimension elementary `OCCURS` is resolved: a table is an array, `TBL(I)` reads
  (`elem`) and writes (`setElem`), 1-based. Subscripts may be a literal, a variable, or an
  **arithmetic expression** (`TBL(WS-I - 1)`, evaluated with the decimal runtime).
  Multi-dimension subscripts (`TBL(I, J)`) and reference modification (`X(1:3)`) are kept
  whole in the JSON contract (a faithful operand) but route to an external guard /
  `notModeled` in the runnable JS rather than emitting a wrong reference. Group `OCCURS`
  and nested subscripts are flagged / fall back, not guessed. `REDEFINES` is recorded and
  **classified**: a same-category/size redefinition is reported as a safe value *alias*,
  while a different-PICTURE/USAGE one is flagged as genuine byte reinterpretation (not
  modeled — never silently reinterpreted; full byte-aliasing needs a byte buffer).
  Conditions cover relational/class/sign/88/AND-OR-NOT, **decimal literals** (`> 500.00`),
  **arithmetic-expression operands** (`WS-A + WS-B > WS-LIMIT`), parenthesized
  sub-conditions, COBOL *abbreviated* combined relations (`IF A = 1 OR 2` → `A = 1 OR
  A = 2`, with the subject and operator — NOT included — implied from the prior relation),
  and 88-level `VALUE lo THRU hi` ranges (emitted as `lo <= x <= hi`). Forms still beyond
  this fall back to `{op:'raw'}` — and a raw fallback **always emits a flag** (nothing is
  left only in `semantics` where a flag-only triage would miss it), routed to an external
  guard.
- **Step semantics:** one record cycle = one macrostep, STATEMATE next-step sensing
  (a flag set this cycle is sensed next cycle). Same-cycle cross-region dependencies
  should be reviewed.

When in doubt, the tool flags rather than guesses. Treat every `flags` entry as a spot
that needs a human against the original source.

## What's next: the state axis

Every target above answers *"what does this **program** do?"*. A migration needs the
transpose — *"what happens to the **balance**, across every program?"* — because one piece
of state is affected by many programs, so **the new system's service boundaries will not
match the old program boundaries**.

[**docs/state-graph-plan.md**](docs/state-graph-plan.md) is the build spec: emit the join
keys here (the SQL column↔host-variable mapping; `program`/`member`/`file` on lineage
rows), then load N bundles into a **Neo4j graph** — where *"which programs affect the
balance"* is one hop and *"where are the service boundaries"* is community detection.
Identity is **provable only**; anything unproven is an explicit work list, never a guess.

[**docs/mainframe-artifacts.md**](docs/mainframe-artifacts.md) is its prerequisite, and
the reason: *the COBOL tells you what a program does — it cannot tell you what it does it
to.* `READ CUST-FILE` never says which dataset that is; only the JCL does. That document
inventories every artifact the join depends on — JCL/PROCs, copybook libraries, DCLGEN and
Db2 DDL, CICS/IMS definitions, utility control cards, MQ, ASM, the scheduler — sorted by
whether it **resolves an identity**, hides **behavior**, carries **orchestration**, or
defines the **boundary**, with the order to build them in and what each will lie about if
parsed carelessly.

## Development

```bash
PYTHONPATH=src python -m pytest -q     # 304 tests: normalizer, lexer, parser, preprocessor, data, semantics, analysis, statechart, emitter, interface, reactive, business, golden-master
```

The emitter (`--target js`) and golden-master tests need Node + a local `xstate`
install (`npm install`); they skip cleanly when those are absent.

Layout:

```
src/cobol_xstate/   normalizer · lexer · model · parser · preprocessor · data_division · semantics · analysis · naming · statechart · emitter · cli · __main__
src/cobol_xstate/runtime/   package data, emitted alongside `--target js` output (never
                    executed by the converter itself):
                    cobolRuntime.mjs (fixed-point decimal ops + field-aware store)
                    cobolDriver.mjs  (reference driver: invoke interpreter + file I/O for golden-master)
examples/           custrpt.cbl  (canonical batch loop)
                    banktran.cbl (EVALUATE dispatch + dynamic CALL resolved by constant propagation)
                    altswitch.cbl (ALTER first-time-switch idiom + an unresolvable dynamic CALL)
                    accum.cbl / nestperf.cbl (PERFORM-UNTIL & nested PERFORM call-return)
                    tblsum.cbl (OCCURS table: subscripted reads/writes)
                    sorter.cbl (SORT INPUT/OUTPUT PROCEDURE as call-return)
                    fileerr.cbl (DECLARATIVES USE AFTER ERROR as a parallel handler region)
                    thrurange.cbl (PERFORM p THRU q as a range actor)
tests/              one module per pipeline stage (289 tests)
```

## License

MIT.
