"""Stage 3 - recover PROCEDURE DIVISION structure.

Two passes:

1. Split the PROCEDURE DIVISION into sections/paragraphs using **Area A** header
   detection (a header stands alone in Area A as ``NAME.`` or ``NAME SECTION.``).
   Doing this at the line/area level, not the token level, is what makes header
   detection reliable (see references/parsing-cobol.md, Stage 1 + Stage 5).

2. Parse each paragraph body into a control-flow statement AST (model.py) with a
   small recursive-descent parser that understands the block-structured verbs
   (IF / EVALUATE / PERFORM / READ-WRITE handlers) and folds everything else into
   opaque ``Action`` nodes.

This is a heuristic control-flow recovery, not a conformant COBOL parser: it has no
copybook preprocessor, no embedded-SQL/CICS extraction, and no full grammar. It is
honest about that - constructs it cannot resolve statically are surfaced as flags by
the statechart stage, never silently smoothed.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Tuple

from .normalizer import CodeLine, SourceFormat, detect_source_format, normalize
from .lexer import Token, tokenize
from .data_division import parse_data_division
from .preprocessor import CopybookResolver, preprocess
from .model import (
    Action,
    AlterStmt,
    CallStmt,
    ContinueStmt,
    EvaluateStmt,
    ExecStmt,
    ExitStmt,
    GoToStmt,
    HandledStmt,
    IfStmt,
    IoStmt,
    Paragraph,
    PerformStmt,
    Program,
    SearchStmt,
    SortStmt,
    Stmt,
    TerminateStmt,
    walk_statements,
)

# Verbs that begin a statement. Used to bound opaque actions and conditions.
ACTION_VERBS: Set[str] = {
    "MOVE", "ADD", "SUBTRACT", "MULTIPLY", "DIVIDE", "COMPUTE", "OPEN", "CLOSE",
    "DISPLAY", "ACCEPT", "SET", "INITIALIZE", "RELEASE", "SORT", "MERGE", "CANCEL",
    "INVOKE", "UNLOCK", "STRING", "UNSTRING", "INSPECT", "SEARCH", "ALLOCATE", "FREE",
}
CONTROL_VERBS: Set[str] = {
    "IF", "EVALUATE", "PERFORM", "GO", "READ", "WRITE", "REWRITE", "DELETE", "START",
    "RETURN", "CALL", "ALTER", "EXIT", "CONTINUE", "NEXT", "STOP", "GOBACK", "EXEC",
}
STARTERS: Set[str] = ACTION_VERBS | CONTROL_VERBS

# Verbs consumed opaquely up to their END- terminator (STRING/UNSTRING carry an inner
# ON OVERFLOW clause that the statechart stage flags). SEARCH is parsed structurally
# (its WHEN/AT END branches are real control flow) - see parse_search.
OPAQUE_SCOPED = {"STRING": "END-STRING", "UNSTRING": "END-UNSTRING"}

IO_VERBS = {"READ", "WRITE", "REWRITE", "DELETE", "START", "RETURN"}

# Verbs whose trailing [NOT] ON SIZE ERROR / EXCEPTION phrase guards an imperative:
# the handler body is a conditional branch, captured as a HandledStmt (never hoisted).
_SIZE_VERBS = {"ADD", "SUBTRACT", "MULTIPLY", "DIVIDE", "COMPUTE"}
_EXC_VERBS = {"ACCEPT", "DISPLAY"}
_HANDLED_VERBS = _SIZE_VERBS | _EXC_VERBS

# Clause words that may follow the file/record name inside an I/O statement; NEXT
# collides with the NEXT SENTENCE starter, so it must be recognized as a clause here
# (READ f NEXT RECORD is the standard VSAM browse idiom).
_IO_CLAUSE_WORDS = {"NEXT", "PREVIOUS", "RECORD", "KEY", "WITH", "NO", "LOCK",
                    "KEPT", "WAIT", "ADVANCING", "BEFORE", "AFTER", "PAGE",
                    "LINE", "LINES", "IGNORING"}

_HEADER_RE = re.compile(r"^([A-Z0-9][A-Z0-9-]*)(\s+SECTION)?\s*\.\s*$", re.I)
_RESERVED_HEADER = STARTERS | {
    "END-IF", "END-PERFORM", "END-EVALUATE", "END-READ", "END-WRITE", "END-CALL",
    "ELSE", "WHEN", "THEN", "DECLARATIVES",
}


# --------------------------------------------------------------------------- #
# Pass 1: program structure
# --------------------------------------------------------------------------- #

def _find_program_id(lines: List[CodeLine]) -> str:
    for cl in lines:
        m = re.search(r"\bPROGRAM-ID\b\s*\.?\s*([A-Z0-9][A-Z0-9-]*)", cl.text, re.I)
        if m:
            return m.group(1).upper()
    return "RECOVERED"


def _header_name(cl: CodeLine) -> Optional[str]:
    """Return the paragraph/section name if this line is an Area-A header."""
    if not cl.area_a:
        return None
    m = _HEADER_RE.match(cl.text.strip())
    if not m:
        return None
    name = m.group(1).upper()
    if name in _RESERVED_HEADER:
        return None
    return name


_VALUE_RE = re.compile(
    r"^\s*\d+\s+([A-Z0-9][A-Z0-9-]*)\b.*?\bVALUE\b\s+(?:IS\s+)?(['\"])(.*?)\2", re.I)


def _scan_value_clauses(lines: List[CodeLine]) -> dict:
    """Capture `<level> NAME ... VALUE 'lit'` initial values (string literals only)
    from the DATA DIVISION, for constant propagation of CALL targets."""
    out = {}
    for cl in lines:
        m = _VALUE_RE.match(cl.text)
        if m:
            out[m.group(1).upper()] = m.group(3).rstrip()
    return out


def _procedure_lines(lines: List[CodeLine]) -> List[CodeLine]:
    """Return body lines after the PROCEDURE DIVISION header clause (skipping any
    ``USING ...`` continuation up to its terminating period)."""
    start = None
    for i, cl in enumerate(lines):
        if re.search(r"\bPROCEDURE\s+DIVISION\b", cl.text, re.I):
            start = i
            break
    if start is None:
        return []
    # Consume header lines until the one carrying the terminating period.
    j = start
    while j < len(lines) and "." not in lines[j].text:
        j += 1
    return lines[j + 1:]


def _procedure_interface(lines: List[CodeLine]) -> Tuple[List[str], Optional[str]]:
    """Extract the program's own parameter interface from the PROCEDURE DIVISION header:
    ``PROCEDURE DIVISION USING p1 p2 ... [RETURNING r].`` Returns ``(using, returning)``.

    These LINKAGE-backed items are the perimeter at the program's entry point - what the
    caller (COMMAREA / parameter list) passes in and what is returned."""
    start = None
    for i, cl in enumerate(lines):
        if re.search(r"\bPROCEDURE\s+DIVISION\b", cl.text, re.I):
            start = i
            break
    if start is None:
        return [], None
    header = []
    j = start
    while j < len(lines):
        header.append(lines[j].text)
        if "." in lines[j].text:
            break
        j += 1
    text = " ".join(header)
    text = text.split(".", 1)[0]  # header clause only, up to the terminating period
    returning = None
    mret = re.search(r"\bRETURNING\s+([A-Z0-9][A-Z0-9-]*)", text, re.I)
    if mret:
        returning = mret.group(1).upper()
    using: List[str] = []
    mus = re.search(r"\bUSING\b(.*?)(?:\bRETURNING\b|$)", text, re.I)
    if mus:
        for tok in re.split(r"[,\s]+", mus.group(1).strip()):
            u = tok.upper()
            if u and u not in ("BY", "REFERENCE", "CONTENT", "VALUE"):
                using.append(u)
    return using, returning


def parse_program(source: str, fmt: Optional[SourceFormat] = None,
                  resolver: Optional[CopybookResolver] = None) -> Program:
    if fmt is None:
        fmt = detect_source_format(source).format
    lines = normalize(source, fmt)
    pre = preprocess(lines, resolver, fmt=fmt)
    lines = pre.lines
    prog = Program(program_id=_find_program_id(lines))
    if pre.expanded:
        prog.notes.append("Expanded copybooks: " + ", ".join(sorted(set(pre.expanded))))
    for member in sorted(set(pre.missing)):
        prog.notes.append(
            f"COPY {member}: not found - data/logic it defines is missing from the model")
    prog.notes.extend(n for n in pre.notes if "not found" not in n and "recursive" in n)

    if any(re.search(r"\bPROCEDURE\s+DIVISION\b", cl.text, re.I) for cl in lines):
        prog.has_procedure_division = True
        prog.using, prog.returning = _procedure_interface(lines)
    if any(re.search(r"\bDECLARATIVES\b", cl.text, re.I) for cl in lines):
        prog.notes.append(
            "DECLARATIVES present: USE-procedure error handlers form an implicit "
            "orthogonal region; recovered chart does not model the implicit transfer."
        )

    prog.working_values = _scan_value_clauses(lines)
    prog.data_items, prog.data_by_name = parse_data_division(lines)

    body = _procedure_lines(lines)
    if not body:
        return prog

    # DECLARATIVES ... END DECLARATIVES is an orthogonal error-handler region, not part of
    # the main sequential flow - split it out so its USE sections don't pollute it.
    decl_lines, main_lines = _split_declaratives(body)
    prog.paragraphs = _group_paragraphs(main_lines)
    if decl_lines:
        prog.declaratives = _mark_use_handlers(_group_paragraphs(decl_lines))

    # Collect CICS HANDLE CONDITION registrations across all statements.
    prog.cics_handlers = _collect_cics_handlers(prog.paragraphs + prog.declaratives)
    return prog


def _split_declaratives(body: List[CodeLine]):
    """Return (declaratives_lines, main_lines), splitting at DECLARATIVES/END DECLARATIVES."""
    start = end = None
    for i, cl in enumerate(body):
        u = cl.text.strip().upper()
        if start is None and re.match(r"^DECLARATIVES\s*\.?$", u):
            start = i
        elif re.match(r"^END\s+DECLARATIVES\s*\.?$", u):
            end = i
            break
    if start is None or end is None:
        return [], body
    return body[start + 1:end], body[:start] + body[end + 1:]


def _group_paragraphs(body: List[CodeLine]) -> List[Paragraph]:
    """Group body lines into paragraphs by Area-A headers and parse each one's statements."""
    if not body:
        return []
    current = Paragraph(name="_ENTRY_", line=body[0].line)
    section: Optional[str] = None
    buckets: List[Paragraph] = [current]
    bucket_lines: List[List[CodeLine]] = [[]]
    for cl in body:
        name = _header_name(cl)
        if name is not None:
            is_section = bool(re.search(r"\bSECTION\b", cl.text, re.I))
            section = name if is_section else section
            current = Paragraph(name=name, line=cl.line,
                                section=None if is_section else section,
                                origin=cl.origin)
            buckets.append(current)
            bucket_lines.append([])
            if is_section:
                continue
        else:
            bucket_lines[-1].append(cl)

    for para, plines in zip(buckets, bucket_lines):
        # Robustness at scale: an unparseable paragraph must not abort the whole program
        # (or a batch of thousands). On failure, fall back to one opaque action carrying
        # the raw text and mark the paragraph so the statechart stage can flag it.
        try:
            para.statements = StmtParser(tokenize(plines)).parse_paragraph()
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all for corpus safety
            raw = " ".join(cl.text.strip() for cl in plines).strip()
            para.statements = [Action(line=para.line, text=raw[:200], verb="?")]
            para.parse_error = f"{type(exc).__name__}: {exc}"

    if buckets and buckets[0].name == "_ENTRY_" and not buckets[0].statements:
        buckets.pop(0)
    return buckets


