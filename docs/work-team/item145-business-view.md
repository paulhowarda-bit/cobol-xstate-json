# Item 145: the business view on F100WB01

Item 145 is fixed at its cause. Each business transition used to be kept once for every route through the program's loops; now it is kept once. Loop-heavy programs like F100WB01 now walk in proportion to their size instead of exponentially, and no business step the old view found is lost. Separately, a business view that is still too big is now refused with an ERROR, not cut short, and `--timing` reports how many walk steps each run took.

| repo | commits | what |
|---|---|---|
| mainframe-common | `cdfbfd7` | `--timing` rows carry the process's peak memory (`peakMB`) |
| mainframe-common | `8400b39` | a timing stage can carry counts (`StageTimer.count`) |
| cobol-xstate-json | `3d65e18` | the business walk per PERFORM call site; the walk-step budget and its refusal |
| cobol-xstate-json | `8546485` | `walkSteps` on the business view's timing row |
| cobol-xstate-json | `9b4c63c` | the item 145 fix: one transition per business step |

Take the two repos together. cobol-xstate-json from `8546485` on needs mainframe-common `8400b39` or later. Without it the business view fails with an `AttributeError` on `StageTimer.count`: a default run logs a warning and writes no business view, and `--target business` crashes.

---

## What the tracer has to change

**1. Accept extra keys on timing rows.** Every row a `timing_sink` receives now has `peakMB` (megabytes, or `None` on a platform with no counter). The `view:business` row also has `walkSteps`, the walk steps the business view took. If you check a row's key set exactly, allow these.

**2. Treat a refused business view as that view failing, not as the run failing.** The business view has a budget of 250,000 walk steps (`--business-budget STEPS`, `analyze(business_budget=)`; `0` means no limit). A view that needs more is **not written**, never a shortened one:

* a default run logs `ERROR: business view not produced: it needs more than its budget of N walk steps (...)`, writes every other artifact, and exits 0;
* `write_views` leaves `business` out of the mapping it returns;
* `--target business` exits 3;
* `Analysis.business()` raises `ViewBudgetExceeded` (a `CobolXstateError`, with `view`, `limit`, `unit`, `knob` attributes).

You asked for a budget that returns a partial view with a flag. We did not do that: a view cut off where the budget ran out reads as complete, and nothing downstream could tell. Re-running with `--business-budget 0` gives the full view. With the fix below, we do not expect your programs to reach the budget; if one does, the ERROR line is what to look for.

**3. Re-record business-view goldens that you keep over our output.** The business view's shape and `formatVersion` (still 4) are unchanged, but its content changes in two ways:

* **Transitions that differed only in loop exits are gone.** Where the old view had the same `from` and `to` several times, with the same business guards but different `UNTIL` guards, there is now one transition: the route with the fewest guards. Its `meta.guards` and `via` are that route's.
* **An IF branch or WHEN arm keeps its own transition when the ELSE reaches the same state.** The old view showed only the ELSE's, as if no condition applied, and hid the branch's. It did that only when the ELSE route happened not to cross a loop exit. Now each branch says where it goes.

Six of our 67 example programs moved, each gaining one or two transitions of the second kind: `callfilter`, `dynsql`, `sqlcallvar`, `sqlincnp`, `sqlsel`, `timesexit`. No other view changed. On our 11,000-line generated program, transitions went from 1,606 to 2,809, almost all EVALUATE arms that each lead to the same next IF.

**4. If you stub our builders in tests:** `build_business_view(machine, budget=..., stats=None)` takes an optional `stats` dict, which receives `walkSteps`, and `Analysis.business(timer=None)` takes an optional `StageTimer`.

---

## 145. The business view keeps every edge (F100WB01 killed at 16.8 GB)

**Your diagnosis was right.** F100WB01 is a report splitter. The main paragraph runs about 1,000 report loops one after another, each `PERFORM A-n-PROCESS-REPORT THRU ... UNTIL` a new report starts or the input ends. Every report paragraph writes its record and PERFORMs the one shared read paragraph, `X-100-READ`.

