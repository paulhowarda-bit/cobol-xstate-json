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
* Effects (``DISPLAY``/``OPEN``/``READ``/exec) and the call-return ``PERFORM`` action are
  emitted as no-ops - they change no modeled data (``PERFORM``'s target is its own
  region; a flat no-op does not execute it - the documented limitation).
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
from typing import Dict, List, Optional, Tuple

from .data_division import expand_pic
from .statechart import Machine

RUNTIME_IMPORT = "./cobolRuntime.mjs"
_HELPERS = ("D", "add", "sub", "mul", "div", "pow", "store", "storeStr",
            "rel", "isClass", "isSign", "notModeled")

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
        up = t.upper()
        if up in _FIGURATIVE_NUM:
            return {"kind": "num", "text": _FIGURATIVE_NUM[up]}
        return {"kind": "ref", "name": up}


def _emit_num_node(node: dict) -> str:
    if "kind" in node:
        if node["kind"] == "num":
            return f'D({_js_str(node["text"])})'
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
            out[name] = {
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
    fld = fields.get(up)
    numeric = bool(fld and fld.get("category") == "numeric")
    return f"context[{_js_str(up)}]", numeric


def _emit_guard(tree: dict, fields: Dict[str, dict]) -> Optional[str]:
    """Boolean condition tree -> JS bool expression, or ``None`` if it can't be modeled
    (caller routes it to an external guard, honestly, rather than inventing a truth)."""
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
        if not parent or not values:
            return None
        fld = fields.get(str(parent).upper())
        numeric = "true" if (fld and fld.get("category") == "numeric") else "false"
        tests = []
        for v in values:
            rval, _ = _operand_js(str(v), fields)
            tests.append(f'rel(context[{_js_str(str(parent).upper())}], "=", {rval}, {numeric})')
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
    ctx = dict(config.get("context", {}))
    for k, v in list(ctx.items()):
        fld = fields.get(k)
        if fld and fld.get("category") == "numeric" and not isinstance(v, str):
            ctx[k] = format(v, "f") if isinstance(v, float) else str(v)
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
# module assembly
# --------------------------------------------------------------------------- #

def emit_setup_module(machine: Machine, runtime_import: str = RUNTIME_IMPORT) -> str:
    fields = _field_table(machine)
    config = _strip_meta(copy.deepcopy(machine.config))
    config["context"] = _js_context(config, fields)

    ref_actions, ref_guards = _collect_referenced(config)
    ops, sem_effects = _build_ops(machine, fields)
    # every referenced action that is not a data op is an effect no-op
    effect_actions = sorted((ref_actions | set(sem_effects)) - set(ops))
    guard_fns, external_guards = _build_guards(machine, ref_guards, fields)

    out: List[str] = []
    out.append(f"// Generated by cobol-xstate from {machine.source_name} "
               f"(program {machine.program_id}).")
    out.append("// Runnable XState v5 machine: setup({ guards, actions }) over the "
               "fixed-point")
    out.append("// DECIMAL runtime (cobolRuntime.mjs). Do not edit by hand; see the JSON "
               "bundle")
    out.append("// for provenance, flags, and notes. Effects and PERFORM are no-ops; "
               "I-O / ALTER /")
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

    out.append("export const machineConfig = " + json.dumps(config, indent=2) + ";")
    out.append("")
    out.append("export const machine = setup({ actions, guards })"
               ".createMachine(machineConfig);")
    out.append("export default machine;")
    out.append("")
    return "\n".join(out)
