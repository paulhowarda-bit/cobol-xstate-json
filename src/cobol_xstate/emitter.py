"""Stage 5 - lower the captured *semantics* to a runnable XState v5 ``setup()`` module.

The JSON statechart (stage 4) references every guard and action **by name only** - the
meaning lives in ``semantics`` (``target := expr`` assignments and Boolean condition
trees) and ``data`` (PIC/USAGE types). That bundle is a faithful *contract* but it does
not run: nothing implements the names.

This module closes that gap. It emits an ES module that pairs the same machine config
with an XState v5 ``setup({ guards, actions })`` block whose bodies implement the
semantics over the **fixed-point decimal** runtime (``cobolRuntime.mjs``), never binary
float - so the receiving field's PICTURE (digits / scale / sign) is honored on every
store, exactly as COBOL would.

What is and isn't modeled (kept honest):

* Data verbs (``MOVE``/``ADD``/``COMPUTE``/...) become ``assign`` actions computed with
  decimal arithmetic and stored through the receiver's type. COBOL arithmetic
  expressions are parsed here (``+ - * / **``, parens, refs, literals, figuratives).
* Conditions (relational / class / sign / 88-level / AND-OR-NOT) become guard functions.
* ``PERFORM`` is rewritten into a real call-return: each performed paragraph becomes an
  XState actor, the PERFORM site ``invoke``s it (context in as ``input``, result assigned
  back on ``onDone``), so the machine runs end-to-end under stock ``createActor`` with
  WORKING-STORAGE threaded through nested calls. See the ``PERFORM -> invoke`` section.
* Other effects (``DISPLAY``/``OPEN``/``READ``/exec) are emitted as no-ops - they change
  no modeled data; sequential file I/O is supplied by the golden-master driver.
* Conditions that ride on runtime/external state (I-O ``AT END``/``INVALID KEY``,
  ALTER-switch, ``GO TO ... DEPENDING ON``, and any ``{op:'raw'}`` fallback) become
  **external guards**: they read an explicit ``context.__cobol_external`` channel
  (default ``false``) rather than being invented. The driver/harness supplies them.
* Anything un-parseable emits a body that calls ``notModeled(...)`` - it throws if and
  only if that path actually executes, never silently wrong.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Dict, List, Optional, Tuple

from .data_division import expand_pic
from .statechart import Machine

RUNTIME_IMPORT = "./cobolRuntime.mjs"
_HELPERS = ("D", "add", "sub", "mul", "div", "pow", "store", "storeStr",
            "elem", "setElem", "rel", "isClass", "isSign", "notModeled")

# An OCCURS subscript / reference-modification: NAME( inner ). `inner` may be one
# identifier or integer (the common case), an arithmetic expression (TBL(I - 1)), or a
# multi-dimension list (TBL(I, J)). The first two are emittable; multi-dimension is not
# (the data dictionary models one OCCURS dimension), so it is routed out honestly.
_SUBSCRIPT = re.compile(r"^([A-Za-z][A-Za-z0-9-]*)\((.+)\)$")
_SIMPLE_SUB = re.compile(r"^[A-Za-z0-9-]+$")


def _split_subscript(tok: str) -> Tuple[str, Optional[str]]:
    """Return (name, inner) for a single-dimension subscript, else (tok, None). A
    multi-dimension or reference-modification (`a:b`) subscript returns (tok, None) so the
    caller degrades to an external guard / notModeled rather than emitting wrong JS."""
    m = _SUBSCRIPT.match(tok)
    if not m:
        return tok, None
    inner = m.group(2).strip()
    if "," in inner or ":" in inner:   # multi-dimension or reference modification
        return tok, None
    return m.group(1), inner


def _subscript_js(sub: str) -> str:
    """JS for the subscript value: an integer literal, a subscript variable's value, or an
    arithmetic subscript expression (TBL(I - 1)) evaluated with the decimal runtime.
    Raises ``_ExprError`` if the subscript expression cannot be parsed."""
    if _is_num_literal(sub):
        return _js_str(sub)
    if _SIMPLE_SUB.match(sub):
        return f"context[{_js_str(sub.upper())}]"
    return _emit_numeric_expr(sub)   # arithmetic subscript -> a Dec; elem() coerces it

_FIGURATIVE_NUM = {"ZERO": "0", "ZEROS": "0", "ZEROES": "0"}
_FIGURATIVE_STR = {"SPACE": "", "SPACES": "", "ZERO": "0", "ZEROS": "0", "ZEROES": "0"}
_NUM_OPS = {"+": "add", "-": "sub", "*": "mul", "/": "div", "**": "pow"}


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def _js_str(s: str) -> str:
    """A double-quoted JS string literal for arbitrary text."""
    return json.dumps(s)


def _is_num_literal(tok: str) -> bool:
    t = tok.lstrip("+-")
    return t.replace(".", "", 1).isdigit() and t != ""


def _is_str_literal(tok: str) -> bool:
    return tok[:1] in ("'", '"')


def _unquote(tok: str) -> str:
    if _is_str_literal(tok):
        return tok[1:-1] if len(tok) >= 2 else ""
    return tok


# --------------------------------------------------------------------------- #
# COBOL arithmetic expression parser  ->  decimal-runtime JS
# --------------------------------------------------------------------------- #

class _ExprError(Exception):
    pass


def _tokenize_expr(expr: str) -> List[str]:
    """Tokenize a COBOL arithmetic expression. COBOL mandates spaces around binary
    operators (so a hyphen inside ``WS-NET-PAY`` is unambiguous), which lets us split on
    whitespace and then peel abutting parentheses off operand tokens."""
    out: List[str] = []
    for raw in expr.split():
        if _SUBSCRIPT.match(raw):   # a subscript token: keep its parens, don't peel
            out.append(raw)
            continue
        # peel leading '(' and trailing ')'
        while raw.startswith("("):
            out.append("(")
            raw = raw[1:]
        trailing = 0
        while raw.endswith(")"):
            trailing += 1
            raw = raw[:-1]
        if raw:
            out.append(raw)
        out.extend([")"] * trailing)
    return out


class _ExprParser:
    """Recursive-descent over ``expr := add``; precedence (low->high):
    ``+ -`` , ``* /`` , ``**`` (right-assoc), unary ``+ -`` , primary."""

    def __init__(self, tokens: List[str]):
        self.toks = tokens
        self.i = 0

    def _peek(self) -> Optional[str]:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _adv(self) -> str:
        t = self.toks[self.i]
        self.i += 1
        return t

    def parse(self) -> dict:
        node = self._add()
        if self.i != len(self.toks):
            raise _ExprError(f"trailing tokens: {self.toks[self.i:]}")
        return node

    def _add(self) -> dict:
        node = self._mul()
        while self._peek() in ("+", "-"):
            op = self._adv()
            node = {"op": op, "l": node, "r": self._mul()}
        return node

    def _mul(self) -> dict:
        node = self._power()
        while self._peek() in ("*", "/"):
            op = self._adv()
            node = {"op": op, "l": node, "r": self._power()}
        return node

    def _power(self) -> dict:
        node = self._unary()
        if self._peek() == "**":
            self._adv()
            return {"op": "**", "l": node, "r": self._power()}  # right-assoc
        return node

    def _unary(self) -> dict:
        if self._peek() in ("+", "-"):
            op = self._adv()
            node = self._unary()
            if op == "-":
                return {"op": "-", "l": {"kind": "num", "text": "0"}, "r": node}
            return node
        return self._primary()

    def _primary(self) -> dict:
        t = self._peek()
        if t is None:
            raise _ExprError("unexpected end of expression")
        if t == "(":
            self._adv()
            node = self._add()
            if self._peek() != ")":
                raise _ExprError("missing ')'")
            self._adv()
            return node
        self._adv()
        if t in ("+", "-", "*", "/", "**", ")"):
            raise _ExprError(f"unexpected operator {t!r}")
        if _is_str_literal(t):
            return {"kind": "num", "text": _unquote(t)}
        if _is_num_literal(t):
            return {"kind": "num", "text": t}
        name, sub = _split_subscript(t)
        if sub is not None:
            return {"kind": "ref", "name": name.upper(), "sub": sub}
        up = t.upper()
        if up in _FIGURATIVE_NUM:
            return {"kind": "num", "text": _FIGURATIVE_NUM[up]}
        return {"kind": "ref", "name": up}


def _emit_num_node(node: dict) -> str:
    if "kind" in node:
        if node["kind"] == "num":
            return f'D({_js_str(node["text"])})'
        if node.get("sub") is not None:  # TBL(I) -> the i-th element
            return (f'D(elem(context[{_js_str(node["name"])}], '
                    f'{_subscript_js(node["sub"])}))')
        return f'D(context[{_js_str(node["name"])}])'
    fn = _NUM_OPS[node["op"]]
    return f'{fn}({_emit_num_node(node["l"])}, {_emit_num_node(node["r"])})'


def _emit_numeric_expr(expr: str) -> str:
    """COBOL arithmetic expression -> a JS expression yielding a runtime ``Dec``.
    Raises ``_ExprError`` if it cannot be parsed (caller falls back to notModeled)."""
    node = _ExprParser(_tokenize_expr(expr)).parse()
    return _emit_num_node(node)


def _emit_string_expr(expr: str, fields: Dict[str, dict]) -> str:
    """A MOVE/SET source into an alphanumeric receiver -> a JS string-valued expression."""
    tok = expr.strip()
    if _is_str_literal(tok):
        return _js_str(_unquote(tok))
    up = tok.upper()
    if up in _FIGURATIVE_STR:
        return _js_str(_FIGURATIVE_STR[up])
    if _is_num_literal(tok):
        return _js_str(tok)
    name, sub = _split_subscript(tok)
    if sub is not None and name.upper() in fields:  # TBL(I) text element
        return f"elem(context[{_js_str(name.upper())}], {_subscript_js(sub)})"
    if up in fields:  # a field reference
        return f"context[{_js_str(up)}]"
    # multi-token or unrecognized source (e.g. an expression into a text item)
    raise _ExprError(f"non-elementary string source: {expr!r}")


# --------------------------------------------------------------------------- #
# field type table
# --------------------------------------------------------------------------- #

def _field_table(machine: Machine) -> Dict[str, dict]:
    """Per-field type spec the runtime needs to store/compare faithfully."""
    out: Dict[str, dict] = {}
    for name, d in machine.data.items():
        if d.get("kind") == "condition-name":
            continue
        t = d.get("type") or {}
        cat = t.get("category", "unknown")
        if cat.startswith("numeric"):
            spec = {
                "category": "numeric",
                "digits": t.get("digits", 0),
                "scale": t.get("scale", 0),
                "signed": bool(t.get("signed", False)),
            }
        elif cat == "group":
            continue
        else:
            spec = {"category": cat}
            pic = t.get("pic")
            if pic:
                exp = expand_pic(pic)
                spec["len"] = sum(1 for c in exp.upper() if c not in "SV")
        if d.get("occurs"):  # OCCURS n -> the spec describes one element of an n-array
            spec["occurs"] = d["occurs"]
        out[name] = spec
    return out


def _field_spec_js(name: str, rounded: bool) -> str:
    ref = f"FIELDS[{_js_str(name)}]"
    if rounded:
        return f"{{ ...{ref}, rounded: true }}"
    return ref


# --------------------------------------------------------------------------- #
# actions  ->  ops (data) + effect no-ops
# --------------------------------------------------------------------------- #

_DATA_KINDS = {"assign", "arith", "compute", "initialize", "input"}


def _emit_assignment_value(target: str, expr: str, kind: str, rounded: bool,
                           fields: Dict[str, dict]) -> str:
    fld = fields.get(target, {"category": "unknown"})
    numeric = fld.get("category") == "numeric"
    spec = _field_spec_js(target, rounded)
    if kind == "initialize":
        if numeric:
            return f'store(D("0"), {spec})'
        return f'storeStr("", {spec})'
    if kind == "input":
        chan = f'(context.__cobol_external || {{}})[{_js_str(target)}]'
        if numeric:
            return f'store(D({chan} != null ? {chan} : "0"), {spec})'
        return f'storeStr({chan} != null ? {chan} : "", {spec})'
    try:
        if numeric:
            return f"store({_emit_numeric_expr(expr)}, {spec})"
        return f"storeStr({_emit_string_expr(expr, fields)}, {spec})"
    except _ExprError as e:
        return f'notModeled({_js_str(f"expr {expr!r} -> {target}: {e}")})'


def _build_ops(machine: Machine, fields: Dict[str, dict]
               ) -> Tuple[Dict[str, str], List[str]]:
    """Return (ops, effect_names). ``ops[name]`` is the body of ``(context) => ({...})``;
    effect_names are data-less actions emitted as no-ops."""
    ops: Dict[str, str] = {}
    effects: List[str] = []
    actions = machine.semantics.get("actions", {})
    for name, spec in actions.items():
        kind = spec.get("kind")
        if kind not in _DATA_KINDS or not spec.get("assignments"):
            effects.append(name)
            continue
        rounded = bool(spec.get("rounded"))
        pairs = []
        for a in spec["assignments"]:
            base, sub = _split_subscript(a["target"])
            if sub is not None:  # MOVE/COMPUTE ... INTO TBL(I): replace one element
                bu = base.upper()
                val = _emit_assignment_value(bu, a.get("expr", ""), kind, rounded, fields)
                val = f"setElem(context[{_js_str(bu)}], {_subscript_js(sub)}, {val})"
                pairs.append(f"{_js_str(bu)}: {val}")
            else:
                val = _emit_assignment_value(a["target"], a.get("expr", ""), kind,
                                             rounded, fields)
                pairs.append(f"{_js_str(a['target'])}: {val}")
        ops[name] = "{ " + ", ".join(pairs) + " }"
    return ops, effects


# --------------------------------------------------------------------------- #
# guards  ->  guard functions (data) + external guards
# --------------------------------------------------------------------------- #

def _operand_js(tok: str, fields: Dict[str, dict]) -> Tuple[str, bool]:
    """Return (js_value, is_numeric) for a relational operand."""
    up = tok.upper()
    if _is_str_literal(tok):
        return _js_str(_unquote(tok)), False
    if up in _FIGURATIVE_STR:
        return _js_str(_FIGURATIVE_STR[up]), up in _FIGURATIVE_NUM
    if _is_num_literal(tok):
        return _js_str(tok), True
    name, sub = _split_subscript(tok)
    if sub is not None:  # TBL(I) operand
        fld = fields.get(name.upper())
        numeric = bool(fld and fld.get("category") == "numeric")
        return f"elem(context[{_js_str(name.upper())}], {_subscript_js(sub)})", numeric
    # An arithmetic-expression operand (WS-A + WS-B, TBL(I) * 2): evaluate it with the
    # decimal runtime and compare numerically. COBOL spaces binary operators, so a real
    # operator is surrounded by whitespace - a hyphen inside WS-NET-PAY is not.
    if re.search(r"\s(?:\*\*|[-+*/])\s|\*\*", tok):
        try:
            return _emit_numeric_expr(tok), True
        except _ExprError:
            pass
    # A subscript / reference-modification we could not resolve faithfully (multi-dim,
    # nested, ref-mod): do NOT emit context["TBL(I,J)"] (silently undefined) - signal the
    # caller to route this to an external guard / notModeled instead.
    if "(" in tok and up not in fields:
        raise _ExprError(f"unresolved subscript operand {tok!r}")
    fld = fields.get(up)
    numeric = bool(fld and fld.get("category") == "numeric")
    return f"context[{_js_str(up)}]", numeric


def _emit_guard(tree: dict, fields: Dict[str, dict]) -> Optional[str]:
    """Boolean condition tree -> JS bool expression, or ``None`` if it can't be modeled
    (caller routes it to an external guard, honestly, rather than inventing a truth)."""
    try:
        return _emit_guard_inner(tree, fields)
    except _ExprError:
        return None  # an operand could not be faithfully emitted -> external guard


def _emit_guard_inner(tree: dict, fields: Dict[str, dict]) -> Optional[str]:
    op = tree.get("op")
    if op == "and":
        parts = [_emit_guard(a, fields) for a in tree["args"]]
        if any(p is None for p in parts):
            return None
        return "(" + " && ".join(parts) + ")"
    if op == "or":
        parts = [_emit_guard(a, fields) for a in tree["args"]]
        if any(p is None for p in parts):
            return None
        return "(" + " || ".join(parts) + ")"
    if op == "not":
        inner = _emit_guard(tree["arg"], fields)
        return f"(!{inner})" if inner is not None else None
    if op == "rel":
        lval, lnum = _operand_js(str(tree["left"]), fields)
        rval, rnum = _operand_js(str(tree["right"]), fields)
        numeric = "true" if (lnum or rnum) else "false"
        return f'rel({lval}, {_js_str(tree["rel"])}, {rval}, {numeric})'
    if op == "class":
        operand, _ = _operand_js(str(tree["operand"]), fields)
        expr = f'isClass({operand}, {_js_str(tree["class"])})'
        return f"(!{expr})" if tree.get("negated") else expr
    if op == "sign":
        operand, _ = _operand_js(str(tree["operand"]), fields)
        expr = f'isSign({operand}, {_js_str(tree["sign"])})'
        return f"(!{expr})" if tree.get("negated") else expr
    if op == "cond-name":
        parent = tree.get("parent")
        values = tree.get("values") or []
        ranges = tree.get("ranges") or []
        if not parent or (not values and not ranges):
            return None
        pkey = str(parent).upper()
        fld = fields.get(pkey)
        numeric = "true" if (fld and fld.get("category") == "numeric") else "false"
        pref = f"context[{_js_str(pkey)}]"
        tests = []
        for v in values:
            rval, _ = _operand_js(str(v), fields)
            tests.append(f'rel({pref}, "=", {rval}, {numeric})')
        for lo, hi in ranges:  # 88 VALUE lo THRU hi  ->  lo <= parent <= hi
            loj, _ = _operand_js(str(lo), fields)
            hij, _ = _operand_js(str(hi), fields)
            tests.append(f'(rel({pref}, ">=", {loj}, {numeric}) && '
                         f'rel({pref}, "<=", {hij}, {numeric}))')
        return "(" + " || ".join(tests) + ")"
    return None  # raw / unknown -> external


def _build_guards(machine: Machine, referenced: set, fields: Dict[str, dict]
                  ) -> Tuple[Dict[str, str], List[str]]:
    guard_sem = machine.semantics.get("guards", {})
    guard_fns: Dict[str, str] = {}
    for name, tree in guard_sem.items():
        js = _emit_guard(tree, fields)
        if js is not None:
            guard_fns[name] = js
    external = sorted(g for g in referenced if g not in guard_fns)
    return guard_fns, external


# --------------------------------------------------------------------------- #
# machine config (strip provenance meta; numeric context -> decimal strings)
# --------------------------------------------------------------------------- #

def _strip_meta(obj):
    if isinstance(obj, dict):
        return {k: _strip_meta(v) for k, v in obj.items() if k != "meta"}
    if isinstance(obj, list):
        return [_strip_meta(v) for v in obj]
    return obj


def _js_context(config: dict, fields: Dict[str, dict]) -> dict:
    def _num_str(x):
        return format(x, "f") if isinstance(x, float) else str(x)

    ctx = dict(config.get("context", {}))
    for k, v in list(ctx.items()):
        fld = fields.get(k)
        numeric = fld and fld.get("category") == "numeric"
        if isinstance(v, list):  # an OCCURS table
            ctx[k] = [_num_str(e) if numeric and not isinstance(e, str) else e for e in v]
        elif numeric and not isinstance(v, str):
            ctx[k] = _num_str(v)
    return ctx


def _collect_referenced(config: dict) -> Tuple[set, set]:
    """(action_names, guard_names) referenced anywhere in the machine config."""
    actions: set = set()
    guards: set = set()

    def walk(o):
        if isinstance(o, dict):
            for key in ("entry", "exit"):
                for a in o.get(key, []) or []:
                    if isinstance(a, str):
                        actions.add(a)
            if isinstance(o.get("guard"), str):
                guards.add(o["guard"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(config)
    return actions, guards


# --------------------------------------------------------------------------- #
# PERFORM -> invoke: call-return via per-paragraph actor machines
# --------------------------------------------------------------------------- #
#
# The flat config models a PERFORM as a no-op `perform_X` entry action: the target
# paragraph's states are reachable only by fall-through, never by the PERFORM, so the
# call never executes its body (XState has no call stack). Here we rebuild the runnable
# machine so a PERFORM is a real call-return: each performed paragraph becomes an XState
# actor; a PERFORM site `invoke`s it, passing the whole context as input and assigning
# the actor's output back on `onDone`. WORKING-STORAGE therefore threads through every
# (possibly nested) call exactly as COBOL's shared storage would. A paragraph "returns"
# when control would fall through to a *different* paragraph (rerouted to a final
# `__RET__` state). GO TO into another paragraph is indistinguishable from fall-through
# once provenance is stripped, so it is modeled as a return too (documented limitation).

def _para_of(key: str) -> str:
    return key.split("__", 1)[0]


def _transition_targets(state: dict) -> List[str]:
    out: List[str] = []
    for t in state.get("always", []) or []:
        if "target" in t:
            out.append(t["target"])
    inv = state.get("invoke")
    if inv and inv.get("onDone", {}).get("target"):
        out.append(inv["onDone"]["target"])
    on = state.get("on")
    if isinstance(on, dict):  # event-driven handler edges (orthogonal HANDLERS region)
        for v in on.values():
            for item in (v if isinstance(v, list) else [v]):
                if isinstance(item, str):
                    out.append(item)
                elif isinstance(item, dict) and item.get("target"):
                    out.append(item["target"])
    return out


def _emit_split(key: str, st: dict, out: dict, buildable: set, needed: set) -> None:
    """Rewrite one state into a chain so each PERFORM of a buildable paragraph becomes an
    `invoke` sub-state. Non-PERFORM entry actions and PERFORMs of non-buildable targets
    stay as ordinary (no-op) actions. The state's control (always/type/...) rides on the
    last node so inbound transitions to `key` still land on the first executed node."""
    entry = st.get("entry", []) or []
    has_invoke_perform = any(
        a.startswith("perform_") and a[len("perform_"):] in buildable for a in entry)
    if not has_invoke_perform:
        out[key] = st
        return

    segments: List[Tuple[str, object]] = []
    cur: List[str] = []
    for a in entry:
        para = a[len("perform_"):] if a.startswith("perform_") else None
        if para is not None and para in buildable:
            segments.append(("ops", cur)); cur = []
            segments.append(("perform", para))
            needed.add(para)
        else:
            cur.append(a)
    segments.append(("ops", cur))

    steps = [s for s in segments if s[0] == "perform" or (s[0] == "ops" and s[1])]
    control = {k: v for k, v in st.items() if k != "entry"}
    if steps and steps[-1][0] == "ops":  # fold trailing ops into the control node
        control = {"entry": steps[-1][1], **control}
        steps = steps[:-1]

    n = len(steps)
    ids = [key] + [f"{key}__k{i}" for i in range(1, n + 1)]
    for i, step in enumerate(steps):
        sid, nxt = ids[i], ids[i + 1]
        if step[0] == "ops":
            out[sid] = {"entry": step[1], "always": [{"target": nxt}]}
        else:
            out[sid] = {"invoke": {"src": f"actor:{step[1]}", "onDone": {"target": nxt}}}
    out[ids[n]] = control


def _target_owner(target: str, ordered: List[str]) -> Tuple[Optional[set], Optional[str]]:
    """Resolve a PERFORM target to (owner_paragraph_set, initial_paragraph). A plain name
    owns just itself; ``HEAD__THRU__TAIL`` owns the source-order paragraph span head..tail
    (PERFORM p THRU q runs p through q, then returns)."""
    if "__THRU__" in target:
        head, tail = target.split("__THRU__", 1)
        if head in ordered and tail in ordered:
            i, j = ordered.index(head), ordered.index(tail)
            if i <= j:
                return set(ordered[i:j + 1]), head
        return None, None
    return ({target}, target) if target in ordered else (None, None)


def _buildable_targets(pool: dict, ordered: List[str]) -> set:
    performed = {a[len("perform_"):] for st in pool.values()
                 for a in (st.get("entry", []) or []) if a.startswith("perform_")}
    return {t for t in performed if _target_owner(t, ordered)[0] is not None}


def _reroute_to_return(states: dict, owner: set) -> None:
    """Inside an actor, any transition leaving the owned paragraph(s) (fall-through past the
    range, GO TO out, or the program-end sentinel) is the return point."""
    def leaves(tgt: str) -> bool:
        return bool(tgt) and (_para_of(tgt) not in owner or tgt == "__END__")

    for st in states.values():
        for t in st.get("always", []) or []:
            if leaves(t.get("target")):
                t["target"] = "__RET__"
        inv = st.get("invoke")
        if inv and inv.get("onDone") and leaves(inv["onDone"].get("target")):
            inv["onDone"]["target"] = "__RET__"


def _prune(states: dict, initial: str) -> dict:
    seen: set = set()
    stack = [initial]
    while stack:
        k = stack.pop()
        if k in seen or k not in states:
            continue
        seen.add(k)
        stack.extend(_transition_targets(states[k]))
    return {k: v for k, v in states.items() if k in seen}


def _build_actors(pool: dict, buildable: set, seed: set,
                  ordered: List[str]) -> Dict[str, dict]:
    """Build an actor config for every PERFORM target reachable from `seed`, slicing the
    owned paragraph(s) out of the shared `pool` (so cross-region and THRU-range PERFORMs
    resolve). A range target owns its whole paragraph span; a plain target owns itself."""
    actor_configs: Dict[str, dict] = {}
    work = list(seed)
    while work:
        target = work.pop()
        name = f"actor:{target}"
        if name in actor_configs:
            continue
        owner, initial = _target_owner(target, ordered)
        if owner is None:
            continue
        own = copy.deepcopy({k: v for k, v in pool.items() if _para_of(k) in owner})
        if initial not in own:
            continue
        needed: set = set()
        states: dict = {}
        for k, st in own.items():
            _emit_split(k, st, states, buildable, needed)
        _reroute_to_return(states, owner)
        states["__RET__"] = {"type": "final"}
        actor_configs[name] = {"initial": initial, "states": states}
        work.extend(needed)
    return actor_configs


def _invoke_transform(orig_states: dict, initial: str,
                      ordered: List[str]) -> Tuple[dict, Dict[str, dict]]:
    """Return (main_states, actor_configs). Performs become invokes of actor machines;
    main is pruned to what is reachable from `initial` (the un-performed paragraph copies
    fall away). Each actor config is {initial, states} with a `__RET__` final."""
    buildable = _buildable_targets(orig_states, ordered)
    main_src = copy.deepcopy(orig_states)
    main_new: dict = {}
    sink: set = set()
    for k, st in main_src.items():
        _emit_split(k, st, main_new, buildable, sink)
    main_new = _prune(main_new, initial)

    seed = {inv["src"][len("actor:"):] for s in main_new.values()
            for inv in [s.get("invoke") or {}] if inv.get("src")}
    return main_new, _build_actors(orig_states, buildable, seed, ordered)


def _invoke_transform_parallel(regions: dict,
                               ordered: List[str]) -> Tuple[dict, Dict[str, dict]]:
    """Parallel (DECLARATIVES/HANDLE) machine: transform each region's flow into invokes,
    building actors from a pool unioned across all regions so a handler can PERFORM a
    main-flow paragraph and vice versa."""
    pool: dict = {}
    for r in regions.values():
        pool.update(r.get("states", {}))
    buildable = _buildable_targets(pool, ordered)

    new_regions: dict = {}
    seed: set = set()
    for name, r in regions.items():
        src = copy.deepcopy(r.get("states", {}))
        new_states: dict = {}
        sink: set = set()
        for k, st in src.items():
            _emit_split(k, st, new_states, buildable, sink)
        nr = dict(r)
        nr["states"] = _prune(new_states, r["initial"])
        new_regions[name] = nr
        seed |= sink

    return new_regions, _build_actors(pool, buildable, seed, ordered)


# --------------------------------------------------------------------------- #
# module assembly
# --------------------------------------------------------------------------- #

def emit_setup_module(machine: Machine, runtime_import: str = RUNTIME_IMPORT) -> str:
    fields = _field_table(machine)
    config = _strip_meta(copy.deepcopy(machine.config))
    config["context"] = _js_context(config, fields)

    # PERFORM -> invoke: rebuild the runnable flow with real call-return.
    ordered = machine.paragraph_order
    actor_configs: Dict[str, dict] = {}
    if config.get("type") == "parallel":
        new_regions, actor_configs = _invoke_transform_parallel(config["states"], ordered)
        config["states"] = new_regions
    elif config.get("states") and config.get("initial"):
        main_states, actor_configs = _invoke_transform(config["states"],
                                                       config["initial"], ordered)
        config["states"] = main_states

    # collect referenced names across the main machine AND every actor body
    scan = {"main": config, "actors": {n: c["states"] for n, c in actor_configs.items()}}
    ref_actions, ref_guards = _collect_referenced(scan)
    ops, sem_effects = _build_ops(machine, fields)
    # every referenced action that is not a data op is an effect no-op
    effect_actions = sorted((ref_actions | set(sem_effects)) - set(ops))
    guard_fns, external_guards = _build_guards(machine, ref_guards, fields)

    out: List[str] = []
    out.append(f"// Generated by cobol-xstate from {machine.source_name} "
               f"(program {machine.program_id}).")
    out.append("// Runnable XState v5 machine: setup({ actions, guards, actors }) over "
               "the fixed-point")
    out.append("// DECIMAL runtime (cobolRuntime.mjs). Do not edit by hand; see the JSON "
               "bundle")
    out.append("// for provenance, flags, and notes. PERFORM is a real call-return: each "
               "performed")
    out.append("// paragraph is an actor invoked with the context as input, its output "
               "assigned back")
    out.append("// on return. Other effects (DISPLAY/OPEN/READ/exec) are no-ops; I-O / "
               "ALTER /")
    out.append("// DEPENDING-ON / raw conditions read context.__cobol_external (default "
               "false).")
    out.append("import { setup, assign } from 'xstate';")
    out.append(f"import {{ {', '.join(_HELPERS)} }} from {_js_str(runtime_import)};")
    out.append("")

    out.append("export const FIELDS = " + json.dumps(fields, indent=2) + ";")
    out.append("")

    # data actions: (context) => partial context
    out.append("export const ops = {")
    for name, body in ops.items():
        out.append(f"  {_js_str(name)}: (context) => ({body}),")
    out.append("};")
    out.append("")

    out.append("export const guardFns = {")
    for name, body in guard_fns.items():
        out.append(f"  {_js_str(name)}: (context) => {body},")
    out.append("};")
    out.append("")

    out.append("export const externalGuards = " + json.dumps(external_guards) + ";")
    out.append("export const effectActions = " + json.dumps(effect_actions) + ";")
    out.append("")

    out.append("const actions = {};")
    out.append("for (const [k, fn] of Object.entries(ops)) "
               "actions[k] = assign(({ context }) => fn(context));")
    out.append("for (const k of effectActions) actions[k] = function () {};")
    out.append("const guards = {};")
    out.append("for (const [k, fn] of Object.entries(guardFns)) "
               "guards[k] = ({ context }) => fn(context);")
    out.append("for (const k of externalGuards) "
               "guards[k] = ({ context }) => Boolean(context.__cobol_external "
               "&& context.__cobol_external[k]);")
    out.append("")

    # PERFORM-target paragraphs, each an invokable actor machine. Context threads in via
    # `input` and back out via the final state's `output`; the call site assigns it on
    # onDone. The shared `actors` object is filled before use (XState also resolves the
    # string `src` lazily, so order is not load-bearing).
    out.append("const actors = {};")
    out.append("export const actorConfigs = " + json.dumps(actor_configs, indent=2) + ";")
    out.append("function wireInvokes(states) {")
    out.append("  for (const k in states) {")
    out.append("    const inv = states[k].invoke;")
    out.append("    if (inv) {")
    out.append("      inv.input = ({ context }) => context;")
    out.append("      if (inv.onDone) inv.onDone.actions = assign(({ event }) => event.output);")
    out.append("    }")
    out.append("    if (states[k].states) wireInvokes(states[k].states);  // nested regions")
    out.append("  }")
    out.append("}")
    out.append("for (const [name, cfg] of Object.entries(actorConfigs)) {")
    out.append("  wireInvokes(cfg.states);")
    out.append("  actors[name] = setup({ actions, guards, actors }).createMachine({")
    out.append("    ...cfg, context: ({ input }) => ({ ...(input || {}) }), "
               "output: ({ context }) => context,")
    out.append("  });")
    out.append("}")
    out.append("")

    out.append("export const machineConfig = " + json.dumps(config, indent=2) + ";")
    out.append("wireInvokes(machineConfig.states);")
    out.append("")
    out.append("export const machine = setup({ actions, guards, actors })"
               ".createMachine(machineConfig);")
    out.append("export default machine;")
    out.append("")
    return "\n".join(out)
