"""Where a program's business transitions come from: a breakdown of the business walk.

    python tools/business_edges.py PROG.cbl [--budget N] [--top N]

Runs only the parse, the machine and the business walk (no estate, no other views) and
prints what multiplies the transitions: how many business states there are, how many
distinct (from, to) pairs the transitions cover, how many guard variants each pair has,
which guards those variants differ by, and which states have the most. With --budget
(default: the business view's own) a walk that is refused still reports what it had
found when it stopped. Diagnostic only; writes nothing.
"""

import argparse
import collections
import statistics
import sys
import time
from pathlib import Path

from cobol_xstate.api import analyze
from cobol_xstate.business import BUSINESS_BUDGET, _BusinessView
from cobol_xstate.errors import ViewBudgetExceeded


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source")
    ap.add_argument("--budget", type=int, default=BUSINESS_BUDGET,
                    help="walk steps before stopping (0: no limit)")
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args(argv)

    path = Path(args.source)
    analysis = analyze(path.read_text(errors="replace"), source_name=path.name,
                       paths=[str(path.parent)], retrieve=False)
    view = _BusinessView(analysis.machine, args.budget)
    t0 = time.perf_counter()
    refused = None
    try:
        entry, _ = view._build_flow()
    except ViewBudgetExceeded as exc:
        refused, entry = exc, []
    secs = time.perf_counter() - t0
    found = view._found

    roles = collections.Counter(view.role(n, st) for n, st in view.states.items())
    business = sum(v for k, v in roles.items() if k != "technical")
    print(f"{path.name}: {len(view.states):,} states, {business:,} business "
          f"({', '.join(f'{k} {v:,}' for k, v in sorted(roles.items()))})")
    print(f"walk: {view._steps:,} steps, {view._contexts:,} call contexts, {secs:.1f} s"
          + (f"  -- REFUSED at the budget ({args.budget:,}); counts below are partial"
             if refused else ""))

    pairs = collections.defaultdict(list)
    for t in found:
        pairs[(t["from"], t["to"])].append(t)
    print(f"transitions: {len(found):,} over {len(pairs):,} distinct (from, to) pairs "
          f"= {len(found) / max(len(pairs), 1):.1f} guard/event variants per pair")
    if not found:
        return 0

    sets = {(f, to, frozenset(g["name"] for g in t["guards"]), tuple(t["events"]))
            for (f, to), ts in pairs.items() for t in ts}
    print(f"  of which differ only in the ORDER of the same guards: "
          f"{len(found) - len(sets):,}")
    kinds = collections.Counter(g["kind"] for t in found for g in t["guards"])
    print(f"  guards on them: {dict(kinds)}")
    glen = [len(t["guards"]) for t in found]
    vlen = [len(t["via"]) for t in found]
    print(f"  guards per transition: median {statistics.median(glen)}, max {max(glen)}")
    print(f"  via (collapsed states) per transition: median {statistics.median(vlen)}, "
          f"max {max(vlen)}, total {sum(vlen):,}")

    per_from = collections.Counter(t["from"] for t in found)
    targets = collections.defaultdict(set)
    for f, to in pairs:
        targets[f].add(to)
    print(f"\ntop {args.top} source states (transitions, distinct targets, role):")
    for f, n in per_from.most_common(args.top):
        print(f"  {n:9,}  {len(targets[f]):6,}  {view.role(f, view.states[f]):<17} {f}")

    print(f"\ntop {args.top} (from, to) pairs by variants, with the guards that vary:")
    for (f, to), ts in sorted(pairs.items(), key=lambda kv: -len(kv[1]))[:args.top]:
        every = set.intersection(*({g["name"] for g in t["guards"]} for t in ts))
        varying = collections.Counter(g["name"] for t in ts for g in t["guards"]
                                      if g["name"] not in every)
        print(f"  {len(ts):9,}  {f} -> {to}")
        print(f"             common to all: {sorted(every)[:6]}")
        print(f"             varying ({len(varying)}): "
              f"{[f'{n} x{c}' for n, c in varying.most_common(6)]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
