# JCL / PROC: dataflow lineage + artifact manifest

The COBOL side of this tool recovers what a program *does*; it cannot recover what it does
it *to*, because the binding `ddname -> dataset` is finished outside the program, in JCL
(the thesis of [mainframe-artifacts.md](mainframe-artifacts.md)). This reads the JCL itself.
Given a job or a PROC it produces two views, the same pair the COBOL side produces:

- **lineage** (`build_jcl_lineage`) - the dataflow across steps, plus byte-field lineage
  from utility control cards;
- **artifacts** (`build_jcl_artifacts`) - the dependency manifest, in the **same shape** as
  the COBOL artifact manifest ([artifacts-target.md](artifacts-target.md)).

```python
from cobol_xstate.jcl import parse_jcl
from cobol_xstate.jcl_views import build_jcl_lineage, build_jcl_artifacts

job = parse_jcl(open("acctunld.jcl").read(), resolver=my_fetch)   # resolver is optional
lineage   = build_jcl_lineage(job)
artifacts = build_jcl_artifacts(job)
```

From the CLI (auto-detected for `.jcl`/`.prc`/`.proc` or a `// JOB`/`// PROC` first line):

```bash
cobol-xstate acctunld.jcl                 # -> acctunld.jcl.artifacts.json + .jcl.lineage.json
cobol-xstate acctunld.jcl -o -            # both views as one bundle on stdout
```

## The resolver — a function *you* provide

Cataloged PROCs, `INCLUDE` members, and control-card datasets
(`//SYSIN DD DSN=PARM.LIB(SORTCRD)`) live outside the JCL file. This module does **not**
fetch them - you pass ``resolver``, a function ``resolver(name) -> text | None``, and it
substitutes what you return. Anything the resolver cannot return is **flagged, never
guessed** - the same rule the COBOL side follows for a missing copybook. The CLI passes no
resolver, so from the CLI those members are listed and flagged; wire the function in via the
Python API to resolve them.

## What the lineage view answers

For each step, its **inputs** and **outputs** - the DDs, resolved to datasets - and:

- **`dataflow`** - the producer -> consumer edges across steps. *Step 1 writes a dataset
  step 2 reads* is a real program-to-program dataflow that **no single-program view can
  see**, and it is the old service boundary written down (see
  [state-graph-plan.md](state-graph-plan.md)). A dataset produced then consumed within the
  job is marked `intermediate`.

- **`fieldLineage`** - real byte-field lineage where a utility control card defines it. A
  `SORT` `OUTREC BUILD=(1,5,6,20,28,8)` is three output fields, each traced to the input
  bytes it copies; `INCLUDE/OMIT COND` is the filter that decides which records survive;
  `IDCAMS REPRO` is a copy edge. This is the field granularity the COBOL side has, recovered
  at the job level from the card that actually performs the transform.

```jsonc
{ "step": "STEP02", "utility": "SORT/DFSORT",
  "input": "PROD.ACCT.UNLOAD", "output": "PROD.ACCT.SORTED",
  "filter": { "kind": "INCLUDE", "cond": "(28,1,CH,EQ,C'A')" },
  "fields": [
    { "outField": 1, "from": "input", "inBytes": "1-5",   "outBytes": "1-5" },
    { "outField": 2, "from": "input", "inBytes": "6-25",  "outBytes": "6-25" },
    { "outField": 3, "from": "input", "inBytes": "28-35", "outBytes": "26-33" } ] }
```

