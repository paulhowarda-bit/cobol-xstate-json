# True dynamic calls — `<name>.dynamic-calls.json`

## The question this changes

A `CALL identifier` whose target this program proves constant is not really a dynamic
call: `analysis.py` resolves it, and the callee becomes an ordinary dependency that gets
fetched like any other. What survives that are the **true** dynamic calls — the target is
genuinely determined at run time.

Until now the tool said exactly that, and stopped:

> `program WS-SUBPGM: dynamic target - WS-SUBPGM is a data item whose run-time value
> names the program; not resolvable from this program alone`

Honest, and nearly useless. It tells a migration team an edge exists without telling them
where to go and find it. But the question *is* answerable — just not in the form it was
asked. Turn it around:

> This program cannot tell you **which** program it calls.
> It can tell you exactly **where the name comes from**.

`CALL WS-SUBPGM` is preceded by `MOVE CTL-PGM-NAME TO WS-SUBPGM`; `CTL-PGM-NAME` is a
field of the record read from `CTL-FILE`; `CTL-FILE` is ddname `CTLDD`; the JCL binds
`CTLDD` to `PROD.PARM.CNTL`. **That dataset is where the call graph is written down.**
Read it and the edge resolves — not by static analysis, but by looking at the one artifact
the program itself is looking at.

## What a row says

```json
{
  "item": "WS-SUBPGM",
  "names": "program",
  "verb": "CALL",
  "line": 21,
  "why": "WS-SUBPGM is a data item, not a program name: the target is whatever value it
          holds when the CALL runs, and this program does not fix it - WS-SUBPGM is
          declared but never assigned a literal; target runtime-determined",
  "sources": [{
    "artifact": "CTL-FILE",
    "kind": "file",
    "ddname": "CTLDD",
    "dataset": "PROD.PARM.CNTL",
    "how": { "verb": "READ", "field": "CTL-PGM-NAME", "line": 17,
             "statement": "READ CTL-FILE" },
    "chain": [
      {"from": "CTL-PGM-NAME", "to": "WS-HOLD",   "cobol": "MOVE CTL-PGM-NAME TO WS-HOLD"},
      {"from": "WS-HOLD",      "to": "WS-SUBPGM", "cobol": "MOVE WS-HOLD TO WS-SUBPGM"}
    ]
  }]
}
```

Three questions, three answers:

1. **Is it truly dynamic?** Resolved targets are not in this view at all. A row's presence
   is the claim that constant propagation failed, and `why` carries the analysis's own
   reason — *two literals reach it*, *a variable assignment also reaches it*, *it is never
   assigned a literal at all* are three different situations with three different fixes.
2. **Which artifact supplies the name?** `sources`, traced by a backward walk over the
   same flow-sensitive reaching-origins fixpoint the lineage view uses — so only sources
   that actually reach *this* call are listed.
3. **How does the name get here?** `how` is the retrieval (verb, statement, and the field
   the value lands in); `chain` is every assignment between there and the CALL, in source
   order.

## The last mile: `extract`

Naming the artifact and the field still leaves someone to work out how to actually get
the values. Both halves of that are derivable from what we already parsed, so each source
carries an `extract` block:

- **Db2** — `SELECT DISTINCT HANDLER FROM ROUTING`. Every distinct value the column holds
  is a possible target; the live table *is* the authoritative call graph.
- **A file** — the field's byte position, `bytes 5-12 of the 78-byte record`, because a
  flat dataset has no column headers to look a field up by.

The byte position comes from `storage.py`, and it is **withheld whenever the arithmetic
is not fully determined** — `OCCURS DEPENDING ON`, a `REDEFINES` overlay, a `SYNCHRONIZED`
item (slack bytes depend on where the record starts), or a PICTURE we could not read.
When withheld you still get the ordered field layout with every PICTURE, which is enough
to count by hand, plus the reason. The rule behind that:

> A wrong offset is indistinguishable from a right one. The reader finds garbage and
> blames the data.

`SIGN IS SEPARATE` is the exception that proves it — an exact, knowable +1 byte, so it
does *not* block a position. (`SYNCHRONIZED` and `SIGN IS SEPARATE` are parsed for this
purpose alone; nothing else in the tool reads them.)

## What it will not do

**It never guesses the target.** A control file's *contents* are run-time data. Naming the
artifact is a fact; enumerating what it might contain is a fiction. The row points at the
evidence and stops — there is a test asserting no program name appears anywhere in the
output for the dispatcher above.

