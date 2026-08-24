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

`--target reactive` writes two files - one lowering, two encodings:

| File | For |
|---|---|
| `prog.reactive.json` | **drawing / reviewing** - the XState config plus `interface` and `manifest`. Same shape as the other machine views, so the renderer draws it identically. The waits and publishes on this chart *are* the new system's message contract. |
| `prog.reactive.mjs` | **running** - the same config plus the decimal ops, guards and `recvOps`, so it executes under stock XState by sending events. |

A test asserts the two carry an identical machine, so the picture cannot drift from the
thing that runs.

```bash
cobol-xstate prog.cbl --target reactive
```

## PERFORM: flattened into one machine

A queue delivers events to the **root** actor, and XState does not forward them into
invoked children — so the faithful target's `invoke`-an-actor-per-paragraph shape would
bury every wait where no event could reach it. The reactive target therefore **flattens**:

1. `emitter._invoke_transform` resolves the call structure (reused verbatim — sections,
   `THRU` ranges and all).
2. Each callee's body is inlined into the single machine under its own namespace (a
   paragraph can appear both standalone and inside a `THRU` range; the copies stay
   disjoint).
3. Call/return becomes a **return-address context field** — the mechanism already proven
   for `ALTER` switches: the call site assigns `RET-<para> := '<site>'` and jumps in; the
   callee's return is a state whose guarded edges dispatch back to the right site.

```
call site   entry: [set_ret_1000-INIT_at_0000-MAIN]   always -> 1000-INIT__1000-INIT
1000-INIT__RET   always: [ {guard: ret_1000-INIT_at_0000-MAIN, target: 0000-MAIN__k1} ]
```

Context is then genuinely shared — *more* faithful to COBOL WORKING-STORAGE than the js
target's invoke input/output copying. Every dispatch guard is real and evaluable, never an
external stub, and every edge is guarded: a stall beats jumping somewhere plausible but
wrong.

**Recursion is refused, not flattened.** With one return-address field per paragraph, a
re-entrant call overwrites the address and would return to the wrong place, so a call
cycle raises rather than emitting a broken machine (`--target js` can express it — its
actors are separate copies).

**`STOP RUN` inside a performed paragraph** ends the flat machine, which is what COBOL
does; the js target resumes the caller (its documented limitation). The reactive machine is
the more faithful of the two on that path.

## End of stream

A synchronous `READ` learns the file ended from a return code. Under push there is no
return code, so end-of-stream arrives as its own event: a file-read wait accepts both
`GET.FILE.<X>` and **`END.FILE.<X>`**, and the latter's `recv` raises exactly the `atEnd`
flag the faithful machine's `AT END` guards already read. `NOT AT END` is the negation of
that flag (`negatedExternal`), so it is the per-record path until END arrives.

## Data on arrival

An inbound field is stored through the **same PICTURE rules as any internal `MOVE`** —
a record does not become exempt from COBOL data semantics by arriving as an event. A
`PIC X(20)` field pads; a `COMP-3 S9(7)V99` quantizes. A field the publisher omits leaves
context untouched.

## Scope

Proven end-to-end under stock XState by *sending events* (`tests/test_reactive.py`):

- **`custrpt`** — PERFORM-structured batch read loop: record events + `END` produce
  `WS-TOTAL = 113.20`, the same exact decimal the synchronous golden master gives.
- **`notend`** — `NOT AT END` fires per record; `END` stops it.
- **`retdisp`** — one paragraph PERFORMed from three sites (including from inside an
  inlined `THRU` range) returns to the right one each time.
- **`sectperf` / `thrurange` / `accum`** — sections, `THRU` ranges, `PERFORM UNTIL`.
- **`sqlsel`** — inbound row + `SQLCODE` response branch.
- **`twogets`** — two reads folded in one state split into two waits.
- **`recur`** — refused.

Deliberately deferred (each its own increment, flagged, never faked):

- **`type: parallel` machines** (CICS HANDLE handler regions) — still refused.
- **Create verbs beyond the publish shape**, and `FETCH` cursors / CICS `RECEIVE` / DLI:
  the rules are written generally and the machinery is shared, but only the verbs above
  are proven.
- **Collapsing a singleton SELECT's row + SQLCODE into one event.** The general form is two
  events (row, then response); a singleton could carry both. Kept as two for generality.