_USE_RE = re.compile(
    r"\bUSE\b\s+(?:GLOBAL\s+)?(?:AFTER\s+)?(?:STANDARD\s+)?"
    r"(?:(ERROR|EXCEPTION)\s+PROCEDURE|FOR\s+DEBUGGING)\s*(?:ON\s+(.+))?", re.I)


def _mark_use_handlers(paras: List[Paragraph]) -> List[Paragraph]:
    """A declarative section head carries a USE statement; pull its trigger/files onto the
    section paragraph and drop the USE from the statement list (it is not executable)."""
    for p in paras:
        for st in p.statements:
            if isinstance(st, Action) and st.verb == "USE":
                m = _USE_RE.search(st.text)
                if m:
                    p.use_trigger = (m.group(1) or "DEBUGGING").upper()
                    if m.group(2):
                        p.use_files = [w.upper() for w in re.split(r"[\s,]+", m.group(2).strip())
                                       if w and w.upper() not in ("INPUT", "OUTPUT", "I-O", "EXTEND")]
                else:
                    p.use_trigger = "EXCEPTION"
                p.statements = [s for s in p.statements
                                if not (isinstance(s, Action) and s.verb == "USE")]
                break
    return paras


def _collect_cics_handlers(paras: List[Paragraph]):
    """Pull (condition, target) pairs out of every EXEC CICS HANDLE CONDITION statement."""
    out = []
    for p in paras:
        for st in walk_statements(p.statements):
            if isinstance(st, ExecStmt) and st.kind == "handle" and st.lang == "CICS" \
                    and st.verb == "HANDLE":
                names = st.conditions
                for i in range(0, len(names) - 1, 2):  # interleaved cond, target, ...
                    out.append((names[i], names[i + 1]))
    return out


