# The other artifacts: what the COBOL cannot tell you

Companion to [state-graph-plan.md](state-graph-plan.md). That plan builds the state axis —
*what happens to the collected balance, across everything?* — by joining programs on the
state they share. This document is the inventory of **everything else on the mainframe
that the join depends on**, why each one matters, and what each will lie about if parsed
carelessly.

## How to read this — three kinds of claim, not equally trustworthy

This document was drafted by an AI assistant working in this repository. It contains three
kinds of statement, and an earlier draft presented all three in the same confident voice.
That was the document's worst defect, so they are now labelled:

| Tag | Means | How much to trust it |
|---|---|---|
| **`[repo]`** | Verified by running this tool and reading its output during drafting | Reproducible — the command is cited. Check it in a minute. |
| **`[check]`** | Standard mainframe behavior, recalled from training, **not verified against IBM documentation** | Directionally right; **the details are exactly where an AI is wrong most expensively.** Your own people know this better than this document does. |
| **`[reasoning]`** | An argument, not a fact | Judge it on its merits. It has no authority. |

**The intended reader knows the mainframe better than the author of this document does.**
So the value here is not the JCL tutorial — it is the *reasoning*: which artifact resolves
which identity, what breaks without it, and the order that removes wrong answers fastest.
Read the `[check]` material as *"here is the shape of the problem"*, and take the mechanics
from IBM's references and your own estate.

Nothing here describes **your** estate. An earlier draft contained sentences like *"most
ASM in a COBOL shop is a date routine"* — invention, asserted as fact, about systems the
author has never seen. Those have been removed rather than labelled. Where the right answer
depends on what you actually have, this document says so instead of guessing.

## The thesis

> The COBOL tells you what a program **does**. It cannot tell you what it does it **to**.

`READ CUST-FILE` is complete information about the verb and the fields. It is *no*
information about which dataset was read. That binding — `CUST-FILE` → ddname `CUSTIN` →
`PROD.CUSTOMER.MASTER` — is finished outside the program, in JCL. The same is true of
almost every name a COBOL program uses: they are **program-local**, and the artifact that
makes them **system-global** is somewhere else.

`[repo]` This is not a hypothesis about the tool, it is its current output. For
`SELECT CUST-FILE ASSIGN TO CUSTIN`, the emitted interface endpoint is:

```json
{ "endpoint": "CUST-FILE", "type": "file", "directions": ["get"],
  "assign": "CUSTIN", "organization": "SEQUENTIAL" }
```

The program-local name and the ddname are both there. **The dataset is absent**, and no
amount of COBOL parsing will produce it.

`[repo]` The `--target artifacts` view ([artifacts-target.md](artifacts-target.md)) is this
argument made into a per-program checklist: it lists each such endpoint as a related
artifact, marks the file `program-local`, and records `resolvedBy: "JCL DD statement"` with
a `needs` line for the DSN. It does not resolve the identity — it *names what would*, which
is exactly as far as a single program's source can honestly go. The estate-wide resolution
this document plans is the next step, not that view's job.

`[reasoning]` This is the argument already accepted for Db2 in the main plan: a host
variable is program-local, the *column* is the database's, so the column is the only thing
proving two programs read the same state. Every row below **generalizes that same argument**
to a different artifact. The generalization is the author's, not a citation — it holds up
under the examples given, but it is an argument you should test rather than a finding.
The identity chain is always the same shape:

```
program-local name   →   an intermediate binding   →   the system-global identity
CUST-FILE                CUSTIN (ddname)               PROD.CUSTOMER.MASTER
WS-BALANCE               CUSTREC (copybook)            SYS1.COPYLIB(CUSTREC) + REPLACING
CUST-BALANCE             :host var / DCLGEN            CUSTOMER.BAL
```

**The middle column is not the identity.** Stopping there is what produces false joins.

## The correction this forces on the main plan

`state-graph-plan.md` currently marks two of three identity mechanisms ✅ *provable*. On a
single-program view they are. **At corpus scale both are weaker than stated**, in the same
direction and for the same reason:

