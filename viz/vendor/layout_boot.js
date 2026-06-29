/* layout_boot.js — in-browser ELK layout + viewer boot.
 *
 * A faithful port of the skill's scripts/elk_layout.mjs, adapted to run in the
 * browser instead of Node: it reads the raw ELK graph from the inlined
 * <script id="raw-graph"> block, lays it out with the global elkjs bundle
 * (ELK), flattens to absolute coordinates exactly as elk_layout.mjs does, sets
 * window.GRAPH, then evaluates the inlined viewer source.
 *
 * Moving layout client-side is what lets the generating program stay pure
 * Python (no Node at build time) while the emitted HTML stays self-contained.
 * The layout math below is line-for-line the same as the skill's so fidelity is
 * identical.
 */
(function () {
  "use strict";

  function setStatus(msg, isError) {
    var el = document.getElementById("boot-status");
    if (!el) return;
    if (msg === null) { el.style.display = "none"; return; }
    el.style.display = "flex";
    el.className = isError ? "error" : "";
    el.querySelector(".boot-msg").textContent = msg;
  }

  function layout(graph) {
    // Rough text sizing so leaf boxes fit their labels AND their entry/exit/SR
    // compartments (now shown at L2). Width tracks the widest visible line so the
    // PERFORM entry actions don't overflow the box; height reserves one row per
    // compartment.
    var CHAR_W = 7.2, LINE_H = 18, PAD_X = 16, PAD_Y = 12;
    function leafSize(node) {
      var lines = [(node.labels && node.labels[0] && node.labels[0].text) || node.id];
      (node.entry || []).forEach(function (a) { lines.push("entry / " + a); });
      (node.exit || []).forEach(function (a) { lines.push("exit / " + a); });
      ((node.harel && node.harel.staticReactions) || []).forEach(function (sr) {
        lines.push("SR: " + sr);
      });
      var maxLen = lines.reduce(function (m, s) { return Math.max(m, s.length); }, 0);
      var w = Math.max(70, maxLen * CHAR_W + PAD_X * 2);
      var rows = node.entry.length + node.exit.length +
        (node.harel.staticReactions ? node.harel.staticReactions.length : 0);
      return { w: w, h: LINE_H + PAD_Y * 2 + rows * LINE_H };
    }

    var HEADER_H = 22, REGION_PAD = 10;

    function buildElk(path) {
      var n = graph.nodes[path];
      var kids = n.children || [];
      var base = {
        id: path,
        labels: [{ text: (n.labels[0] && n.labels[0].text) || path }],
        _kind: n.kind, _depth: n.depth, _harel: n.harel, _entry: n.entry,
        _exit: n.exit, _provenance: n.provenance, _initial: n.initial || null,
        _historyDepth: n.historyDepth || null, _description: n.description || null,
      };
      if (kids.length === 0) {
        var s = leafSize(n);
        base.width = s.w; base.height = s.h;
        return base;
      }
      base.layoutOptions = {
        "elk.algorithm": "layered",
        "elk.direction": "DOWN",
        "elk.padding": "[top=" + (HEADER_H + REGION_PAD) + ",left=" + REGION_PAD +
          ",bottom=" + REGION_PAD + ",right=" + REGION_PAD + "]",
        "elk.spacing.nodeNode": "24",
        "elk.layered.spacing.nodeNodeBetweenLayers": "28",
      };
      base.children = kids.map(buildElk);
      base.edges = [];
      return base;
    }

    function lca(aPath, bPath) {
      if (!aPath || !bPath) return graph.root;
      var a = aPath.split("."), b = bPath.split(".");
      var i = 0;
      while (i < a.length && i < b.length && a[i] === b[i]) i++;
      var common = a.slice(0, i).join(".");
      return common || graph.root;
    }

    var root = buildElk(graph.root);
    root.layoutOptions = {
      "elk.algorithm": "layered",
      "elk.direction": "DOWN",
      "elk.hierarchyHandling": "INCLUDE_CHILDREN",
      "elk.spacing.nodeNode": "28",
      "elk.layered.spacing.nodeNodeBetweenLayers": "32",
      "elk.edgeRouting": "ORTHOGONAL",
    };

    var containerById = {};
    (function indexContainers(node) {
      containerById[node.id] = node;
      (node.children || []).forEach(indexContainers);
    })(root);

    for (var ei = 0; ei < graph.edges.length; ei++) {
      var e = graph.edges[ei];
      if (e.internal || !e.target || (typeof e.target === "string" && e.target.startsWith("#"))) {
        continue;
      }
      var host = containerById[lca(e.source, e.target)] || root;
      host.edges = host.edges || [];
      host.edges.push({
        id: e.id, sources: [e.source], targets: [e.target],
        _event: e.event, _guard: e.guard, _actions: e.actions,
      });
    }

    var elk = new ELK();
    return elk.layout(root).then(function (laid) {
      var out = { root: graph.root, nodes: [], edges: [], index: graph.index };

      function flatten(node, ox, oy) {
        var ax = ox + (node.x || 0), ay = oy + (node.y || 0);
        if (node.id !== "root" || graph.nodes[node.id]) {
          out.nodes.push({
            id: node.id,
            x: ax, y: ay, width: node.width, height: node.height,
            kind: node._kind, depth: node._depth, harel: node._harel,
            entry: node._entry, exit: node._exit, provenance: node._provenance,
            initial: node._initial, historyDepth: node._historyDepth,
            description: node._description,
            label: (node.labels && node.labels[0] && node.labels[0].text) || node.id,
            isContainer: !!(node.children && node.children.length),
            ioBadges: graph.nodes[node.id] ? (graph.nodes[node.id].ioBadges || null) : null,
          });
        }
        (node.edges || []).forEach(function (e) {
          var secs = (e.sections || []).map(function (s) {
            return {
              start: { x: ax + s.startPoint.x, y: ay + s.startPoint.y },
              bends: (s.bendPoints || []).map(function (b) { return { x: ax + b.x, y: ay + b.y }; }),
              end: { x: ax + s.endPoint.x, y: ay + s.endPoint.y },
            };
          });
          out.edges.push({
            id: e.id, source: e.sources[0], target: e.targets[0],
            event: e._event, guard: e._guard, actions: e._actions, sections: secs,
          });
        });
        (node.children || []).forEach(function (c) { flatten(c, ax, ay); });
      }
      flatten(laid, 0, 0);

      graph.edges.forEach(function (e) {
        if (e.internal && e.source) {
          out.edges.push({
            id: e.id, source: e.source, target: null,
            event: e.event, guard: e.guard, actions: e.actions, internal: true, sections: [],
          });
        }
      });

      out.width = laid.width; out.height = laid.height;

      // external-boundary placement (left/right gutters), identical to elk_layout.mjs
      var boundary = graph.boundary || { nodes: [], edges: [] };
      var nodeAbs = {};
      out.nodes.forEach(function (n) { nodeAbs[n.id] = n; });

      var GUT = 220, BN_W = 150, BN_H = 46, BN_VGAP = 24;
      var epDir = {};
      boundary.edges.forEach(function (e) {
        epDir[e.endpoint] = epDir[e.endpoint] || { in: 0, out: 0 };
        epDir[e.endpoint][e.direction]++;
      });
      var leftEps = [], rightEps = [];
      boundary.nodes.forEach(function (bn) {
        var d = epDir[bn.endpointId] || { in: 0, out: 0 };
        (d.in >= d.out ? leftEps : rightEps).push(bn);
      });
      function placeColumn(eps, x) {
        var total = eps.length * BN_H + Math.max(0, eps.length - 1) * BN_VGAP;
        var y = (out.height - total) / 2;
        eps.forEach(function (bn) {
          bn.x = x; bn.y = y; bn.width = BN_W; bn.height = BN_H;
          bn.isBoundary = true;
          y += BN_H + BN_VGAP;
          out.nodes.push(bn);
        });
      }
      placeColumn(leftEps, -GUT);
      placeColumn(rightEps, out.width + GUT - BN_W);

      var bnById = {};
      boundary.nodes.forEach(function (bn) { bnById[bn.id] = bn; });
      boundary.edges.forEach(function (be) {
        var st = nodeAbs[be.state]; var bn = bnById[be.endpointNode];
        if (!st || !bn) return;
        var stPt = { x: st.x + (be.direction === "in" ? 0 : st.width), y: st.y + st.height / 2 };
        var bnPt = { x: bn.x + (bn.x < 0 ? bn.width : 0), y: bn.y + bn.height / 2 };
        var start = be.direction === "in" ? bnPt : stPt;
        var end = be.direction === "in" ? stPt : bnPt;
        out.boundaryEdges = out.boundaryEdges || [];
        out.boundaryEdges.push({
          id: be.id, endpoint: be.endpoint, state: be.state,
          direction: be.direction, kind: be.kind, label: be.label,
          unconfirmedEndpoint: !!be.unconfirmedEndpoint,
          sections: [{ start: start, bends: [], end: end }],
        });
      });
      out.boundaryNodes = out.nodes.filter(function (n) { return n.isBoundary; });
      out.index = graph.index;

      var minX = 0, maxX = out.width;
      out.nodes.forEach(function (n) {
        if (n.x < minX) minX = n.x;
        if (n.x + n.width > maxX) maxX = n.x + n.width;
      });
      out.viewMinX = minX; out.viewMaxX = maxX;

      return out;
    });
  }

  function boot() {
    if (typeof ELK === "undefined") {
      setStatus("elkjs failed to load — cannot lay out the statechart.", true);
      return;
    }
    var raw;
    try {
      raw = JSON.parse(document.getElementById("raw-graph").textContent);
    } catch (err) {
      setStatus("Could not parse the embedded graph: " + err.message, true);
      return;
    }
    setStatus("Laying out " + (raw.index ? raw.index.states.length : "?") + " states…", false);
    layout(raw).then(function (laid) {
      window.GRAPH = laid;
      setStatus(null);
      var code = document.getElementById("viewer-src").textContent;
      // eslint-disable-next-line no-eval
      (0, eval)(code); // runs the (unmodified) viewer IIFE now that GRAPH exists
    }).catch(function (err) {
      setStatus("Layout failed: " + (err && err.message ? err.message : err), true);
      // eslint-disable-next-line no-console
      console.error(err);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
