/* viewer.js — renders the laid-out Harel statechart and drives all interaction.
   Expects globals: GRAPH (laid-out graph), d3.
   See references/interaction-design.md and elk-harel-mapping.md. */
(function () {
  "use strict";
  const G = window.GRAPH;
  const svg = d3.select("#canvas");
  const stage = document.getElementById("stage");
  const root = svg.append("g").attr("class", "viewport-root");
  // Layer order = paint order (later groups draw on top). Nodes are drawn first
  // so that container fills (e.g. a root OR-state wrapping the whole machine, as
  // the COBOL→XState emitter produces) do NOT paint over the transitions. Edges
  // and their labels sit above the node boxes; annotations on top of all.
  const gNodes = root.append("g").attr("class", "nodes");
  const gBoundary = root.append("g").attr("class", "boundary-nodes");
  const gBoundaryEdges = root.append("g").attr("class", "boundary-edges");
  const gEdges = root.append("g").attr("class", "edges");
  const gAnnot = root.append("g").attr("class", "annots");

  // arrow markers
  const defs = svg.append("defs");
  defs.append("marker").attr("id", "arrow").attr("viewBox", "0 0 10 10")
    .attr("refX", 9).attr("refY", 5).attr("markerWidth", 7).attr("markerHeight", 7)
    .attr("orient", "auto-start-reverse")
    .append("path").attr("d", "M0,0 L10,5 L0,10 z");
  // distinct arrowhead for conditional (guarded) transitions
  defs.append("marker").attr("id", "arrow-cond").attr("viewBox", "0 0 10 10")
    .attr("refX", 9).attr("refY", 5).attr("markerWidth", 7.5).attr("markerHeight", 7.5)
    .attr("orient", "auto-start-reverse")
    .append("path").attr("d", "M0,0 L10,5 L0,10 z").attr("class", "condarrowhead");
  defs.append("marker").attr("id", "ioarrow").attr("viewBox", "0 0 10 10")
    .attr("refX", 9).attr("refY", 5).attr("markerWidth", 8).attr("markerHeight", 8)
    .attr("orient", "auto-start-reverse")
    .append("path").attr("d", "M0,0 L10,5 L0,10 z").attr("class", "ioarrowhead");

  const nodeById = {};
  G.nodes.forEach(n => nodeById[n.id] = n);

  // ---- draw nodes (containers first so children sit on top) ----
  const ordered = G.nodes.slice().sort((a, b) => a.depth - b.depth);

  const nodeSel = gNodes.selectAll("g.state").data(ordered, d => d.id)
    .enter().append("g")
    .attr("class", d => `state ${d.kind}${d.isContainer ? " container" : ""}`)
    .attr("data-depth", d => d.depth)
    .attr("data-id", d => d.id)
    .attr("transform", d => `translate(${d.x},${d.y})`)
    .on("click", (ev, d) => { ev.stopPropagation(); inspectState(d); });

  nodeSel.append("rect").attr("class", "box")
    .attr("width", d => d.width).attr("height", d => d.height)
    .attr("rx", 8).attr("ry", 8);

  // header band for containers
  nodeSel.filter(d => d.isContainer).append("rect").attr("class", "hband")
    .attr("width", d => d.width).attr("height", 22).attr("rx", 8).attr("ry", 8);

  // name
  nodeSel.append("text").attr("class", "name")
    .attr("x", 8).attr("y", 5).text(d => d.label);

  // region dividers inside AND-states
  nodeSel.filter(d => d.kind === "and").each(function (d) {
    const regions = G.nodes.filter(n =>
      n.id.startsWith(d.id + ".") && n.id.split(".").length === d.id.split(".").length + 1);
    const g = d3.select(this);
    regions.slice(1).forEach(r => {
      const lx = r.x - d.x - 5;
      g.append("line").attr("class", "region-divider")
        .attr("x1", lx).attr("y1", 22).attr("x2", lx).attr("y2", d.height);
    });
  });

  // default-entry dot for OR-states
  nodeSel.filter(d => d.kind === "or" && d.initial && nodeById[d.initial]).each(function (d) {
    const init = nodeById[d.initial];
    const g = d3.select(this).append("g").attr("class", "glyph default-dot lod-l2");
    const dx = init.x - d.x + 10, dy = init.y - d.y - 10;
    g.append("circle").attr("cx", dx).attr("cy", dy).attr("r", 4);
  });

  // history glyph
  nodeSel.filter(d => d.kind === "history").each(function (d) {
    const g = d3.select(this).append("g").attr("class", "glyph history");
    g.append("circle").attr("cx", d.width / 2).attr("cy", d.height / 2).attr("r", 11);
    g.append("text").attr("x", d.width / 2).attr("y", d.height / 2)
      .text(d.historyDepth === "deep" ? "H*" : "H");
  });

  // final-state inner ring
  nodeSel.filter(d => d.kind === "final").append("rect").attr("class", "box")
    .attr("x", 4).attr("y", 4).attr("width", d => d.width - 8).attr("height", d => d.height - 8)
    .attr("rx", 5).attr("fill", "#44506a");

  // Compartments: entry / exit / static reactions / activities ("do") at L2 so
  // they are visible at the normal fit zoom (the COBOL behavior lives here —
  // entry actions are PERFORMs). Provenance (COBOL file/para/line) stays at L3
  // as the finest reference detail; it is also in the click inspector.
  nodeSel.each(function (d) {
    const g = d3.select(this);
    let yy = 22;
    const add = (cls, txt) => {
      g.append("text").attr("class", `compartment ${cls} lod-l2`)
        .attr("x", 8).attr("y", yy + 12).text(txt);
      yy += 16;
    };
    (d.entry || []).forEach(a => add("entry", `entry / ${a}`));
    (d.exit || []).forEach(a => add("exit", `exit / ${a}`));
    ((d.harel && d.harel.staticReactions) || []).forEach(sr => add("sr", `SR: ${sr}`));
    ((d.harel && d.harel.activities) || []).forEach(act =>
      g.append("text").attr("class", "activity-badge lod-l2")
        .attr("x", 8).attr("y", (yy += 16) - 4).text(`⏲ ${act.name} (${act.binding})`));
    if (d.provenance && d.provenance.cobolParagraph) {
      const p = d.provenance;
      const ln = p.sourceLines ? ` ${p.sourceLines.join("–")}` : "";
      g.append("text").attr("class", "provenance lod-l3")
        .attr("x", 8).attr("y", d.height - 6).text(`${p.file || ""} ${p.cobolParagraph}${ln}`);
    }
  });

  // ---- draw edges ----
  function edgePath(e) {
    if (!e.sections || !e.sections.length) return null;
    const s = e.sections[0];
    let pts = [s.start, ...(s.bends || []), s.end];
    return "M" + pts.map(p => `${p.x},${p.y}`).join(" L");
  }

  // An "automatic" transition is XState's `always` (and ε/after): it fires on
  // its own, so its only meaningful content is the guard. A guarded automatic
  // edge is a *decision*; an unguarded one is just sequential flow.
  function isAuto(e) { return !e.event || e.event === "ε(always)" || e.event.startsWith("after("); }

  const edgeSel = gEdges.selectAll("g.edge").data(G.edges.filter(e => !e.internal), d => d.id)
    .enter().append("g")
    .attr("class", d => "edge" + (d.guard ? " conditional" : (isAuto(d) ? " seq" : "")))
    .attr("data-id", d => d.id)
    .on("click", (ev, d) => { ev.stopPropagation(); inspectEdge(d); });

  edgeSel.append("path").attr("d", edgePath)
    .attr("marker-end", d => d.guard ? "url(#arrow-cond)" : "url(#arrow)");

  // native tooltip: full transition meaning, available at any zoom level
  edgeSel.append("title").text(tooltipFor);

  edgeSel.each(function (e) {
    const p = edgePath(e); if (!p) return;
    const s = e.sections[0];
    const mid = (s.bends && s.bends.length) ? s.bends[Math.floor(s.bends.length / 2)]
      : { x: (s.start.x + s.end.x) / 2, y: (s.start.y + s.end.y) / 2 };
    const label = labelFor(e);
    if (!label.cap && !label.ac) return;   // drop the "ε(always)" noise: nothing to say
    const g = d3.select(this);
    const t = g.append("text").attr("class", "elabel").attr("x", mid.x).attr("y", mid.y - 4);
    if (label.cap) t.append("tspan").attr("class", label.capClass).text(label.cap);
    if (label.ac) t.append("tspan").attr("class", "ac lod-l2").text(" " + label.ac);
  });

  // Display-only prettifier: the emitter slugs COBOL conditions into
  // identifier-safe guard NAMES — relational operators become lowercase words
  // (= -> eq, < -> lt, …) and separators become underscores, while data names
  // stay UPPER-CASE and hyphenated (see naming.py `_slug`). This restores the
  // readable form for captions/tooltips WITHOUT touching the stored guard name
  // (search and provenance still use the raw name). Operator words are matched
  // lowercase/word-bounded, so UPPER-CASE field names are never rewritten.
  function prettyGuard(g) {
    if (!g) return g;
    return g.replace(/_/g, " ")
      .replace(/\beq\b/g, "=").replace(/\bne\b/g, "≠")
      .replace(/\bge\b/g, "≥").replace(/\ble\b/g, "≤")
      .replace(/\bgt\b/g, ">").replace(/\blt\b/g, "<")
      .replace(/\s+/g, " ").trim();
  }

  // caption: the meaningful part of the transition. For automatic edges the
  // guard IS the caption ("ε(always)" is suppressed); real events show the event.
  function labelFor(e) {
    const guard = e.guard ? `[${prettyGuard(e.guard)}]` : "";
    let cap = "", capClass = "ev";
    if (isAuto(e)) { cap = guard; capClass = "gd"; }
    else { cap = e.event + (guard ? " " + guard : ""); capClass = "ev"; }
    const ac = (e.actions && e.actions.length) ? "/ " + e.actions.join("; ") : "";
    return { cap, capClass, ac };
  }

  function tooltipFor(e) {
    const nm = id => (nodeById[id] ? nodeById[id].label : id);
    let s = nm(e.source) + "  →  " + nm(e.target);
    if (e.guard) s += "\nwhen [" + prettyGuard(e.guard) + "]";
    else if (isAuto(e)) s += "\n(unconditional — always)";
    if (e.event && !isAuto(e)) s += "\non " + e.event;
    if (e.actions && e.actions.length) s += "\ndo " + e.actions.join("; ");
    return s;
  }

  // ---- broadcast annotation edges (from meta.harel) ----
  G.nodes.forEach(d => {
    const bc = d.harel && d.harel.broadcast;
    if (!bc || !bc.length) return;
    bc.forEach(b => {
      const fromId = `${d.id}.${b.from}`, toIds = (b.to || []).map(t => `${d.id}.${t}`);
      const from = nodeById[fromId]; if (!from) return;
      toIds.forEach(tid => {
        const to = nodeById[tid]; if (!to) return;
        const x1 = from.x + from.width / 2, y1 = from.y + from.height / 2;
        const x2 = to.x + to.width / 2, y2 = to.y + to.height / 2;
        const g = gAnnot.append("g").attr("class", "annot lod-l2");
        g.append("path").attr("d", `M${x1},${y1} L${x2},${y2}`);
        const tag = `broadcast ${b.event}` + (d.harel.sensing ? ` (${d.harel.sensing})` : "");
        g.append("text").attr("x", (x1 + x2) / 2).attr("y", (y1 + y2) / 2 - 3).text(tag);
      });
    });
  });

  // ---- external I/O boundary: endpoint nodes, arrows, per-state badges ----
  const epKindClass = k => "ep-" + String(k || "external").toLowerCase().replace(/[^a-z0-9]/g, "");

  // boundary endpoint nodes (typed by endpoint kind)
  const bNodes = G.boundaryNodes || [];
  const bSel = gBoundary.selectAll("g.boundary").data(bNodes, d => d.id)
    .enter().append("g")
    .attr("class", d => `boundary ${epKindClass(d.kind)}${d.endpointId === "__unspecified_in__" ? " unconfirmed" : ""}`)
    .attr("data-endpoint", d => d.endpointId)
    .attr("transform", d => `translate(${d.x},${d.y})`)
    .on("click", (ev, d) => { ev.stopPropagation(); inspectEndpoint(d); });
  bSel.append("rect").attr("class", "ep-box")
    .attr("width", d => d.width).attr("height", d => d.height).attr("rx", 6);
  bSel.append("text").attr("class", "ep-kind").attr("x", 8).attr("y", 14)
    .text(d => d.kind);
  bSel.append("text").attr("class", "ep-label").attr("x", 8).attr("y", 30)
    .text(d => d.label.length > 20 ? d.label.slice(0, 19) + "…" : d.label);

  // boundary edges (input arrows into states, output arrows out of states)
  const bEdges = G.boundaryEdges || [];
  const beSel = gBoundaryEdges.selectAll("g.bedge").data(bEdges, d => d.id)
    .enter().append("g")
    .attr("class", d => `bedge ${d.direction}${d.unconfirmedEndpoint ? " unconfirmed" : ""}`)
    .attr("data-id", d => d.id);
  beSel.append("path")
    .attr("d", d => {
      const s = d.sections[0];
      return `M${s.start.x},${s.start.y} L${s.end.x},${s.end.y}`;
    })
    .attr("marker-end", "url(#ioarrow)");
  beSel.append("text").attr("class", "bedge-label lod-l3")
    .attr("x", d => (d.sections[0].start.x + d.sections[0].end.x) / 2)
    .attr("y", d => (d.sections[0].start.y + d.sections[0].end.y) / 2 - 3)
    .text(d => d.label);

  // per-state in/out badges (so a state's interface reads without tracing to edge)
  gNodes.selectAll("g.state").each(function (d) {
    const b = d.ioBadges; if (!b) return;
    const g = d3.select(this);
    const mk = (items, side) => {
      let yy = 4;
      items.forEach(it => {
        const bw = 7;
        const grp = g.append("g").attr("class",
          `io-badge ${side}${it.unconfirmed ? " unconfirmed" : ""}`);
        grp.append("circle")
          .attr("cx", side === "in" ? -bw : d.width + bw)
          .attr("cy", yy + 6).attr("r", 4);
        grp.append("text").attr("class", "io-badge-text lod-l3")
          .attr("x", side === "in" ? -bw - 6 : d.width + bw + 6)
          .attr("y", yy + 9)
          .attr("text-anchor", side === "in" ? "end" : "start")
          .text((it.kind === "event" ? "▸" : "▪") + " " + it.label);
        yy += 16;
      });
    };
    mk(b.in || [], "in");
    mk(b.out || [], "out");
  });

  // ---- zoom + semantic LOD ----
  const zoom = d3.zoom().scaleExtent([0.05, 4])
    .on("zoom", (ev) => {
      root.attr("transform", ev.transform);
      const k = ev.transform.k;
      stage.dataset.lod = k < 0.4 ? "1" : (k < 0.9 ? "2" : "3");
      updateMinimapViewport(ev.transform);
    });
  svg.call(zoom).on("dblclick.zoom", null);
  svg.on("click", clearInspect);

  function fit() {
    const sw = stage.clientWidth, sh = stage.clientHeight;
    const vx = (G.viewMinX != null) ? G.viewMinX : 0;
    const vw = ((G.viewMaxX != null) ? G.viewMaxX : G.width) - vx;
    const k = Math.min(sw / vw, sh / G.height) * 0.92;
    const tx = (sw - vw * k) / 2 - vx * k, ty = (sh - G.height * k) / 2;
    svg.transition().duration(300).call(zoom.transform,
      d3.zoomIdentity.translate(tx, ty).scale(k));
  }
  window.addEventListener("resize", () => updateMinimapViewport(d3.zoomTransform(svg.node())));

  // double-click a container → zoom to its subtree bbox
  nodeSel.filter(d => d.isContainer).on("dblclick", (ev, d) => {
    ev.stopPropagation();
    const sw = stage.clientWidth, sh = stage.clientHeight;
    const pad = 30, k = Math.min(sw / (d.width + pad * 2), sh / (d.height + pad * 2));
    const tx = sw / 2 - (d.x + d.width / 2) * k, ty = sh / 2 - (d.y + d.height / 2) * k;
    svg.transition().duration(400).call(zoom.transform,
      d3.zoomIdentity.translate(tx, ty).scale(k));
  });

  // ---- mini-map ----
  const mini = d3.select("#minimap").append("svg")
    .attr("viewBox", `0 0 ${G.width} ${G.height}`).attr("preserveAspectRatio", "xMidYMid meet");
  mini.selectAll("rect.mini-node").data(G.nodes.filter(n => !n.isContainer || n.kind === "and"))
    .enter().append("rect")
    .attr("class", d => "mini-node" + (d.kind === "and" ? " and" : ""))
    .attr("x", d => d.x).attr("y", d => d.y).attr("width", d => d.width).attr("height", d => d.height);
  const viewportRect = mini.append("rect").attr("class", "viewport");

  function updateMinimapViewport(t) {
    const sw = stage.clientWidth, sh = stage.clientHeight;
    const x = (-t.x) / t.k, y = (-t.y) / t.k, w = sw / t.k, h = sh / t.k;
    viewportRect.attr("x", x).attr("y", y).attr("width", w).attr("height", h);
  }
  d3.select("#minimap").on("click", function (ev) {
    const pt = d3.pointer(ev, mini.node());
    const vb = mini.node().viewBox.baseVal;
    const rect = mini.node().getBoundingClientRect();
    const gx = pt[0] / rect.width * vb.width, gy = pt[1] / rect.height * vb.height;
    const t = d3.zoomTransform(svg.node());
    const sw = stage.clientWidth, sh = stage.clientHeight;
    svg.transition().duration(200).call(zoom.transform,
      d3.zoomIdentity.translate(sw / 2 - gx * t.k, sh / 2 - gy * t.k).scale(t.k));
  });

  // ---- search (four modes) ----
  const SI = G.index;
  const searchBox = document.getElementById("search");
  const radiusEl = document.getElementById("radius");
  const filterToggle = document.getElementById("filterToggle");
  const matchCountEl = document.getElementById("matchcount");
  let matches = [], matchIdx = -1;

  function runSearch() {
    const q = (searchBox.value || "").trim().toLowerCase();
    clearClasses();
    if (!q) { matches = []; matchIdx = -1; matchCountEl.textContent = ""; return; }

    const stateHits = new Set();
    const edgeHits = new Set();

    // mode 1: state name
    SI.states.forEach(s => { if (s.name.toLowerCase().includes(q) || s.id.toLowerCase().includes(q)) stateHits.add(s.id); });
    // mode 2: transitions/events/guards/provenance
    SI.transitions.forEach((t, i) => {
      const hay = `${t.event} ${t.guard || ""} ${(t.actions || []).join(" ")}`.toLowerCase();
      if (hay.includes(q)) {
        const e = G.edges.find(e => e.source === t.source && e.target === t.target && e.event === t.event);
        if (e) edgeHits.add(e.id);
      }
    });
    SI.provenance.forEach(p => {
      const hay = `${p.paragraph || ""} ${p.file || ""} ${(p.lines || []).join(" ")}`.toLowerCase();
      if (hay.includes(q)) stateHits.add(p.stateId);
    });

    // mode 2b: external I/O — endpoints, fields, input/output events
    const epHits = new Set();
    (SI.inputs || []).forEach(io => {
      const hay = `${io.label || ""} ${io.event || ""} ${io.field || ""} ${io.endpoint || ""} in input`.toLowerCase();
      if (hay.includes(q)) { stateHits.add(io.state); if (io.endpoint) epHits.add(io.endpoint); }
    });
    (SI.outputs || []).forEach(io => {
      const hay = `${io.label || ""} ${io.event || ""} ${io.field || ""} ${io.endpoint || ""} out output`.toLowerCase();
      if (hay.includes(q)) { stateHits.add(io.state); if (io.endpoint) epHits.add(io.endpoint); }
    });
    (SI.endpoints || []).forEach(ep => {
      const hay = `${ep.label || ""} ${ep.kind || ""} ${ep.id || ""}`.toLowerCase();
      if (hay.includes(q)) epHits.add(ep.id);
    });
    gBoundary.selectAll("g.boundary").classed("match", d => epHits.has(d.endpointId));
    gBoundaryEdges.selectAll("g.bedge").classed("match",
      d => epHits.has(d.endpoint) || stateHits.has(d.state));

    matches = [...stateHits];
    matchIdx = matches.length ? 0 : -1;

    gNodes.selectAll("g.state").classed("match", d => stateHits.has(d.id));
    gEdges.selectAll("g.edge").classed("match", d => edgeHits.has(d.id));

    // mode 3: neighborhood filter (dim non-matches)
    if (filterToggle.checked) applyFilter(stateHits, edgeHits, +radiusEl.value);

    matchCountEl.textContent = `${stateHits.size} state(s), ${edgeHits.size} edge(s)`;
    if (matchIdx >= 0) centerOn(matches[matchIdx]);
  }

  function applyFilter(stateHits, edgeHits, radius) {
    const keep = new Set(stateHits);
    // ancestors for context
    stateHits.forEach(id => { const parts = id.split("."); for (let i = 1; i < parts.length; i++) keep.add(parts.slice(0, i).join(".")); });
    // neighborhood hops
    let frontier = new Set(stateHits);
    for (let r = 0; r < radius; r++) {
      const next = new Set();
      G.edges.forEach(e => {
        if (frontier.has(e.source) && e.target) { keep.add(e.target); next.add(e.target); }
        if (frontier.has(e.target) && e.source) { keep.add(e.source); next.add(e.source); }
      });
      frontier = next;
    }
    gNodes.selectAll("g.state").classed("dim", d => !keep.has(d.id));
    gEdges.selectAll("g.edge").classed("dim", d =>
      !(keep.has(d.source) && keep.has(d.target)) && !edgeHits.has(d.id));
  }

  function clearClasses() {
    gNodes.selectAll("g.state").classed("match", false).classed("dim", false);
    gEdges.selectAll("g.edge").classed("match", false).classed("dim", false);
    gBoundary.selectAll("g.boundary").classed("match", false).classed("dim", false);
    gBoundaryEdges.selectAll("g.bedge").classed("match", false).classed("dim", false);
  }

  function centerOn(id) {
    const n = nodeById[id]; if (!n) return;
    const t = d3.zoomTransform(svg.node());
    const sw = stage.clientWidth, sh = stage.clientHeight;
    const k = Math.max(t.k, 0.9);
    svg.transition().duration(300).call(zoom.transform,
      d3.zoomIdentity.translate(sw / 2 - (n.x + n.width / 2) * k, sh / 2 - (n.y + n.height / 2) * k).scale(k));
  }

  searchBox.addEventListener("input", runSearch);
  radiusEl.addEventListener("input", runSearch);
  filterToggle.addEventListener("change", runSearch);

  // ---- inspector ----
  function inspectState(d) {
    const el = document.getElementById("inspector");
    const h = d.harel || {};
    let html = `<span class="close" onclick="document.getElementById('inspector').classList.remove('show')">✕</span>`;
    html += `<h3>${d.label}</h3><span class="kind">${kindLabel(d.kind)}</span>`;
    if (d.kind === "history") html += `<div class="row"><span class="k">history:</span> ${d.historyDepth}</div>`;
    if (d.entry && d.entry.length) html += `<div class="row"><span class="k">entry:</span> ${d.entry.join("; ")}</div>`;
    if (d.exit && d.exit.length) html += `<div class="row"><span class="k">exit:</span> ${d.exit.join("; ")}</div>`;
    if (h.staticReactions && h.staticReactions.length) html += `<div class="row"><span class="k">static reactions:</span> ${h.staticReactions.join("; ")}</div>`;
    if (h.activities && h.activities.length) html += `<div class="row"><span class="k">activities:</span> ${h.activities.map(a => `${a.name} (${a.binding})`).join("; ")}</div>`;
    if (h.broadcast && h.broadcast.length) html += `<div class="row annot-note">broadcast: ${h.broadcast.map(b => `${b.event}: ${b.from}→${(b.to || []).join(",")}`).join("; ")}${h.sensing ? ` (${h.sensing})` : ""}</div>`;
    if (d.provenance && d.provenance.cobolParagraph) {
      const p = d.provenance;
      html += `<div class="row"><span class="k">COBOL:</span> ${p.file || ""} ${p.cobolParagraph}${p.sourceLines ? ` (lines ${p.sourceLines.join("–")})` : ""}</div>`;
    }
    if (d.kind === "and") {
      const regions = G.nodes.filter(n => n.id.startsWith(d.id + ".") && n.id.split(".").length === d.id.split(".").length + 1);
      html += `<div class="row"><span class="k">regions:</span> ${regions.map(r => r.label + (r.initial ? ` (default ${nodeById[r.initial] ? nodeById[r.initial].label : "?"})` : "")).join(", ")}</div>`;
    }
    const b = d.ioBadges;
    if (b && ((b.in && b.in.length) || (b.out && b.out.length))) {
      const fmt = it => `${it.kind === "event" ? "event" : "field"} ${it.label}` +
        (it.endpoint ? ` ← ${epLabel(it.endpoint)}` : (it.unconfirmed ? " (endpoint unconfirmed)" : ""));
      if (b.in && b.in.length)
        html += `<div class="row"><span class="k">inputs:</span> ${b.in.map(fmt).join("; ")}</div>`;
      if (b.out && b.out.length)
        html += `<div class="row"><span class="k">outputs:</span> ${b.out.map(it =>
          `${it.kind === "event" ? "event" : "field"} ${it.label}` +
          (it.endpoint ? ` → ${epLabel(it.endpoint)}` : "")).join("; ")}</div>`;
    }
    el.innerHTML = html; el.classList.add("show");
  }
  function epLabel(id) {
    const ep = (G.index.endpoints || []).find(e => e.id === id);
    return ep ? `${ep.label} [${ep.kind}]` : id;
  }
  function inspectEndpoint(d) {
    const el = document.getElementById("inspector");
    let html = `<span class="close" onclick="document.getElementById('inspector').classList.remove('show')">✕</span>`;
    html += `<h3>${d.label}</h3><span class="kind">endpoint · ${d.kind}</span>`;
    const ins = (G.index.inputs || []).filter(io => io.endpoint === d.endpointId);
    const outs = (G.index.outputs || []).filter(io => io.endpoint === d.endpointId);
    if (ins.length)
      html += `<div class="row"><span class="k">into program:</span> ${ins.map(io => `${io.label} → ${nodeById[io.state] ? nodeById[io.state].label : io.state}`).join("; ")}</div>`;
    if (outs.length)
      html += `<div class="row"><span class="k">out of program:</span> ${outs.map(io => `${nodeById[io.state] ? nodeById[io.state].label : io.state} → ${io.label}`).join("; ")}</div>`;
    if (d.endpointId === "__unspecified_in__")
      html += `<div class="row annot-note">External input events detected structurally but no endpoint recorded in meta.io — endpoint unconfirmed.</div>`;
    el.innerHTML = html; el.classList.add("show");
  }
  function inspectEdge(e) {
    const el = document.getElementById("inspector");
    const lab = labelFor(e);
    let html = `<span class="close" onclick="document.getElementById('inspector').classList.remove('show')">✕</span>`;
    html += `<h3>transition</h3><span class="kind">${nodeById[e.source] ? nodeById[e.source].label : e.source} → ${nodeById[e.target] ? nodeById[e.target].label : e.target}</span>`;
    html += `<div class="row"><span class="k">label:</span> ${lab.ev} ${lab.gd} ${lab.ac}</div>`;
    el.innerHTML = html; el.classList.add("show");
  }
  function clearInspect() { document.getElementById("inspector").classList.remove("show"); }
  function kindLabel(k) { return { or: "OR-state", and: "AND-state (orthogonal)", basic: "basic state", final: "final state", history: "history" }[k] || k; }

  // ---- controls ----
  document.getElementById("fitBtn").addEventListener("click", fit);
  document.getElementById("mmBtn").addEventListener("click", () => document.getElementById("minimap").classList.toggle("hidden"));
  document.getElementById("lgBtn").addEventListener("click", () => document.getElementById("legend").classList.toggle("hidden"));
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "/" && document.activeElement !== searchBox) { ev.preventDefault(); searchBox.focus(); }
    else if (ev.key === "Escape") { searchBox.value = ""; runSearch(); clearInspect(); }
    else if (ev.key === "f") fit();
    else if (ev.key === "m") document.getElementById("minimap").classList.toggle("hidden");
    else if (ev.key === "n") { if (matches.length) { matchIdx = (matchIdx + (ev.shiftKey ? -1 : 1) + matches.length) % matches.length; centerOn(matches[matchIdx]); } }
  });

  // legend semantics line
  document.getElementById("semantics").textContent =
    "Assumes STATEMATE next-step sensing unless an edge annotation says otherwise.";

  fit();
  updateMinimapViewport(d3.zoomTransform(svg.node()));
})();
