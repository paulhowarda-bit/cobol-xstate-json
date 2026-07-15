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
- **The report keys remain** for querying rather than drawing: `businessStates`, `entry`,
  `transitions`, `collapsed` (every removed state, with its reason), `counts`,
  `nameFillIn`, `flags`.

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
- **Naming is not inferred** — `suggestedName` / `label` are always `null`. A pure-calculation
  program (no boundary crossings, no business decisions) correctly distills to ~no business
  states: it has no business *state* changes, only data transformation.
