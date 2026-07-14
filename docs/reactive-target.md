# The reactive target (`--target reactive`)

## What this is

The shipped deliverable of cobol-xstate is a **reactive** XState v5 machine: one whose
boundary I/O is *event-driven* rather than synchronous. The synchronous "faithful" machine
(`--target js`) stays as an internal, golden-master-tested stage and verification oracle.
The reactive machine is a **mechanical lowering** of the perimeter overlay
(`src/cobol_xstate/interface.py`) over that faithful machine — it inherits trust *through*
the validated IR; it is never regenerated from raw COBOL text.

Pipeline:

```
COBOL --> faithful IR (validated, internal, golden-master tested) --> reactive lowering (shipped)
```

The lowering rewrites only the ~5–15% of states that cross the program boundary (the
`perimeterStates` of the overlay). Internal `always` control flow — the guarded IF/EVALUATE
structure between boundary states — is left exactly as the faithful machine emits it.

## The deployment model (decided 2026-07-14)

The target deployment is fully event-driven (queues + async services). Three rules, one per
direction, fix the lowering:

| Direction | COBOL verbs | Reactive shape |
|---|---|---|
| **Inbound (get)** | `READ`, SQL `SELECT`/`FETCH`, `ACCEPT`, CICS `RECEIVE`, DLI `GU`/`GN` | **push**: the state *waits* `on:{ <GET-EVENT> }`; the record arrives as an event |
| **Outbound (create)** | `WRITE`/`REWRITE`/`DELETE`, SQL `INSERT`/`UPDATE`/`DELETE`, `DISPLAY`, CICS `SEND`, `CALL`/`LINK`/`XCTL` | **publish, fire-and-forget**: an entry effect publishes; control does *not* await |
| **Response** | branch on `SQLCODE`/`SQLSTATE`/`EIBRESP` | the response returns later as its *own* inbound event; a guarded transition consumes it |

The event names are exactly the overlay's: `GET.<ENDPOINTTYPE>.<ENDPOINT>` and
`CREATE.<ENDPOINTTYPE>.<ENDPOINT>` (e.g. `GET.DB2.CUSTOMER`, `GET.RESPONSE.DB2`,
`CREATE.FILE.TRAN-FILE`). Consumers wire real queues/services to these names.

### Ordering assumption (must be stated, not silently relied on)

Under the synchronous target, invoke-and-await preserved COBOL's happens-before ordering
(e.g. a running total across sequential reads stayed ordered) *for free*. Under **push**, the
machine no longer enforces that ordering — XState processes one event at a time, so per-event
work stays ordered, but the *sequence of records* is only ordered if the **event source
delivers them in order** (a queue/partition property). This is an explicit deployment
contract, surfaced in the emitted manifest, not an invariant the machine guarantees.

### Error model

Service failures and non-zero response codes surface as **response events feeding guarded
transitions** — the same `SQLCODE`/`EIBRESP` guards the overlay already captured. A failed
outbound publish, if the program cares, comes back as a later inbound response event too.
There is no separate per-invoke error state; this mirrors how COBOL already inspects response
codes inline, so the faithful guard structure carries over unchanged.

## The rewrite, precisely

For each perimeter state `S` (driven off `interface.perimeterStates` + `interface.events`):

1. **Inbound data get** (`GET.<db2|file|console|terminal|ims>.<endpoint>`):
   - Drop the synchronous read/exec action from `S.entry` (identified by re-classifying each
     entry action through `interface._classify`). Non-read entry actions (e.g. a `MOVE`) stay.
   - The transition(s) `S` took *after* the read become the `on` handler. If `S.always` was a
     single unconditional edge, the handler targets that edge's target directly; otherwise the
     original guarded `always` edges move to a synthetic `S__ready` state and the handler
     targets `S__ready`.
   - The handler runs a generated `recv_<EVENT>` action that assigns the event's `fields` (the
     `INTO` host variables / record fields) from the event payload into context.
   - `S.always` is removed — `S` now *blocks* until the event arrives.

2. **Response get** (`GET.RESPONSE.<DB2|CICS>`):
   - `S`'s guarded `always` edges move to a synthetic `S__ready` state.
   - `S.on = { <RESPONSE-EVENT>: { actions:[recv_<EVENT>], target:"S__ready" } }`.
   - `recv_<EVENT>` assigns the response items (`SQLCODE`, …) from the event into context, so
     the existing **context-based** guards (`SQLCODE_eq_0`, …) evaluate against the delivered
     value — no event-reading guards needed.

3. **Outbound create** (`CREATE.<...>`):
   - The write/send/exec entry action is replaced by a `publish_<EVENT>` effect (a no-op in the
     runnable module; a real deployment injects the publisher). `S.always` is left intact —
     fire-and-forget does not await.

`recv_*` actions take the event (`assign(({context,event}) => …)`); every other action/guard
is reused verbatim from the faithful emitter, so decimal arithmetic and the data dictionary
are unchanged.

## Scope of the first vertical slice

Implemented and tested end-to-end (`examples/sqlsel.cbl`, runs under stock XState by *sending
events*): a flat, single-region SQL `SELECT` — inbound row get + `SQLCODE` response branch.

Deliberately deferred (each its own increment, flagged, never faked):

- **Perimeter states inside a performed paragraph.** A `PERFORM`ed paragraph becomes an
  invoke-actor in the faithful target; delivering external events into a nested actor needs
  event forwarding/spawning and is the next increment. The slice inlines its SELECT to stay
  flat.
- **`type: parallel` machines** (CICS HANDLE handler regions).
- **Verb classes beyond SQL SELECT**: file `READ` loops (AT-END as an end-of-stream event),
  `FETCH` cursors, `ACCEPT`, CICS `RECEIVE`, DLI — and every **create** verb. The rules above
  are written generally; only SELECT is proven so far.
- **Collapsing a singleton SELECT's row + SQLCODE into one event.** The general form is two
  events (row, then response); a singleton could carry both. Kept as two for generality.
