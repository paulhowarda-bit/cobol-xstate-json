# `--target artifacts` — the related-artifact manifest

Companion to [mainframe-artifacts.md](mainframe-artifacts.md), which is the *reasoning*;
this is the *output*. That document argues that the COBOL names things with program-local
names and the real identity lives elsewhere. This target emits, per program, the list of
those names with the resolution chain attached — one small, honest manifest of *what else
this program touches*.

## The question it answers

Every other view answers *what does this program do?* This one answers the flatter thing a
migration planner asks first:

> For this program, what other artifacts on the estate are in play — and what do I have to
> read next to pin each one down?

- an `EXEC SQL` names a **Db2 table**;
- a `SELECT ... ASSIGN` (or a CICS `FILE(...)`) names a **file / dataset** — a control
  (CNTL) file read, a batch file written, the output of an unload;
- a `CALL` / CICS `LINK` / `XCTL` names an **external program**;
- a CICS `READQ` / `WRITEQ` a **queue**, a `SEND MAP` a **terminal map**, DLI a **segment**,
  `RETURN` / `USING` the **caller**;
- a `COPY` / `EXEC SQL INCLUDE` names a **copybook** the program is *built from*.

Each row is tagged `dependency: "runtime"` (an endpoint it touches when it runs) or
`dependency: "compile-time"` (a copybook it is assembled from) — the two natures live in one
list but never get confused.

## It re-projects, it does not re-parse

The manifest is a projection of the external-interface overlay
([`interface.py`](../src/cobol_xstate/interface.py)) that the faithful bundle already
carries. Every artifact here is an *endpoint* there; this view groups the endpoints by the
thing they touch, aggregates the verbs and source lines, and attaches the resolution chain.
It invents nothing the interface did not already recover.

## Every row wears the identity problem

The point of [mainframe-artifacts.md](mainframe-artifacts.md) is that the middle binding is
not the identity:

```
program-local name   →   an intermediate binding   →   the system-global identity
OUT-FILE                 OUTDD (ddname)                PROD.UNLOAD.ACCOUNT   (in the JCL)
```

So each row records what it *is* in this program, and — when that is not already a global
identity — names the artifact you must read to make it joinable across programs:

| `kind` | `identity` | `resolvedBy` (the artifact that resolves it) |
|---|---|---|
| `db2-table` | `global` | — (the table name is catalog-global; `needs` DDL/DCLGEN only for **columns/types**) |
| `file` (with ddname) | `program-local` | **JCL DD statement** — ddname → DSN |
| `file` (CICS) | `program-local` | **CICS CSD** — `DEFINE FILE ... DSNAME=` |
| `file` (no `SELECT`) | `program-local` | *nothing here* — even the ddname is unknown; **flagged** |
| `program` (batch `CALL`) | `global` | **binder / link-edit control** |
| `program` (CICS `LINK`/`XCTL`) | `global` | **CICS CSD** — `DEFINE PROGRAM` |
| `queue` | `program-local` | **CICS CSD** (TDQUEUE/TSMODEL) or **MQ** `QALIAS` |
| `cics-transaction` | `global` | **CICS CSD** — TRANSACTION → PROGRAM |
| `terminal-map` | `program-local` | **BMS mapset** |
| `ims-segment` | `program-local` | **IMS PSB/PCB + DBD** |
| `caller` | `program-local` | **JCL / binder** (batch) or **CICS CSD** (online) |
| `copybook` | `program-local` | **copybook library + SYSLIB order** (and `REPLACING` renames its fields) |

