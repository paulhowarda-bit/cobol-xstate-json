# The state graph: making state, not programs, the axis

**Status:** a build spec. Part 1 lands in this repo; Part 2 is a separate tool.
Written to be built by someone who was not present when it was designed — every claim
below carries the evidence it rests on, and every hazard names the failure it prevents.

## Why

A single piece of state — say a running collected balance — is affected by **many**
programs. So **the new system's application boundaries will not match the old program
boundaries.** That is the central fact of a mainframe migration, and it is the thing this
tool currently cannot help with.

Everything cobol-xstate emits today is on the **program axis**: *what does CUSTRPT do?*
Each artifact describes one program. What a migration actually needs is the transpose —
the **state axis**: *what happens to the balance, across everything?*

Cluster programs by the state they touch, and **the clusters are the candidate service
boundaries** — derived from the code, rather than inherited from the old job schedule.
COBOL programs are organised around processing schedules and file layouts; services
should be organised around the state they own. Program-per-service is the wrong
decomposition, and it is the default one teams fall into.

## The shape of the answer: a graph

The data is a graph — programs × state × copybooks × tables × events — and the questions
are graph queries:

- *Which programs affect the balance?* → one hop.
- *Where are the service boundaries?* → **community detection** over the program↔state
  bipartite graph. Neo4j's GDS library does this properly (Louvain, label propagation);
  it is a solved algorithm, not something to hand-roll.
- *What feeds what?* → a path query.

**Neo4j, not a report.** A fixed report can only answer questions its author
anticipated. At corpus scale (millions of lines, thousands of programs) a JSON report is
not a viable artifact anyway. And the graph makes the identity rule *structural* rather
than a policy someone has to enforce — see below.

## The identity rule: provable only

The hard problem is knowing when two programs' fields are **the same state**. Program A
calls it `WS-BALANCE`; program B calls it `CUST-BAL`. Three mechanisms prove sameness
from the code:

| Programs share state via | Provable from COBOL alone? | How |
|---|---|---|
| **Copybook** — both `COPY CUSTREC` | ⚠️ **partly** | `data[f].member == "CUSTREC"` is captured — but a member name is unique only *within a library*, and `COPY ... REPLACING` renames fields per program. Needs the copybook library + SYSLIB order. See [mainframe-artifacts.md](mainframe-artifacts.md). |
| **File** — both read `CUST-FILE` | ❌ **no** | `data[f].file` is a *program-local* name, and `assign` is a ddname. The identity is the **dataset**, and it exists only in the JCL. See [mainframe-artifacts.md](mainframe-artifacts.md). |
| **Db2 column** — both `SELECT BAL FROM CUST` | ✅ **now** | the mapping `BAL → CUST-BALANCE`. **This was Part 1b (done).** DCLGEN, where it exists, states it authoritatively rather than by positional inference. |

**This table was wrong when first written**, and in the dangerous direction: it marked
File ✅ and Copybook ✅ on the strength of keys that are program-local. A missing join is a
work item; a **false** join asserts that two programs share state when they do not. A
generic ddname (`SYSUT1`, `INFILE`) or a member name reused across two teams' libraries
will produce exactly that. Resolving them needs artifacts outside the COBOL — that
inventory, and the order to build it in, is
**[mainframe-artifacts.md](mainframe-artifacts.md)**, which is a prerequisite for Part 2,
not a follow-on to it.

**Anything not provable is reported as unresolved, never guessed.** No name-similarity
heuristics, no alias files. A field that reaches no shared node is a work item for a
human, not an assumption the tool makes on their behalf. This is the same rule the rest
of the tool follows: an honest "we cannot prove this" beats a plausible lie in a
contract.

In the graph this rule needs no enforcement — it *is* the schema. Two fields are the same
state **iff they reach a common node**. Unresolved fields are simply isolated:

```cypher
MATCH (f:Field) WHERE NOT (f)-[:DECLARED_IN|MAPS_TO|IN_RECORD_OF]->()
RETURN f.program, f.name   // the work list
```

---

# Part 1 (this repo): emit the join keys

The corpus tool reads only the published JSON, so **the bundle is a published
interface**. It must carry everything the graph needs. Today it does not.

## 1a. Lineage rows must be joinable — `lineage.py`

**The gap.** A lineage row today is:

