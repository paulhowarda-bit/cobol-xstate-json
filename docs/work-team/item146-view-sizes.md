# Item 146: view sizes (F100WB01's 926 MB business view)

Item 146 is fixed at its cause. F100WB01's business view was 926 MB because it wrote every route twice and copied every guard's condition tree at every use. It now writes each guard and each route once, and on our 1,000-report reproduction the view went from 782 MB to 18.7 MB. We then looked for the same mistake in our other views and found three more that repeated something once per use: overlapping `PERFORM ... THRU` ranges in the bundle and the reactive view, READ field lists, and record layouts in the dynamic-calls view. Each now writes it once. No content is lost: every change comes with a function that rebuilds the previous form, and we checked those functions against every golden we keep.

**These are format changes, and the tracer has to change for each.** `formatVersion` goes from 4 to 5 (pushed), then to 6, then to 7 (both committed on our master, not pushed). The steps are in the first section.

| repo | commits | status | what |
|---|---|---|---|
| cobol-xstate-json | `86b7e6b` | pushed | business view: each transition shows its shortest route; no format change |
| cobol-xstate-json | `7c4a148` | pushed | long OR/AND chains are one node; no format change (step 7) |
| cobol-xstate-json | `179390b` | pushed | business view: each guard and route written once (`formatVersion` 5) |
| jcl-dependencies | `0beea19` | pushed | its copy of the COBOL artifact manifest, at version 5 |
| cobol-xstate-json | `452b491` | committed, not pushed | overlapping `THRU` ranges: each paragraph written once (`formatVersion` 6) |
| jcl-dependencies | `393209f` | committed, not pushed | its copy of the COBOL artifact manifest, at version 6 |
| cobol-xstate-json | `bbae650`, `f9d98b9`, `ee35418`, `36d8628`, `28e99a1`, `ff7af62`, `8331919`, `925cfa4` and the version-7 commit | committed, not pushed | four follow-ups, shipped together as `formatVersion` 7 |
| jcl-dependencies | the version-7 fixture commit | committed, not pushed | its copy of the COBOL artifact manifest, at version 7 |

Take the two repos together. Only jcl-dependencies' test fixture carries our version number. jcl-dependencies itself never reads it, so `--bind-jcl` binds exactly as before.

**Version 7 is integrated.** The four follow-ups were merged onto our master together and `formatVersion` was bumped to 7 once for all of them. Together they pass our gate 6/6 and our full suite (1,117 tests, the one skip needing mfdep). All 612 of our version-6 goldens reproduce byte for byte from version-7 output, directly and through the parse bundle, once the version line is set back and the expand functions below are applied. 405 digests moved: the bundle, business, dynamic-calls, lineage and artifacts views of all 68 examples, and the 65 reactive views that are not refusals. Lineage and artifacts moved only on the version line.

---

## What the tracer has to change

**1. Accept each new `formatVersion` for every cobol-xstate view.** All of our views share the number: the bundle, `business`, `reactive`, `lineage`, `artifacts`, `dynamic-calls` and `dependents`. Each bump moves all of them, including views whose shape did not change. Move your pin to 5 now, to 6 when `452b491` is pushed, and to 7 when we push it. Until you do, your check will treat every cobol-xstate view as unknown.

**2. Read the business view's routes through its tables (version 5).** A route in `entry` or `transitions` now holds ids:

* `guards` are ids into the document's top-level `guards` table. Its entries are the guard objects (`name`, `kind` business/control, `field`, `condition`).
* `via` are ids into the top-level `stateNames` table: the collapsed technical states, in order.
* A chart edge's `meta` is `{"transition": i}` (or `{"entry": i}` on the `__ENTRY__` edges) instead of its own copy of the route.
* An edge label names the route's business guards, or all of its guards when it has no business guard. When that is one guard, the label is still its name. When it is more than one, the label is `guards#i`, naming that same route, instead of every name joined with AND.

`cobol_xstate.business.expand_business_view(view)` returns the version-4 document: guard objects and state names in place, each edge's `meta` carrying its route, and labels joined with AND.

**3. Resolve chart references in the bundle (version 6).** The bundle, and the `--machine-only` document, have a new top-level `paragraphs` table after `charts`. A paragraph node that more than one chart holds is written there once, keyed by the paragraph name (`P#2` for a second form of the same paragraph, one whose exits leave one range but not another). Each chart that holds it has `{"ref": "<id>"}` in its place. When nothing is shared, `paragraphs` is `{}`, so every bundle gains the key. `cobol_xstate.harel.expand_charts(bundle)` puts every node back in place and drops the table, giving version 5's charts, each a self-contained XState config.

