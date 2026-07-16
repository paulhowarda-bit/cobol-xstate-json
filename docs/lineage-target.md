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
  "changedBy": [{ "action": "COMPUTE_OUT-FEE_eq_LK-QTY_WS-RATE", "line": 44,
                  "conditions": [ /* ...what governs THIS write */ ] }],
  "conditions": [                    // what must hold for the event to happen at all
    { "guard": "WS-TRAN-TYPE_eq_D", "negated": false,
      "expr": "WS-TRAN-TYPE = 'D'", "kind": "business", "line": 28 }
  ],
  "origins": [                       // the external events whose data reaches it HERE
    { "event": "GET.CALLER.CALLER" },
    { "event": "GET.CONSOLE.SYSIN" }
  ]
}
```

Reading it: *the WRITE to OUT-FILE emits `OUT-FEE`; this program computed it at line 44;
its value comes from the caller's parameter combined with a console `ACCEPT`; and it
happens when the transaction is a deposit.*

### `conditions` — the other half of the rule

Origins say **where a value came from**. That is only half a business rule; the other
half is **under what condition**. For requirements work the difference is everything:

> DAILYPOST changes the collected balance

is a dependency. This is a rule:

> DAILYPOST changes the collected balance **when the transaction is a deposit and the
> account is active**

`conditions` is the guards that hold on **every** path to the event — a conjunction, so
every entry is true whenever the event fires. Each carries its `expr` and source `line`,
and a `kind`:

| `kind` | Meaning |
|---|---|
| `business` | a real decision — an `EVALUATE` branch, an `IF` on a data field, an 88-level |
| `control` | plumbing — a loop's `UNTIL` test, a file's end-of-stream check |

Filter to `business` and you have the program's rules; the `control` ones are how COBOL
happened to iterate. The same list appears on each entry of `changedBy`, scoped to that
particular write, so *"this program writes the balance"* becomes *"this program writes
the balance **here**, under **this**"*.

**Negation is first-class.** An `ELSE`, a `WHEN OTHER` and a loop body carry no guard of
their own — their real condition is the negation of the branches before them, and that
negation is usually the interesting rule. So `WHEN OTHER` reports
`NOT (WS-KIND = 'P')` and `NOT (WS-KIND = 'Q')` rather than nothing. It is rendered as
`NOT (...)` rather than by flipping the operator: inverting `=` is safe, inverting an
ordering test is not always the identity a reader assumes once COBOL's figurative
constants and class tests are involved. The same reasoning leaves `NOT (F NOT AT END)`
un-simplified — clumsy to read, impossible to misread.

A file's end-of-stream guard (`IN-FILE_atEnd`, `IN-FILE_notAtEnd`) is synthesized by the
`READ` lowering and has no expression tree, but its meaning is not in doubt: it renders
as `IN-FILE AT END` / `IN-FILE NOT AT END`, `kind: control`. It is **not** marked
`unrecoverable` — crying wolf on the most ordinary branch in COBOL would devalue that
marker where it actually matters.

#### What it will not claim

Conditions are **necessary, not necessarily sufficient**, and the gap is marked rather
than hidden:

- **A disjunction is refused.** One paragraph performed from two guarded sites runs under
  `A OR B`. A conjunction cannot say that. Reporting `A` alone would be a plain lie — it
  would claim the write needs `A` when `B` alone also triggers it — and reporting nothing
  silently would read as *unconditional*. So it reports neither and sets
  `conditionsPartial` with a note.
- **`conditionsPartial`'s absence is best-effort, not a guarantee.** A disjunction inside
  a loop body can evade the check (see *Honest limits*).
- **An unrecoverable guard is named, never guessed.** `ALTER` switches and computed
  `GO TO DEPENDING ON` produce a branch whose *existence* is a fact but whose test was
  not recovered. Those entries carry `unrecoverable: true` and no `expr`.
- **Origins carry no conditions.** Deliberate: an origin reaches a field through a
  *chain* of assignments, so its true condition is the conjunction along the whole chain.
  Tagging it with any single link's condition would look like the answer without being
  it.
- **An unreachable write site** is marked `unreachable` on its `changedBy` entry rather
  than given an empty condition list — it has no path condition *because it has no path*,
  and `conditions: []` there would read as "this always happens".

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

Every row here is unconditional — `LINEAGE` is straight-line, so `conditions` is absent
throughout. See `examples/condlin.cbl` for the conditional shapes: a guarded write, an
`IF`/`ELSE` that rejoins (and is therefore *not* conditional), a `WHEN OTHER`, and a
disjunction that gets refused.

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

### How `conditions` is recovered

Two more passes over the same graph, both exact where they can be and silent where they
cannot:

1. **Per-edge conditions.** A transition list is *first-match-wins*, which is exactly how
   COBOL's `IF`/`ELSE` and `EVALUATE`/`WHEN` lower here: branch *i* is taken when its own
   guard holds **and every guard before it failed**. So an unguarded trailing branch — the
   `ELSE`, the `WHEN OTHER`, the loop body — has the conjunction of those negations as its
   real condition. Recovered, not guessed.
2. **MUST** (meet = intersection): the guards on *every* path to a point. This is what
   gets reported, and it is why it is always sound.
3. **MAY** (meet = union) is computed only to check whether an empty MUST is honest. A
   guard in MAY-but-not-MUST in *both* polarities says nothing — the branches reconverged
   (after `IF A ... ELSE ...` everything downstream has both `A` and `NOT A` behind it),
   or it is loop history. One that survives in a *single* polarity means the real
   condition is a disjunction, and that sets `conditionsPartial`.

A PERFORM's synthetic return edge inherits the condition of the real edges it stands in
for, so `IF X ... END-IF` at a paragraph's tail keeps its `NOT X` on the way out and the
merge below the call is correctly seen as unconditional.

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
  one. That is the safe direction for provenance, and it is flagged when it occurs. The
  same merge is what makes `conditions` correctly *drop* to the guards both call sites
  agree on, rather than claiming either one.
- **`conditionsPartial` can miss a disjunction inside a loop.** The check relies on a
  guard appearing in one polarity only, and loop history puts most in-loop guards in both.
  So two `IF`s performing the same paragraph *inside a loop body* report the loop guard
  and stay quiet about the rest. Everything reported is still true — the "and that's all
  of it" part is best-effort, which is why the output's own note says so.
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