**Why that produced millions of transitions.** The walk from a business state inside `X-100-READ` returns to every one of the 1,000 report paragraphs. From each return point it carries on through the later report loops, and each loop can either run again or exit. The old walk only dropped a route whose guards were a superset of an earlier route's. A route that exited loops 5, 6 and 7 and one that exited loop 6 only have different `UNTIL` guards, and neither set contains the other, so both were kept. Every combination of loop exits became its own transition, all of them the same business step. That is the 1,510,303 distinct edges you saw over only 29,737 configurations, and the roughly 11 KB each was their long `via` paths.

The per-call-site walk in `3d65e18` reduced the configurations (your change 3) but not this, because it finds the same set of transitions by design.

**The fix (`9b4c63c`).** Past its first step, the walk only passes through technical states, and a technical state has no business guard on its edges; that is what makes it technical. So the only business condition a route can carry is its first step's: an IF branch, an EVALUATE WHEN. Everything after that adds only control guards, such as loop exits, AT END and ON OVERFLOW. The walk is therefore plain reachability once per first step:

* **A place is walked once per set of business guards.** The route kept is the one with the fewest guards, because the walk is ordered by guard count.
* **Returning out of a PERFORM continues in the same walk** for each frame that can be underneath, instead of running the walk again for every full call stack.
* **The walks from one state share where they have been.** That state's 1,000 call contexts no longer each walk the rest of the main paragraph again.
* **Transitions are keyed by `(from, to, business guards, events)`.**

**Measured on our programs (not yours):**

| program | before | after |
|---|---|---|
| a report splitter shaped like F100WB01: 300 report loops, a shared read paragraph with 20 IF tests | 293,166 walk steps, 91,260 transitions, 10.3 s | 21,666 steps, 962 transitions, 0.9 s |
| the same with 1,000 report loops | (not run) | 72,066 steps, 3,062 transitions, 1.7 s |
| 3 `PERFORM ... UNTIL` loops per paragraph, 6 levels deep | 47,778 steps, 5,469 transitions among 20 business states | 3,441 steps, 234 transitions |
| the same, 14 levels deep | refused after 3,000,000 steps | 24,285 steps, 1,062 transitions |

**No business step is lost.** We compared the old and new views on 1,061 programs: our 67 examples, 960 random PERFORM-heavy programs, and the synthetic ones above. For every program, every `(from, to, business guards, events)` the old view had is in the new one. 808 programs gained transitions of the per-branch kind described in step 3; that is the only addition. Three programs could not be compared, because the old walk did not finish within 2,000,000 steps.

**What we have not done:** run F100WB01. Our splitter has its shape as you described it, but only your run proves the real program. Run it with `--timing`; the `view:business` line shows `walkSteps`. If it is refused, or the number surprises you, `tools/business_edges.py F100WB01.cbl --budget 0` prints where its transitions come from: business states by role, transitions per (from, to) pair, the guards that vary between them, and the states with the most.

**Your three proposed changes:**

1. **Stop at a budget.** Done, as a refusal rather than a partial view (step 2 above).
2. **Hold less memory per edge.** Not done: the edges themselves are now few, so there is little left to save.
3. **Reduce the configurations.** Done in `3d65e18`: the walk carries only the top PERFORM of each stack. On a 387-line program nesting 8 levels with 3 call sites each, that took the walk from 157,457 walks (12 s, 65 MB) to 1,975 steps (0.2 s) at `9b4c63c`.

## Also new: what `--timing` reports

Each `--timing` line now ends with the process's peak memory as it stood when that stage ended: the operating system's own high-water mark, so it never falls. The stage whose line it first jumps on is the one that drove the run's memory up. The `view:business` line also shows `walkSteps`, the unit `--business-budget` counts. Unlike time and memory it is the same on every machine, so if you want a different budget, set it from the steps your largest programs report.
