# The other artifacts: what the COBOL cannot tell you

Companion to [state-graph-plan.md](state-graph-plan.md). That plan builds the state axis —
*what happens to the collected balance, across everything?* — by joining programs on the
state they share. This document is the inventory of **everything else on the mainframe
that the join depends on**, why each one matters, and what each will lie about if parsed
carelessly.

## The thesis

> The COBOL tells you what a program **does**. It cannot tell you what it does it **to**.

`READ CUST-FILE` is complete information about the verb and the fields. It is *no*
information about which dataset was read. That binding — `CUST-FILE` → ddname `CUSTIN` →
`PROD.CUSTOMER.MASTER` — is finished outside the program, in JCL. The same is true of
almost every name a COBOL program uses: they are **program-local**, and the artifact that
makes them **system-global** is somewhere else.

This is the argument already accepted for Db2 in the main plan: a host variable is
program-local, the *column* is the database's, so the column is the only thing proving two
programs read the same state. Every row below is that same argument applied to a different
artifact. The identity chain is always the same shape:

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

Sorting the estate by what an artifact *does for the analysis* is more useful than sorting
it by technology:

1. **Resolvers** — turn a local name into a global identity. Highest value: without them
   the graph is *wrong*, not merely incomplete.
2. **Hidden behavior** — logic that is not in any COBOL program, but moves the data.
3. **Orchestration** — who runs what, in what order, under what condition.
4. **The boundary** — where the system meets the outside world.

---

# Role 1: Resolvers

Do these **before** the Neo4j loader. The loader's join keys depend on them, and a loader
built on unresolved names will produce a confident, wrong picture of the boundaries.

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

### JCL — the hazards that matter

Resolving a DSN needs a real evaluator, not a regex. Each of these silently produces a
wrong answer if skipped:

- **Symbolic parameters.** `DSN=&HLQ..CUST.MASTER` with `&HLQ` from a `PROC` default, a
  `SET`, an `EXEC` override, or the scheduler. Resolution order is PROC default < EXEC
  parameter < `SET`. Get it wrong and the DSN is fiction.
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
| **ASM (HLASM)** | Utility subroutines called from COBOL; `CSECT`/`ENTRY` are the callable names, `DSECT` is its copybook | **Do not attempt a faithful statechart.** No data division, register-addressed, possibly self-modifying. Recover the *interface* — entry points, DSECT layout, what it calls — and treat the body as a black box with a known signature unless it is business-critical enough to hand-model. |
| **Db2 triggers / stored procedures** | Business rules that fire on a write, entirely outside the calling program | A program's `UPDATE` may do far more than it says. |
| **Easytrieve / SAS / DYL-280** | Whole report and extract programs in many shops | Own grammars; same treatment as COBOL, lower priority. |
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
| **4** | ASM interfaces, Easytrieve, triggers | Coverage of remaining behavior. |
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