The `resolvedBy` column is deliberately the same set of resolvers as the tier table in
[mainframe-artifacts.md](mainframe-artifacts.md#role-1-resolvers) — the manifest is how a
single program *feeds* that estate-wide resolution work.

For **files**, the first of those resolutions is now built in: pass the JCL with
`--bind-jcl job.jcl` (or `bind_cobol_artifacts` in Python) and each file row the JCL
resolves gains `dataset` and `boundBy` — the ddname → DSN chain closed by the actual DD
statement. See [jcl-target.md](jcl-target.md#closing-the-loop-bind_cobol_artifacts---bind-jcl).

## Copybooks — the compile-time dependency

`COPY` and `EXEC SQL INCLUDE` members are listed too, sourced from the preprocessor's own
record of what it expanded, so the list is authoritative rather than reverse-engineered.
Each copybook row carries `via` (`COPY` / `EXEC SQL INCLUDE`), `status`
(`expanded` / `missing` / `skipped-cyclic`), `replacing` (true when the `COPY ... REPLACING`
clause renamed fields), and `contributes` (how many data items / paragraphs it brought into
the model). It is exactly the same identity problem as a ddname: a member name like
`CUSTREC` is unique only *within a library*, so which layout it is depends on **SYSLIB
concatenation order**, and `REPLACING` gives *the same layout different field names per
program* — the two false-join hazards
[mainframe-artifacts.md](mainframe-artifacts.md) names for copybooks.

A **missing** copybook — `COPY`d but not found on the search path — is the highest-value row
in the whole manifest: the data items and logic it defines are silently *absent from every
view of the program*, so it is listed with `status: "missing"` and raised in `flags`, never
dropped.

## What it will not claim

- A file referenced with **no `SELECT ... ASSIGN`** (and no CICS `FILE(...)`) has no known
  ddname. The row says the dataset is unresolvable from this program alone, and the
  manifest `flags` it — a silent row would read as a resolvable binding that does not exist.
- A **dynamic `CALL`** whose target the tool could not resolve statically is already flagged
  in the bundle; its `needs` says the target is a run-time decision.
- **Response registers** (`SQLCODE`, `EIB`, FILE STATUS), **handled conditions** (`NOTFND`,
  end-of-file, I/O errors) and **system intrinsics** (DATE/TIME) are the program *reacting*
  to a subsystem, not a second artifact it touches. They are dropped from `artifacts` and
  listed under `excluded` **with the reason**, so the omission is visible rather than silent.

## Structural patterns

Two program shapes the manifest can prove outright — and the corpus is named for them:

- **`unload`** — a Db2 read paired with a file write (`sqlunld.cbl`: `FETCH ACCOUNT` +
  `WRITE OUT-FILE`).
- **`load`** — a file read paired with a Db2 write (`sqlload.cbl`: `READ IN-FILE` +
  `INSERT ACCOUNT`).

They are stated only when *both* halves are present, so the label is a fact, not a guess.

## Example

```bash
cobol-xstate examples/sqlunld.cbl --target artifacts -o -
```

```jsonc
{
  "format": "cobol-xstate-artifacts",
  "program": "SQLUNLD",
  "artifacts": [
    { "artifact": "ACCOUNT", "kind": "db2-table", "io": "read", "verbs": ["FETCH"],
      "identity": "global", "lines": [38],
      "needs": "Db2 DDL / DCLGEN to resolve columns, types, and keys (the table name
                itself is catalog-global)" },
    { "artifact": "OUT-FILE", "kind": "file", "dependency": "runtime", "io": "write",
      "verbs": ["WRITE", "OPEN OUTPUT"], "identity": "program-local", "lines": [35, 47],
      "ddname": "OUTDD", "organization": "SEQUENTIAL", "resolvedBy": "JCL DD statement",
      "needs": "the JCL //<ddname> DD DSN=... to resolve the dataset name (DSN)..." },
    { "artifact": "CUSTREC", "kind": "copybook", "dependency": "compile-time", "via": "COPY",
      "status": "expanded", "identity": "program-local", "contributes": { "dataItems": 7 },
      "resolvedBy": "copybook library + SYSLIB concatenation order",
      "needs": "the SYSLIB the compile saw: a member name is unique only within a library..." }
  ],
  "patterns": ["unload: reads Db2 (ACCOUNT) and writes a file (OUT-FILE)"],
  "excluded": [
    { "name": "DB2", "endpointType": "response",
      "reason": "a response register (SQLCODE / EIB / FILE STATUS)..." }
  ],
  "flags": []
}
```

*The program touches two artifacts: the Db2 table `ACCOUNT` (already a global identity — you
need its DDL only for the columns) and the output file `OUT-FILE`, whose ddname `OUTDD` is a
binding into the JCL where the real dataset name lives. The pair is an unload. The `SQLCODE`
the loop branches on is not an artifact, and the manifest says so.*