## Candidates: fetched, graded, kept out of the manifest

When literals *do* reach the item, they are retrieved — a candidate is a real member name
and having it locally beats not. But they appear **only in `fetch.json`**, never as
program rows in `artifacts.json`, because the manifest's value is that everything in it is
a proven dependency. Candidate rows carry `forDynamicCall` (which item they belong to) and
an `evidence` grade:

| `evidence` | Meaning |
|---|---|
| `assigned` | a `MOVE` or `VALUE` clause provably stores this literal |
| `declared-88` | an `88`-level names it, but **nothing** proves it is ever stored |

That second grade is the `88 WS-POST VALUE 'POSTLOG'` case: it says what the program was
*written to allow*, not what it *does*. Those values are held in `declaredCandidates`,
separate from proven `candidates`, and never counted as the target set — an earlier
version of this view merged them and claimed the result was complete and
inspection-resolvable, which was an overclaim on both counts.

A candidate the estate cannot produce counts as `not-found` like anything else. We had a
concrete name and it wasn't there; how we came by the name doesn't change that.

## The other four answers

"A file feeds it" is only one outcome, and the others must not be printed the same way,
because each sends the reader somewhere different:

| Outcome | What it means | Where to go |
|---|---|---|
| an artifact source | a file/table/queue supplies the name | read that artifact |
| `kind: "caller"` | the item is LINKAGE — the value is passed in | enumerate **this program's callers** and what each passes |
| `kind: "called-program"` | the item is passed BY REFERENCE to a callee, which may write it | **analyse that callee** — the target is decided there |
| `candidates`, no sources | nothing external writes it; only literals reach it | the target is one of a **known set** — better than resolvable |
| `chainBroken` | a REDEFINES alias, reference-modified store, unresolved subscript or unparsed paragraph ended the trace | read the machine; the trace is incomplete |
| `deadEnds` | the chain bottoms out at an item **nothing ever assigns** | usually a **defect**, not an indirection |
| nothing, item undeclared | the item is not in the visible source at all | **check the prefetch report** — a copybook almost certainly did not resolve |

The last two are where a careless implementation does real damage, because both look like
"unresolvable" and neither is.

`deadEnds` is the sharper of the pair. If `WS-PGM` is assigned only from `WS-ROUTE`, and
nothing in the program ever assigns `WS-ROUTE`, the call target is whatever that item was
initialised to — that is a **bug**, and filing it as a modelling limitation buries it. The
repo's own `examples/altswitch.cbl` is exactly this case, and was what caught the first
version of this view reporting it as a missing copybook.

"Not declared anywhere" is nearly always a missing copybook rather than a genuine run-time
indirection. Such a row stays in the view but is marked **`provisional`**, naming the
member that failed to resolve: it rests on an incomplete model, not on a property of the
program, and supplying the member may resolve the target and delete the row entirely.
Presenting it as an equal finding would overstate what we know.

`called-program` deserves its own note. `CALL 'SETUP' USING WS-ROUTE` passes the item BY
REFERENCE, so `SETUP` may write the very name this program then calls — the target is
decided in a *downstream* program, the mirror of the `caller` case. It is marked `maybe`,
because BY REFERENCE means the callee *can* write the argument, not that it does. An
earlier version reported this as an untraceable group-level move, throwing away the one
fact that mattered.

A Db2 source names the **column**, not the host variable: `WS-PGM` is this program's
private name for the value, but `ROUTING.HANDLER` is the database's, and it is what
someone actually goes and selects.

## Where else the answer appears

The same finding is attached to the rows a reader is more likely to hit first:

- **`<name>.artifacts.json`** — the dynamic row gains `namedBy`, and its `needs` text is
  *replaced* (not appended to), because it used to say a reaching-definition trace was
  needed and that trace has now been done.
- **`<name>.fetch.json`** — the skipped row still cannot be fetched, but its reason now
  ends `"the name is supplied by PROD.PARM.CNTL - fetch that instead and read the targets
  out of it"`. A dead end becomes an instruction.

## Scope

Every unresolved dynamic *target* is reported, not only batch `CALL`: `EXEC CICS LINK`/
`XCTL`/`START` with a `PROGRAM(data-name)` or `TRANSID(data-name)` operand is the same
problem with the same answer. `<dynamic-sql>` (a `PREPARE`/`EXECUTE` whose statement text
is assembled at run time) gets its own row explaining that the operation and tables are
not statically knowable at all.