| Claimed | Actually |
|---|---|
| **File** — `data[f].file` proves sharing | `CUST-FILE` is a name inside one program. Two programs reading one dataset as `CUST-FILE` and `CUSTMAST` **will not join**. Two unrelated programs that both happen to call theirs `CUST-FILE` **will falsely join**. The DSN is the identity, and it is only in the JCL. |
| **Copybook** — `member == "CUSTREC"` proves sharing | Member names are unique *within a library*, not across them. `CUSTREC` from `TEAM-A.COPYLIB` and `TEAM-B.COPYLIB` are different layouts with one name. Which one a compile saw depends on **SYSLIB concatenation order** in the compile JCL. And `COPY CUSTREC REPLACING ==WS-== BY ==CUST-==` gives *the same layout different field names per program* — so joining on `(member, field-name)` misses; the honest key is `(library-resolved member, position in the layout)`. |

Both are the same failure the Db2 mapping was built to avoid, and the false-join half is
the dangerous one: a missing join is a work item, a **false join asserts two programs
share state when they do not**. Wrong is worse than none — the rule the rest of the tool
already follows.

## The four roles

`[reasoning]` This taxonomy is the author's own, invented for this document — it is an
organizing lens, not a standard anyone else uses. Sorting the estate by what an artifact
*does for the analysis* is more useful than sorting it by technology:

1. **Resolvers** — turn a local name into a global identity. Highest value: without them
   the graph is *wrong*, not merely incomplete.
2. **Hidden behavior** — logic that is not in any COBOL program, but moves the data.
3. **Orchestration** — who runs what, in what order, under what condition.
4. **The boundary** — where the system meets the outside world.

---

# Role 1: Resolvers

Do these **before** the Neo4j loader. The loader's join keys depend on them, and a loader
built on unresolved names will produce a confident, wrong picture of the boundaries.

`[check]` Every artifact in this table is recalled from training, not verified. The
*claim that each one resolves an identity* is the reasoning; the *syntax and semantics* are
yours to confirm.

