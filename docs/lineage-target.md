# The lineage target (`--target lineage`)

## What it is

One row per **(external event, field)**, answering: *which event is responsible for this
field's state?*

For an **input** event the fields are the ones that event **fills**. For an **output**
event they are the ones that **fill it** — traced back through the program's assignments
to the external event(s) their data ultimately came from.

```bash
cobol-xstate prog.cbl --outdir out    # -> out/prog.json + out/prog.lineage.json
```

It is a **projection**: a pure read over the validated IR, in its own file. A default
run writes it alongside the bundle (`prog.json` + `prog.lineage.json`) because the table
is a companion to the machine - they are read together. `--target lineage` emits the
table alone; `--no-lineage` skips it.

## The row

```jsonc
{
  "event": "CREATE.FILE.OUT-FILE",   // the boundary crossing
  "direction": "output",             // input = event fills it | output = it fills event
  "endpoint": "OUT-FILE",
  "endpointType": "file",
  "verb": "WRITE",
  "state": "0000-MAIN__L2",          // the state performing the I/O
  "line": 38,
  "field": "OUT-FEE",
  "pic": "9(5)V99",
  "section": "FILE",
  "changedByProgram": true,          // this program assigns it (not just passes it)
  "changedBy": [{ "action": "COMPUTE_OUT-FEE_eq_LK-QTY_WS-RATE", "line": 44 }],
  "origins": [                       // the external events whose data reaches it HERE
    { "event": "GET.CALLER.CALLER" },
    { "event": "GET.CONSOLE.SYSIN" }
  ]
}
```

Reading it: *the WRITE to OUT-FILE emits `OUT-FEE`; this program computed it at line 44;
and its value comes from the caller's parameter combined with a console `ACCEPT`.*

### The cross-program identity keys

Each row also carries `program`, and - **when the code proves it** - `member` (the
copybook the field was declared in) or `file` (the FD whose record it belongs to). A
field name is program-*local*: A's `WS-BALANCE` and B's `CUST-BAL` may be the same state
or unrelated. What proves sameness is a shared declaration. These keys are what let rows
from many programs be concatenated and joined to answer *"what touches this state?"* -
see [state-graph-plan.md](state-graph-plan.md).

A field declared inline carries **neither** key. That is deliberate: nothing proves it is
shared, so it is honestly unresolvable rather than matched on a name that happens to look
similar.

### "Did a LINKAGE item change it?"

There is no such column, because it falls out for free: reading a linkage field **is** a
`GET.CALLER.CALLER` event, so it appears in `origins` like any other source. LINKAGE
items are seeded at program entry as originating from the caller — which is exactly what
a parameter is.

### `changedByProgram`

True when this program **assigns** the field. An input event's own fill (`ACCEPT`,
`SELECT ... INTO`) is *not* a change by the program — the value came from outside; the
program only received it.

## Worked example

`examples/lineage.cbl` — the caller passes `LK-CUST`/`LK-QTY`, the program `ACCEPT`s a
rate, `CALL`s `SUBFEE` by reference, `STRING`s two fields, writes a file:

| dir | field | changed | origins |
|---|---|---|---|
| input | `WS-RATE` | false | `GET.CONSOLE.SYSIN` |
| output | `OUT-NAME` | true | `GET.CALLER.CALLER` — via `WS-NAME ← LK-CUST` |
| output | `OUT-FEE` | true | `GET.CALLER.CALLER`, `GET.CONSOLE.SYSIN` — `LK-QTY * WS-RATE` |
| output | `OUT-MEMO` | true | `GET.CALLER.CALLER`, `CREATE.PROGRAM.SUBFEE` *(maybe)* |
| output | `OUT-REC` | true | union of its children |
| output | `WS-REF` | false | `CREATE.PROGRAM.SUBFEE` *(maybe → SUBFEE)* |

## How it works

A **flow-sensitive reaching-origins fixpoint** over the emitted state graph:

1. **Seed** — LINKAGE items originate from the caller; everything else starts with no
   external origin.
2. **Transfer**, per state's entry actions in order:
   - an input event **sets** its fields' origins to itself;
   - an assignment gives its target the **union** of its operands' origins;
   - `STRING`/`UNSTRING`/`INSPECT` propagate **dependencies** (see below);
   - a `CALL`'s by-reference arguments gain a **maybe** origin.
3. **Join** at merge points (union), iterate to a fixpoint. The lattice is finite, so it
   terminates.
4. **Emit** a row at every event, using the origins that actually reach it.

PERFORM is followed as a real call and returned from, using the same target resolution as
the runnable emitter (`_target_owner`), so a section's whole extent is analyzed. States
are **split at PERFORM boundaries** first — a folded run like
`[ACCEPT, MOVE, perform_X, WRITE]` would otherwise run the `WRITE` with pre-call origins.

### STRING / UNSTRING / INSPECT

Their *value* semantics are not modeled (concatenation, delimiters, tallying) — but
lineage doesn't need the value, only **which fields feed which**. `STRING WS-A WS-B INTO
WS-C` gives `WS-C ← {WS-A, WS-B}`, which keeps the chain intact. This is why `OUT-MEMO`
above resolves rather than becoming a dead end.

## Honest limits

Each is surfaced in `flags`, never guessed:

- **Context-insensitive.** A paragraph PERFORMed from two sites is analyzed once with the
  *merged* incoming state, so an origin from call site A can appear at an event reached
  only via B. This **over-approximates** — it may name an extra origin, never miss a real
  one. That is the safe direction for provenance, and it is flagged when it occurs.
- **`CALL ... USING`** is BY REFERENCE by default and the callee is a different program,
  so it *may* rewrite any argument. Those arguments get the CALL as a `maybe` origin with
  `resolvedBy` naming the program that would settle it. This turns an unknown into a work
  list rather than a dead end.
- **Reference-modified stores** (`MOVE x TO F(1:2)`) are not modeled; the field is marked
  `unknown` — *"we cannot trace this"*, which is not the same as *"nothing feeds this"*.
- **REDEFINES byte-aliasing** and unresolved/multi-dimension subscripts break a chain for
  the same reason they do elsewhere in the tool.
- **A table is one field.** `TBL(I)` is not resolved per element.

## What it cannot tell you: who calls this program

A `LINKAGE SECTION` says *"someone will pass me this record"*; it never says who. From one
program's source the caller is unknowable, which is why the endpoint is the anonymous
`CALLER`.

It is recoverable **from a corpus**, by inversion: every program names the programs *it*
calls, so joining those backwards yields callers, and a caller's `CALL 'ME' USING WS-A`
can then be matched positionally against this program's `LK-FIELD` — extending lineage
across the program boundary. That join needs N bundles and no IR, so it belongs in a
separate tool, not here. (Dynamic `CALL`s whose target cannot be constant-proven leave
holes in that graph; they are already flagged.)