# --------------------------------------------------------------------------- #
# Pass 2: statement parser (recursive descent over a paragraph's tokens)
# --------------------------------------------------------------------------- #

def _share_stacked_when_bodies(whens: List[Tuple[str, List[Stmt]]],
                               other_body: Optional[List[Stmt]]) -> None:
    """Stacked WHENs (``WHEN 1 WHEN 2 body``) fall into the next branch's body: a WHEN
    with no imperative of its own executes the body of the WHEN (or WHEN OTHER) that
    follows it. Share the body backwards so the first WHEN doesn't silently skip it."""
    for i in range(len(whens) - 1, -1, -1):
        cond, body = whens[i]
        if body:
            continue
        if i + 1 < len(whens):
            whens[i] = (cond, whens[i + 1][1])
        elif other_body:
            whens[i] = (cond, other_body)


class StmtParser:
    def __init__(self, tokens: List[Token]):
        self.toks = tokens
        self.i = 0

    # -- token helpers -----------------------------------------------------
    def _peek(self, k: int = 0) -> Optional[Token]:
        j = self.i + k
        return self.toks[j] if 0 <= j < len(self.toks) else None

    def _next(self) -> Optional[Token]:
        t = self._peek()
        if t is not None:
            self.i += 1
        return t

    def _at_period(self) -> bool:
        t = self._peek()
        return t is not None and t.kind == "period"

    def _eof(self) -> bool:
        return self.i >= len(self.toks)

    @staticmethod
    def _is_end_word(t: Optional[Token]) -> bool:
        return t is not None and t.kind == "word" and t.up.startswith("END-")

    def _line(self) -> int:
        t = self._peek()
        return t.line if t else (self.toks[-1].line if self.toks else 0)

    # -- top level ---------------------------------------------------------
    def parse_paragraph(self) -> List[Stmt]:
        stmts: List[Stmt] = []
        while not self._eof():
            if self._at_period():
                self._next()  # consume sentence-ending period
                continue
            stmts.extend(self.parse_block(stops=set()))
            if self._at_period():
                self._next()
            elif not self._eof():
                # Defensive: avoid an infinite loop on an unexpected token.
                self._next()
        return stmts

    def parse_block(self, stops: Set[str]) -> List[Stmt]:
        """Parse statements until a period, EOF, an outer END- terminator, or a token
        in ``stops`` (none consumed)."""
        out: List[Stmt] = []
        while not self._eof():
            t = self._peek()
            if t.kind == "period":
                break
            if t.kind == "word" and (t.up in stops or self._is_end_word(t)):
                break
            stmt = self.parse_statement(stops)
            if stmt is not None:
                out.append(stmt)
            else:
                break
        return out

    def parse_statement(self, stops: Set[str]) -> Optional[Stmt]:
        t = self._peek()
        if t is None or t.kind != "word":
            # Stray token; consume so we make progress.
            self._next()
            return None
        v = t.up
        if v == "EXEC":
            return self.parse_exec()
        if v == "IF":
            return self.parse_if()
        if v == "EVALUATE":
            return self.parse_evaluate()
        if v == "PERFORM":
            return self.parse_perform()
        if v == "GO":
            return self.parse_goto()
        if v in ("SORT", "MERGE"):
            return self.parse_sort()
        if v == "SEARCH":
            return self.parse_search()
        if v in IO_VERBS:
            return self.parse_io()
        if v == "CALL":
            return self.parse_call()
        if v == "ALTER":
            return self.parse_alter()
        if v == "EXIT":
            return self.parse_exit()
        if v == "CONTINUE":
            ln = self._next().line
            return ContinueStmt(line=ln)
        if v == "NEXT":
            ln = self._next().line  # NEXT
            if self._peek() and self._peek().is_word("SENTENCE"):
                self._next()
            return ContinueStmt(line=ln, next_sentence=True)
        if v == "STOP":
            ln = self._next().line
            if self._peek() and self._peek().is_word("RUN"):
                self._next()
                return TerminateStmt(line=ln, kind="STOP_RUN")
            return Action(line=ln, text="STOP", verb="STOP")
        if v == "GOBACK":
            ln = self._next().line
            return TerminateStmt(line=ln, kind="GOBACK")
        if v in OPAQUE_SCOPED:
            return self.parse_opaque_scoped(OPAQUE_SCOPED[v], stops)
        if v in _HANDLED_VERBS:
            return self.parse_handled_action(stops)
        return self.parse_action(stops)

    # -- opaque action -----------------------------------------------------
    def parse_action(self, stops: Set[str]) -> Stmt:
        start = self._next()
        verb = start.up
        parts = [start.text]
        line = start.line
        exc_verb = verb in _EXC_VERBS
        while not self._eof():
            t = self._peek()
            if t.kind == "period":
                break
            if t.kind == "word":
                u = t.up
                if u in STARTERS or u in stops or self._is_end_word(t):
                    break
                if u in {"ELSE", "WHEN", "THEN"}:
                    break
                if u == "AT" or (u == "NOT" and self._peek(1)
                                 and self._peek(1).up in {"AT", "INVALID", "ON",
                                                          "SIZE", "EXCEPTION"}):
                    break
                if u == "INVALID":
                    break
                # An ON-condition handler phrase opens here: [NOT] [ON] SIZE ERROR,
                # or (for ACCEPT/DISPLAY) [NOT] [ON] EXCEPTION. Its imperative is a
                # conditional branch - stop so the caller captures it as a handler.
                if u == "SIZE" and self._peek(1) and self._peek(1).is_word("ERROR"):
                    break
                if u == "ON" and self._peek(1) and self._peek(1).up in {
                        "SIZE", "EXCEPTION", "OVERFLOW"}:
                    break
                if exc_verb and u in {"EXCEPTION", "OVERFLOW"}:
                    break
            parts.append(self._next().text)
        return Action(line=line, text=" ".join(parts), verb=verb)

    def _handler_phrase(self, exc_ok: bool) -> Optional[str]:
        """If the upcoming tokens open an ON-condition handler phrase, consume the
        phrase words and return its key ('ON_SIZE_ERROR', 'NOT_ON_EXCEPTION', ...);
        otherwise consume nothing and return None."""
        j = 0
        neg = False
        t = self._peek(j)
        if t is None or t.kind != "word":
            return None
        if t.up == "NOT":
            neg = True
            j += 1
            t = self._peek(j)
            if t is None or t.kind != "word":
                return None
        if t.up == "ON":
            j += 1
            t = self._peek(j)
            if t is None or t.kind != "word":
                return None
        if t.up == "SIZE":
            nxt = self._peek(j + 1)
            if nxt is not None and nxt.is_word("ERROR"):
                for _ in range(j + 2):
                    self._next()
                return ("NOT_" if neg else "") + "ON_SIZE_ERROR"
            return None
        if t.up in ("EXCEPTION", "OVERFLOW") and (exc_ok or j > 0 or neg):
            for _ in range(j + 1):
                self._next()
            return ("NOT_" if neg else "") + "ON_" + t.up
        return None

    def parse_handled_action(self, stops: Set[str]) -> Stmt:
        """An arithmetic / ACCEPT / DISPLAY statement whose [NOT] ON SIZE ERROR /
        EXCEPTION handler imperatives are captured as real conditional branches."""
        inner = self.parse_action(stops)
        endword = "END-" + inner.verb
        exc_ok = inner.verb in _EXC_VERBS
        handlers: Dict[str, List[Stmt]] = {}
        while not self._eof():
            t = self._peek()
            if t.kind == "period":
                break
            if t.kind == "word" and t.up == endword:
                self._next()
                break
            key = self._handler_phrase(exc_ok)
            if key is None:
                break
            handlers[key] = self.parse_block(stops={"NOT", "ON", "SIZE"})
        if handlers:
            return HandledStmt(line=inner.line, inner=inner, handlers=handlers)
        return inner

    def parse_opaque_scoped(self, endword: str, stops: Set[str]) -> Stmt:
        """Consume a STRING / UNSTRING statement as one opaque action.

        These carry an *optional* ``END-<verb>`` scope terminator. When it is present
        we consume up to it. When it is ABSENT, COBOL terminates the statement
        implicitly at the next statement-starting verb, so we must stop there too -
        exactly like :meth:`parse_action`. Otherwise a terminator-less STRING swallows
        the entire rest of the paragraph (every following IF / PERFORM / EVALUATE) as a
        single opaque blob, which is the common one-period-per-paragraph style and was
        collapsing real control flow.

        The one exception is an ``ON OVERFLOW`` / ``NOT ON OVERFLOW`` phrase, whose
        imperative legitimately contains verbs; those belong to the statement until its
        ``END-`` word or the sentence period, so we keep consuming while inside it.
        """
        start = self._next()
        parts = [start.text]
        line = start.line
        depth = 1
        in_overflow = False
        while not self._eof():
            t = self._peek()
            if t.kind == "period":
                break
            if t.kind == "word":
                u = t.up
                if u == endword:
                    parts.append(self._next().text)
                    depth -= 1
                    if depth == 0:
                        break
                    continue
                if u == "OVERFLOW":
                    in_overflow = True
                elif not in_overflow and (
                    u in STARTERS or u in stops or self._is_end_word(t)
                    or u in {"ELSE", "WHEN", "THEN"}
                ):
                    break  # implicit terminator: no END- word for this statement
            parts.append(self._next().text)
        return Action(line=line, text=" ".join(parts), verb=start.up)

    # -- IF ----------------------------------------------------------------
    def parse_if(self) -> Stmt:
        line = self._next().line  # IF
        cond = self._collect_condition()
        if self._peek() and self._peek().is_word("THEN"):
            self._next()
        then_body = self.parse_block(stops={"ELSE"})
        else_body: List[Stmt] = []
        if self._peek() and self._peek().is_word("ELSE"):
            self._next()
            else_body = self.parse_block(stops=set())
        if self._peek() and self._peek().is_word("END-IF"):
            self._next()
        return IfStmt(line=line, cond_text=cond, then_body=then_body, else_body=else_body)

    def _collect_condition(self) -> str:
        parts: List[str] = []
        while not self._eof():
            t = self._peek()
            if t.kind == "period":
                break
            if t.kind == "word":
                u = t.up
                if u in STARTERS or u in {"THEN", "ELSE", "WHEN", "CONTINUE", "NEXT"}:
                    break
                if self._is_end_word(t):
                    break
            parts.append(self._next().text)
        return " ".join(parts).strip()

    # -- EVALUATE ----------------------------------------------------------
    def parse_evaluate(self) -> Stmt:
        line = self._next().line  # EVALUATE
        subject = self._collect_until_word({"WHEN"})
        ev = EvaluateStmt(line=line, subject=subject.strip())
        while self._peek() and self._peek().is_word("WHEN"):
            self._next()  # WHEN
            if self._peek() and self._peek().is_word("OTHER"):
                self._next()
                ev.other_body = self.parse_block(stops={"WHEN"})
            else:
                cond = self._collect_condition_when()
                body = self.parse_block(stops={"WHEN"})
                ev.whens.append((cond.strip(), body))
        if self._peek() and self._peek().is_word("END-EVALUATE"):
            self._next()
        _share_stacked_when_bodies(ev.whens, ev.other_body)
        return ev

    def _collect_condition_when(self) -> str:
        parts: List[str] = []
        while not self._eof():
            t = self._peek()
            if t.kind == "period":
                break
            if t.kind == "word":
                u = t.up
                if u in STARTERS or u in {"WHEN"} or self._is_end_word(t):
                    break
            parts.append(self._next().text)
        return " ".join(parts)

    # -- SEARCH ------------------------------------------------------------
    def parse_search(self) -> Stmt:
        line = self._next().line  # SEARCH
        is_all = False
        if self._peek() and self._peek().is_word("ALL"):
            self._next()
            is_all = True
        table = None
        if self._peek() and self._peek().kind == "word" and self._peek().up not in STARTERS:
            table = self._next().up
        varying = None
        if self._peek() and self._peek().is_word("VARYING"):
            self._next()
            if self._peek() and self._peek().kind == "word":
                varying = self._next().up
        st = SearchStmt(line=line, table=table or "?", all=is_all, varying=varying)
        # AT END handler and WHEN branches, in any order, until END-SEARCH / period.
        while not self._eof():
            t = self._peek()
            if t.kind == "period":
                break
            if t.is_word("END-SEARCH"):
                self._next()
                break
            if t.up == "AT" and self._peek(1) and self._peek(1).is_word("END"):
                self._next(); self._next()
                st.at_end_body = self.parse_block(stops={"WHEN"})
                continue
            if t.is_word("WHEN"):
                self._next()
                cond = self._collect_condition_when()
                body = self.parse_block(stops={"WHEN"})
                st.whens.append((cond.strip(), body))
                continue
            # Stray token inside SEARCH: consume to make progress.
            self._next()
        _share_stacked_when_bodies(st.whens, None)
        return st

    def _collect_until_word(self, words: Set[str]) -> str:
        parts: List[str] = []
        while not self._eof():
            t = self._peek()
            if t.kind == "period":
                break
            if t.kind == "word" and (t.up in words or self._is_end_word(t)):
                break
            parts.append(self._next().text)
        return " ".join(parts)

    # -- PERFORM -----------------------------------------------------------
    def parse_perform(self) -> Stmt:
        line = self._next().line  # PERFORM
        t = self._peek()
        control_words = {"UNTIL", "VARYING", "WITH", "TEST", "TIMES", "FOREVER"}
        # Inline PERFORM: PERFORM [control] ... END-PERFORM (no procedure target).
        is_inline = (
            t is None
            or t.kind == "period"
            or (t.kind == "word" and (t.up in control_words or t.up in STARTERS))
        )
        target = None
        thru = None
        if not is_inline and t.kind == "word":
            target = self._next().up
            if self._peek() and self._peek().up in {"THRU", "THROUGH"}:
                self._next()
                if self._peek() and self._peek().kind == "word":
                    thru = self._next().up
        control = self._collect_control_clause()
        kind, test_after = _perform_kind(control, inline=is_inline)
        inline_body: List[Stmt] = []
        if is_inline:
            inline_body = self.parse_block(stops=set())
            if self._peek() and self._peek().is_word("END-PERFORM"):
                self._next()
        return PerformStmt(line=line, kind=kind, target=target, thru=thru,
                           control_text=control.strip(), test_after=test_after,
                           inline_body=inline_body)

    def _collect_control_clause(self) -> str:
        parts: List[str] = []
        while not self._eof():
            t = self._peek()
            if t.kind == "period":
                break
            if t.kind == "word":
                u = t.up
                if u in STARTERS or self._is_end_word(t) or u in {"ELSE", "WHEN"}:
                    break
            parts.append(self._next().text)
        return " ".join(parts)

    # -- GO TO -------------------------------------------------------------
    def parse_goto(self) -> Stmt:
        line = self._next().line  # GO
        if self._peek() and self._peek().is_word("TO"):
            self._next()
        targets: List[str] = []
        depending = False
        while not self._eof():
            t = self._peek()
            if t.kind == "period":
                break
            if t.is_word("DEPENDING"):
                depending = True
                while not self._eof() and not self._at_period():
                    self._next()
                break
            if (t.kind == "word" and t.up not in STARTERS
                    and t.up not in {"ELSE", "WHEN", "THEN"}
                    and not self._is_end_word(t)):
                targets.append(self._next().up)
            else:
                break
        return GoToStmt(line=line, targets=targets, depending=depending)

    # -- CALL --------------------------------------------------------------
    def parse_call(self) -> Stmt:
        line = self._next().line  # CALL
        t = self._peek()
        target = "?"
        dynamic = True
        if t is not None:
            if t.kind == "string":
                target = t.text.strip("'\"")
                dynamic = False
                self._next()
            elif t.kind == "word":
                target = t.up
                dynamic = True
                self._next()
        using: List[str] = []
        by_content: List[str] = []
        returning: Optional[str] = None
        handlers: Dict[str, List[Stmt]] = {}
        mode = None  # 'using' | 'returning' while collecting arg names
        passing = "REFERENCE"  # BY REFERENCE (default) | CONTENT | VALUE, sticky per arg
        while not self._eof():
            t = self._peek()
            if t.kind == "period":
                break
            if t.kind == "word":
                if t.up == "END-CALL":
                    self._next()
                    break
                if t.up in {"NOT", "ON", "EXCEPTION", "OVERFLOW"}:
                    # [NOT] [ON] EXCEPTION/OVERFLOW imperative: a conditional branch,
                    # captured as a handler body (not hoisted into the main flow).
                    key = self._handler_phrase(exc_ok=True)
                    if key is not None:
                        handlers[key] = self.parse_block(stops={"NOT", "ON"})
                        mode = None
                        continue
                    self._next()  # stray NOT/ON: skip so we make progress
                    continue
                if t.up == "USING":
                    mode = "using"
                    passing = "REFERENCE"
                    self._next()
                    continue
                if t.up in {"RETURNING", "GIVING"}:
                    mode = "returning"
                    self._next()
                    continue
                if t.up == "BY":
                    self._next()
                    continue
                if t.up in {"REFERENCE", "CONTENT", "VALUE"}:
                    passing = t.up
                    self._next()
                    continue
                if t.up in STARTERS and t.up != "CALL":
                    break
                if mode == "using":
                    using.append(t.up)
                    if passing in ("CONTENT", "VALUE"):
                        by_content.append(t.up)
                elif mode == "returning":
                    returning = t.up
                    mode = None
            self._next()
        return CallStmt(line=line, target=target, dynamic=dynamic,
                        on_exception=bool(handlers), using=using, returning=returning,
                        by_content=by_content, handlers=handlers)

    # -- ALTER -------------------------------------------------------------
    def parse_alter(self) -> Stmt:
        line = self._next().line  # ALTER
        parts = ["ALTER"]
        words: List[str] = []
        # ALTER operands are only paragraph-names + TO/PROCEED; stop at the next
        # statement (there may be no period before it, e.g. a following GO TO).
        while not self._eof() and not self._at_period():
            t = self._peek()
            if t.kind == "word" and (t.up in STARTERS or self._is_end_word(t)
                                     or t.up in {"ELSE", "WHEN"}):
                break
            self._next()
            parts.append(t.text)
            if t.kind == "word" and t.up not in {"TO", "PROCEED"}:
                words.append(t.up)
        # words arrive as [altered, target, altered, target, ...]
        pairs = [(words[i], words[i + 1]) for i in range(0, len(words) - 1, 2)]
        return AlterStmt(line=line, text=" ".join(parts), pairs=pairs)

    # -- EXIT --------------------------------------------------------------
    def parse_exit(self) -> Stmt:
        line = self._next().line  # EXIT
        t = self._peek()
        if t and t.is_word("PROGRAM"):
            self._next()
            return TerminateStmt(line=line, kind="EXIT_PROGRAM")
        if t and t.is_word("PERFORM"):
            self._next()
            if self._peek() and self._peek().is_word("CYCLE"):
                self._next()
                return ExitStmt(line=line, kind="PERFORM_CYCLE")
            return ExitStmt(line=line, kind="PERFORM")
        if t and t.is_word("PARAGRAPH"):
            self._next()
            return ExitStmt(line=line, kind="PARAGRAPH")
        if t and t.is_word("SECTION"):
            self._next()
            return ExitStmt(line=line, kind="SECTION")
        return ExitStmt(line=line, kind="PLAIN")

    # -- EXEC SQL / CICS / DLI ---------------------------------------------
    def parse_exec(self) -> Stmt:
        line = self._next().line  # EXEC
        lang = "?"
        if self._peek() and self._peek().kind == "word":
            lang = self._next().up
        toks: List[Token] = []
        while not self._eof():
            t = self._peek()
            if t.kind == "word" and t.up == "END-EXEC":
                self._next()
                break
            toks.append(self._next())
        # optional terminating period
        if self._at_period():
            self._next()
        words = [t.up for t in toks if t.kind == "word"]
        verb = words[0] if words else "?"
        text = " ".join(t.text for t in toks)
        # host variables: ':' immediately followed by a word
        host_vars: List[str] = []
        for idx, t in enumerate(toks):
            if t.kind == "punct" and t.text == ":" and idx + 1 < len(toks) \
                    and toks[idx + 1].kind == "word":
                host_vars.append(":" + toks[idx + 1].text.upper())

        kind, target, conditions = "effect", None, []
        into_vars: List[str] = []
        if lang == "CICS":
            if verb in ("RETURN", "ABEND"):
                kind = "terminate"
            elif verb == "XCTL":
                kind, target = "transfer", self._exec_program(toks)
            elif verb == "LINK":
                kind, target = "call", self._exec_program(toks)
            elif verb == "HANDLE":
                kind = "handle"
                conditions = self._exec_handle_conditions(toks)
        elif lang == "SQL":
            if verb == "WHENEVER":
                kind = "handle"
            elif verb in ("SELECT", "FETCH"):
                # SELECT/FETCH ... INTO :a, :b ... FROM/WHERE: the DB populates the host
                # variables. Capture them so the action models a real (external) input.
                into_vars = self._exec_into_vars(toks)
                if into_vars:
                    kind = "input"
        return ExecStmt(line=line, lang=lang, verb=verb, text=text, kind=kind,
                        target=target, host_vars=host_vars, conditions=conditions,
                        into_vars=into_vars)

    @staticmethod
    def _exec_into_vars(toks: List[Token]) -> List[str]:
        """Collect the :host-vars in the INTO clause of a SELECT/FETCH (up to the next
        clause keyword)."""
        out: List[str] = []
        i = 0
        while i < len(toks):
            if toks[i].kind == "word" and toks[i].up == "INTO":
                i += 1
                while i < len(toks):
                    t = toks[i]
                    if t.kind == "word" and t.up in ("FROM", "WHERE", "ORDER", "GROUP",
                                                     "HAVING", "FOR"):
                        break
                    if t.kind == "punct" and t.text == ":" and i + 1 < len(toks) \
                            and toks[i + 1].kind == "word":
                        out.append(toks[i + 1].up)
                        i += 2
                        continue
                    i += 1
                break
            i += 1
        return out

    @staticmethod
    def _exec_program(toks: List[Token]) -> Optional[str]:
        """Pull PROGRAM('NAME') or PROGRAM(NAME) from a CICS command."""
        for idx, t in enumerate(toks):
            if t.kind == "word" and t.up == "PROGRAM":
                for k in range(idx + 1, min(idx + 4, len(toks))):
                    if toks[k].kind == "string":
                        return toks[k].text.strip("'\"").strip()
                    if toks[k].kind == "word":
                        return toks[k].up
        return None

    @staticmethod
    def _exec_handle_conditions(toks: List[Token]) -> List[str]:
        names = [t.up for t in toks if t.kind == "word"
                 and t.up not in ("HANDLE", "CONDITION", "AID")]
        return names

    # -- I/O ---------------------------------------------------------------
    # -- SORT / MERGE ------------------------------------------------------
    _SORT_KEYWORDS = {
        "INPUT", "OUTPUT", "USING", "GIVING", "ON", "ASCENDING", "DESCENDING", "KEY",
        "WITH", "DUPLICATES", "IN", "ORDER", "COLLATING", "SEQUENCE", "IS", "THRU",
        "THROUGH", "PROCEDURE",
    }

    def _sort_name(self):
        """Read a single procedure/file name token (not a keyword or statement starter)."""
        t = self._peek()
        if t and t.kind == "word" and t.up not in self._SORT_KEYWORDS and t.up not in STARTERS:
            return self._next()
        return None

    def parse_sort(self) -> Stmt:
        vt = self._next()
        verb, line = vt.up, vt.line
        parts = [vt.text]
        file_name = None
        nt = self._peek()
        if nt and nt.kind == "word" and nt.up not in STARTERS:
            file_name = self._next().up
            parts.append(file_name)

        in_proc = in_thru = out_proc = out_thru = None
        using: List[str] = []
        giving: List[str] = []

        def read_proc():
            if self._peek() and self._peek().is_word("IS"):
                self._next()
            head = self._sort_name()
            thru = None
            if head and self._peek() and self._peek().up in ("THRU", "THROUGH"):
                self._next()
                tt = self._sort_name()
                thru = tt.up if tt else None
            return (head.up if head else None), thru

        while not self._eof():
            t = self._peek()
            if t.kind == "period":
                break
            if t.kind == "word":
                u = t.up
                if u in STARTERS:
                    break
                if u == "INPUT" and self._peek(1) and self._peek(1).up == "PROCEDURE":
                    self._next(); self._next()
                    in_proc, in_thru = read_proc()
                    continue
                if u == "OUTPUT" and self._peek(1) and self._peek(1).up == "PROCEDURE":
                    self._next(); self._next()
                    out_proc, out_thru = read_proc()
                    continue
                if u in ("USING", "GIVING"):
                    self._next()
                    bucket = using if u == "USING" else giving
                    while True:
                        nm = self._sort_name()
                        if nm is None:
                            break
                        bucket.append(nm.up)
                    continue
            self._next()  # skip ordering noise (ON ASCENDING KEY ..., COLLATING ...)

        return SortStmt(line=line, verb=verb, file=file_name,
                        input_proc=in_proc, input_thru=in_thru,
                        output_proc=out_proc, output_thru=out_thru,
                        using=using, giving=giving, raw=" ".join(parts))

    def parse_io(self) -> Stmt:
        verb_tok = self._next()
        verb = verb_tok.up
        line = verb_tok.line
        endword = "END-" + verb
        file_name = None
        if self._peek() and self._peek().kind == "word" and self._peek().up not in STARTERS:
            file_name = self._next().up
        handlers = {}
        into: Optional[str] = None
        from_: Optional[str] = None

        def _end_key(consume_at: bool) -> str:
            """Consume the END / END-OF-PAGE word after [NOT] AT and return the key stem."""
            if consume_at and self._peek() and self._peek().is_word("AT"):
                self._next()
            t = self._peek()
            if t is not None and t.kind == "word" and t.up in ("END-OF-PAGE", "EOP"):
                self._next()
                return "AT_EOP"
            if t is not None and t.is_word("END"):
                self._next()
            return "AT_END"

        # Skip clause noise (NEXT/KEY/ADVANCING/...) until a handler intro, end, or
        # period; capture INTO/FROM data targets on the way.
        while not self._eof():
            t = self._peek()
            if t.kind == "period":
                break
            if t.kind == "word":
                u = t.up
                if u == endword:
                    self._next()
                    break
                if u == "INTO" or u == "FROM":
                    self._next()
                    nxt = self._peek()
                    if nxt is not None and nxt.kind == "word" and nxt.up not in STARTERS:
                        if u == "INTO":
                            into = self._next().up
                        else:
                            from_ = self._next().up
                    continue
                if u == "AT" or u in ("END-OF-PAGE", "EOP"):
                    key = _end_key(consume_at=(u == "AT")) if u == "AT" else "AT_EOP"
                    if u != "AT":
                        self._next()
                    handlers[key] = self.parse_block(stops={"NOT", "AT", "END"})
                    continue
                if u == "INVALID":
                    self._next()
                    if self._peek() and self._peek().is_word("KEY"):
                        self._next()
                    handlers["INVALID_KEY"] = self.parse_block(stops={"NOT", "INVALID"})
                    continue
                if u == "NOT":
                    self._next()
                    nxt = self._peek()
                    if nxt is not None and (nxt.is_word("AT") or nxt.kind == "word"
                                            and nxt.up in ("END", "END-OF-PAGE", "EOP")):
                        key = "NOT_" + _end_key(consume_at=nxt.is_word("AT"))
                        handlers[key] = self.parse_block(stops={"AT", "INVALID", "NOT"})
                        continue
                    if nxt is not None and nxt.is_word("INVALID"):
                        self._next()
                        if self._peek() and self._peek().is_word("KEY"):
                            self._next()
                        handlers["NOT_INVALID_KEY"] = self.parse_block(
                            stops={"AT", "INVALID", "NOT"})
                        continue
                    continue
                if u in _IO_CLAUSE_WORDS:
                    self._next()  # I/O clause word (READ f NEXT RECORD, AFTER ADVANCING...)
                    continue
                if u in STARTERS:
                    break
            self._next()
        return IoStmt(line=line, verb=verb, file=file_name, handlers=handlers,
                      into=into, from_=from_)


def _perform_kind(control: str, inline: bool):
    up = control.upper()
    test_after = "TEST AFTER" in up or ("AFTER" in up and "TEST" in up and "BEFORE" not in up)
    if "VARYING" in up:
        return "varying", test_after
    if "UNTIL" in up:
        return "until", test_after
    if "TIMES" in up:
        return "times", test_after
    if inline:
        return "inline", test_after
    return "call", test_after
