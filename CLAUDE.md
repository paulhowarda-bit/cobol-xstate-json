# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Parse IBM Enterprise COBOL and recover its behavior as an **XState v5 JSON Harel statechart** — a *rewrite contract* for mainframe modernization. Pure Python standard library, no runtime dependencies. `README.md` is the overview and `MANUAL.md` is the exhaustive reference (every flag, output field, COBOL construct, and flag-triage entry) — consult them before assuming a behavior.

## Commands

**One distribution ships from this repo**; its two dependencies ship from the sibling
**mainframe-common repository** (one repo, two distributions, each with its own
`pyproject.toml`), and the JCL front-end is its **own repository**:

| Where | Distribution | What it is | Depends on |
|---|---|---|---|
| *(this repo, root)* | `cobol-xstate` | `Program` → statechart + all views (`cobol-xstate`) | mainframe-artifacts + cobol-parser |
| [`mainframe-common`](https://github.com/paulhowarda-bit/mainframe-common) `mainframe-artifacts/` | `mainframe-artifacts` | the estate boundary, two-stage retrieval, the replayable bundle | nothing |
| [`mainframe-common`](https://github.com/paulhowarda-bit/mainframe-common) `cobol-parser/` | `cobol-parser` | the COBOL parse front-end: source → `Program` AST (normalize / preprocess / lex / parse / data division), reusable by any program | mainframe-artifacts |
| *(sibling repo)* | [`jcl-dependencies`](https://github.com/paulhowarda-bit/jcl-dependencies) | JCL → dataflow + dependencies (`jcl-dependencies`) | mainframe-artifacts only |

`cobol_parser` carries no modelling engine: `parse_program(source, fmt, resolver) ->
Program` is its whole surface, and `cobol_xstate` keeps thin re-export shims at the old
module paths (`cobol_xstate.parser`, `.model`, …) so existing imports work unchanged.
The boundary is enforced by `tests/test_package_boundaries.py` and
`tools/prove_separation.py`, like the others.

The two front-ends are **peers**: neither imports the other, and a JCL install carries no
COBOL modelling engine. They meet only at `--bind-jcl`, through a plain manifest dict, via
the lazy orchestrator in `src/cobol_xstate/bind.py`. `pip install cobol-xstate[jcl]`
adds that one join. The JCL front-end and both mainframe-common distributions started
as directories in this repo — each was lifted out and the duplicate deleted, so every
package has exactly one source (pre-split history stays reachable here via
`git log --follow`). The suite finds sibling
`../mainframe-common` and `../jcl-dependencies` checkouts automatically (see
`tests/conftest.py` and `_mainframe_common.py`; override with
`MAINFRAME_COMMON_REPO` / `JCL_DEPENDENCIES_REPO`). Without a jcl checkout the bridge
tests skip with the pip command; without mainframe-common nothing here can import, so
the run collapses to ONE clean skip (`test_sibling_distributions.py`) naming the exact
pip command.

```bash
# Run from a checkout, no install needed (root pyproject + conftest sibling discovery)
python -m pytest -q                                        # this repo's suite (~645 tests)
PYTHONPATH="../mainframe-common/mainframe-artifacts/src;../mainframe-common/cobol-parser/src;src" python -m cobol_xstate examples/custrpt.cbl

# Or install for real (editable), which is what gives you the console scripts
python -m pip install -e ../mainframe-common/mainframe-artifacts -e ../mainframe-common/cobol-parser -e .
python -m pip install -e ../jcl-dependencies      # optional: the JCL front-end + --bind-jcl
cobol-xstate prog.cbl --summary        # 8 JSON views into ./out/
cobol-xstate prog.cbl --target js      # runnable ES module + cobolRuntime.mjs
jcl-dependencies job.jcl               # 2 views + both retrieval reports (its own repo)

# Gather where the estate is reachable, model where it is not
cobol-xstate prog.cbl --gather-only ./bundle
cobol-xstate prog.cbl --from-bundle ./bundle     # no network at all

# Parse upfront, model later (the parse bundle = serialized Program, sha256-pinned)
cobol-parser prog.cbl -o prog.parse.json
cobol-xstate prog.cbl --from-parse prog.parse.json           # skips the parse
cobol-xstate prog.cbl --from-bundle ./b --from-parse prog.parse.json  # offline + parse-free

# Optional second parser (Java): per-line coverage diff vs the Koopa island grammar.
# Our preprocessor stays the provenance owner - Koopa sees the pre-expanded stream.
COBOL_PARSER_KOOPA_JAR=~/tools/koopa.jar cobol-parser prog.cbl --diff-producers

# Db2 synonym->base-table map (catalog knowledge as input): lets a column-list-less
# INSERT written under a synonym find the base table's DECLARE TABLE column order.
cobol-xstate prog.cbl --synonym-map synonyms.json

# mfdep naming-conventions fallback (docs/mfdep-conventions-integration.md): always on.
# mfdep ships in the runtime environment; it is imported lazily on the FIRST failed
# correlation that needs it, and needed-but-missing is a loud ImportError, never a
# silent conventions-less run. Only an UNKNOWN column list (invisible cursor DECLARE,
# SELECT *) is recovered - a count mismatch (indicator variables / host structures)
# never is, and a table contradicting the statement or the program's references is
# rejected (docs/issues/conventions-indicator-variable-bug.md). Tests and the gate pin
# conventions=None (tests/conftest.py autouse fixture + tools/byteproof*.py): a
# determinism seam so output never depends on the day's mfdep.db - which is what keeps
# the goldens valid on every machine, this mfdep-less one included.

python -m pytest tests/test_emitter.py -q            # one module
python -m pytest tests/test_reactive.py -k retarget  # one test by name substring
```

Node-backed tests need `npm install` at the repo root (`node_modules`).

`--target` ∈ `{json (default bundle), js, reactive, business, lineage, artifacts}`. There is **no build step and no linter configured** — do not invent one. Python ≥ 3.9.

### Node-backed tests (integration)

`test_emitter.py`, `test_reactive.py`, and `test_golden_master.py` emit an XState module, run it under **real XState v5**, and assert exact decimal results. They need `node` on PATH and a local `xstate` in `node_modules/` (`npm install`; it is gitignored). They **skip cleanly** when either is absent — so a green `pytest` run does not prove they ran; check for `skipped` when a change touches the JS/reactive emitters, or run `node` yourself against an emitted module.

## Architecture

### The pipeline builds one hub object, then many views project it

Source → **`Machine`** (`statechart.build_machine`) via: `normalizer` (fixed/free format, column-7, continuation-literal stitching) → `preprocessor` (COPY/REPLACING/EXEC SQL INCLUDE expansion) → `lexer` → `parser` (+ `model`, `data_division`) → `statechart` (+ `semantics`, `analysis`, `naming`). The front-end half up to and including `parser`/`data_division`/`model` lives in the **`cobol_parser` package** (`cobol-parser/src/cobol_parser/` in the mainframe-common repository); the seam is exactly `parse_program(...) -> Program` then `build_machine(program) -> Machine` (two adjacent calls in `api.py`). The `Machine` carries `.config` (the XState config), `.data` (typed dictionary), `.semantics` (actions/guards), `.provenance`, `.flags`, plus `.paragraph_order`, `.sections`, `.files`.

**`Machine.config` is deliberately FLAT**: one state per program point, hierarchy encoded only in mangled names (`0000-MAIN__loop3`, `__seq2`, `__if4`), and `PERFORM p` recorded as a **marker action** `perform_p` with no target and no return. This is the convenient working IR for analyses that walk it — it is *not* the final statechart. Every "view" is a **pure function over the `Machine`** that transforms this flat IR into one answer:

| Module | View | Question |
|---|---|---|
| `harel.py` | default `json` bundle's `machine`+`charts` | Hierarchical, PERFORM resolved to `invoke`, phantom fall-through pruned, `meta` kept (drawable) |
| `emitter.py` | `--target js` | The same PERFORM lowering, but runnable: real `invoke` actors, decimal ops, `meta` stripped |
| `interface.py` | `interface` overlay | Which states cross the program boundary, in which direction, carrying which fields |
| `lineage.py` | `--target lineage` | (external event, field) → its origin event + the guards governing the write |
| `business.py` | `--target business` | Scaffolding collapsed to boundary/decision/calculation states |
| `artifacts.py` / `dynamic_calls.py` | `artifacts` / `dynamic-calls` | Db2 tables, files, called programs it touches; and the dynamic call targets it won't name |
| `reactive.py` | `--target reactive` | Event-driven push machine: `on` waits + `publish_*` effects = the new system's message contract |

### `emitter.py` owns the cross-cutting primitives — reuse them, never re-implement

The flat IR is walked and rewritten the same way by several views, so the shared logic lives **once** in `emitter.py` and every other view imports it. If you touch how transitions/PERFORMs/entry-runs are handled, change the primitive, not a copy:

- `_invoke_transform` / `_invoke_transform_parallel` — lower `perform_p` markers into real `invoke` call/return actors. Used by both `emitter` (runnable) and `harel` (drawable, which nests on top and keeps `meta`). It is **meta-transparent**: it propagates whatever `meta` the input states carry.
- `segment_entry(entry, is_boundary, isolate)` — split a folded `entry` run at its boundary actions. Used by the three splitters (`emitter._emit_split` for PERFORM→invoke, `lineage._split` for `__L` chains, `reactive._split_multi_gets` for `__g` per-read states).
- `edge_target(edge)` / `iter_transitions(state, invoke=)` / `retarget_on(on, rewrite)` — read/walk/rewrite a state's outgoing transitions. **Handler targets can be a bare string** (`on: {EVENT: "__H_x"}`, from `statechart._build_handlers_region`'s parallel HANDLERS region) as well as `{target: …}`; these helpers know both forms so no walker drops the bare one.

### Core principle: no invented logic; flag, never guess

Every state/guard/action expression is a faithful translation of the COBOL its `provenance` entry points to. Anything whose behavior rides on runtime data (dynamic `CALL`, `ALTER`, byte-reinterpreting `REDEFINES`, un-parseable conditions → `{op:'raw'}`, opaque `STRING`/`INSPECT` effects) is **drawn if its shape is static, then added to `flags`** — never smoothed over. A raw-condition fallback *always* emits a flag. When editing, preserve this: if a construct can't be pinned statically, flag it rather than emitting something plausibly wrong.

### Two-stage dependency retrieval, and the JCL axis

Every run retrieves dependencies with no flag to disable it (`prefetch.py` → `fetch.py`, via `artifact_service.py`; `cast_clients.mf_fetch` is the default estate client). Order matters: a copybook that doesn't arrive drops its `VALUE` clauses, which turns a resolvable dynamic `CALL` into an unresolved name — so it never becomes a fetchable row. The COBOL says *what* a program does, not *what dataset* it does it to — that binding lives in JCL: the `jcl-dependencies` repository parses jobs/PROCs, and `--bind-jcl` joins a program's file ddnames to real datasets through its `bind_cobol_artifacts`.

### The decimal runtime ships but is never executed by the converter

`src/cobol_xstate/runtime/*.mjs` (`cobolRuntime.mjs` = fixed-point decimal ops per `data`'s PIC/USAGE — **not** float; `cobolDriver.mjs` = reference interpreter for the golden master) is package data emitted beside `--target js` output. The Python side only writes it out.

## Conventions when editing

- **Output is byte-stable and deterministic.** A refactor that should not change output must produce identical bytes — a green test run does not prove this. Verify with **`python tools/gate.py`**, which hashes every view of every `examples/*.cbl` and both retrieval reports, and checks them under two `PYTHONHASHSEED` values, at `--jobs 1` and `8`, and through the parse-bundle round trip (Program → JSON → Program before modelling, against the SAME goldens):

  ```bash
  python tools/gate.py
  ```

  Goldens live in `goldens/`. `tools/byteproof.py` covers the views (estate-free); `tools/byteproof_reports.py` covers `.prefetch.json`/`.fetch.json` against the recorded fake estate client in `tests/fakes/estate.py` (which deliberately covers local / fetched / not-found / **error** / probe-chain / alternatives). Re-record with `python tools/gate.py --record` **only** when an output change is intended and reviewed — re-recording to turn a red gate green destroys the guarantee. Actor/chart key ordering is sorted deliberately; don't iterate a set into output.

  Two things genuinely are machine-dependent and are normalized before hashing, rather than pretended away: `copiedTo` in both reports, and the `source` of any member resolved from disk (a copybook row in the artifact manifest carries it, so `<program>::artifacts` embeds a local path). Note also that `core.autocrlf=true` here, so a fresh clone gets CRLF example files while this working tree has LF — which changes the `bytes:` count in the prefetch report. The goldens are recorded from the working tree.
- **Prove the distributions still stand apart** after touching any `pyproject.toml`: `python tools/prove_separation.py` builds four throwaway venvs (installing mainframe-artifacts/cobol-parser from the sibling `../mainframe-common` checkout; `MAINFRAME_COMMON_REPO` overrides, and without one it refuses to run rather than proving nothing) and checks that an `artifacts+jcl` box cannot even find `cobol_xstate` or `cobol_parser`, that an `artifacts+parser` box parses (and runs `cobol-parser`) with no modelling engine at all, and that `--bind-jcl` on an `artifacts+parser+cobol` box fails naming the exact pip command. `tests/test_package_boundaries.py` is the fast in-process version that runs in the suite.
- **The parse bundle is a versioned contract over `model.py`.** Any change to a dataclass field set in mainframe-common's `cobol-parser/src/cobol_parser/model.py` (or `DataItem`/`PicType`) is a `VERSION` bump in `parse_bundle.py`; a newer bundle is refused, never partially read — and it is a change in ANOTHER repository that this repo's gate and `--from-parse` consume, so run both repos' ratchets (`python tools/gate.py` here, `python tools/byteproof.py --check goldens/parse.sha256` there).
- **Prove runnable changes under real XState**, not just in Python — an emitted machine that type-checks can still compute the wrong decimal.
- One test module per pipeline stage/view in `tests/`; `examples/*.cbl` are the fixtures each construct is exercised against (add one when adding a construct).