- **`conditions`** - when each step actually runs. A JCL job genuinely is a state machine
  (steps are states, conditions are guards - [mainframe-artifacts.md](mainframe-artifacts.md#role-3-orchestration)),
  and this recovers the guards. `if` is the `IF/THEN/ELSE/ENDIF` nesting: every test must
  hold, in its stated polarity - a step in an `ELSE` branch carries the IF's expression with
  `negated: true`, and nested IFs conjoin. `cond` is the parsed `COND=` with its notorious
  back-to-front sense **spelt out**: `COND=(4,LT)` *bypasses* the step when 4 < a preceding
  RC, so the structure states both `bypassedWhen` (the literal semantics) and `runsWhen`
  (the negation a reader actually wants), plus `EVEN`/`ONLY` abend modifiers. The same
  conditions ride on the dataflow edges a conditional step contributes (an edge holds only
  when both its steps run), on its `ddBindings`, and as `conditional: true` on the artifact
  manifest's `touchedBy` entries.

```jsonc
{ "step": "FALLBACK", "program": "DAYREPAIR",
  "conditions": { "if": [ { "test": "(EXTRACT.RC = 0)", "negated": true } ] } }
{ "step": "REPORT", "program": "DAYRPT",
  "conditions": { "cond": { "raw": "(4,LT)", "sense": "bypass-when-true",
    "bypassedWhen": "4 LT the RC of any preceding step",
    "runsWhen": "runs unless 4 LT the RC of any preceding step" } } }
```

- **`ddBindings`** - the join that closes the loop with the COBOL side. For each step
  running a program, the `ddname -> dataset` binding. A COBOL program's interface knows only
  `SELECT OUT-FILE ASSIGN OUTDD`; its artifact manifest could only say *"OUTDD, DSN in the
  JCL"*. This says `OUTDD -> PROD.ACCT.UNLOAD` - the dataset that program was missing. Join
  a COBOL `file` artifact's `ddname` to a JCL `ddBindings` row on `(program, ddname)` and the
  program-local name becomes the estate-wide identity.

## What the artifact manifest lists

The same shape as the COBOL manifest - one row per related artifact, `dependency` tagged
`runtime` or `compile-time`, each carrying the identity/resolution honesty:

| `kind` | `dependency` | `identity` | resolution |
|---|---|---|---|
| `dataset` | runtime | `global` (a real DSN is the catalog identity) | `resolvedBy: null`; DDL/record layout gives its fields |
| `dataset` (`&&`) | runtime | `job-scoped` | temporary scratch - no estate identity |
| `control-card` (`SYSIN DSN=...`) | runtime | `global` | a parameter dataset the utility reads |
| `program` | runtime | `global` | the load library (STEPLIB/JOBLIB/LINKLIST) |
| `proc` | compile-time | `program-local` | PROCLIB / JCLLIB ORDER (the SYSLIB-order hazard again) |
| `include-member` | compile-time | `program-local` | JCLLIB ORDER / system PROCLIB |

A **GDG** relative generation (`(+1)`/`(0)`) is keyed on its base - the stable identity -
with the generation recorded, so one dataset is not fragmented across every run. `SYSOUT`
spool and `DUMMY` are listed under `excluded` with the reason, not treated as related
datasets. PROCs/INCLUDE are `compile-time` because they are assembled into the effective JCL
before the job runs - exactly the role a copybook plays before a compile.

## Honest limits (all flagged, never guessed)

The JCL hazards in [mainframe-artifacts.md](mainframe-artifacts.md#jcl--the-hazards-that-matter)
are the specification. This first version handles the common cases and flags the rest:

- **Symbolic parameters** are resolved from `SET`, PROC defaults, and EXEC overrides (in
  that precedence). One it cannot resolve is left **visible** (`&SYM`) and flagged - a
  silently-wrong DSN is far worse than an obviously-unresolved one.
- **PROC / INCLUDE / control-card members** are resolved only through the caller's
  `resolver`; unresolved ones are flagged with their name.
- **`OLD` / I-O DISP** is direction-ambiguous; such a DD is recorded on both sides of the
  dataflow and marked `directionAmbiguous` rather than asserted.
- **`IF` expressions are captured verbatim**, not evaluated - `(EXTRACT.RC = 0)` is the
  recovered guard, and whether it held on a given night is a run-time fact. An unbalanced
  `ELSE`/`ENDIF` (or an `IF` left open at end-of-member) is flagged because every condition
  after it may be wrong. An unrecognized `COND=` form is kept raw and marked, never guessed.
- **Not statically knowable** - dynamic allocation (SVC 99, `BPXWDYN`), scheduler-set
  symbolics, `DDNAME=` referbacks - is out of scope by nature; flagged where seen.
- **Utility grammars** beyond `SORT`/`IDCAMS` `REPRO`/`IEBGENER` are summarized, not fully
  parsed; an unrecognized control deck is recorded as `utility: "unknown"` with a card
  count, never invented.

## Where it fits

This is the resolver the COBOL side has been pointing at all along. The COBOL `artifacts`
manifest names, for each file, the `ddname` and says *"the DSN is in the JCL"*; this view
**is** that JCL, and `ddBindings` is the edge that connects them. Loaded together, a file's
program-local name (`OUT-FILE`), its ddname (`OUTDD`), and its dataset (`PROD.ACCT.UNLOAD`)
become one identity - which is exactly what [state-graph-plan.md](state-graph-plan.md) needs
to stop two programs reading one dataset under different local names from looking unrelated.
