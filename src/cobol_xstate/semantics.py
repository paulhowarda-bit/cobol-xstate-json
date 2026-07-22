"""Statement and condition *semantics* - the data transformation logic.

A Harel/STATEMATE action is an assignment over typed data items (`X := X + 1`), and a
condition is a Boolean expression over them. This module translates COBOL's
straight-line data verbs and its conditions into exactly that, faithfully (the source
states the operation; nothing is inferred):

* ``MOVE``/``ADD``/``SUBTRACT``/``MULTIPLY``/``DIVIDE``/``COMPUTE``/``SET`` ->
  one or more ``target := expression`` assignments.
* relational / class / sign / 88-level conditions -> a Boolean expression tree.

Numeric subtleties that a faithful rewrite must honor - ``ROUNDED``, ``ON SIZE
ERROR``, and the fact that COBOL arithmetic is **fixed-point decimal**, not binary
float - are captured as annotations on the operation, not silently dropped.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

# data_division.DataItem, but we only touch a few attributes - keep it duck-typed.

_NUM = re.compile(r"^[+-]?\d+(\.\d+)?$")

# A subscripted / reference-modified reference: collapse the space between the name and
# its `(` so `NAME ( ... )` survives downstream whitespace splitting as one token. The
# inner content (a subscript list `I, J`, an arithmetic subscript `I - 1`, or a
# reference modification `1:3`) is preserved verbatim, only edge-trimmed. We must NOT do
# this for a logical/keyword `WORD ( sub-condition )` - excluded by reserved-word and a
# relational-operator check so `AND ( A < 1 )` stays a grouped condition.
_SUBNORM = re.compile(r"([A-Za-z][A-Za-z0-9-]*)\s+\(\s*([^()]*?)\s*\)")
_RESERVED_BEFORE_PAREN = {
    "AND", "OR", "NOT", "IF", "WHEN", "UNTIL", "WHILE", "THEN", "ELSE", "TO", "FROM",
    "GIVING", "BY", "INTO", "THAN", "EQUAL", "GREATER", "LESS", "IS", "ALSO", "OF", "IN",
}


_SUB_RELATION = re.compile(r"[<>]|(?<![A-Za-z0-9-])=")
_SUB_COMMA = re.compile(r"\s*,\s*")


def _norm_subscripts(s: str) -> str:
    if "(" not in s:
        return s        # per-statement fast path: most statements have no parentheses
    def repl(m):
        name, content = m.group(1), m.group(2).strip()
        if name.upper() in _RESERVED_BEFORE_PAREN:
            return m.group(0)
        if _SUB_RELATION.search(content):        # a relation -> sub-condition
            return m.group(0)
        content = _SUB_COMMA.sub(",", content)   # tighten a subscript list: I, J -> I,J
        return f"{name}({content})"
    return _SUBNORM.sub(repl, s)


def _operands(s: str) -> List[str]:
    """Split an operand list on whitespace/commas, but NOT on commas or spaces that sit
    inside parentheses (so a subscript ``TBL(I,J)`` or ref-mod ``X(1:3)`` stays whole)."""
    out: List[str] = []
    buf: List[str] = []
    depth = 0
    for ch in s.strip():
        if ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif depth == 0 and (ch.isspace() or ch == ","):
            if buf:
                out.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def _is_literal(tok: str) -> bool:
    return tok[:1] in ("'", '"') or bool(_NUM.match(tok)) or tok.upper() in _FIGURATIVE


_FIGURATIVE = {"ZERO", "ZEROS", "ZEROES", "SPACE", "SPACES", "HIGH-VALUE",
               "HIGH-VALUES", "LOW-VALUE", "LOW-VALUES", "QUOTES", "NULL", "NULLS"}


_ROUNDED = re.compile(r"\bROUNDED\b", re.I)
_SIZE_ERROR = re.compile(r"\bON\s+SIZE\s+ERROR\b", re.I)
# Matches the NOT form too, so `... NOT ON SIZE ERROR ...` does not leave a dangling
# "NOT" on the end of the core statement.
_SIZE_ERROR_CLAUSE = re.compile(r"\b(?:NOT\s+)?ON\s+SIZE\s+ERROR\b", re.I)


def _strip_arith_clauses(s: str):
    """Pull ROUNDED / ON SIZE ERROR off an arithmetic statement; return (core, flags).

    Gated on a cheap substring test first: this runs for EVERY statement, and MOVE -
    by far the most common verb - can carry none of these clauses, so the common case
    should not pay for three full case-insensitive scans of the statement text."""
    up = s.upper()
    if "ROUNDED" not in up and "SIZE ERROR" not in up:
        return s.strip(), False, False
    rounded = bool(_ROUNDED.search(s))
    size_err = bool(_SIZE_ERROR.search(s))
    # The two clauses are not the same shape. ON SIZE ERROR opens a HANDLER: everything
    # after it is a separate statement list and is not part of the operation, so it
    # truncates. ROUNDED is an inline modifier attached to a receiver, so it has to be
    # DELETED from the text. Truncating on it too turned `COMPUTE X ROUNDED = A / B`
    # into `COMPUTE X`, which then failed to match and dropped the assignment from the
    # model altogether - the statement was emitted as an empty function.
    core = _SIZE_ERROR_CLAUSE.split(s)[0]
    core = _ROUNDED.sub(" ", core)
    return " ".join(core.split()), rounded, size_err


def parse_operation(text: str, data: Optional[Dict] = None) -> Optional[dict]:
    """Translate a straight-line statement into a serializable operation spec.

    Returns ``{verb, kind, assignments:[{target, expr}], rounded, onSizeError, raw,
    notes}`` for data transforms; an effect spec for I/O; or ``None`` if the verb has
    no data/effect meaning worth recording.
    """
    data = data or {}
    s = _norm_subscripts(text.strip().rstrip("."))
    verb = (s.split() or [""])[0].upper()
    core, rounded, size_err = _strip_arith_clauses(s)

    def spec(kind, assignments=None, notes=None):
        d = {"verb": verb, "kind": kind, "raw": text.strip()}
        if assignments:
            d["assignments"] = assignments
        if rounded:
            d["rounded"] = True
        if size_err:
            d["onSizeError"] = True
        if notes:
            d["notes"] = notes
        return d

    if verb == "MOVE":
        m = re.match(r"MOVE\s+(.+?)\s+TO\s+(.+)$", core, re.I)
        if m:
            src = m.group(1).strip()
            targets = _operands(m.group(2))
            return spec("assign", [{"target": t, "expr": src} for t in targets])
    elif verb == "ADD":
        return _arith_add(core, spec)
    elif verb == "SUBTRACT":
        return _arith_sub(core, spec)
    elif verb == "MULTIPLY":
        return _arith_mul(core, spec)
    elif verb == "DIVIDE":
        return _arith_div(core, spec)
    elif verb == "COMPUTE":
        m = re.match(r"COMPUTE\s+(.+?)\s*=\s*(.+)$", core, re.I)
        if m:
            targets = _operands(re.sub(r"\bROUNDED\b", "", m.group(1), flags=re.I))
            expr = m.group(2).strip()
            return spec("compute", [{"target": t, "expr": expr} for t in targets])
    elif verb == "SET":
        return _set(core, data, spec)
    elif verb == "INITIALIZE":
        targets = _operands(re.sub(r"^INITIALIZE\s+", "", core, flags=re.I))
        return spec("initialize",
                    [{"target": t, "expr": "<type default>"} for t in targets],
                    notes=["INITIALIZE sets each item to its category default"])
    elif verb in ("ACCEPT",):
        m = re.match(r"ACCEPT\s+([A-Z0-9-]+)", core, re.I)
        if m:
            return spec("input", [{"target": m.group(1).upper(), "expr": "<external input>"}])
    elif verb in ("DISPLAY", "OPEN", "CLOSE", "READ", "WRITE", "REWRITE",
                  "DELETE", "START", "STRING", "UNSTRING", "INSPECT", "RETURN",
                  "RELEASE", "CALL", "GOBACK", "STOP"):
        return spec("effect")
    return None


def _sum_expr(operands: List[str]) -> str:
    return " + ".join(operands)


def _arith_add(core, spec):
    m = re.match(r"ADD\s+(.+?)\s+(?:TO|GIVING)\s+(.+)$", core, re.I)
    if not m:
        return spec("effect")
    addends = _operands(m.group(1))
    if re.search(r"\bGIVING\b", core, re.I):
        g = re.split(r"\bGIVING\b", core, flags=re.I)[1]
        to_part = re.match(r"ADD\s+.+?\s+TO\s+(.+?)\s+GIVING", core, re.I)
        base = _operands(to_part.group(1)) if to_part else []
        targets = _operands(g)
        expr = _sum_expr(addends + base)
        return spec("arith", [{"target": t, "expr": expr} for t in targets])
    targets = _operands(m.group(2))
    return spec("arith", [{"target": t, "expr": _sum_expr([t] + addends)} for t in targets])


def _arith_sub(core, spec):
    m = re.match(r"SUBTRACT\s+(.+?)\s+FROM\s+(.+)$", core, re.I)
    if not m:
        return spec("effect")
    subs = _operands(m.group(1))
    if re.search(r"\bGIVING\b", core, re.I):
        parts = re.split(r"\bGIVING\b", m.group(2), flags=re.I)
        minuend = _operands(parts[0])[0]
        targets = _operands(parts[1])
        expr = f"{minuend} - ({_sum_expr(subs)})"
        return spec("arith", [{"target": t, "expr": expr} for t in targets])
    targets = _operands(m.group(2))
    return spec("arith", [{"target": t, "expr": f"{t} - ({_sum_expr(subs)})"} for t in targets])


def _arith_mul(core, spec):
    m = re.match(r"MULTIPLY\s+(.+?)\s+BY\s+(.+)$", core, re.I)
    if not m:
        return spec("effect")
    a = m.group(1).strip()
    if re.search(r"\bGIVING\b", core, re.I):
        parts = re.split(r"\bGIVING\b", m.group(2), flags=re.I)
        b = _operands(parts[0])[0]
        targets = _operands(parts[1])
        return spec("arith", [{"target": t, "expr": f"{a} * {b}"} for t in targets])
    targets = _operands(m.group(2))
    return spec("arith", [{"target": t, "expr": f"{t} * {a}"} for t in targets])


def _arith_div(core, spec):
    # DIVIDE a INTO b [GIVING c [REMAINDER r]]  |  DIVIDE a BY b GIVING c [REMAINDER r]
    m = re.match(r"DIVIDE\s+(.+?)\s+(INTO|BY)\s+(.+)$", core, re.I)
    if not m:
        return spec("effect")
    a, kw, rest = m.group(1).strip(), m.group(2).upper(), m.group(3)
    if re.search(r"\bGIVING\b", core, re.I):
        parts = re.split(r"\bGIVING\b", rest, flags=re.I)
        b = _operands(parts[0])[0]
        rem_split = re.split(r"\bREMAINDER\b", parts[1], flags=re.I)
        targets = _operands(rem_split[0])
        dividend, divisor = (b, a) if kw == "INTO" else (a, b)
        assigns = [{"target": t, "expr": f"{dividend} / {divisor}"} for t in targets]
        if len(rem_split) > 1 and targets:
            # REMAINDER = dividend - stored (truncated) quotient * divisor. Assignments
            # apply in order, so the quotient target already holds its stored value.
            q = targets[0]
            for rt in _operands(rem_split[1]):
                assigns.append({
                    "target": rt,
                    "expr": f"{dividend} - ( {q} * {divisor} )",
                    "note": "REMAINDER = dividend - stored quotient * divisor",
                })
        return spec("arith", assigns)
    targets = _operands(rest)
    return spec("arith", [{"target": t, "expr": f"{t} / {a}"} for t in targets])


def _set(core, data, spec):
    m = re.match(r"SET\s+(.+?)\s+TO\s+(.+)$", core, re.I)
    if m:
        targets = _operands(m.group(1))
        val = m.group(2).strip()
        assigns = []
        for t in targets:
            di = data.get(t.upper())
            if di is not None and getattr(di, "level", None) == 88 and val.upper() == "TRUE":
                parent = di.cond_parent or t
                ranges = getattr(di, "condition_ranges", None) or []
                if di.condition_values:
                    v = di.condition_values[0]
                elif ranges:
                    v = ranges[0][0]   # low end of the first range satisfies the condition
                else:
                    v = "TRUE"
                assigns.append({"target": parent, "expr": v,
                                "note": f"SET condition-name {t} TO TRUE"})
            else:
                assigns.append({"target": t, "expr": val})
        return spec("assign", assigns)
    m = re.match(r"SET\s+(.+?)\s+(UP|DOWN)\s+BY\s+(.+)$", core, re.I)
    if m:
        targets = _operands(m.group(1))
        op = "+" if m.group(2).upper() == "UP" else "-"
        n = m.group(3).strip()
        return spec("assign", [{"target": t, "expr": f"{t} {op} {n}"} for t in targets])
    return spec("effect")


# --------------------------------------------------------------------------- #
# Conditions -> Boolean expression trees
# --------------------------------------------------------------------------- #

_REL = {
    "=": "=", "EQUAL": "=", "EQUALS": "=", "EQ": "=",
    ">": ">", "GREATER": ">", "GT": ">",
    "<": "<", "LESS": "<", "LT": "<",
    ">=": ">=", "GE": ">=", "<=": "<=", "LE": "<=",
    "<>": "<>", "NE": "<>",
}
_CLASS = {"NUMERIC", "ALPHABETIC", "ALPHABETIC-UPPER", "ALPHABETIC-LOWER"}
_SIGN = {"POSITIVE", "NEGATIVE", "ZERO"}


def _ctokens(s: str) -> List[str]:
    # Token classes, longest/most-specific first so they win at each scan position:
    #   * string literals
    #   * a subscripted / reference-modified reference - NAME( ... ) - kept WHOLE (incl.
    #     inner spaces, commas, and arithmetic) so a relational operand survives intact
    #   * decimal or integer numeric literals (the decimal point must not split the token)
    #   * multi-char then single-char operators (** >= <= <>  + - * / = > < ( ))
    #   * a COBOL word (data-name / keyword)
    return re.findall(
        r"'[^']*'|\"[^\"]*\""
        r"|[A-Za-z][A-Za-z0-9-]*\([^)]*\)"
        r"|\d+(?:\.\d+)?"
        r"|\*\*|>=|<=|<>|[-+*/=><()]"
        r"|[A-Za-z][A-Za-z0-9-]*",
        s)


_ARITH_OPS = {"+", "-", "*", "/", "**"}


def _norm_operand(s: str) -> str:
    """Uppercase an operand reference, but leave literals (and arithmetic expressions
    containing them) spelled as written."""
    return s if _is_literal(s) else s.upper()


def parse_condition(text: str, data: Optional[Dict] = None) -> dict:
    """Parse a COBOL condition into a serializable Boolean expression tree.

    Handles relational / class / sign / 88-level / AND-OR-NOT conditions and COBOL's
    *abbreviated combined relation conditions* - where the subject and/or relational
    operator are implied from the previous relation after a logical connective
    (``A = 1 OR 2`` -> ``A = 1 OR A = 2``; ``A > 1 AND < 9`` -> ``A > 1 AND A < 9``).
    88-level condition-names carry their singleton ``values`` and any ``ranges`` (THRU).
    Falls back to ``{op:'raw', text}`` for forms still beyond this recovery so nothing is
    silently lost.
    """
    data = data or {}
    toks = _ctokens(_norm_subscripts(text))
    pos = 0
    # COBOL abbreviation: the last *stated* subject and relational operator are implied
    # when omitted after AND/OR. Tracked across the whole condition in textual order.
    last = {"subject": None, "rel": None, "neg": False}

    def peek():
        return toks[pos] if pos < len(toks) else None

    def adv():
        nonlocal pos
        t = toks[pos]
        pos += 1
        return t

    def parse_or():
        node = parse_and()
        while peek() and peek().upper() == "OR":
            adv()
            node = {"op": "or", "args": [node, parse_and()]}
        return node

    def parse_and():
        node = parse_not()
        while peek() and peek().upper() == "AND":
            adv()
            node = {"op": "and", "args": [node, parse_not()]}
        return node

    def parse_not():
        if peek() and peek().upper() == "NOT":
            adv()
            return {"op": "not", "arg": parse_atom()}
        return parse_atom()

    def _norm(tok):
        return tok if _is_literal(tok) else tok.upper()

    def read_operand():
        """Consume one relational operand: a primary term, then any run of arithmetic
        operators + terms (``A``, ``18.5``, ``TBL(I)``, ``WS-A + WS-B``). Stops before a
        relational operator, a logical connective, or a class/sign keyword."""
        if peek() is None:
            return None
        parts = [adv()]
        while peek() in _ARITH_OPS:
            parts.append(adv())            # arithmetic operator
            if peek() is None:
                break
            parts.append(adv())            # next term
        return " ".join(parts)

    def _read_rel_op():
        """Consume a relational operator (worded or symbolic) and return its canonical
        form, or None. Handles a leading NOT (``NOT =``) as relation negation."""
        nonlocal pos
        save = pos
        neg = False
        if peek() and peek().upper() == "NOT":
            adv()
            neg = True
        nxt = peek()
        if nxt and (nxt in _REL or nxt.upper() in _REL):
            rel = _REL.get(nxt, _REL.get(nxt.upper()))
            adv()
            if peek() and peek().upper() in ("THAN", "TO"):  # GREATER THAN / EQUAL TO
                adv()
            return rel, neg
        pos = save
        return None, False

    def _rel_node(left, rel, neg):
        right = read_operand() if peek() is not None else "?"
        subj = _norm_operand(left) if isinstance(left, str) else left
        last["subject"], last["rel"], last["neg"] = subj, rel, neg
        node = {"op": "rel", "left": subj, "rel": rel, "right": _norm_operand(right)}
        return {"op": "not", "arg": node} if neg else node

    def _condname_node(di, name):
        node = {"op": "cond-name", "name": name.upper(),
                "parent": di.cond_parent, "values": di.condition_values}
        ranges = getattr(di, "condition_ranges", None)
        if ranges:
            node["ranges"] = ranges
        return node

    def parse_atom():
        nonlocal pos
        if peek() == "(":
            adv()
            node = parse_or()
            if peek() == ")":
                adv()
            return node
        if peek() is None:
            return {"op": "raw", "text": text.strip()}
        # abbreviated: a leading relational operator implies the previous subject
        # (A = 1 OR > 5  ->  A > 5).
        if last["subject"] is not None and (peek() in _REL or peek().upper() in _REL):
            rel, neg = _read_rel_op()
            if rel is not None:
                return _rel_node(last["subject"], rel, neg)
        left = read_operand()
        nxt = peek()
        # class / sign condition: A [IS] [NOT] NUMERIC|POSITIVE|...
        save = pos
        negated = False
        if nxt and nxt.upper() == "IS":
            adv()
            nxt = peek()
        if nxt and nxt.upper() == "NOT":
            adv()
            negated = True
            nxt = peek()
        if nxt and nxt.upper() in _CLASS:
            adv()
            return {"op": "class", "operand": left.upper(), "class": nxt.upper(), "negated": negated}
        if nxt and nxt.upper() in _SIGN:
            adv()
            return {"op": "sign", "operand": left.upper(), "sign": nxt.upper(), "negated": negated}
        pos = save
        # full relation: left [NOT] rel right
        rel, neg = _read_rel_op()
        if rel is not None:
            return _rel_node(left, rel, neg)
        # bare term: an 88-level condition-name; else (in an abbreviation context) an
        # implied-subject object (A = 1 OR FOO -> A = FOO); else a standalone flag name.
        di = data.get(left.upper())
        if di is not None and getattr(di, "level", None) == 88:
            return _condname_node(di, left)
        if last["subject"] is not None and last["rel"] is not None \
                and (_is_literal(left) or di is not None):
            node = {"op": "rel", "left": last["subject"], "rel": last["rel"],
                    "right": _norm_operand(left)}
            return {"op": "not", "arg": node} if last["neg"] else node
        return {"op": "cond-name", "name": left.upper()}

    node = parse_or()
    if pos < len(toks):  # leftover we did not consume -> be honest
        return {"op": "raw", "text": text.strip()}
    return node