**4. In the reactive view, follow `sharedParagraphs` (version 6).** Overlapping ranges that never call one another now share their states and one return. For such a range, the `inline` entry has `return` and `retField` shared with the other ranges, and its `states` lists only what sits under its own prefix. A new `sharedParagraphs` maps each paragraph the range runs from elsewhere to the range whose `states` holds it. There is no expand function for this one, because the machine is genuinely different: fewer states, the same behaviour. We proved the behaviour under real XState on `examples/restart.cbl`, restarted at each step. A program with no overlapping ranges gets a reactive view identical to version 5's apart from the version line.

**5. Resolve field lists through `fieldSets` (version 7).** A READ with no INTO used to write every field of every record in its FD at every READ site. Now each distinct field list is written once:

* **The bundle and the reactive view** have a top-level `fieldSets` right after `interface`. Each `interface.events[i].fields` is an index into it, an empty list included. `cobol_xstate.interface.expand_fields(doc)` restores the lists.
* **The business view** has one top-level `fieldSets`. Interface events' `fields`, boundary actions' `fields` (in `machine` meta and in `businessStates`), and meta `inputFields` / `outputFields` all index it. The view also has a top-level `pics` map from field name to picture, written once, sorted by name, and `null` where none is declared. A boundary action's old `{name, pic}` list is `fieldSets[i]` typed through `pics`. `cobol_xstate.business.expand_business_fields(view)` gives the version-6 business view. `expand_business_view` now resolves the field lists too, so it still returns the full version-4 form.
* **Id 0 is always the empty list.** A reader written for the old form that does `ev.get("fields") or []` therefore fails loudly on any other id, instead of drawing a real list as empty.
* Only `fields` is interned. `params` lists (SQL WHERE, RIDFLD, RETURNING) stay inline.
* This applies in memory as well as on disk. `analysis.machine.bundle()`, `Analysis.machine_json()`, `Analysis.business()` and `Analysis.reactive()` all return ids. Only the internal overlay, `Machine.interface()`, keeps its lists.

**6. Resolve record layouts in the dynamic-calls view (version 7).** The view has a new top-level `layouts` list between `dynamicCalls` and `flags`. It is always present, and is `[]` when no source reads a file. A source's `extract.layout` is now an index into it, or `null` as before when the field is not a declared item. Layouts are interned by content, not by record name, so two contained programs with a same-named record laid out differently get two entries. `cobol_xstate.dynamic_calls.expand_dynamic_calls(view)` puts a copy of each layout back into its source and drops the table.

**Summary: what to call to get the previous form.** None of these functions rewrites `formatVersion`; set it yourself if you compare against old goldens.

| view | version 5 | version 6 | version 7 |
|---|---|---|---|
| bundle | unchanged | `harel.expand_charts` gives v5 | `interface.expand_fields` gives v6 |
| business | `business.expand_business_view` gives v4 | unchanged | `business.expand_business_fields` gives v6; `expand_business_view` gives v4 |
| reactive | unchanged | no expand: same behaviour, fewer states | `interface.expand_fields` gives v6 |
| dynamic-calls | unchanged | unchanged | `dynamic_calls.expand_dynamic_calls` gives v6 |
| lineage, artifacts, dependents | unchanged | unchanged | unchanged |

To turn a version-7 bundle into version 5's form, call `expand_fields` and then `expand_charts`.

**7. Expect some content to change where the shape did not.**

* `86b7e6b`: a business transition's `guards` and `via` are now its shortest route from any call context. Which transitions there are did not change.
* `7c4a148`: a run of three or more ORs, or of ANDs, in one condition is now one `{"op": "or"}` (or `"and"`) node whose `args` hold every term. It used to be two-term nodes nested one inside the next. A parenthesized group stays its own node. If you walk condition trees assuming `args` has exactly two entries, allow any number. No golden of ours moved, because no example joined three terms with one operator before this commit. The same commit leaves an arithmetic expression `notModeled`, with the reason stated, when it nests more than 500 operations or 100 parentheses. These shapes come from generated COBOL; they used to crash the run.
* Lineage (version 7): the origins fixpoint now always runs to the end. The flag `lineage fixpoint hit its iteration bound` no longer appears, and the rows it marked now carry their real origin. A group containing FILLER that is filled by input and later cleared now reads as its input instead of as "internally set". Both are described under the lineage section below.

