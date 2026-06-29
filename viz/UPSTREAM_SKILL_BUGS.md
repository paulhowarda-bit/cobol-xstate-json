# Upstream bugs in `harel-statechart-render` (v2)

The viewer in `viz/` is built on the `harel-statechart-render` skill and vendors
its `assets/viewer.js` / `viewer.css`. While integrating it, three bugs in the
skill surfaced. They are fixed in this repo's vendored copies; this file records
them so they can be fixed upstream too. Each bites any machine whose states are
all nested under a single root state — which is exactly what a COBOL→XState
emitter produces (program id as the root OR-state, paragraphs nested inside).

---

## Bug 1 — transitions are occluded by the root container fill (severity: high)

**Symptom.** The diagram shows state boxes but **no transition arrows at all**.
The edges are computed and present in the DOM (correct `d`, arrow markers, sane
stroke), but invisible on screen.

**Cause.** `assets/viewer.js` builds the SVG layer groups in this order:

```js
const gBoundaryEdges = root.append("g").attr("class", "boundary-edges");
const gEdges         = root.append("g").attr("class", "edges");
const gNodes         = root.append("g").attr("class", "nodes");   // <-- after edges
```

SVG paint order is document order, so `nodes` paints *after* `edges`. The root
OR-state (e.g. the whole program) is a node with an **opaque fill** spanning the
entire canvas, so it paints over every edge beneath it. Machines with top-level
sibling states (like the skill's own `posting` example, if its states were not
wrapped) hide it; a single wrapping root state exposes it for *all* edges.

**Fix.** Append the node layer first, then the edge layers, so transitions paint
on top of container fills:

```js
const gNodes         = root.append("g").attr("class", "nodes");
const gBoundary      = root.append("g").attr("class", "boundary-nodes");
const gBoundaryEdges = root.append("g").attr("class", "boundary-edges");
const gEdges         = root.append("g").attr("class", "edges");
const gAnnot         = root.append("g").attr("class", "annots");
```

---

## Bug 2 — entry/exit/do/transition-action labels never show at fit zoom (severity: medium)

**Symptom.** State boxes show only their names; no `entry / …`, `exit / …`, do
activities, or transition `/ action` labels — even though the machine has them.

**Cause.** All of that detail is tagged `lod-l3` in `viewer.js`, and the
semantic-zoom thresholds (`k < 0.4 ? "1" : k < 0.9 ? "2" : "3"`) only enable
`lod-l3` at zoom ≥ 0.9. Any non-trivial machine fits to screen well below 0.9
(LOD 2), so the behavior — the entire point of the diagram for modernization —
is hidden by default and only appears if the user manually zooms past 90%.

**Fix.** Promote the behavior labels that carry program semantics from `lod-l3`
to `lod-l2` (visible at the normal fit zoom): the entry/exit/SR compartments, the
activity ("do") badges, and the transition-action tspan. Keep the finest
reference detail (COBOL file/paragraph/line provenance, external-I/O field
labels) at `lod-l3`. When promoting, also size leaf boxes to the widest *visible*
line — `leafSize` in `scripts/elk_layout.mjs` measures only the state name, so
once entry text shows, long `entry / PERFORM-…` labels overflow the box; include
the compartment strings in the width measure.

---

## Bug 3 — `validate_render.py` crashes on a non-UTF-8 console (severity: low)

**Symptom.** On Windows (default cp1252 console), `scripts/validate_render.py`
aborts mid-report with `UnicodeEncodeError: 'charmap' codec can't encode
character '✓'` when printing the `✓` / `!` markers.

**Fix.** Reconfigure stdout to UTF-8 at startup, e.g.

```python
import sys
sys.stdout.reconfigure(encoding="utf-8")   # Python 3.7+
```

(or emit ASCII markers). Workaround without a code change:
`PYTHONIOENCODING=utf-8 python scripts/validate_render.py …`.

---

### Verification

After the fixes, a COBOL-derived machine (24 states, 28 transitions) renders with
all transitions visible and every `entry / PERFORM-…` label shown at the default
fit zoom, console clean. Regression guards for bugs 1 and 2 live in
`tests/test_render_viewer.py`.
