# cobol-xstate — Complete Manual

A full reference for what this program does, what it produces, and how to use it.

For a short overview, see [README.md](README.md). This document is the long form: every
command-line flag, every output field, every COBOL construct it understands, and the
exact meaning of everything it emits.

---

## Table of contents

1. [What this program is](#1-what-this-program-is)
2. [Install and first run](#2-install-and-first-run)
3. [Command-line reference](#3-command-line-reference)
4. [The four output targets](#4-the-four-output-targets)
5. [The JSON bundle, section by section](#5-the-json-bundle-section-by-section)
6. [What COBOL it understands](#6-what-cobol-it-understands)
7. [The external interface (inputs, outputs, fields)](#7-the-external-interface-inputs-outputs-fields)
8. [Flags: what they mean and how to triage them](#8-flags-what-they-mean-and-how-to-triage-them)
9. [Running the recovered machine](#9-running-the-recovered-machine)
10. [Architecture: the pipeline](#10-architecture-the-pipeline)
11. [Known limitations](#11-known-limitations)
12. [Example programs](#12-example-programs)
13. [Development and testing](#13-development-and-testing)
14. [Troubleshooting](#14-troubleshooting)

---

## 1. What this program is

`cobol-xstate` reads IBM Enterprise COBOL and recovers its behavior as an **XState v5
Harel statechart**. The output is a **rewrite contract**: a machine-readable, fully
traceable description of what the program does, meant to drive a modernization rewrite
or to be rendered as a diagram.

### Why a statechart, and why Harel

A flowchart or UML activity diagram captures control flow and stops there. A
Harel/STATEMATE statechart carries more: typed data, actions as assignments, conditions
as expressions, orthogonal (concurrent) regions, and hierarchy. That extra capacity is
what lets this tool aim at capturing **all** the program logic rather than a sketch of
it — the paragraph control flow *and* the data layer underneath it.

### The governing rule: no invented logic

Every state, guard, action, and expression is a faithful translation of source text that
traces back through a `provenance` table to a specific line. Where a static parse
genuinely cannot pin down behavior (a target chosen at runtime, a byte-level
reinterpretation), the tool **draws the shape and raises a flag** rather than guessing.

A flag means *"this is drawn, but its behavior depends on runtime data — verify against
the source."* It does not mean "skipped." Treat every flag as a spot that needs a human.

### What you get

| You want | Use |
|---|---|
| A review/rewrite contract, diagram source | `--target json` (default) |
| A machine that actually runs and computes | `--target js` |
| An event-driven (queue/async) machine | `--target reactive` |
| The business-level story, scaffolding removed | `--target business` |

---

## 2. Install and first run

A normal Python package: install it, then run it. Pure standard library — **no runtime
dependencies**, no build step. Python ≥ 3.9. `pytest` only for the tests.

```bash
python -m pip install -e .        # editable (development)
python -m pip install .           # regular
```

That gives you two equivalent ways to run it:

```bash
cobol-xstate prog.cbl             # the console script
python -m cobol_xstate prog.cbl   # interpreter-explicit
```

Prefer `python -m cobol_xstate` in scripts and CI: it bypasses PATH and Windows
file-association surprises entirely.

### First run

```bash
cobol-xstate examples/custrpt.cbl --summary
```

This writes `./custrpt.json` and prints a summary to stderr:

```
[custrpt.cbl] detected source format = fixed (97%: column 7 is a valid indicator on all 40 lines, incl. 5 comment/continuation line(s))
[custrpt.cbl] wrote custrpt.json
[CUSTRPT] 13 state(s), 24 provenance entr(ies), 0 flag(s), 4 perimeter state(s)
  external interface:
    file      CUST-FILE                (get)
    console   SYSOUT                   (create)
  PERIMETER 1000-INIT__io5 [CUSTRPT] (input): gets GET.FILE.CUST-FILE
  PERIMETER 1000-INIT [CUSTRPT] (input): gets GET.FILE.CUST-FILE
  ...
```

Zero flags means every construct in this program was modeled outright. State names like
`1000-INIT__io5` are structural sub-states of the `1000-INIT` paragraph — see
[section 5](#5-the-json-bundle-section-by-section).

Everything on stdout is the artifact; everything on stderr is commentary. So
`cobol-xstate prog.cbl -o - > chart.json` gives you a clean file.

---

## 3. Command-line reference

```
cobol-xstate [-h] [-o OUTPUT] [--outdir DIR]
             [--target {json,js,reactive,business}]
             [--format {fixed,free}] [-I DIR] [--copybook-ext EXT]
             [--machine-only] [--indent N] [--summary]
             source
```

### `source` (positional, required)

Path to a COBOL source file, or `-` to read from stdin.

```bash
cobol-xstate prog.cbl
cobol-xstate - < prog.cbl        # output name falls back to the PROGRAM-ID
```

### `-o, --output PATH`

Exact output path. Overrides `--outdir` and the automatic name. `-o -` writes the
artifact to **stdout** instead of a file (useful for piping).

```bash
cobol-xstate prog.cbl -o build/custom.json
cobol-xstate prog.cbl -o - | jq '.flags'
```

### `--outdir DIR`

Directory for the auto-named output file. Default: current directory. Relative paths
resolve against the current directory; `.` is the current directory. **Created with
parents if it does not exist.**

The file is named after the source stem (`prog.cbl` → `prog.json`), or after the
PROGRAM-ID when reading stdin.

```bash
cobol-xstate prog.cbl --outdir build/charts     # -> build/charts/prog.json
```

### `--target {json,js,reactive,business}`

Which artifact to emit. Default `json`. See [section 4](#4-the-four-output-targets).
Extension follows the target: `.json` for `json`/`business`, `.mjs` for `js`/`reactive`.

### `--format {fixed,free}`

Force the source format instead of auto-detecting. **Auto-detection is layered and
definitive-first**, and it prints what it chose to stderr:

1. A `>>SOURCE FORMAT [IS] FREE|FIXED` directive is authoritative (100% confidence).
2. **The column-7 invariant**: if every non-blank line carries a valid indicator in
   column 7 (space, `*`, `/`, `-`, `D`, `d`, `$`), the file is conclusively FIXED.
3. The first DIVISION header's column (8 → fixed, ≤4 → free).
4. Any line longer than 80 columns → free.
5. Column-7 violation ratio ≥ 0.15 → free.
6. Otherwise: default to fixed at low confidence, **with a warning**.

> **Why column 7 only?** Fixed-format COBOL routinely carries alphanumeric *change
> markers* in columns 1–6 (`CHG001`, `PR1234`) which the compiler ignores. Any heuristic
> that reads columns 1–6 misfires on real corpora. Column 7 is the invariant.

If detection is not confident the tool warns and recommends `--format`. A silent wrong
guess corrupts every downstream stage, so this is deliberately loud.

### `-I, --copybook-path DIR` (repeatable)

Copybook search directory for `COPY` / `EXEC SQL INCLUDE`. The source file's own
directory is always searched as well.

```bash
cobol-xstate prog.cbl -I copybooks -I shared/cpy
```

### `--copybook-ext EXT` (repeatable)

Extra extension to try when resolving a copybook. Defaults already tried:
(bare name), `.cpy`, `.CPY`, `.cbl`, `.cob`, `.copy`, `.CBL`.

### `--machine-only`

Emit only the bare XState config — no provenance, flags, notes, data, semantics, or
interface. Use when you want to feed `createMachine` directly and have already reviewed
the contract.

### `--indent N`

JSON indent. Default 2.

### `--summary`

Print a human-readable summary to **stderr**: state/provenance/flag counts, the external
interface endpoint table, every perimeter state with its gets/creates, and every flag
with its paragraph and line. This is the fastest way to triage one program.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 2 | Source file not found |

A program that parses badly does **not** fail the run — it emits with flags. See
[section 8](#8-flags-what-they-mean-and-how-to-triage-them).

---

## 4. The four output targets

All four derive from the **same validated intermediate representation**. The faithful
machine is the trusted core; the other targets are mechanical projections of it, so they
inherit that trust rather than re-deriving it from COBOL text.

```
COBOL ──► faithful IR (validated, golden-master tested)
             ├──► --target json      the contract (default)
             ├──► --target js        runnable, decimal-exact
             ├──► --target reactive  event-driven lowering
             └──► --target business  business-level distillation
```

### `--target json` — the contract (default)

The full bundle: machine config + data dictionary + semantics + external interface +
provenance + flags + notes. This is the review artifact and the diagram source.
Detailed in [section 5](#5-the-json-bundle-section-by-section).

In this target a `PERFORM p` is the flat marker action `perform_p` — the review contract
records *that* a call happens; the literal jump-and-return pair is not drawn here.

### `--target js` — the runnable machine

A complete XState v5 `setup({ actions, guards, actors }).createMachine(...)` ES module,
backed by the fixed-point decimal runtime (`cobolRuntime.mjs`, dropped beside the module
automatically).

This target **synthesizes real call-return**: every performed paragraph becomes an XState
actor; the PERFORM site `invoke`s it with the context as input and assigns the output
back on `onDone`, so WORKING-STORAGE threads correctly through nested calls. The machine
runs end-to-end under stock `createActor` with no custom interpreter.

```bash
cobol-xstate prog.cbl --target js -o out/prog.machine.mjs
# writes out/prog.machine.mjs and out/cobolRuntime.mjs
```

Exports:

| Export | What it is |
|---|---|
| `machine` (also default) | the wired XState machine |
| `machineConfig` | the raw config |
| `actorConfigs` | per-paragraph actor configs (PERFORM call-return) |
| `FIELDS` | per-field type spec (digits/scale/signed/len/occurs) |
| `ops` | data actions: `(context) => partialContext` |
| `guardFns` | evaluable guards: `(context) => boolean` |
| `externalGuards` | guard names driven by runtime conditions (default false) |
| `negatedExternal` | map of NOT-guards to the positive condition they negate |
| `effectActions` | effect no-ops (DISPLAY/OPEN/CALL/exec) |

**Arithmetic is fixed-point decimal, never float.** Stores honor the receiver's PICTURE:
decimal alignment, high-order truncation, `ROUNDED` as half-away-from-zero, unsigned
magnitude.

### `--target reactive` — the event-driven machine

A lowering in which boundary I/O is **push / fire-and-forget** rather than synchronous:
inbound data arrives as events the machine waits on (`on`), outbound data is published.
Only the ~5–15% of states that cross the boundary are rewritten; internal guarded control
flow is left exactly as the faithful machine emits it.

See [docs/reactive-target.md](docs/reactive-target.md).

> **Caveat:** this slice does **not** apply the PERFORM→invoke transform, so `perform_*`
> is a no-op here and performed paragraphs' logic does not run. It is faithful only for
> flat (non-PERFORM) flow. The tool flags this loudly. Use `--target js` when call-return
> matters. It also refuses `type: parallel` machines (DECLARATIVES/CICS HANDLE).

### `--target business` — the business view

A **read-only distillation**: technical scaffolding collapsed, only the states that
matter from a business viewpoint kept. Each state is classified:

| Role | Meaning |
|---|---|
| `boundary` | crosses the program perimeter (external I/O) |
| `decision` | branches on a business condition |
| `boundary+decision` | both |
| `calculation` | does real arithmetic/COMPUTE work (a pricing/accumulation step) |
| `terminal` | program end |
| `technical` | scaffolding — collapsed away |

Business **names are deliberately left `null`** as fill-in. Mapping COBOL identifiers to
business vocabulary is the one step this pass cannot infer; a human (or an LLM) supplies
it. Collapsed states are listed individually with the reason.

The traversal is call/return-aware — it follows PERFORM into paragraphs and back, using
the same resolution as the runnable emitter, so the business flow matches real call
semantics.

See [docs/business-view.md](docs/business-view.md).

---

## 5. The JSON bundle, section by section

```jsonc
{
  "format":     "xstate-v5-config",
  "metadata":   { "program": "...", "source": "...", "generator": "...", "disclaimer": "..." },
  "machine":    { "id", "context", "states", "initial" },
  "data":       { /* typed data dictionary */ },
  "semantics":  { "actions": {...}, "guards": {...} },
  "interface":  { "endpoints": [], "events": [], "perimeterStates": {}, "parameters": {} },
  "provenance": { /* name -> source trace */ },
  "flags":      [ /* things needing human verification */ ],
  "notes":      [ /* program-level remarks */ ]
}
```

### `machine`

A bare XState v5 `createMachine` config: `id`, `initial`, `context`, `states`.

- States are **flat with mangled names** (`0000-MAIN__if3`, `1000-READ__seq2`); structure
  is encoded in the names, not in nesting.
- Transitions are eventless `always` edges, ordered **guarded-first then default**, so
  XState's first-enabled-wins gives correct IF/EVALUATE else semantics.
- `context` is seeded with each elementary item's start-of-run value (its `VALUE` clause,
  else the typed default).
- A perimeter state carries `meta.perimeter` (`input`/`output`/`input-output`) plus its
  `gets`/`creates` **on the state node itself**, so a consumer reading only `machine`
  still sees the boundary.
- With DECLARATIVES or CICS HANDLE the root becomes `type: "parallel"` with a `PROGRAM`
  region and an orthogonal `HANDLERS` region.

### `data` — the typed data dictionary

Every DATA DIVISION item, keyed by name:

```json
"CUST-AMT": {
  "level": 5,
  "line": 17,
  "section": "FILE",
  "parent": "CUST-REC",
  "file": "CUST-FILE",
  "type": {
    "category": "numeric",
    "usage": "COMP-3",
    "pic": "9(7)V99",
    "digits": 9,
    "scale": 2,
    "signed": false
  }
}
```

| Field | Meaning |
|---|---|
| `level` | COBOL level number |
| `section` | `FILE` / `WORKING-STORAGE` / `LINKAGE` / `LOCAL-STORAGE` / `SYNTHETIC` |
| `parent` | enclosing group item |
| `file` | (FILE SECTION) the FD/SD file this record belongs to |
| `member` | copybook member, when the item came from a `COPY` |
| `occurs` / `occursDependingOn` | table size (the **maximum** for `OCCURS m TO n`) and its length variable |
| `redefines` | the item redefined |
| `value` | the `VALUE` clause |
| `type` | category, usage, pic, digits, decimal scale, signed |

88-levels appear as `{"kind": "condition-name", "of": parent, "values": [...], "ranges": [[lo,hi]]}`.

`type.category` is one of `numeric`, `numeric-edited`, `alphanumeric`, `alphabetic`,
`group`, `unknown`. **This type information governs COBOL's fixed-point decimal
arithmetic** — a rewrite that uses binary float will not match.

### `semantics.actions`

Each action's real operation, not just a name:

```json
"ADD_CUST-AMT_TO_WS-TOTAL": {
  "verb": "ADD",
  "kind": "arith",
  "raw": "ADD CUST-AMT TO WS-TOTAL",
  "assignments": [ { "target": "WS-TOTAL", "expr": "WS-TOTAL + CUST-AMT" } ]
}
```

`kind` is one of:

| kind | Meaning |
|---|---|
| `assign` | MOVE / SET |
| `arith` | ADD / SUBTRACT / MULTIPLY / DIVIDE |
| `compute` | COMPUTE |
| `initialize` | INITIALIZE (target := category default) |
| `input` | ACCEPT, or SQL `SELECT/FETCH … INTO` (external-sourced assignment) |
| `io` | file I/O, carrying `file` / `into` / `from` |
| `effect` | opaque side effect (DISPLAY/OPEN/CALL/STRING/…) |
| `exec-sql` / `exec-cics` / `exec-dli` | embedded sub-language, with `hostVars` |

Optional annotations: `rounded`, `onSizeError`, `notes`.

**Assignments apply in order and later ones see earlier stored results** — that is how
`DIVIDE … GIVING q REMAINDER r` reads the truncated quotient.

### `semantics.guards`

Each guard's Boolean expression tree:

```json
"UNTIL_WS-EOF_eq_Y": { "op": "rel", "left": "WS-EOF", "rel": "=", "right": "'Y'" }
```

| `op` | Node |
|---|---|
| `rel` | relational: `left`, `rel`, `right` |
| `and` / `or` | `args: [...]` |
| `not` | `arg: {...}` |
| `class` | class condition (NUMERIC / ALPHABETIC / …) |
| `sign` | sign condition (POSITIVE / NEGATIVE / ZERO) |
| `cond` | 88-level condition-name, resolved to parent + `values` / `ranges` |
| `raw` | **could not be modeled** — always accompanied by a flag, routed to an external guard |

### `interface`

The external perimeter. Fully detailed in [section 7](#7-the-external-interface-inputs-outputs-fields).

### `provenance`

Every emitted name traced to source:

```json
"0000-MAIN": { "kind": "state", "cobol": "paragraph 0000-MAIN", "line": 23 }
```

`kind` is `state` / `guard` / `action`; `member` appears when the name came from a
copybook. **This is the audit trail** — it is what makes "nothing invented" checkable
rather than a claim.

### `flags`

`{ "paragraph": "...", "line": N, "message": "..." }` — see
[section 8](#8-flags-what-they-mean-and-how-to-triage-them).

### `notes`

Program-level remarks: expanded copybooks, **missing** copybooks, DECLARATIVES presence,
step semantics, and the decimal-arithmetic caveat.

---

## 6. What COBOL it understands

Each paragraph's *entire* statement tree is compiled recursively. The only thing
collapsed is a run of genuinely straight-line statements, which folds into one state's
`entry` action list. Nothing conditional or order-bearing is folded away.

### Control flow

| COBOL | XState v5 |
|---|---|
| Paragraph / section | an entry state; its body compiles to sub-states |
| Straight-line run of `MOVE`/`ADD`/`OPEN`/… | one state's `entry` action-name list |
| `IF … ELSE … END-IF` (incl. nested) | guarded `always` split converging on the continuation |
| `EVALUATE … WHEN … WHEN OTHER` | guarded `always` per WHEN. `ALSO` pairs → `a = x AND b = y`; `THRU` ranges, abbreviated relations (`WHEN > 5`), `ANY` handled |
| **Stacked `WHEN`s** (`WHEN 1 WHEN 2 body`) | each shares the following branch's body (COBOL fall-in) |
| `SEARCH` / `SEARCH ALL … WHEN … AT END` | each `WHEN` a guarded branch, `AT END` a guarded branch; index iteration is an opaque effect (flagged) |
| `PERFORM p UNTIL/VARYING`, inline `PERFORM` | a loop state (exit guard + body looping back); `TEST AFTER` ⇒ do-while; `VARYING` inits (`var := from`) and steps (`var := var + by`) |
| **`PERFORM p n TIMES`** | a synthetic typed counter (`TIMES-CTR-n`), stepped, with a real exit guard `ctr >= n` |
| `PERFORM p` (simple) | call-return action `perform_p`; a real `invoke` in `--target js` |
| **`PERFORM section-name`** | owns the section's whole extent — header **plus all member paragraphs** |
| `PERFORM p THRU q` | a range actor spanning `p..q` in source order (a THRU tail that is a section extends through its members) |
| `SORT/MERGE … INPUT/OUTPUT PROCEDURE` | `perform_input` → `sort_file` effect → `perform_output` |
| `GO TO p` | exit `always` edge (no return); suppresses fall-through |
| **`GO TO p OF sec`** | qualification consumed; the unqualified name is the target |
| **`GO TO` unknown paragraph** | flagged and rerouted to program end (never a dangling edge) |
| `GO TO … DEPENDING ON var` | guarded fan-out with the **real guard `var = i`** per target + out-of-range edge + flag |
| Fall-through / end of paragraph | eventless `always` edge to the next paragraph (or shared `final`) |
| `STOP RUN` / `GOBACK` / `EXIT PROGRAM` | `type: 'final'` |
| **`EXIT PARAGRAPH` / `EXIT SECTION`** | edge to the paragraph's / section's continuation (skips the rest) |
| **`EXIT PERFORM [CYCLE]`** | breaks / continues the enclosing inline loop |
| `NEXT SENTENCE` | edge to the next statement + flag (true skip-past-period not modeled) |
| `CONTINUE` | no-op |
| `ALTER … TO PROCEED TO` | **real evaluable guards** over a synthetic switch field `ALT-<para>`; the ALTER is a real assignment that flips it (+ flag) |
| dynamic `CALL ident` | constant-propagated to a literal where provable, else flagged |
| `DECLARATIVES` USE / CICS `HANDLE CONDITION` | `type:'parallel'`: a `PROGRAM` region + an orthogonal `HANDLERS` region watching a trigger event (`IO.ERROR.file` / `CICS.cond`) |

### Conditional handler phrases — real branches, never hoisted

These compile to a guarded edge per handler plus a normal continue edge. The trigger is a
runtime condition, so the guard is external **and flagged**:

- `READ/WRITE/REWRITE/DELETE/START/RETURN … [NOT] AT END`
- `… [NOT] INVALID KEY`
- `WRITE … AT END-OF-PAGE / EOP` (its own handler key, not conflated with AT END)
- `CALL … [NOT] ON EXCEPTION / ON OVERFLOW`
- arithmetic `… [NOT] ON SIZE ERROR`
- `ACCEPT/DISPLAY … [NOT] ON EXCEPTION`

**NOT-form guards are the negation of their positive condition.** `notAtEnd` is true
exactly when `atEnd` has not been raised — so `NOT AT END` is the normal per-record path,
both under stock XState and in the reference driver. The module exports `negatedExternal`
to make this explicit.

`READ f NEXT RECORD` (the standard VSAM browse idiom) is parsed correctly — I/O clause
words are recognized rather than terminating the statement.

### DATA DIVISION

Levels 01–49/66/77/88, groups vs elementary, FILLER, `PIC`, `USAGE`
(DISPLAY/COMP/COMP-3/COMP-4/COMP-5/BINARY/INDEX/POINTER), `VALUE`, `REDEFINES`,
`OCCURS` (incl. `m TO n DEPENDING ON`, sized at the **maximum** + flag), 88-level
condition names with singleton values **and** `VALUE lo THRU hi` ranges.

FD/SD record ↔ file association is recovered, so a record knows its physical file.

### ENVIRONMENT DIVISION

`FILE-CONTROL` `SELECT` entries are parsed: `ASSIGN TO ddname`, `ORGANIZATION`, `ACCESS`,
`RECORD KEY`, and **`FILE STATUS`**. The status field matters — branching on it is the
program reacting to the file subsystem's response, the VSAM/QSAM analogue of `SQLCODE`.

### Conditions

Relational, class, sign, 88-level, AND/OR/NOT, parenthesized sub-conditions, decimal
literals (`> 500.00`), arithmetic-expression operands (`WS-A + WS-B > WS-LIMIT`),
COBOL **abbreviated combined relations** (`IF A = 1 OR 2` → `A = 1 OR A = 2`, with subject
and operator implied from the prior relation), and 88 `VALUE lo THRU hi` ranges
(`lo <= x <= hi`).

Anything beyond this falls back to `{op: 'raw'}` — **and a raw fallback always emits a
flag**, so a reviewer scanning only `flags` cannot miss it.

### Preprocessor

- `COPY member [OF lib] [REPLACING ==a== BY ==b==]` — recursive with a cycle guard;
  expanded lines carry their `origin` member for provenance.
- **Code preceding a `COPY` in the same sentence is preserved.**
- `EXEC SQL INCLUDE member END-EXEC` — behaves like COPY.
- Standalone `REPLACE ==a== BY ==b==` … `REPLACE OFF`.
- A member that cannot be found is reported in `notes` as **missing** — its data/logic is
  not in the model — rather than being silently dropped.
- Copybooks **inherit** the including program's source format (a fragment is too small to
  auto-detect).

### Embedded sub-languages

`EXEC SQL` / `EXEC CICS` / `EXEC DLI` are extracted with host variables preserved.
`SELECT`/`FETCH … INTO :hv` becomes a real input assignment to each host variable.
`LINK`/`XCTL`/`RETURN`/`HANDLE` map to call/transfer/terminate/handler-region. The rest
of the sub-language is **not interpreted** — it stays an opaque effect.

---

## 7. The external interface (inputs, outputs, fields)

The `interface` section is a **pure read** over the emitted machine — it changes nothing
and invents nothing. It classifies which states cross the program boundary, in which
direction, to which external actor, and **which fields cross**.

Two directions:

- **`get`** — the state receives external data/events: file `READ`, SQL `SELECT`/`FETCH`,
  `ACCEPT`, CICS `RECEIVE`/`READQ`, a handled error condition, end-of-file, a response
  code (`SQLCODE`, `EIBRESP`, a `FILE STATUS` field).
- **`create`** — the state produces external data/events: file `WRITE`/`REWRITE`/`DELETE`,
  SQL `INSERT`/`UPDATE`/`DELETE`, `DISPLAY`, CICS `SEND`/`WRITEQ`, `CALL`/`LINK`/`XCTL`,
  CICS `RETURN`.

### `endpoints` — the external actors

```json
{ "endpoint": "CUST-FILE", "type": "file", "directions": ["get"],
  "assign": "CUSTIN", "organization": "SEQUENTIAL" }
```

Endpoint types: `file`, `db2`, `program`, `console`, `terminal`, `caller`, `condition`,
`ims`, `queue`, `system`, `transaction`, `response`.

File endpoints carry their FILE-CONTROL binding (`assign` = the DD name / dataset,
`organization`, `access`, `recordKey`, `statusField`).

### `events` — the per-crossing detail

```json
{ "event": "GET.DB2.CUST", "direction": "get", "endpointType": "db2",
  "endpoint": "CUST", "verb": "SELECT",
  "fields": ["CUST-NAME", "CUST-BALANCE"],
  "params": ["CUST-ID"],
  "state": "1000-LOOKUP", "region": "PROGRAM", "line": 42,
  "cobol": "EXEC SQL SELECT ... END-EXEC" }
```

| Key | Meaning |
|---|---|
| `event` | `GET.<TYPE>.<ENDPOINT>` / `CREATE.<TYPE>.<ENDPOINT>` |
| `fields` | **the data crossing in the event's direction** |
| `params` | data flowing the *other* way in the same command (SQL `WHERE` host vars, CICS `RIDFLD` keys, `CALL … RETURNING`) |
| `state` / `region` | which state performs the I/O — lets a renderer draw the arrow |
| `line` / `cobol` | source trace |

### Field-level fidelity — what lands in `fields`

| Channel | Fields captured |
|---|---|
| `READ f INTO x` | `x` |
| `READ f` (no INTO) | the FD record **and its elementary fields** |
| `WRITE rec FROM y` | the record, its fields, and `y` — endpoint resolves to the **physical file** via the FD link |
| `ACCEPT x` / `DISPLAY a b` | the operands (literals dropped) |
| `ACCEPT x FROM DATE/DAY/TIME` | a **system-clock** read, not terminal input |
| SQL `SELECT/FETCH … INTO` | INTO host vars in `fields`; `WHERE` host vars in `params` |
| SQL `INSERT/UPDATE/DELETE` | its host variables |
| SQL cursor `FETCH` | endpoint resolves to the **table** via `DECLARE … CURSOR FOR … FROM t` |
| CICS `RETURN` | the COMMAREA; `TRANSID(x)` appears in the verb (the pseudo-conversational contract) |
| CICS `LINK`/`XCTL` | the COMMAREA |
| CICS `READ`/`WRITE` dataset | `INTO`/`FROM` area; `RIDFLD` key in `params` |
| CICS `READQ`/`WRITEQ` TS/TD | the queue endpoint + `INTO`/`FROM` area |
| `CALL 'P' USING a b` | the arguments; `RETURNING` in `params` |
| LINKAGE traffic | **any** assignment verb writing a linkage item (send response) or reading one (receive request), including **guards** that read one |
| `MOVE … TO RETURN-CODE` | a caller-visible output |

### `perimeterStates`

```json
"1000-LOOKUP": { "region": "PROGRAM", "gets": ["GET.DB2.CUST"],
                 "creates": [], "perimeter": "input" }
```

Labelled `input` / `output` / `input-output`. The same information is tagged **onto the
machine's state nodes** as `meta.perimeter` / `meta.gets` / `meta.creates`.

### `parameters` — the program's own entry interface

```json
{ "using": ["LK-PARM"], "returning": null,
  "linkage": ["LK-PARM"], "commarea": false,
  "fields": { "LK-PARM": ["LK-MODE", "LK-RESULT"] } }
```

`PROCEDURE DIVISION USING` / `RETURNING`, the LINKAGE records, whether a CICS
`DFHCOMMAREA` is present, and **each parameter record expanded to its elementary
fields** — so the caller contract is field-level, not just record names. Surfaced as
`get`/`create` against the caller (since `USING` is BY REFERENCE, the caller sees updates).

### Response events

Branching on `SQLCODE`/`SQLSTATE`/`EIBRESP` **or on a file's `FILE STATUS` field** emits a
`get` response event from that subsystem — the program reacting to an external response.
Reads of `EIBCALEN`/`EIBAID`/`EIBTRNID` are CICS-supplied inputs.

---

## 8. Flags: what they mean and how to triage them

A flag is not an error. It means: **the shape is drawn, but its behavior depends on
runtime data — verify against the source.**

The tool never crashes on a corpus: a paragraph whose body fails to parse becomes one
opaque action and a flag, so a batch of thousands converts without a hard stop and every
unrecovered spot is visible.

### Flag categories

| Flag says | What to check |
|---|---|
| `condition not fully modeled (left as raw)` | the condition is beyond the parser; it routes to an external guard — implement it by hand |
| `ALTER-switched exit … verify` | the shape is modeled with real guards over `context.ALT-*`; confirm the active target |
| `GO TO … DEPENDING ON` | the fan-out is modeled with `var = i` guards; confirm the index range |
| `… handler(s) modeled as guarded branch(es)` | the trigger is a runtime condition (external guard) — confirm when it fires |
| `transition target X does not exist` | a `GO TO` to an unknown paragraph; **rerouted to program end** — likely dead code or a missing copybook |
| `paragraph body did not parse` | **logic here is NOT modeled** — review manually. The highest-priority flag |
| `STRING/UNSTRING/INSPECT is an opaque effect` | receivers/tallies are **unchanged** in the model — implement by hand |
| `writes reference-modified target X(a:b)` | substring store not modeled; the runnable machine calls `notModeled` (fails loudly) |
| `OCCURS … DEPENDING ON` | table modeled at **maximum** size; the dynamic extent is not enforced |
| `REDEFINES … DIFFERENT PICTURE/USAGE` | genuine byte reinterpretation — **not** modeled; the views are independent fields |
| `REDEFINES … same category/size` | safe value alias; mirror the value if one is written and the other read |
| `SEARCH … index iteration is an opaque effect` | WHEN/AT END are real; the advance-until-match loop is not |
| `SORT … opaque effect` | record ordering (ASCENDING/DESCENDING KEY) is not modeled |
| `dynamic CALL … ` | the target could not be proven constant — genuinely runtime |
| `EXEC SQL/CICS … registers implicit handler(s)` | a later transfer is invisible at this site; model as a handler region |
| `NEXT SENTENCE` | differs from CONTINUE; verify the intended skip |
| `arithmetic writes non-numeric X` | **S0C7 risk** — verify the type |
| `PERFORM VARYING … AFTER` | only the primary index is stepped; verify inner loops |

### Triage recipe

```bash
# every flag for one program
cobol-xstate prog.cbl --summary -o /dev/null

# flags across a corpus, ranked by frequency
for f in src/*.cbl; do cobol-xstate "$f" -o - 2>/dev/null; done \
  | jq -r '.flags[].message' | sed 's/[A-Z0-9-]\{3,\}//g' | sort | uniq -c | sort -rn
```

Priority order: `did not parse` → `raw condition` → opaque data effects
(STRING/INSPECT) → REDEFINES byte-reinterpretation → everything else.

---

## 9. Running the recovered machine

### Under stock XState

```bash
cobol-xstate examples/accum.cbl --target js -o out/accum.machine.mjs
```

```js
import { createActor } from 'xstate';
import machine from './accum.machine.mjs';

const actor = createActor(machine);
actor.start();
console.log(actor.getSnapshot().status);    // 'done'
console.log(actor.getSnapshot().context);   // { 'WS-I': '5', 'WS-SUM': '15' }
```

Numeric context values are **decimal strings**, not JS numbers — that is what keeps money
arithmetic exact.

### Driving external conditions

External guards (AT END, INVALID KEY, SIZE ERROR, …) read
`context.__cobol_external` and default to false. NOT-forms are handled via
`negatedExternal`. Override guards to drive a scenario:

```js
const driven = machine.provide({ guards: { 'UNTIL_WS-EOF_eq_Y': () => true } });
```

### The reference driver (golden-master)

`runtime/cobolDriver.mjs` runs the whole machine and supplies the one thing stock XState
cannot — sequential file I/O:

```js
import * as mod from './machine.mjs';
import { drive } from './cobolDriver.mjs';

const r = drive(mod, {
  files: { 'CUST-FILE': [ { 'CUST-AMT': '0.10' }, { 'CUST-AMT': '100.55' } ] }
});

r.context;   // final business context
r.display;   // DISPLAY output, in order
r.cycles;    // context snapshot after each READ (per-record trace)
r.halted;    // STOP RUN reached
r.steps;     // step count (guards against non-termination)
```

Every data mutation still flows through the emitted `ops` and every branch through the
emitted `guards` — the driver only feeds records and captures DISPLAY. A match against
hand-computed golden values is evidence the recovery reproduces the program.

### The decimal runtime

`runtime/cobolRuntime.mjs` — fixed-point decimal (`D`, `add`, `sub`, `mul`, `div`, `pow`),
PICTURE-faithful stores (`store`, `storeStr`), table access (`elem`, `setElem`),
comparison (`rel`, `isClass`, `isSign`), and `notModeled` — the honesty backstop that
throws rather than silently computing something wrong.

---

## 10. Architecture: the pipeline

```
raw source
  → normalizer   fixed/free detection (column-7 invariant), column-7 comment/
                 continuation/debug, *> comments, continuation-literal stitching,
                 Area-A detection                                    (normalizer.py)
  → preprocessor COPY / REPLACING / EXEC SQL INCLUDE / REPLACE, via a configurable
                 copybook resolver (paths, exts, missing policy)   (preprocessor.py)
  → lexer        words / numbers / string literals / period / operators, each
                 carrying its source line and copybook origin            (lexer.py)
  → parser       ENVIRONMENT → FILE-CONTROL (ASSIGN/STATUS/KEY);
                 DATA DIVISION → typed dictionary (PIC/USAGE/sign, 88s, FD↔record);
                 EXEC SQL/CICS/DLI extraction;
                 PROCEDURE DIVISION → sections/paragraphs + statement AST
                            (parser.py, model.py, data_division.py)
  → statechart   recursively compile each paragraph's full statement tree to guarded
                 states/loops/handlers; MOVE/COMPUTE → target := expr; conditions →
                 Boolean trees; type the context; constant-propagate dynamic CALL;
                 validate transition targets; provenance + flags
                    (statechart.py, semantics.py, analysis.py, naming.py)
  → interface    classify the boundary crossings (pure read)          (interface.py)
  → emit         json bundle | js setup() module | reactive | business
                            (emitter.py, reactive.py, business.py, cli.py)
```

### Module map

| Module | Responsibility |
|---|---|
| `normalizer.py` | source format detection, column handling, continuation |
| `preprocessor.py` | COPY / REPLACE / INCLUDE expansion |
| `lexer.py` | tokenization with line + origin |
| `data_division.py` | DATA DIVISION → typed `DataItem`s |
| `model.py` | the IR (statement dataclasses, `Program`, `Paragraph`) |
| `parser.py` | recursive-descent statement parser + program structure |
| `semantics.py` | statements → `target := expr`; conditions → Boolean trees |
| `analysis.py` | constant propagation (dynamic CALL resolution) |
| `naming.py` | stable name registry + provenance |
| `statechart.py` | the compiler: IR → XState config + flags |
| `interface.py` | the perimeter overlay (pure read) |
| `emitter.py` | runnable JS: ops, guards, PERFORM→invoke actors |
| `reactive.py` | event-driven lowering |
| `business.py` | business distillation |
| `cli.py` | argument handling, output routing |

---

## 11. Known limitations

This is a **heuristic control-flow recovery**, not a conformant COBOL compiler
front-end. Where it stops, it says so.

### Modeled but flagged (shape drawn, behavior runtime-dependent)

- Dynamic `CALL` that cannot be constant-proven.
- `ALTER` / `GO TO DEPENDING ON` — now real evaluable guards, still flagged for review.
- `SEARCH` index iteration; `SORT` record ordering.
- DECLARATIVES/CICS HANDLE trigger edges (they are runtime events).
- `PERFORM VARYING … AFTER` (only the primary index steps); `VARYING WITH TEST AFTER`
  (modeled test-before).

### Not modeled (explicitly, with flags)

- **STRING / UNSTRING / INSPECT data effects** — opaque effects; receivers and TALLYING
  counters are unchanged in the model. *This is the largest remaining gap.*
- **REDEFINES byte-aliasing** across different PICTUREs — the views are independent
  fields; true reinterpretation needs a byte buffer.
- **Multi-dimension `OCCURS`** (`TBL(I,J)`) and **nested subscripts** (`TBL(IDX(I))`) —
  kept whole in the contract, routed to `notModeled` in the runnable JS.
- **Reference-modification stores** (`MOVE x TO F(1:2)`) — flagged, `notModeled`.
- **The SQL/CICS sub-language** beyond the mapped verbs.
- **`XML PARSE` / `JSON GENERATE`** processing-procedure control flow.
- Statement-level copybook `member` provenance (paragraph- and data-level work).
- Multi-paragraph DECLARATIVES USE sections perform only the first body paragraph.

### Structural caveats

- **`GO TO` out of a performed range** is modeled as a return — once provenance is
  stripped, it is indistinguishable from fall-through.
- **The JSON contract is not executable**; it carries types and semantics, but the
  decimal evaluator lives in `--target js` / your own `setup()` stubs.
- **The machine is largely a flat FSM** with one optional parallel region. Hierarchy,
  history, and exit actions are not used; PERFORM-resume is handled by `invoke` instead.
- **`--target reactive` does not lower PERFORM** (flagged).
- **Step semantics:** one record cycle = one macrostep, STATEMATE next-step sensing.
  Same-cycle cross-region dependencies deserve review.

---

## 12. Example programs

Every fixture in `examples/` is a runnable demonstration of one recovery feature, and
most are pinned by a test.

| Fixture | Demonstrates |
|---|---|
| `custrpt.cbl` | the canonical batch read loop; exact money accumulation (golden master) |
| `banktran.cbl` | EVALUATE dispatch + dynamic CALL resolved by constant propagation |
| `altswitch.cbl` | the ALTER first-time-switch idiom + an unresolvable dynamic CALL |
| `accum.cbl` | `PERFORM UNTIL` call-return |
| `nestperf.cbl` | nested PERFORM threading context through two call levels |
| `varysum.cbl` | `PERFORM VARYING` index init/step |
| `thrurange.cbl` | `PERFORM p THRU q` as a range actor |
| `sectperf.cbl` | **`PERFORM section-name` running the whole section extent** |
| `timesexit.cbl` | **`PERFORM n TIMES`, `EXIT PERFORM`, `EXIT PARAGRAPH`, stacked WHENs** |
| `notend.cbl` | **`NOT AT END` as the per-record path** (golden master) |
| `depending.cbl` | **`GO TO … DEPENDING ON` selecting by index** |
| `divrem.cbl` | **`DIVIDE … REMAINDER`** |
| `tblsum.cbl` | OCCURS table: subscripted reads/writes |
| `sorter.cbl` | SORT INPUT/OUTPUT PROCEDURE as call-return |
| `fileerr.cbl` | DECLARATIVES USE AFTER ERROR as a parallel handler region |
| `cicsinq.cbl` | CICS LINK/XCTL/RETURN/HANDLE + EXEC SQL SELECT |
| `sqlsel.cbl` / `sqldml.cbl` | SQL SELECT INTO; INSERT/UPDATE/DELETE |
| `sqlload.cbl` / `sqlunld.cbl` | file→Db2 load; Db2 cursor→file unload |
| `txnflat.cbl` | flat transaction flow (reactive-target subject) |
| `custrec.cpy` | a copybook (COPY expansion + `member` provenance) |

---

## 13. Development and testing

```bash
PYTHONPATH=src python -m pytest -q      # 206 tests
```

Tests requiring Node + a local `xstate` (`npm install`) — the `--target js` integration
and golden-master suites — **skip cleanly** when those are absent.

| Test module | Covers |
|---|---|
| `test_normalizer.py` | format detection, column handling, continuation |
| `test_lexer.py` | tokenization |
| `test_preprocessor.py` | COPY / REPLACE / missing members |
| `test_parser.py` | statement AST, handlers, headers, GO TO |
| `test_data_semantics.py` | PIC types, `target := expr`, conditions |
| `test_statechart.py` | the compiled config, flags, ALTER |
| `test_emitter.py` | ops/guards + **Node integration under stock XState** |
| `test_golden_master.py` | whole-machine runs vs hand-computed values |
| `test_interface.py` | the perimeter overlay, field capture |
| `test_sql_fixtures.py` | SQL/CICS endpoint + field classification |
| `test_reactive.py` / `test_business.py` | the projection targets |
| `test_cli.py` | argument handling, output routing |

**The load-bearing tests are the golden-master ones**: they run the emitted machine
end-to-end and diff exact decimal results against hand-computed values. A change that
breaks recovery fidelity fails there.

---

## 14. Troubleshooting

**The output looks corrupted / everything is one opaque blob.**
Almost certainly source-format misdetection. The tool prints its choice and confidence to
stderr; if it warned, re-run with `--format fixed` or `--format free`.

**A paragraph I expected is missing.**
Check `notes` for a **missing copybook** — its logic is not in the model. Add `-I DIR`.

**`flags` says "paragraph body did not parse".**
That paragraph's logic is *not* modeled — it degraded to one opaque action so the batch
could continue. Review it by hand; consider reporting the construct.

**The machine runs forever / hits the step limit.**
A loop whose exit guard is external and never fed. Feed it via
`machine.provide({ guards: ... })` or the driver's `guards` option. If it is a
`PERFORM n TIMES`, the counter is modeled — check the count expression instead.

**A `NOT AT END` body never runs.**
Should not happen — NOT-guards negate their positive condition. If you see it, confirm
your consumer honors the module's `negatedExternal` map (the shipped driver does).

**Numbers come out as strings.**
Intentional. Context numerics are **decimal strings** so money arithmetic stays exact.
Use the runtime's `D()` to compute with them; never `parseFloat`.

**`notModeled` threw at runtime.**
The honesty backstop: the machine hit a construct the contract flagged as unmodeled
(multi-dim subscript, ref-mod store, unknown class). The message names it. Supply a
faithful implementation — the alternative would have been a silently wrong answer.

**A PR/branch question:** the recovery is deterministic — same source, same output. Diff
two bundles directly to see what a source change did to the behavior.

---

## License

MIT.