**8. Re-record any goldens you keep over our output** at each version. Every view's golden moves on the `formatVersion` line, and the views named above also move in shape. Our own goldens reproduce byte for byte through the expand functions at each step; the counts are in each section below.

**9. If you draw our views with harel-statechart-renderer, it needs a change before version 7.** On a version-7 bundle, business or reactive view whose events move data, it stops with `TypeError: 'int' object is not iterable` at its `fields` loop. We have a tested 7-line patch that looks each id up in `fieldSets`. With it, all 192 of our example views render to the same HTML as before. The patch has not landed in the renderer yet; it should land with or before version 7.

The renderer also has no code for version 5's route tables or version 6's `{"ref"}` chart states. On `examples/restart.cbl` it draws the version-6 bundle without an error, but not as it draws the same bundle after `expand_charts`. We have not looked at how the two differ. Until the renderer reads these tables, expand first.

---

## 146. The business view at 926 MB

**Why it was that big.** The cause was storage, not the walk. F100WB01's routes are genuinely long: from OPEN, report k is reached through the exits of the k−1 report loops before it. Version 4 wrote each route twice, once on the chart edge and once in `transitions`. It copied each guard's full condition tree at every use, which came to half a million guard objects for 33 distinct guards. It also labelled every multi-guard edge with every guard name joined with AND.

**First, `86b7e6b`: the shortest route.** The walks from one state's call contexts share where they have been, so each place belonged to whichever context reached it first, and the view kept that context's route. Take a read paragraph PERFORMed from 1,000 report loops. The context under report 1 reached every later report first, so the transition to report 1,000 listed 999 loop exits. A state with several call contexts is now walked a second time from all of them together, and each transition takes the fewest-guard route from any of them. On our 1,000-report splitter the view went from 808 MB to 4.3 MB, with 22 guard entries instead of 500,521. We compared against item 145's `9b4c63c` on 1,045 programs: every program has the same transitions in the same order, and no route has more guards. This fixes routes that were long by accident. It cannot shorten F100WB01's, which are long because the program is.

**Then `179390b`: each guard and route written once (`formatVersion` 5).** These are the tables in step 2 above. On our 1,000-report reproduction with genuinely long routes, the business view went from 782 MB to 18.7 MB as written (283.5 MB to 6.4 MB without indentation). Expanded, it equals the previous view on 378 programs. All 603 old goldens reproduce byte for byte once version 5 is undone: the version line set back to 4 and the business views expanded.

**What we have not done:** run F100WB01. Run it with `--timing` and tell us the size of `F100WB01.business.json`. If it is still large, `tools/business_edges.py F100WB01.cbl --budget 0` shows where its transitions come from.

---

## Overlapping `THRU` ranges (`452b491`, `formatVersion` 6)

**Found while looking for the same repetition elsewhere.** Restart-from-step code performs `STEP-k THRU STEP-EXIT` for every k, so each range holds every later step. Each range's chart in the bundle, and each range's inlined body in the reactive view, carried its own copy of every paragraph it spans: N ranges, N²/2 copies. A 400-step restart program made a 131 MB bundle (14 s) and a reactive view of about 80 MB. 800 steps made a 542 MB bundle.

**The fix.**

* **Bundle:** each paragraph's chart states are built once, and a node that more than one chart holds goes in `paragraphs` (step 3). `--target js` is unchanged: it still gives each range its own runnable copy.
* **Reactive:** ranges that hold the same paragraphs and never call one another, directly or through other calls, can never be active at once. They now share one return state and one return-address field, whose dispatch covers all their call sites. A paragraph's states are shared between ranges whose copies behave the same: the same paragraph, the same return, and edges that lead to copies that are themselves shared. Where one range calls another, each keeps its own copy and return, exactly as before.

**Measured at 400 steps:** the bundle went from 131 MB to 6.4 MB (0.5 s), and the reactive view from about 80 MB to 4.7 MB.

**Verified.** Every example's bundle equals its version-5 bundle after `expand_charts`, and `expand_charts` was proven equal to version 5's charts on 369 programs. All 603 old goldens reproduce once version 6 is undone. 466 digests moved (the version line and the empty `paragraphs` key), and 11 are new, from the new `examples/restart.cbl`. That example runs under real XState for both `--target js` and the reactive view, restarted at each step, with the same results from both (WS-CNT 210, WS-AUDIT 1).

**Status:** committed on our master, not pushed yet.

---

