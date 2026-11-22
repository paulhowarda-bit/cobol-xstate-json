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
  `suggestedName: null`. Each edge's `meta` names its transition — `{"transition": i}`,
  an index into `transitions` (`{"entry": i}` into `entry` on the `__ENTRY__` edges) —
  where its route is kept once. XState allows only one `guard` per transition: an edge
  with one guard is labelled with that guard's name, and one with more by
  `guards#k`, `k` being the route its `meta` names. Terminals are `type: "final"`. A synthetic **`__ENTRY__`** state fans out to the first
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
- **Routes are ids into tables, each thing written once** (`formatVersion` 5). Each
  `entry` and `transitions` item has `via` (the technical states it collapsed, in order)
  and `guards` (every guard on the route, in order) as ids: `via` indexes `stateNames`,
  `guards` indexes `guards`, whose entries are the guard objects (`name`, `kind`
  business/control, `field`, `condition`). A `guards#k` edge label stands for route
  `k`'s business guards (all its guards if it has no business guard). `cobol_xstate.business.expand_business_view(view)` returns the
  full form — guard objects and state names in place, each edge's `meta` carrying its
  route, labels joined with AND — which is exactly what `formatVersion` 4 wrote.

  Version 4 wrote every route out whole, twice (on the edge and in `transitions`), with
  each guard's condition tree copied at every use. Routes on a report splitter are long
  and genuine — the step from the main paragraph to report k exits k−1 report loops —
  so a 27,627-line splitter's business view was 926 MB, half a million guard objects for
  33 distinct guards. On our 1,000-report reproduction the tables take it from
  782 MB to 18.7 MB as written (283.5 MB to 6.4 MB compact), and
  `expand_business_view` gives back the version-4 document exactly.

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
  under which. A walk runs without knowing what is under its top frame, and when it
  returns out of that frame it carries on, in the same walk, at the return point once for
  each frame that can sit there. A 387-line program nesting 8 levels with 3 call sites
  each went from 157,457 walks (12 s, 65 MB) to well under a second.

  The one exception is a program where a PERFORM could land inside a range already on the
  stack: the recursion check reads the whole stack, so such a program keeps the per-stack
  walk.
- **One transition per business step, not per route.** Past its first step a walk only
  steps through technical states, which carry no business guards, so the only business
  condition a route can have is its first step's: an IF branch, an EVALUATE WHEN. Routes
  that differ only in control guards — which loops they exited, AT END, ON OVERFLOW —
  are the same step, and the view keeps the one with the fewest guards. Each IF branch
  and WHEN arm still gets its own transition to every business state it reaches, even
  when another branch reaches the same state. A place already walked from a state under
  the same business guards is not walked again, from any of that state's call contexts.

  Before this, every combination of loop exits was its own transition: k loops per
  paragraph, d levels deep, gave k^(d-1) transitions between one pair of states. A
  report splitter whose shared read paragraph returns into a thousand report loops
  produced millions, and was killed at 16.8 GB; its shape now takes 143,131 walk steps
  and 3,062 transitions (1.2 s).
- **A transition shows its shortest route from any call context.** The walks from one
  state's call contexts share where they have been, so each place belongs to whichever
  context reached it first, and its route was the one kept. From a read paragraph
  PERFORMed under a thousand report loops, the context under report 1 reached every
  later report first, so the transition to report 1,000 listed 999 loop exits in its
  guards and `via` — on every such transition, which made the splitter's business view
  808 MB. A state with more than one call context is now walked once more, from all of
  them together in order of guard count, and each transition takes the route with the
  fewest guards from any of them (one loop exit here: 4.3 MB). Which transitions there
  are does not change, only the route each one shows.
- **Overlapping PERFORM ... THRU ranges are walked once.** Restart-from-step code
  PERFORMs STEP-k THRU STEP-EXIT for every k, so each range is a different call context
  at every step it holds, and every step was walked once per range holding it: 400 steps
  needed 643,997 walk steps and were refused at the budget. Where no PERFORM can be
  reached any more, a frame's range is refined to the part of it still reachable - at
  STEP-j every earlier range is STEP-j THRU STEP-EXIT - and frames that agree on that,
  on their return point and on what they sit on become one context. 400 steps now take
  2,403 walk steps (0.2 s, was 18 s); the view is unchanged, byte for byte.
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
