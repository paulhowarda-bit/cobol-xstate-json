# The business view (`--target business`)

## What it is

A **read-only distillation** of the faithful machine into the *business* state machine — the
states that matter from a business viewpoint — with the technical scaffolding collapsed away.
It invents nothing: every surviving state and transition traces back to the faithful machine,
and every business *name* is left as a fill-in for a human (or an LLM) to supply, because
mapping COBOL identifiers to business vocabulary is the one step the pass cannot infer.

It is a **projection**, not a rewrite — orthogonal to `--target reactive` (that changes *how*
boundary I/O happens; this changes *which states you see*). The business view is derived from
the same validated IR, so it inherits its trust.

## State classification

Each emitted state is classified:

| Role | Meaning |
|---|---|
| **boundary** | a perimeter state — file / Db2 / console / terminal / caller / CICS I/O (from the interface overlay) |
| **decision** | branches on a **business** condition — a guard that is *not* a loop `UNTIL_…`, a file `…_atEnd`, or an unmodeled `{op:'raw'}` control guard |
| **boundary+decision** | both (e.g. a state that reads a Db2 response *and* branches on `SQLCODE`) |
| **terminal** | a final state (program end) |
| **technical** | none of the above — loop mechanics, a `CONTINUE` no-op, a `perform_` call site, sequence scaffolding. **Collapsed away.** |

## The collapse (call/return aware)

The business flow is built by reachability over **configurations** `(state, call-stack)` from
the program's initial state:

- **PERFORM is a call.** `perform P` in a state's entry pushes a frame and jumps to `P`'s
  entry; when control leaves `P`'s owned paragraph(s) — a fall-through past the range, a GO TO
  out, or the `__END__` sentinel — that is the **return** (pop the frame, resume at the saved
  continuation). This reuses the emitter's `_target_owner` / `_reroute_to_return` conventions,
  so the business flow matches how the runnable machine lowers PERFORM to `invoke`/`__RET__`.
- **Technical states are transparent.** Walking an edge, the pass steps *through* technical
  states (recording them in the transition's `via` list) until it reaches the next
  business/terminal state — then emits one collapsed edge, carrying the guards/events seen
  along the way, each labelled `business` or `control`.
- A final at the **top level** (empty stack) is program end; a final reached **inside a call**
  is a return.

So a dispatcher like `banktran` — `PERFORM 2000-DISPATCH … EVALUATE WS-TRAN-TYPE WHEN 'D'
PERFORM 2100-DEPOSIT …` — distills to: read a transaction → **decide type** → post deposit /
process withdrawal / report inquiry / reject → loop back to the read → close on end-of-file.
Its ~23 faithful states collapse to ~9 business states, the dispatch fan-out recovered by
following the `perform_` calls.

## Output

**A real XState v5 config** (`format: "xstate-v5-config"`), so anything that renders the
faithful bundle renders this identically — no special-casing. A projection of a state
machine *is* a state machine, and this is the view a human actually wants to look at, so
it must be drawable.

```bash
cobol-xstate prog.cbl --target business   # -> prog.business.json + prog.lineage.json
```

- **`machine`** — `{id: "<PROG>__business", initial, states}`. Each state carries its
  distillation in **`meta`**: `role`, `boundaryActions`, `decisions`, `internalSteps` (the
  stripped-out MOVE/COMPUTE detail), `cobol` provenance, `perimeter`, and
  `suggestedName: null`. Each edge carries `meta.via` (the technical states it collapsed),
  the full `meta.guards` list (XState allows only one `guard` per transition, so the
  readable label rides on `guard` and the detail stays in meta), and `meta.label: null`.
  Terminals are `type: "final"`. A synthetic **`__ENTRY__`** state fans out to the first
  business state(s) — the collapse can reach several under different guards, which a
  single XState `initial` cannot express.
- **`interface`** — the external perimeter, in the same shape as the faithful bundle's:
  `endpoints` (typed external actors), `events` (each with `direction`, `endpoint`, the
  **`fields`** crossing, and `params` for data flowing the other way), and `parameters`
  (the caller contract). A renderer draws the boundary here exactly as it does there.
  Each boundary state's `meta` also carries `perimeter`, `inputFields` and
  `outputFields`, so a consumer reading only `machine` sees the data crossing without
  joining to `interface`. Boundary states are never collapsed, so an event's host
  survives; the program's own parameter events re-anchor to `__ENTRY__`.
- **The report keys remain** for querying rather than drawing: `businessStates` (each
  boundary action carrying its typed `fields`), `entry`, `transitions`, `collapsed`
  (every removed state, with its reason), `counts`, `nameFillIn`, `flags`.

### Limitation: parallel machines

A program with DECLARATIVES or CICS `HANDLE CONDITION` compiles to a `type: parallel`
machine (a PROGRAM region plus an orthogonal HANDLERS region). The business pass does not
descend into regions, so it yields an **empty view** and flags
`type:parallel (handler regions) - business view not lowered`. Use `--target json` for
those programs until this is lowered.

It is a **view, not a runnable machine** — the collapsed steps are summarised in `meta`,
not executed. Use `--target js` to run anything.

The field-lineage companion (`prog.lineage.json`) is written alongside, since the two are
read together: the business view shows *which steps matter*, the lineage table shows
*where each field's value came from*.

## Honest limits (flagged, never faked)

- **GO TO out of a performed paragraph** is modeled as a *return* (as the runnable machine
  does — provenance-stripping makes a GO TO indistinguishable from a fall-through), so a
  genuine cross-paragraph jump can be routed to the caller instead of the target. Flagged when
  any GO TO is present.
- **`type: parallel`** machines (CICS HANDLE / DECLARATIVES handler regions) are not lowered —
  flagged.
- **Recursive PERFORM**, **multiple PERFORMs in one state**, and **unresolved PERFORM targets**
  are flagged and the call is not followed.
- **Nested PERFORMs are walked per call site, not per call path.** Distinct stacks of
  PERFORMs multiply — k call sites per level, d levels deep, is k^d of them — so the flow
  carries only the top PERFORM of each stack. A pre-pass records which call site can sit
  under which. A walk runs without knowing what is under its top frame, and if it ever
  returns out of that frame, it is rerun once for each frame that can sit there. So
  every finished walk is exactly the walk from each real stack it stands for. The
  transitions are the same set as walking every stack, but their order may differ, and
  so may which of several collapsed paths is kept as an edge's `via`. A 387-line program
  nesting 8 levels with 3 call sites each went from 157,457 walks (12 s, 65 MB) to 737
  (0.1 s, 1 MB), and 16 levels takes 0.3 s.

  The one exception is a program where a PERFORM could land inside a range already on the
  stack: the recursion check reads the whole stack, so such a program keeps the per-stack
  walk.
- **A view that needs too much work is refused, not cut short.** The walk has a budget
  of 250,000 steps (`--business-budget`, `analyze(business_budget=)`; `0` means no
  limit). A view that needs more raises `ViewBudgetExceeded`, and nothing partial is
  written: a view cut off at its budget would read as complete. The run logs an ERROR
  saying how far the walk got, the other artifacts are written as usual, and re-running
  with the budget lifted gives the full view. It is a count rather than a memory reading,
  so the refusal lands at the same place on every machine.
- **Naming is not inferred** — `suggestedName` / `label` are always `null`. A pure-calculation
  program (no boundary crossings, no business decisions) correctly distills to ~no business
  states: it has no business *state* changes, only data transformation.