## `formatVersion` 7: four follow-ups

Each follow-up was implemented on its own branch, then reviewed by a second agent who tried to break it, then fixed where the review found problems. Three reviews found problems and one found none. The fixes have not been reviewed a second time. Each passed our full suite on its own, and all four together pass it too (1,117 tests, with the Node-backed XState tests running; the one skip is a conventions test that needs mfdep).

### The business walk on overlapping `THRU` ranges (`bbae650`, `f9d98b9`)

**What it fixes.** On restart programs the business view's *size* was already linear, but the *work* to build it was quadratic. Each `PERFORM STEP-k THRU STEP-LAST-EXIT` frame was a separate call context at every step it holds, so every step was walked once per range holding it. At 400 steps the view needed 643,997 walk steps and 18 s, and was refused at the default budget of 250,000.

**The fix.** The walk still goes over frames exactly as they were PERFORMed. Separately, for each configuration it now works out which part of its range can still be reached. A configuration is skipped when an equivalent one has already walked everything reachable from it inside its frame, and none of those walks was cut short. Such a configuration is a copy of one already walked, so walking it would find no new transition. Three other costs that grew too fast were also removed:

* The `_call_sites` pre-pass uses the same shortcut.
* Each call state's PERFORM is resolved once, instead of on every walk through it.
* The recursion test no longer compares every pair of ranges. That was 10.7 million pairs and about 20 s at 400 steps.

**Measured (budget 0, best of 3):**

| program | before | after |
|---|---|---|
| 50 steps | 0.10 s, 10,497 walk steps | 0.01 s, 303 |
| 200 steps | 2.8–4.9 s, 161,997 | 0.03 s, 1,203 |
| 400 steps | 18.0 s, 643,997, refused at the default budget | 0.09 s, 2,403, within budget |
| 1,600 steps | not run | 0.81 s, 9,603 |

Walk steps are now exactly 6N+3. The output is unchanged. At 400 steps it is the same output the old code gave when run with no budget.

**Verified.** The gate passed 6/6 with no digest moved. Beyond our examples, the view is byte-identical to master's on 3,654 generated programs: 1,054 from earlier work, and 2,600 generated specifically to stress overlapping ranges called from several places, with GO TO into and out of them. The one exception is the 400-step program, which master refuses at the default budget.

**What the review found, and the fix.** The first version merged equivalent configurations instead of skipping them. That gave the same transitions and routes, but in a different order, on 3 of the reviewer's 600 stress programs, so its claim to be byte-identical was false. The fix walks configurations in master's order and only skips them. The reviewer's 26-line program is now a test pinning master's order.

**Open.**

* Byte-identity with master is argued, and tested on those 3,654 programs. It is not proven.
* Time still grows slightly faster than linear (0.81 s at 1,600 steps against 0.22 s at 800), from building each range's paragraph set.

No output shape changes, so this one needs nothing from you.

### Each field list written once (`ee35418`, `36d8628`, `28e99a1`)

**What it fixes.** A READ with no INTO carries every field of every record its FD declares, and that list was written in full at every READ site. In the bundle and the reactive view it was on the interface event. In the business view it appeared up to three more times per site. Size grew with READ sites times record width. The new shape is step 5 above.

**Measured** on a generated program with 40 records of 50 fields, read at 300 sites. Times were taken with Python's memory tracing on, so they are inflated, but the two columns are comparable.

| view | before | after |
|---|---|---|
| bundle | 23.7 MB | 8.8 MB |
| business | 167.7 MB, 38.8 s | 11.0 MB, 3.1 s |
| reactive | 20.4 MB | 5.5 MB |

`fieldSets` held the same handful of lists at every program size we tried. These sizes were measured before id 0 was reserved, which adds one empty list per table.

**Verified.** The gate moved 201 of 612 view digests, as intended: every example's bundle and business view, and every reactive view that is not a refusal. After `expand_fields` / `expand_business_fields`, all 612 goldens reproduce byte for byte. That held directly and through the parse bundle, under two hash seeds. Every stress output, expanded, equals the old output byte for byte. The reviewer's own generator agreed.

**What the review found, and the fix.**

* Our renderer crashes on the new output (step 9), and the docs still promised it would draw these views unchanged. The docs now say a reader must resolve the index.
* Id 0 is now reserved for the empty list. Without that, the unpatched renderer drew 18 of our 192 example views silently wrong instead of failing.
* A misplaced docs paragraph, two stale doc lines and a stray whitespace edit were fixed.