```
event, direction, endpoint, endpointType, verb, state, line, field, pic, section,
changedByProgram, origins, changedBy?, unknown?, note?
```

There is **no `program`, no `member`, no `file`**. Concatenate rows from N bundles and
you lose *which program they came from* and *the copybook that is the only provable
shared identity for a field*. `program`/`source` exist once at the top level of the file,
which does not survive a concatenation.

**The fix.** `_row` (`lineage.py:349`) already does `item = self.data.get(fu) or {}` and
reads `section` from it, so this is a two-line addition at the same site:

- `program` — from the machine's `program_id`
- `member` — `data[f]["member"]`, the copybook
- `file` — `data[f]["file"]`, for FILE SECTION items

## 1b. The SQL column ↔ host-variable mapping — `parser.py` → `interface.py`

**The gap.** `EXEC SQL SELECT NAME, BAL INTO :CUST-NAME, :CUST-BALANCE FROM CUST` yields
`fields=[CUST-NAME, CUST-BALANCE]`, `endpoint=CUST` — but **not that column `BAL` maps to
host variable `CUST-BALANCE`**. So if A reads `BAL INTO :WS-BALANCE` and B reads
`BAL INTO :CUST-BAL`, nothing proves they are the same balance. A running collected
balance is almost certainly Db2, so this gap blocks precisely the analysis this document
exists for.

**Parse it in `parser.py`, not `interface.py`.** `ExecStmt.text` is space-joined
*tokens*, not verbatim SQL (`parser.py:968`):

```
'SELECT NAME , BAL INTO : CUST-NAME , : CUST-BALANCE FROM CUST WHERE ID = : CUST-ID'
```

Re-parsing that string loses paren depth, which `SUM(A,B)` and `SUBSTR(X,1,3)` need — a
naive comma split breaks on both. Parse in `parse_exec` where the token list still has
structure; carry the result on a new `ExecStmt.columns`; have `_exec_action`
(`statechart.py:461`) put it on the `kind:"input"` spec; have `_classify_exec` *read*
`spec["columns"]` rather than re-parse text.

**Shapes, in order of value:**

| Shape | Correlate | Notes |
|---|---|---|
| `UPDATE t SET c = :h` | **explicit pairwise** | best fidelity of all — no positional guessing |
| `SELECT c1, c2 INTO :h1, :h2` | positional | strip `DISTINCT`/`ALL`; `BAL AS B` → the column is `BAL` |
| `DECLARE cur CURSOR FOR SELECT cols FROM t` + `FETCH cur INTO :hs` | positional, cross-statement | columns are on the DECLARE, host vars on the FETCH. Extend `_cursor_tables` (`interface.py:485`) with a **parallel** `_cursor_columns` map — do **not** widen its return type; three call sites depend on it (`interface.py:512`, `lineage.py:122`, `business.py`) |
| `INSERT INTO t (cols) VALUES (:hs)` | positional | zip the *whole* VALUES list — literals and `CURRENT DATE` occupy slots. `INSERT INTO t VALUES(...)` with no column list: not correlatable |
| `DELETE FROM t WHERE c = :h` | predicate only | columns map to *params*, not fields |
| `SELECT *`, `INSERT … SELECT`, dynamic SQL (`PREPARE`/`EXECUTE`) | **not correlatable** | needs a Db2 catalog. Flag; never guess |
| `SUM(X)`, `A + B` | slot yes, identity no | record the expression text with `derived: true` |

**Two hazards that must gate the zip.** Both are real in the current code:

1. **Indicator variables.** `INTO :WS-NAME:IND-NAME, :WS-BAL` — `_exec_into_vars`
   (`parser.py:1002`) has no notion of indicators and returns **three** host vars for
   **two** columns. A naive positional zip maps `BAL → IND-NAME`: **silently wrong
   lineage**, which is worse than none.
2. **Host structures.** `INTO :CUST-REC` — one host var, N columns.

→ **Require `len(columns) == len(into_vars)` before emitting a mapping.** Otherwise emit
none, plus a flag.

**The trap that would make this silently do nothing.** `build_interface.add()`
(`interface.py:523`) **rebuilds the event dict key by key**. A new `columns` key on the
classification hit is dropped unless it is explicitly copied there. `lineage.py` and
`business.py` call `_classify` directly and see the raw hit — so the feature would appear
to work in two of three places, which is the worst possible failure mode.

