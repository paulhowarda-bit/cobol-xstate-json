# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Parse IBM Enterprise COBOL and recover its behavior as an **XState v5 JSON Harel statechart** — a *rewrite contract* for mainframe modernization. Pure Python standard library, no runtime dependencies. `README.md` is the overview and `MANUAL.md` is the exhaustive reference (every flag, output field, COBOL construct, and flag-triage entry) — consult them before assuming a behavior.

## Commands

```bash
# Run the converter (from a checkout, no install needed)
PYTHONPATH=src python -m cobol_xstate examples/custrpt.cbl        # writes ./out/ (8 JSON views)
PYTHONPATH=src python -m cobol_xstate prog.cbl --target js        # runnable ES module + cobolRuntime.mjs
PYTHONPATH=src python -m cobol_xstate prog.cbl --summary          # + human summary & flags on stderr
# Or install the console script: python -m pip install -e .  ->  cobol-xstate prog.cbl

# Tests — pyproject sets pythonpath=src, so NO PYTHONPATH needed for pytest
python -m pytest -q                                  # full suite (~540 tests)
python -m pytest tests/test_emitter.py -q            # one module
python -m pytest tests/test_reactive.py -k retarget  # one test by name substring
```

`--target` ∈ `{json (default bundle), js, reactive, business, lineage, artifacts}`. There is **no build step and no linter configured** — do not invent one. Python ≥ 3.9.

### Node-backed tests (integration)

`test_emitter.py`, `test_reactive.py`, and `test_golden_master.py` emit an XState module, run it under **real XState v5**, and assert exact decimal results. They need `node` on PATH and a local `xstate` in `node_modules/` (`npm install`; it is gitignored). They **skip cleanly** when either is absent — so a green `pytest` run does not prove they ran; check for `skipped` when a change touches the JS/reactive emitters, or run `node` yourself against an emitted module.

## Architecture

### The pipeline builds one hub object, then many views project it

Source → **`Machine`** (`statechart.build_machine`) via: `normalizer` (fixed/free format, column-7, continuation-literal stitching) → `preprocessor` (COPY/REPLACING/EXEC SQL INCLUDE expansion) → `lexer` → `parser` (+ `model`, `data_division`) → `statechart` (+ `semantics`, `analysis`, `naming`). The `Machine` carries `.config` (the XState config), `.data` (typed dictionary), `.semantics` (actions/guards), `.provenance`, `.flags`, plus `.paragraph_order`, `.sections`, `.files`.

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

Every run retrieves dependencies with no flag to disable it (`prefetch.py` → `fetch.py`, via `artifact_service.py`; `cast_clients.mf_fetch` is the default estate client). Order matters: a copybook that doesn't arrive drops its `VALUE` clauses, which turns a resolvable dynamic `CALL` into an unresolved name — so it never becomes a fetchable row. The COBOL says *what* a program does, not *what dataset* it does it to — that binding lives in JCL: `jcl.py` + `jcl_views.py` parse jobs/PROCs, and `--bind-jcl` joins a program's file ddnames to real datasets.

### The decimal runtime ships but is never executed by the converter

`src/cobol_xstate/runtime/*.mjs` (`cobolRuntime.mjs` = fixed-point decimal ops per `data`'s PIC/USAGE — **not** float; `cobolDriver.mjs` = reference interpreter for the golden master) is package data emitted beside `--target js` output. The Python side only writes it out.

## Conventions when editing

- **Output is byte-stable and deterministic.** Views are compared byte-for-byte across all `examples/*.cbl` and across `PYTHONHASHSEED`. A refactor that should not change output must produce an identical bundle — verify by diffing every view over every example (build a `Machine` per example, serialize each view, hash), not just by a green test run. Actor/chart key ordering is sorted deliberately for this reason; don't iterate a set into output.
- **Prove runnable changes under real XState**, not just in Python — an emitted machine that type-checks can still compute the wrong decimal.
- One test module per pipeline stage/view in `tests/`; `examples/*.cbl` are the fixtures each construct is exercised against (add one when adding a construct).