**Open.**

* We have not run F100WB01, so whether READ field lists are what made its `interface` 16.5 MB is still our guess.
* The renderer patch has not landed.
* We did not check mainframe-tracer's reads of `interface.events[i].fields`; they will get an int.
* The lineage view still repeats the READ field list at every site. On the reviewer's stress program, one field name appears 320 times in a 77 MB lineage file. Fixing that is a separate shape change and is not in version 7.

### Each record layout written once (`ff7af62`)

**What it fixes.** In the dynamic-calls view, every source fed from a file field copied that record's whole layout into `extract.layout`, so the view grew with sources times record width. The new shape is step 6 above. Each program's record is now also laid out once, instead of once per source; that changes no output.

**Measured:**

| program | before | after |
|---|---|---|
| one 1,000-field record, 100 dynamic CALLs | 21.1 MB, 1.36 s, 199 MB peak | 329 KB, 11 ms, 36 MB |
| generated artifact-heavy program (901 sources) | 58.2 MB, 1.8 s, 520 MB peak | 1.6 MB, 49 ms, 71 MB |

**Verified.** The gate moved exactly 68 digests per check, every one a dynamic-calls view. After `expand_dynamic_calls`, all 612 goldens reproduce on both the direct and the parse-bundle path, under two hash seeds. The reviewer found no problems. Their own test program had contained programs, OCCURS DEPENDING ON and REDEFINES, and its expanded output equalled the old output byte for byte.

**Open.**

* Only one of our examples, `sqlcallvar.cbl`, has a non-empty `layouts` table, so the goldens cover the substitution thinly. The new tests and the stress programs cover it further.
* We did not check mainframe-tracer's reads of `extract.layout`.

### Lineage runs to its fixpoint (`8331919`, `925cfa4`)

**What it fixes.** The lineage origins fixpoint stopped after `max(50,000, states × 500)` iterations, a limit that was a guess. A long loop-carried chain of MOVEs could reach it. The row then said the field had no external origin ("internally set") and carried the flag `lineage fixpoint hit its iteration bound`. On a 502-field shift loop, `DISPLAY F502` lost its real origin, `GET.CONSOLE.SYSIN`. The limit is gone: the loop runs until nothing changes, which is the fixpoint.

**Why it still ends.** Each state's origin map only grows, and a map can grow only so far, so the number of changes per state is bounded. A check runs at a state's 16th change and at every doubling after that. If a map ever kept changing past that bound, the check raises an error instead of publishing an unsettled answer. On the 989 programs we measured, no state changed more than 6 times, so the check never ran on them. On our long generated chains it does run, and it held.

**What the review found, and the fix.** The argument above needs every map to only grow. The reviewer found an 18-line program where one shrank, through a group containing a FILLER:

* An input fill wrote the group but not its FILLER key.
* A later `MOVE SPACES` to the group brought an empty FILLER key over the loop's back edge.
* The read then dropped the fill's origin.

This was an older bug, present on master too: `MOVE G TO T` after `ACCEPT G` published T as "internally set", with no flag. It is fixed. A group now always reads as all of its leaf and FILLER keys, and every write of a group writes all of them.

**Verified.** The gate passed 6/6 with no digest moved. On 989 programs, only the reviewer's two FILLER programs changed lineage, and both changes are the corrections. With the check run at every single change on those 989 programs, no map shrank. The 502-field chain now gives the right origin, with no flag.

**What changes for you.**

* A row that used to carry the iteration-bound flag now carries its real origin.
* Lineage can take longer on such programs, since it no longer stops early. The reviewer measured about 20 s for a 600-field chain on the first version of this branch; we have not re-timed it on the final version.
* If the fixpoint ever failed to settle, which would be a bug on our side, the whole run fails with a `RuntimeError` and writes nothing. The failure is not limited to the lineage view, because the artifact manifest is built from the same fixpoint.

**Open.** That the maps only grow is argued for each write site and checked on 989 programs, but it is not enforced mechanically. A future write that set a group without its leaf keys would make that group read as empty, and no test guards every write site generically.

---

## What we need from you

* **Run F100WB01** with `--timing` once the version you take includes `179390b`. Send us the business view's size and the `view:business` line. Our reproductions have its shape, but only your program proves the fix.
* **Tell us if mainframe-tracer reads `interface.events[i].fields` or `extract.layout` directly.** Those are the two keys that version 7 turns into ids.
* **Tell us if you walk condition trees assuming two `args`** (step 7). That change is already pushed.