**Additive only.** Leave `fields`/`params` alone; that breaks none of the pinned SQL
tests. (Re-splitting UPDATE's SET vs WHERE *would* break `test_sql_fixtures.py:56` — a
separate change, out of scope.)

**Also worth fixing here (pre-existing):** `_SQL_FROM` (`interface.py:52`) on
`FROM SCHEMA . TABLE` captures `SCHEMA`, not `TABLE` — the lexer splits `.` into its own
token.

## Verification (Part 1)

- New fixture `sqlcols.cbl`: a SELECT with an alias and a `SUM()`, an `UPDATE ... SET`, a
  cursor `DECLARE`/`FETCH` pair, and an indicator variable.
- Assert `BAL → CUST-BALANCE` is present with its table; the indicator case emits **no**
  mapping plus a flag; `SELECT *` flags; and the mapping survives into
  `interface.events[].columns` (proving the `add()` trap was handled).
- Assert a lineage row carries `program`/`member`/`file`, and `member == "CUSTREC"` for a
  copied item.
- Full suite green; `--target js` and the golden masters byte-identical.

---

# Part 2 (separate repo): bundles → Neo4j

A new tool — e.g. `cobol-graph`. It reads **only the published JSON** (`prog.json` +
`prog.lineage.json`): no COBOL parsing, no import from this package. That is why it is a
separate repo rather than another target — it needs none of the compiler's internals, and
the JSON contract is a real interface between the two projects.

Nothing corpus-level exists today: there is no batch driver and no manifest writer in the
repo.

## Model — the identity rule is the schema

```
(:Program  {id, source})
(:Field    {name, pic, program})       program-local
(:Copybook {member})                   the shared identity for WORKING-STORAGE fields
(:Endpoint {name, type})               file / db2 table / queue / program
(:Column   {table, column})            ← Part 1b makes this possible
(:Event    {name, direction})

(:Field)-[:DECLARED_IN]->(:Copybook)          provable ONCE the library resolves the member
(:Field)-[:MAPS_TO]->(:Column)                provable   ← Part 1b (done)
(:Field)-[:IN_RECORD_OF]->(:Endpoint)         NOT provable until a Dataset resolves it:
(:Endpoint)-[:BOUND_TO {job, step}]->(:Dataset {dsn})    ← from JCL; the real file identity
(:Program)-[:WRITES {action, line}]->(:Field)
(:Program)-[:READS]->(:Field)
(:Program)-[:CONSUMES|PUBLISHES]->(:Event)
(:Program)-[:CALLS]->(:Program)               + the caller inversion
(:Field)-[:ORIGIN {maybe, resolvedBy}]->(:Event)
```

Two fields are the same state **iff** they reach a common `(:Copybook)`, `(:Column)`, or
record `(:Endpoint)`.

**The caller inversion.** A `LINKAGE SECTION` says *"someone will pass me this record"*;
it never says who. From one program the caller is unknowable. But every program names the
programs *it* calls, so joining backwards across the corpus yields callers — and a
caller's `CALL 'ME' USING WS-A` can be matched positionally against this program's
`LK-FIELD`, extending lineage across the program boundary. Dynamic `CALL`s whose target
cannot be constant-proven leave holes; those are already flagged per-program.

## Deliverables

- A loader: bundles → CSV/Cypher.
- The schema and its constraints.
- A query cookbook:
  - **who touches the balance** — the headline
  - the system topology (who feeds whom, joined on endpoint)
  - the system boundary (endpoints nothing in the corpus writes = data from outside)
  - the gaps (programs called but not in the corpus = the work list)
  - the unresolved list (fields reaching no shared node)
  - **GDS community detection over program↔state = candidate service boundaries**

## Verification (Part 2)

Load the example bundles and assert:

- the balance query returns exactly the programs touching `CUSTREC.CUST-BALANCE`
- `SQLLOAD ⇒ ACCOUNT ⇒ SQLUNLD` appears in the topology
- a known-unshared field lands on the unresolved list

## Out of scope

- **Name-similarity guessing / alias files.** Decided against: provable identity only.
- **Re-splitting SQL UPDATE `fields` vs `params`** — breaks a pinned test; separate change.
- **Anything needing a Db2 catalog** (`SELECT *` expansion) — flagged, not guessed.
