"""The statement text a PREPARE reads, when the program builds it only from literals.

Ledger item 153. `EXEC SQL PREPARE S FROM :WS-SQL` makes the statement dynamic, and a
cursor DECLAREd FOR S has no select list in the source. But a program very often builds
that text from nothing but literals - a `MOVE 'SELECT ...'`, or a `STRING` of FILLER
items whose VALUEs spell the select list - and then the select list and the table ARE in
the source, one per way the text is built. This module recovers those texts.

It answers only when EVERY store into the text is one it can read as literal text. One
store it cannot read - a READ INTO, a MOVE from a LINKAGE item, an ACCEPT, a CALL that
passes the item by reference, a STRING WITH POINTER, a reference-modified target - and
the answer is None: no candidate at all, never the subset that happened to be readable.

Like the dynamic-CALL resolver this is a may-analysis, not reaching definitions: each
store is one candidate, and which one a given PREPARE sees is control flow. A caller
publishes them marked as candidates, never as the statement.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Tuple

from cobol_parser.analysis import _character_length, _layout, _subtree_end
from cobol_parser.model import (Action, AlterStmt, CallStmt, EvaluateStmt, ExecStmt,
                                IfStmt, Program, walk_statements)
from cobol_parser.normalizer import CodeLine
from cobol_parser.parser import _scan_sql_declarations
from cobol_parser.textutil import mask_literals, split_outside_literals

_PREPARE = re.compile(
    r"^\s*PREPARE\s+([A-Z0-9_#@$-]+)\b.*?\bFROM\s*:\s*([A-Z0-9][A-Z0-9-]*)\s*$",
    re.I | re.S)
_WORD = re.compile(r"[A-Z0-9][A-Z0-9-]*", re.I)
_PLAIN = re.compile(r"^[A-Z0-9][A-Z0-9-]*$", re.I)
_LITERAL = re.compile(r"^(['\"])(.*)\1$", re.S)
_CLEARS = {"SPACE", "SPACES", "LOW-VALUE", "LOW-VALUES"}
# Statements that name an item only to read it.
_READ_VERBS = {"DISPLAY"}
_SQL_READS = {"PREPARE", "EXECUTE", "DESCRIBE", "OPEN"}


class _Opaque(Exception):
    """A store this analysis cannot read as literal text: the answer is None."""


def _literal(token: str) -> Optional[str]:
    m = _LITERAL.match(token.strip())
    if not m:
        return None
    q = m.group(1)
    return m.group(2).replace(q + q, q)


class _Program:
    def __init__(self, program: Program):
        self.items = list(program.data_items)
        self.parent, _conds, self.redefiners = _layout(self.items)
        self.where: Dict[str, List[int]] = {}
        for k, it in enumerate(self.items):
            if it.level != 88:
                self.where.setdefault(str(it.name).upper(), []).append(k)
        self.stmts = [st for para in [*program.declaratives, *program.paragraphs]
                      for st in walk_statements(para.statements)]

    def one(self, name: str) -> int:
        found = self.where.get(name.upper(), [])
        if len(found) != 1:
            raise _Opaque(f"{name} is declared {len(found)} times")
        return found[0]

    def storage(self, k: int, ancestors: bool = True) -> Set[str]:
        """Names a store into item ``k``'s bytes can be spelled by: the item, its
        subordinates, its groups, and everything REDEFINING any of them."""
        idx = {m for m in range(k, _subtree_end(self.items, k))
               if self.items[m].level != 88}
        up = self.parent[k]
        while ancestors and up is not None:
            idx.add(up)
            up = self.parent[up]
        for m in sorted(idx):
            for r in self.redefiners.get(m, ()):
                idx.update(j for j in range(r, _subtree_end(self.items, r))
                           if self.items[j].level != 88)
        return {str(self.items[m].name).upper() for m in idx} - {"FILLER"}

    def stores(self, names: Set[str]) -> List[Tuple[str, str, object]]:
        """Every store into ``names``, as (kind, target, detail): kind "move" (detail
        the sending operand), "string" (detail the sending phrase) or "clear". Raises
        _Opaque at a statement that names one of them in any way not read here."""
        out: List[Tuple[str, str, object]] = []
        for st in self.stmts:
            if isinstance(st, (IfStmt, EvaluateStmt, AlterStmt)):
                continue            # a condition only reads; ALTER names paragraphs
            if isinstance(st, CallStmt):
                passed = set(st.using) - set(st.by_content)
                if passed & names or (st.returning or "").upper() in names:
                    raise _Opaque("passed BY REFERENCE to a CALL")
                continue
            if not isinstance(st, (Action, ExecStmt)):
                self._other(st, names)
                continue
            if not names & {w.upper() for w in _WORD.findall(mask_literals(st.text))}:
                continue
            if isinstance(st, ExecStmt):
                if st.lang == "SQL" and st.verb.upper() in _SQL_READS:
                    continue
                raise _Opaque(f"named by EXEC {st.lang} {st.verb}")
            out.extend(self._action(st.verb.upper(), st.text, names))
        return out

    @staticmethod
    def _other(st, names: Set[str]) -> None:
        """A statement kind with no ``text``: any mention of the item is a store this
        analysis does not read (a PERFORM VARYING, a SEARCH, an I-O INTO)."""
        for key, val in vars(st).items():
            if key in ("line", "origin") or isinstance(val, list) and val and \
                    not isinstance(val[0], str):
                continue
            words = val if isinstance(val, list) else [val]
            for w in words:
                if isinstance(w, str) and names & {x.upper() for x in
                                                   _WORD.findall(mask_literals(w))}:
                    raise _Opaque(f"named by a {type(st).__name__}")

    @staticmethod
    def _action(verb: str, text: str, names: Set[str]) -> List[Tuple[str, str, object]]:
        def named(part: str) -> bool:
            return bool(names & {w.upper() for w in _WORD.findall(mask_literals(part))})

        body = text.strip()[len(verb):]
        if verb in _READ_VERBS:
            return []
        if verb == "MOVE":
            parts = split_outside_literals(body, "TO")
            if parts is None:
                raise _Opaque("an unreadable MOVE")
            src, tgts = parts[0].strip(), parts[1].strip()
            if not named(tgts):
                return []
            targets = [t for t in re.split(r"[\s,]+", tgts) if t]
            out = []
            for n, t in enumerate(targets):
                if t.upper() not in names:
                    continue
                after = targets[n + 1] if n + 1 < len(targets) else ""
                # The statement text is re-spelled from tokens, so `X(5:1)` arrives
                # as `X ( 5 : 1 )`: a subscript, reference modification or qualifier
                # stores into part of the item, or into an item of another name.
                if not _PLAIN.match(t) or after.startswith("(") or \
                        after.upper() in ("OF", "IN"):
                    raise _Opaque("a qualified or reference-modified MOVE target")
                kind = "clear" if src.upper() in _CLEARS else "move"
                out.append((kind, t.upper(), src))
            if not out:
                raise _Opaque("a MOVE target this analysis cannot read")
            return out
        if verb == "STRING":
            parts = split_outside_literals(body, "INTO")
            if parts is None:
                raise _Opaque("an unreadable STRING")
            src, rest = parts
            if not named(rest):
                return []
            words = rest.split()
            if not words or words[0].upper() not in names or not _PLAIN.match(words[0]) \
                    or (len(words) > 1 and (words[1].startswith("(")
                                            or words[1].upper() in ("OF", "IN"))) \
                    or "POINTER" in {w.upper() for w in words}:
                raise _Opaque("a STRING whose receiving item this analysis cannot read")
            return [("string", words[0].upper(), src)]
        if verb == "INITIALIZE":
            ops = body.split()
            if len(ops) == 1 and ops[0].upper() in names:
                return [("clear", ops[0].upper(), None)]
        raise _Opaque(f"named by {verb}")

    # -- values --------------------------------------------------------------
    def item_text(self, name: str) -> str:
        """The text item ``name`` holds whenever it is read: its VALUEs, which nothing
        stores into. Raises _Opaque otherwise."""
        k = self.one(name)
        if self.stores(self.storage(k)):
            raise _Opaque(f"{name} is stored into")
        it = self.items[k]
        if not it.is_group:
            return self._value(it)
        parts = []
        for m in range(k + 1, _subtree_end(self.items, k)):
            sub = self.items[m]
            if sub.level == 88:
                continue
            if sub.redefines or sub.occurs or sub.level == 66:
                raise _Opaque(f"{name}'s bytes are not its items in order")
            if not sub.is_group:
                parts.append(self._value(sub))
        return "".join(parts)

    @staticmethod
    def _value(it) -> str:
        size = _character_length(it)
        if size is None or it.occurs or it.redefines:
            raise _Opaque(f"{it.name} is not a character item")
        val = str(it.value or "")
        if val.upper() in ("SPACE", "SPACES"):
            return " " * size
        lit = _literal(val)
        if lit is None:
            raise _Opaque(f"{it.name} has no literal VALUE")
        return lit[:size].ljust(size)

    def operand_text(self, op: str) -> str:
        lit = _literal(op)
        if lit is not None:
            return lit
        if op.upper() in ("SPACE", "SPACES"):
            return " "
        if not _PLAIN.match(op):
            raise _Opaque(f"operand {op} is not one item")
        return self.item_text(op)

    def string_text(self, phrase: str) -> str:
        """The text `STRING <phrase> INTO ...` stores, from position 1."""
        masked = mask_literals(phrase)
        tokens = [phrase[m.start():m.end()]
                  for m in re.finditer(r"'[^']*'|\"[^\"]*\"|\S+", masked)]
        # mask_literals keeps lengths, so the spans index the original phrase.
        out, pending, k = [], [], 0
        while k < len(tokens):
            t = tokens[k]
            if t.upper() == "DELIMITED":
                k += 1
                if k < len(tokens) and tokens[k].upper() == "BY":
                    k += 1
                if k >= len(tokens) or not pending:
                    raise _Opaque("an unreadable STRING")
                delim = tokens[k]
                for op in pending:
                    text = self.operand_text(op)
                    if delim.upper() == "SIZE":
                        out.append(text)
                    elif delim.upper() in ("SPACE", "SPACES"):
                        out.append(text.split(" ", 1)[0])
                    elif _literal(delim) is not None and _literal(delim):
                        out.append(text.split(_literal(delim), 1)[0])
                    else:
                        raise _Opaque(f"STRING delimited by {delim}")
                pending = []
            else:
                pending.append(t.rstrip(","))
            k += 1
        if pending:
            raise _Opaque("a STRING operand with no DELIMITED phrase")
        return "".join(out)


def _select_of(text: str) -> Optional[dict]:
    """Read ``text`` as a cursor's SELECT, with the parser's own DECLARE reader."""
    cursors, _ = _scan_sql_declarations(
        [CodeLine(text=f"EXEC SQL DECLARE ZZ_CANDIDATE CURSOR FOR {text} END-EXEC",
                  line=0)])
    if len(cursors) != 1 or cursors[0].get("forKind") != "select" \
            or not cursors[0].get("selectList"):
        return None
    return cursors[0]


def prepared_selects(program: Program, statement: str) -> List[dict]:
    """The candidate SELECTs statement ``statement`` is PREPAREd from, one per way the
    program builds its text, in source order (duplicates once). Empty when any PREPARE of
    it, or any store into its text, is not literal text this analysis reads.

    Each is the parser's DECLARE row for that text: selectList, selectDerivations,
    selectTables, table, tables.
    """
    view = _Program(program)
    sources = []
    for st in view.stmts:
        if isinstance(st, ExecStmt) and st.lang == "SQL" and st.verb.upper() == "PREPARE":
            m = _PREPARE.match(st.text)
            if m and m.group(1).upper() == statement.upper():
                sources.append(m.group(2).upper())
            elif not m and re.match(rf"\s*PREPARE\s+{re.escape(statement)}\b",
                                    st.text, re.I):
                return []                       # FROM a literal or an expression
    if not sources:
        return []
    texts: List[str] = []
    try:
        for host in dict.fromkeys(sources):
            k = view.one(host)
            text_item = _text_item(view, k)
            size = _character_length(view.items[text_item])
            stores = view.stores(view.storage(text_item))
            built = [s for s in stores if s[0] != "clear"]
            if not built:
                texts.append(view.item_text(str(view.items[text_item].name)))
            for kind, target, detail in built:
                if target != str(view.items[text_item].name).upper():
                    # Into the VARCHAR group (its length bytes first) or through a
                    # REDEFINES: not the text the statement reads, from position 1.
                    raise _Opaque(f"a store into {target}")
                text = (view.operand_text(detail) if kind == "move"
                        else view.string_text(detail))
                texts.append(text[:size] if size else text)
    except _Opaque:
        return []
    out = []
    for text in dict.fromkeys(t.strip() for t in texts):
        row = _select_of(text) if text else None
        if row is not None:
            out.append(row)
    return out


def _text_item(view: _Program, k: int) -> int:
    """The item holding the statement's characters: a character item itself, or the
    text half of a VARCHAR host variable (`49 LEN PIC S9(4) COMP` + `49 TEXT PIC X`)."""
    it = view.items[k]
    if not it.is_group:
        if _character_length(it) is None:
            raise _Opaque(f"{it.name} is not a character item")
        return k
    subs = [m for m in range(k + 1, _subtree_end(view.items, k))
            if view.items[m].level != 88]
    if len(subs) == 2 and view.items[subs[0]].level == view.items[subs[1]].level == 49 \
            and _character_length(view.items[subs[0]]) is None \
            and _character_length(view.items[subs[1]]) is not None:
        return subs[1]
    raise _Opaque(f"{it.name} is not a VARCHAR host variable")
