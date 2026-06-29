# viz — XState Harel statechart → self-contained HTML viewer

`render_statechart.py` turns an **XState v5 Harel statechart JSON** (a bare
`createMachine` config, or the `cobol-xstate` bundle) into **one self-contained,
offline, interactive HTML file** — zoomable, pannable, searchable, with a
mini-map and COBOL provenance.

It is a single, standalone, **pure-standard-library** Python program built on the
`harel-statechart-render` skill. It reproduces the skill's pipeline in-process and
moves ELK layout **into the browser**, so it needs **no Node.js at runtime** and
the emitted HTML opens anywhere with no server and no network.

## Usage

### One step: COBOL → diagram

The `cobol-xstate` CLI can emit the diagram directly:

```bash
cobol-xstate examples/banktran.cbl --html              # → examples/banktran.html
cobol-xstate examples/banktran.cbl --html -o diag.html
cobol-xstate examples/banktran.cbl --html --open       # write + open in browser
cobol-xstate prog.cbl --target html                    # equivalent to --html
```

### Standalone: XState JSON → diagram

```bash
# bare XState config or cobol-xstate bundle → HTML next to the input
python viz/render_statechart.py out/banktran.machine.json

# explicit output, open when done
python viz/render_statechart.py machine.json -o diagram.html --open

# pipe straight from the cobol-xstate CLI (bare config)
PYTHONPATH=src python -m cobol_xstate.cli examples/banktran.cbl --machine-only \
  | python viz/render_statechart.py - -o out/banktran.html

# full bundle works too — the `machine` key is extracted automatically
PYTHONPATH=src python -m cobol_xstate.cli examples/custrpt.cbl -o custrpt.json
python viz/render_statechart.py custrpt.json        # --machine-key overrides the key
```

Open the resulting `.html` in any browser (double-click / `file://`).

## How it works

```
XState v5 JSON ──► build_graph()  ──► ELK graph + search index   (Python, this file)
                                       │
                   one HTML file inlining:
                     • elkjs (browser build)   ← lays out on load, in the browser
                     • d3                       ← renders + drives interaction
                     • viewer.js / viewer.css   ← the skill's viewer, verbatim
                     • the graph JSON
```

`vendor/layout_boot.js` is a faithful in-browser port of the skill's
`scripts/elk_layout.mjs`: it reads the embedded graph, lays it out with elkjs,
flattens to absolute coordinates, then evaluates the (unmodified) viewer.

## Fidelity

Same rule as the skill: **render Harel faithfully or annotate the gap — never
silently draw UML.** Glyph hints trace to real XState fields or `meta.harel`
annotations. OR/AND states, history, fork/join, default entry, entry/exit
compartments, and the `meta.io` external boundary are drawn faithfully;
subset-inexpressible features are recorded as hints, not upgraded to a
confident-but-false picture.

## vendor/

| file              | source                                   | role                  |
|-------------------|------------------------------------------|-----------------------|
| `d3.min.js`       | d3 v7                                     | rendering             |
| `elk.bundled.js`  | elkjs (browser build)                    | in-browser layout     |
| `viewer.js`       | skill `assets/viewer.js` (verbatim)      | viewer + interaction  |
| `viewer.css`      | skill `assets/viewer.css` (verbatim)     | viewer styles         |
| `layout_boot.js`  | port of skill `scripts/elk_layout.mjs`   | layout + viewer boot  |

If a vendored lib is missing, the program falls back to a CDN `<script>` tag for
d3/elkjs (the output then needs network to open) and says so.