| Artifact | Binds | Without it |
|---|---|---|
| **JCL / PROC** `//CUSTIN DD DSN=...` | ddname → dataset | File identity is a program-local name. False joins on generic ddnames (`SYSUT1`, `INFILE`). |
| **Copybook library + SYSLIB order** | member → the actual layout | `CUSTREC` is ambiguous across libraries; `REPLACING` renames fields per program. |
| **DCLGEN** | column ↔ COBOL field, *authoritatively* | The mapping is inferred positionally from `SELECT ... INTO` and must refuse indicator variables and `SELECT *`. |
| **Db2 DDL** (`CREATE TABLE`) | the real columns, types, PK/FK | Columns are only known where DML mentions them. **FKs — relationships *between* states — are invisible entirely.** |
| **CICS CSD** `DEFINE FILE(x) DSNAME(y)` | CICS file name → dataset | Online programs have no dataset identity at all. This is the DD binding's online twin. |
| **IMS PSB/PCB + DBD** | PCB mask → segment; PROCOPT → intent | An IMS program's data access is unresolvable. |
| **MQ** `DEFINE QALIAS(a) TARGET(b)` | alias → real queue | Two programs on one queue via different aliases look unrelated. |
| **Binder / link control** | `CALL 'X'` → the module actually bound | The `CALLS` edge stays unresolved for statically linked subprograms. |
| **IDCAMS `DEFINE CLUSTER`** | VSAM dataset, key offset, record size | Which field is the key is unknown. |
| **HLASM macro library** | macro → what it expands to | An ASM program that looks like it touches no data. See [Macros](#macros). |

### JCL — the hazards that matter

`[repo]` A first JCL reader now exists — `jcl.py` / `jcl_views.py`, `parse_jcl` +
`build_jcl_lineage` / `build_jcl_artifacts` (see [jcl-target.md](jcl-target.md)). It resolves
symbolics (SET / PROC default / EXEC override), expands PROCs and INCLUDE, parses `SORT` /
`IDCAMS` control cards to byte-field lineage, keys GDGs on their base, and emits `ddBindings`
(`ddname -> dataset`) — the edge that finally resolves a COBOL program's program-local ddname.
It handles the **common** cases below and **flags** the rest; it does not claim to be the
evaluator this section calls for. Where a hazard is not statically knowable it is flagged,
never guessed — the same rule as everywhere else.

`[check]` Resolving a DSN needs a real evaluator, not a regex. Each of these silently
produces a wrong answer if skipped. **Treat this as a checklist of things to look up, not
as the specification** — the point is that each hazard exists, not that the description
here is precise:

- **Symbolic parameters.** `DSN=&HLQ..CUST.MASTER`, with `&HLQ` coming from a `PROC`
  default, a `SET`, an `EXEC` override, or the scheduler. **Take the precedence rules from
  the IBM JCL Reference, not from this document** — an earlier draft stated an order here
  and the author was not confident it was right. Get it wrong and every DSN you resolve is
  fiction, silently.
- **PROC overrides.** `//STEP1.CUSTIN DD DSN=...` replaces a DD *inside* the PROC. The
  effective binding is the merge, not either half.
- **Concatenated DD.** One ddname, several datasets:
  ```
  //CUSTIN   DD DSN=PROD.CUST.EAST,DISP=SHR
  //         DD DSN=PROD.CUST.WEST,DISP=SHR
  ```
  A naive parse takes the first and silently drops the rest. The program reads **both**.
- **GDG relative generations.** `PROD.CUST(+1)` and `PROD.CUST(0)` are *different*
  generations that resolve against the catalog at run time. **Join on the GDG base**
  (`PROD.CUST`) — that is the stable identity. Treating `(+1)` as a distinct dataset
  fragments one state across every run.
- **`INCLUDE MEMBER=` / `JCLLIB ORDER=`** pull in JCL that is not in the member you are
  reading. Without them the step list is incomplete.
- **Not statically knowable at all** — dynamic allocation (SVC 99, `BPXWDYN`), DSNs built
  from scheduler variables, `DDNAME=` referbacks. **Flag; never guess.** Same rule as
  `SELECT *`.
- **`DISP`** tells you direction the COBOL does not: `DISP=(NEW,CATLG)` means this step
  *creates* the dataset — a producer edge, and often the real start of a pipeline.

### DCLGEN — better than what Part 1b could do

DCLGEN emits a `DECLARE TABLE` and a matching host structure. That is the column↔field
map **stated by the tool that generated it**, rather than inferred positionally. Where
DCLGEN members exist they should *outrank* the positional inference, and they resolve the
indicator-variable case that Part 1b has to refuse. Hazard: a DCLGEN copy can be stale —
regenerate or reconcile against the DDL/catalog, and flag a mismatch rather than picking a
winner.

---

# Role 2: Hidden behavior

Logic that moves data while being invisible to a COBOL-only analysis. **A lineage chain
that runs through one of these is not just missing a step — it is silently broken**, and a
broken chain reads as "nothing feeds this field".

### Utility control cards — the mainframe's other programming language

A sort step is not plumbing. It is a data transformation whose source code is a control
card:

```
//SYSIN DD *
  SORT FIELDS=(1,9,CH,A)
  INCLUDE COND=(10,1,CH,EQ,C'A')      <- a filter: a business rule
  OUTREC BUILD=(1,9,20,8,10,1)        <- a reformat: fields MOVE
```

`INCLUDE`/`OMIT` is a `WHERE` clause. `OUTREC BUILD` rearranges the record — the same
field is at a different offset afterwards. `SUM FIELDS` aggregates. `JOINKEYS` is a join.
Any of these between two programs and the field-level chain is wrong unless modelled.
Same for `IDCAMS REPRO INFILE(A) OUTFILE(B)` — a copy edge between two datasets — and
`IEBGENER` with reformatting cards.

Hazard: control cards are often **not instream**. `//SYSIN DD DSN=PARM.LIB(SORTCRD)`
means the behavior lives in a PDS member you also have to read.

### The rest

| Artifact | Why it matters | Honest limit |
|---|---|---|
| **ASM (HLASM)** | Utility subroutines called from COBOL; `CSECT`/`ENTRY` are the callable names, `DSECT` is its copybook. Also Db2 `EDITPROC`/`FIELDPROC` — ASM bolted to a *table* that silently transforms every row | Interface only, never a chart of the body — see [Which of these need a statechart?](#which-of-these-need-a-statechart) |
| **Db2 triggers / stored procedures** | Business rules that fire on a write, entirely outside the calling program | A program's `UPDATE` may do far more than it says. |
| **Easytrieve / SAS / DYL-280** | Where present, whole report and extract programs — real logic, not glue | Own grammars; same treatment as COBOL. Priority depends entirely on how much of your estate is in them. |
| **REXX / CLIST** | Glue that can allocate, call, and branch | Often the dynamic-allocation culprit. |

---

# Role 3: Orchestration

The old boundaries are not written down in any program — they are in the job stream. Since
the whole premise is that *the new boundaries will not match the old ones*, this is what
tells you what the old ones actually were.

- **JCL step sequence + `COND=` / `IF`** — a job genuinely *is* a state machine: steps are
  states, conditions are guards. Step 1 writing a file that step 2 reads is a real
  program-to-program dataflow edge that **no single-program view can see**. This is the
  one place the statechart output is a natural fit, and it comes nearly free once the JCL
  is parsed for bindings.
  - Hazard: `COND=` is *backwards* (it says when to **skip**) and is a notorious source of
    misreading. `IF/THEN/ELSE` is the modern form. Also `RESTART=` and conditional flushes
    change which steps ran.
- **Scheduler** (Control-M, CA-7, TWS/OPC, Zeke) — job-to-job dependencies, triggers, and
  calendars that **JCL does not contain**. Without it you have the steps inside each job
  and no idea what starts the job, so the batch choreography stops at the job boundary.
- **CICS `RETURN TRANSID(...) COMMAREA(...)`** — for a pseudo-conversational application
  the per-program statechart is a **fragment, not the machine**. The program *ends*; the
  conversation resumes on the next input. The real machine appears only when programs are
  stitched together across those edges, with the COMMAREA as the carried state. The CSD's
  transaction→program map is what makes the stitch possible.

---

# Role 4: The boundary

Where the system meets the world — and therefore where the events in an event-driven
target come from.

- **MQ queue definitions** — these are *already* the event channels. For the reactive
  target this is the highest-value boundary artifact: the modern system's message contract
  has a direct ancestor here. `QALIAS`/`QREMOTE` indirection is also a resolver (see Role
  1).
- **BMS mapsets** (`DFHMSD`/`DFHMDI`/`DFHMDF`) — the screen fields are the online input and
  output boundary, with names and lengths. The COBOL `SEND MAP` / `RECEIVE MAP` names the
  map; the mapset names the fields.
- **CICS TDQ / TSQ definitions** — a TDQ with a trigger level is an *asynchronous
  dispatch*: writing to it starts a transaction. That is an event edge disguised as a
  file write.
- **IMS MFS** — the same role as BMS.

---

# Which of these need a statechart?

`[reasoning]` These three tests are the author's, written during this project — not a
received method. The first version of them got Java wrong (see the note below), which is a
fair warning about how much authority to grant them.

A statechart is expensive and only earns it where **all three** hold:

1. the logic is **business-meaningful**;
2. there is **no trustworthy spec** — the behavior is known only by running it;
3. you must **rewrite it and prove the rewrite equivalent**.

> **Test 2 is not "is the syntax cryptic."** An earlier draft of this document used
> *readability*, and got Java wrong as a result. COBOL's individual lines are perfectly
> readable — `ADD CUST-AMT TO WS-TOTAL` is not cryptic. What forces a chart is control
> flow across thousands of lines with no spec anyone trusts. **Legible syntax is not
> recovered behavior**, and any language can fail this test at scale.

| | Business logic? | No trustworthy spec? | Rewriting it? | Verdict |
|---|---|---|---|---|
| **ASM** | sometimes | yes | usually **replaced**, not rewritten | Interface only. A faithful auto-chart is **not constructible** from ASM anyway — see below |
| **Java** | yes | **yes, at scale** | often | **Needs a machine.** Separate front end, same schema |
| **Macros** | *not a program* | — | — | Expand it; charting it is a category error |

Note the ASM row fails on a *different axis* from the others: even where all three tests
pass, this method cannot produce a faithful chart from ASM, because there is no data
division to recover semantics from. That is a capability limit, not a judgement about
whether it would be useful.

## ASM

**Do not attempt a faithful statechart, even for the parts with real logic.** Not because
it is hard but because the result would violate the tool's own rule. This tool's value is
that the data semantics are recovered exactly — PICTURE, USAGE, packed decimal, the
fixed-point arithmetic. ASM has **no data division**: operands are offsets off a base
register, `USING` is dynamic, and `EX`, branch tables and (legally) self-modifying code
mean the control flow itself is not always static. A chart from that would be a
control-flow graph with **no data meaning** — invented logic wearing a contract's clothes.

What *is* recoverable is exactly what the graph needs:

| Recover | Gives |
|---|---|
| `CSECT` / `ENTRY` names | resolves `CALL 'X'` to a real module |
| `DSECT` | the parameter layout — the fields crossing the boundary |
| what it `CALL`s, which SVCs it issues | its effects, incl. dynamic allocation (a flag source) |

`[repo]` **The tool already models ASM correctly** — it just does not know it. A `CALL`
already produces a `maybe` origin naming the callee that would settle it. Real output, from
`banktran.lineage.json`:

```json
"origins": [ { "event": "CREATE.PROGRAM.POSTLOG",
               "maybe": true, "resolvedBy": "POSTLOG" } ]
```

*This callee may rewrite these arguments; POSTLOG would settle it.* That is already the
honest answer for a black box, and an ASM module is a black box.
Parsing the DSECT does not remove the unknown, it **bounds** it: from "may rewrite
something" to "may rewrite these named fields". An ASM module is an **endpoint**, like a
Db2 table — not a program to chart.

Chart it by hand only where it is real business logic, on the critical path, and someone
will verify the result. The test to apply is **"are we rewriting this, or replacing it?"** —
an ASM date routine or string utility becomes a library call in the new system, and a
rewrite contract for something you are not rewriting buys nothing. A posting engine in ASM
is a different matter entirely. **Only your inventory can say which you have**; this
document cannot, and any claim here about the usual mix would be invention.

**The ASM that will bite you** is the kind nobody calls: a Db2 **`EDITPROC`**,
**`FIELDPROC`** or **`VALIDPROC`** is an ASM routine attached to a *table* that transforms
every row or column value on the way in and out. If one exists, the value the COBOL sees is
**not** the value stored, and no amount of reading the COBOL will reveal it. Same for CICS
global user exits and VSAM exits. These belong on the work list as a lineage hazard, not as
charts.

## Java

**Java needs a machine.** It lives in a JZOS batch step, in CICS Liberty via JCICS, in a
Db2 Java stored procedure, or in WebSphere — and wherever it is, it is a program that
changes state under conditions, which is the same thing a COBOL program is.

**Why the graph alone is not enough.** The tempting answer is "put Java in the state graph
and skip the chart". That produces an **asymmetric model of the very state you are trying
to model**. The graph gives *identity* — Java touches the balance. Only the machine gives
*behavior and conditions*. Merge them and COBOL contributes

> adds to the balance **when** the transaction is a deposit and the account is active

while Java contributes

> touches the balance

Those do not compose into a requirement. The whole point of the state axis is to combine
programs into one account of what happens to a piece of state; a participant with no
conditions is a hole in that account, not a cheap approximation of it. If Java writes the
balance, its rules are *part of the balance's rules*.

`[repo]` **Where the machine comes from: not this tool.** The output schema is
language-neutral — it is JSON, and Part 2 reads only JSON, which is exactly why the bundle
is a published interface. But the *producer* is not neutral, and this was measured rather
than assumed: `Machine` carries
`paragraph_order`, `sections`, `PROCEDURE DIVISION USING/RETURNING` and `FILE-CONTROL`
entries, and every downstream module (`interface`, `business`, `lineage`, `reactive`,
`emitter`) has 30–61 COBOL-specific references — `_classify` reads COBOL verb text
directly. There is no clean seam to hang a Java front end on. **A Java front end is a
separate tool that emits the same four views**, so the machines compose. The contract is
the schema, not the codebase.

**What is genuinely harder in Java** — and these are the ones where a naive port of this
tool's analysis would be *silently wrong*, not merely incomplete:

- **Heap aliasing.** COBOL's WORKING-STORAGE is a flat, fixed layout: every field has one
  name and one place, which is what makes reaching-origins lineage tractable. Java has
  references — two variables can name one object, and a callee can mutate through an alias.
  Honest lineage there needs points-to analysis. Skipping it does not produce gaps, it
  produces confident wrong answers.
- **Concurrency.** COBOL batch is single-threaded, so a sequential machine is faithful.
  A thread pool or async pipeline is not sequential, and a sequential chart of it is a lie.
- **Dynamic dispatch.** `CALL 'LITERAL'` is mostly static. An interface plus dependency
  injection means the call target is chosen at runtime by configuration — the unresolved-
  `CALL` problem, but pervasive rather than exceptional.
- **Decimal semantics.** `BigDecimal` is decimal, which is good news — but its rounding
  modes are not COBOL's `ROUNDED`. For equivalence-testing a COBOL original against a Java
  rewrite this is exactly where a golden master earns its keep. And any `double` in
  financial code is a bug the model should surface, not reproduce.

**What is easier.** Identity resolution barely applies: JDBC has the table and column right
there in the SQL, JPA/Hibernate map field→column declaratively, and file access uses real
paths rather than ddnames. Almost the whole of Role 1 evaporates.

## Macros

A macro is **not a program** — it is expanded into one. Charting a macro is the same
category error as charting a copybook: you chart the program with the copybook expanded,
and you analyze ASM **post-expansion**. There is nothing else to decide.

The reason macros appear here at all is that the **macro library is a prerequisite**, in
exactly the way SYSLIB is for `COPY`. Where a house macro wraps a standard pattern — a
`GETCUST` that expands into an entire Db2 call — reading the ASM without the macro library
makes the program appear to touch **no data at all**: a silent, total loss that looks like
a clean result. Whether your macros do that is a question for your assemble JCL's SYSLIB,
not for this document.

It carries the copybook problem's twin, too: the same macro name in two macro libraries
expands to two different things, and which one applied depends on the assemble step's
SYSLIB concatenation. Same false-join risk, same resolution.

Two variants worth naming: **CICS macro-level** programs (`DFHxxx` macros, pre-command-
level) are ASM with CICS embedded — if you have any, they are CICS programs that
COBOL-and-command-level tooling will not see at all. **ISPF edit macros** are developer
tooling, not runtime, and are out of scope entirely.

---

# Suggested order, and why

Not "everything, comprehensively" — the order is chosen so each step removes a class of
*wrong answers* before the next step builds on it.

| # | Do | Because |
|---|---|---|
| **0** | JCL/PROC → ddname/DSN, with symbolics, overrides, concatenation, GDG base | Turns file identity from a false claim into a fact. **Blocks the loader.** |
| **0** | Copybook library + SYSLIB order + `REPLACING` | The other false-join source. **Blocks the loader.** |
| **1** | DCLGEN, then Db2 DDL | Authoritative column↔field; FKs give relationships *between* states. |
| **1** | Utility control cards (SORT/IDCAMS/IEBGENER) | Repairs lineage chains that are currently broken silently. |
| **2** | CICS CSD (+ BMS), MQ definitions | Online identity and the event boundary; MQ doubles as reactive-target input. |
| **3** | Scheduler dependencies | Completes the batch choreography beyond the job boundary. |
| **4** | ASM *interfaces* (CSECT/ENTRY/DSECT), Easytrieve, triggers | Coverage of remaining behavior. Bounds the `maybe` origins a `CALL` already reports; does not chart the ASM. |
| — | **HLASM macro library** | Slot at tier 0 **the moment you read any ASM** — without it an ASM program appears to touch no data. |
| — | **Java** | Needs its **own front end emitting the same four views** — a separate tool; this repo's back half is COBOL-shaped. Slot it by how much state Java already owns: if Java writes the balance, its machine is part of the balance's rules and this is not tier 4. |
| — | IMS DBD/PSB/PCB | Slot at tier 0–1 **if this is an IMS shop** — then it is a resolver, not optional. |

## The rule that does not change

Every artifact above earns its place by making an identity **provable**. None of them
license a guess. Where a binding is not statically knowable — dynamic allocation, a
scheduler-set symbolic, dynamic SQL, a stale DCLGEN that disagrees with the DDL — the
answer is a flag and a work item, exactly as the COBOL side already does for `SELECT *`
and unresolved `CALL`s.

The graph makes this structural rather than a policy: two things are the same state **iff
they reach a common node**. Everything else is visibly unresolved, and the unresolved list
is the plan for the next pass.

## Out of scope

- **RACF / security definitions** — who *may* touch a dataset is a different question from
  what *does*. Useful later for blast radius; not an identity resolver.
- **Name-similarity guessing / alias files** — decided against for COBOL, and nothing here
  changes that.
- **Anything needing a live system** — a Db2 catalog query or a real GDG resolution is a
  better answer than static analysis where it is available, but must not be a
  *requirement*: the corpus has to be analyzable from source.
