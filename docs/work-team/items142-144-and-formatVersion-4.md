# Items 142-144, and the move to `formatVersion` 4

Items 142, 143 and 144 are fixed. Lineage on a utility-heavy program is about 4x faster and uses about a ninth of the memory, with every view byte-identical. Separately, the lineage file now states each field's write sites once instead of in every row. That one is a format change: **`formatVersion` is now 4 for every cobol-xstate view, and the tracer has to change before it takes it.** The steps are in the first section.

| repo | commits | what |
|---|---|---|
| mainframe-common | `33c2dd0`, `1fe0d98` | the fast JSON encoder; then reuse of a shared object's text |
| cobol-xstate-json | `9894b86`, `488c0fa`, `0549fb2`, `9bfd997`, `3f3a4c8` | items 142-144 and further speed-ups, output unchanged |
| cobol-xstate-json | `2d0a49f` | the format change (`formatVersion` 4) |
| jcl-dependencies | `2c2a036` | its copy of the COBOL artifact manifest, regenerated |

Take the three repos together. cobol-xstate-json from `488c0fa` on needs mainframe-common `33c2dd0` or later; it will not import without it.

---

## What the tracer has to change

**1. Accept `formatVersion` 4 for every cobol-xstate view.** The number is shared by all of our views: the bundle, `business`, `reactive`, `lineage`, `artifacts`, `dynamic-calls` and `dependents`. All of them now say 4. Only `lineage` changed shape; the other six differ from version 3 in that one line and nothing else. Wherever you pin the view versions you accept, and in the test that checks the pin, move cobol-xstate from 3 to 4. Until then your check will treat every cobol-xstate view as unknown.

**2. Read a lineage row's write sites from the document, not the row.** A row no longer has a `changedBy` key. The write sites are in a table at the top of the document, keyed by field:

```jsonc
"changedBy": {
  "OUT-FEE": [{ "action": "COMPUTE_OUT-FEE_eq_LK-QTY_WS-RATE", "line": 44,
                "conditionSetId": 7 }]
},
"rows": [
  { "field": "OUT-FEE", "changedByProgram": true, "conditionSetId": 4, ... }
]
```

The table holds exactly the fields that some row marks `changedByProgram: true`, in field-name order. Each list is the same list the rows used to carry, entry for entry and in the same order. Replace

```python
sites = row.get("changedBy", [])
```

with

```python
sites = doc["changedBy"].get(row["field"], [])
```

This applies whether you read `prog.lineage.json` or take the view in memory from `api.analyze(...).lineage()`.

**3. If you concatenate rows from many programs, carry each document's table with them.** A row was never fully self-contained: `conditionSetId` already resolves through the document's `conditions` and `conditionSets`. Now `changedBy` does too. Key it by `(row["program"], row["field"])`.

**4. Re-record any goldens you keep over our output.** Every view's golden moves, on the `formatVersion` line. Lineage goldens also move where rows had `changedBy`. We checked our own before re-recording: for all 67 example programs, each lineage document is identical to version 3 once each field's table entry is put back into its rows, row key order included. The only other differences are the version and the lineage `note` text. Every other view differs only in its version.

**5. If you keep your own copy of `sqlunld.artifacts.json`,** regenerate it. Only its `formatVersion` line changes. jcl-dependencies never reads that number, so `--bind-jcl` binds exactly as before.

Why the change: a field's write sites are a fact about the field, so every row about it carried the same list. On an 11,000-line program with group moves, that repetition was 70% of the lineage file. The file went from 27.7 MB to 8.1 MB, and writing it from 261 ms to about 170 ms. Most of the time saving had already come from the encoder change in `1fe0d98`, so the format change is mainly about size: less to store, and less for the tracer to parse.

---

## 142. `record_layout` recomputed on every visit

**Fixed in `9894b86`.** Each group MOVE now lays out its source and target records once per run, along with which source fields each target field overlaps. Before, both were rebuilt at every visit the fixpoint made to the MOVE. On a 2,172-line generated program, lineage had gone from 0.05 s to 20 s; it is back to its old time.

## 143. `storage._children` scans the whole dictionary

**Fixed in `9894b86`.** One children index is built per run and shared by every layout. A layout no longer scans the whole data dictionary for each group and again for each FILLER.

## 144. Every state holds a private origin map and never shares origin sets

**Fixed in `488c0fa` and `0549fb2`, as you asked, and further.**

* **Equal origin sets are one object.** A large program holds millions of map entries but only a few hundred distinct sets, so each set is now stored once.
* **A state's map is stored as the keys it changed.** It points at the map it grew from, with a full snapshot every few steps, instead of keeping a private copy.
* **A revisit re-joins only the fields that changed.** It uses the fields a predecessor changed since this state last ran, not the whole map. The answer is the same as a whole-map join; nothing assumes the sets only grow.
* **A visit no longer copies its input map.** It reads through to the stored map and records only its own writes.

On our utility-heavy reproducer (5,758 lines: drivers performing shared utilities inside a `PERFORM UNTIL` loop):

| | lineage time | lineage peak memory |
|---|---|---|
| before (`9894b86`) | 9.5 s | 624 MB |
| `0549fb2` | 3.1 s | 70 MB |
| now (`3f3a4c8`) | about 2.3 s | about 70 MB |

On an 11,000-line wide program (group moves, few shared routines), lineage went from 2.2 s and 344 MB to about 0.95 s and 44 MB. These are our generated programs, not yours. We have not measured on your estate, and your 9-17 GB programs are the real test.

**Your question: are 8-15 re-solves per state expected, or would another order settle them in fewer?** On programs shaped like yours they are expected, and no visiting order removes them.

* **Where the passes come from.** The re-solves come from loops. On our reproducer every state runs 5 or 6 times, and every pass re-runs nearly the whole program: 2,047 of its 2,048 states. Each pass carries origins one more trip around the main loop, until the sets stop growing. So the number of passes is the length of the longest chain of field-to-field copies that wraps around the loop. Any iteration order needs at least that many trips; ours is reverse postorder in passes, which runs each state once per pass.
* **The one way to need fewer.** Analyse each PERFORM call separately instead of merging every caller into one routine body. That would give different, more precise origins, so it is a design change for you to ask for rather than a speed-up. We have not done it.
* **What the re-solves cost now.** Each one costs only as much as the fields that actually changed, which is why the time fell even though the number of passes did not.

---

## Also faster, output unchanged

* **The bundle and reactive views** no longer deep-copy the machine several times. A plain tree copy is used, and only where something is rewritten (`9bfd997`). On the 11,000-line program the bundle went from 0.18 to 0.11 s and the reactive view from 0.25 to 0.13 s.
* **The JSON encoder** reuses the text of an object it has already written at the same depth (`1fe0d98`). Its output is still exactly `json.dumps`. On the 11,000-line program, encoding the lineage view went from 0.59 to 0.18 s.
