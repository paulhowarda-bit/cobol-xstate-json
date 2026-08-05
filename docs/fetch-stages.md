# Retrieving dependencies: prefetch, then fetch

Every run of this tool retrieves what the source depends on, in two stages, with no flag
to turn either on. This document says why it has to be two, why neither is optional, and
what the reports mean.

## The circle that made one stage wrong

The dependency manifest is a product of the parse. The parse needs the copybooks. Which
copybooks a program needs is written in the program — but *what those copybooks contain*
is what decides how much of the program the parse can actually see.

Before this split, the tool ran that circle in the wrong direction: parse whatever text
happened to be on disk, build a manifest from it, then fetch. Two things fall out of the
model when it runs that way, and neither of them raises an error:

**A dynamic `CALL` loses its target.** `CALL WS-SUBPGM` is resolvable only when the single
literal reaching `WS-SUBPGM` is visible, and on a real estate that literal is a `VALUE`
clause in a shared copybook — the subprogram name is a shop-wide constant. Copybook
absent → the item is never declared → constant propagation has nothing to propagate → the
target stays an unresolved runtime name → the program it calls is **not a row in the
manifest at all**, so stage 2 never asks for it. Nothing fails. The output just describes
a program that appears to call nothing.

**A JCL job loses its steps.** A cataloged PROC, an `INCLUDE` member and a control-card
dataset each carry `EXEC PGM=` steps and DD statements that appear nowhere in the JCL file
itself. Unresolved, those steps are not programs, not datasets, not anything. A job whose
only statement is `EXEC PAYPROC` reads as an empty job.

Both failures share a shape: **the answer is short, and looks finished.** That is the worst
available failure mode for an impact analysis, and it is the reason prefetch is not a flag.

## Stage 1 — prefetch (`prefetch.py`)

Retrieves the members that **complete the source text**, before it is parsed:

| Source | What is closed over |
|---|---|
| COBOL | `COPY` and `EXEC SQL INCLUDE` members |
| JCL | cataloged PROCs, `INCLUDE` members, control-card datasets (`SYSIN`/`TOOLIN`/`SYSTSIN`/`DFSPARM`) |

**Transitively** — a copybook COPYs copybooks, a PROC INCLUDEs members — because a hole one
level down is still a hole. Cycles terminate on the seen-set.

The two sources are discovered by different mechanisms, deliberately:

- **COBOL is scanned lexically.** `COPY X.` names its member in the text, so no parse is
  needed and prefetch genuinely runs first. It shares the regexes with `preprocess()`
  rather than re-implementing them: a second copy of that grammar would drift, and drift
  here fails silently — a COPY form the scanner misses is a member never retrieved.
- **JCL is discovered by record-and-replay.** Parse with a resolver that fetches nothing
  and only records what it was asked for; retrieve those; re-parse; repeat until the parse
  stops asking for anything new. A second lexical scanner would have been easier and
  wrong: a PROC name can arrive through a symbolic parameter, an `INCLUDE` can be nested
  inside an expanded PROC body, and a control-card DSN can be built from a JCL symbol.
  Only the JCL parser resolves symbols and folds continuations correctly, and it already
  funnels every external member through one call. Replaying the parse asks exactly the
  right questions; a scanner would ask approximately the right ones.

Output: `<name>.prefetch.json`, and a store handed to stage 2 so nothing is fetched twice.

## Stage 2 — fetch (`fetch.py`)

Works from the now-complete manifest and retrieves this program's **immediate** dependent
artifacts: called programs, copybooks, assembler modules, control members, Db2 DDL, BMS
mapsets, PROCs.

It stops there. What a *callee* depends on is a question about the callee, answered by
running the tool on the callee — with its own prefetch and its own complete parse. Walking
transitively from here would answer it from a parse that had never been prefetched, which
is exactly the shortfall stage 1 exists to prevent.

Output: `<name>.fetch.json`. Retrieved members land in `<outdir>/<name>.deps/`, which a
later run can be pointed at with `-I`.

## mf-fetch is the authority on location

Where a member lives — which SYSLIB, which concatenation, which share — is knowledge this
tool does not have and does not model. Both stages ask
`cast_clients.mf_fetch.fetch_artifact(name, type=, copy=)` and report what came back.
`--copybook-fetcher MODULE:FUNC` overrides the client; it does not enable the behavior.

Three fields of the reply are kept that the tool used to discard, each of which it then
went on to guess at:

- **`detected_type`** — the service knows *what* it found. Our own kind is inferred from
  how a name was used in one program. When they disagree the service wins, and the
  disagreement is recorded as `typeNote`: a name we thought was a program and the estate
  says is an assembler module is a finding, not noise.
- **`alternatives`** — the same member name in three libraries is the SYSLIB-order
  ambiguity. Recording which resolved *and* what else could have is the difference between
  a resolved dependency and a coin flip presented as a fact.
- **`source_location`** — a member's identity is the library it came from, never the local
  cache path it landed in. Two programs "using SUBPGMS" are the same dependency only if the
  same member resolved.

## Statuses, and why they are not collapsed

Both reports keep these distinct because each one leads somewhere different:

| Status | Meaning | What to do |
|---|---|---|
| `fetched` | retrieved from the estate; carries `source` and `alternatives` | — |
| `local` | already on the `-I` search path; no round-trip | — |
| `prefetched` | *(stage 2)* stage 1 already retrieved it | — |
| `already-fetched` | another row in this manifest reached the same member | — |
| `not-found` | the service was asked and had nothing | a real gap on the estate |
| `error` | the request itself failed | **fixable** — credentials, connectivity. *Not* evidence the artifact is absent |
| `no-service` | no estate client was reachable, so it was never looked for | install/point at the client |
| `skipped` | the row never named a retrievable artifact, with the reason | nothing — a modelling fact |

The last one covers the three cases that would each fetch the *wrong file* if requested
blindly: a program-local file name with no ddname or DSN (`OUT-FILE` exists inside one
program and nowhere on the estate), a `dynamic` row that names a **data item** rather than
an artifact, and `CALLER`/`SYSOUT` destinations that are not members of anything.

## Without an estate client

Not an error. Both stages run against the local `-I` paths, every unobtainable member is
reported as `no-service` — never as `not-found` — and stderr names each hole individually:

```
MISSING SUBPGMS: the source text is incomplete without it - data items or steps it
defines are NOT in the model
```

Holes get **named** rather than counted because every downstream view is read as if it
were complete. A reader who cannot see which member is absent has no way to tell an
accurate model from a short one.

## What `resolvedBy` means in the artifact manifest

A row carrying:

```json
"resolvedBy": {
  "stage": "prefetch", "member": "SUBPGMS",
  "note": "WS-SUBPGM is declared in SUBPGMS, retrieved before the parse; without it
           this target would still be an unresolved runtime name"
}
```

exists *because* stage 1 ran. Without this annotation the improvement is invisible: the
row looks like it was always resolvable, and nothing in the output distinguishes a model
that got its members from one that got lucky.
