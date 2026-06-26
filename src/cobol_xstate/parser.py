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
from typing import List, Optional, Set

from .normalizer import CodeLine, SourceFormat, normalize
from .lexer import Token, tokenize
from .data_division import parse_data_division
from .model import (
    Action,
    AlterStmt,
    CallStmt,
    ContinueStmt,
    EvaluateStmt,
    ExitStmt,
    GoToStmt,
    IfStmt,
    IoStmt,
    Paragraph,
    PerformStmt,
    Program,
    Stmt,
    TerminateStmt,
)

# Verbs that begin a statement. Used to bound opaque actions and conditions.
ACTION_VERBS: Set[str] = {
    "MOVE", "ADD", "SUBTRACT", "MULTIPLY", "DIVIDE", "COMPUTE", "OPEN", "CLOSE",
    "DISPLAY", "ACCEPT", "SET", "INITIALIZE", "RELEASE", "SORT", "MERGE", "CANCEL",
    "INVOKE", "UNLOCK", "STRING", "UNSTRING", "INSPECT", "SEARCH", "ALLOCATE", "FREE",
}
CONTROL_VERBS: Set[str] = {
    "IF", "EVALUATE", "PERFORM", "GO", "READ", "WRITE", "REWRITE", "DELETE", "START",
    "RETURN", "CALL", "ALTER", "EXIT", "CONTINUE", "NEXT", "STOP", "GOBACK",
}
STARTERS: Set[str] = ACTION_VERBS | CONTROL_VERBS

# Verbs consumed opaquely up to their END- terminator (they carry inner WHEN / AT END
# / ON OVERFLOW clauses we do not model in v0.1).
OPAQUE_SCOPED = {"SEARCH": "END-SEARCH", "STRING": "END-STRING", "UNSTRING": "END-UNSTRING"}

IO_VERBS = {"READ", "WRITE", "REWRITE", "DELETE", "START", "RETURN"}

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


def parse_program(source: str, fmt: Optional[SourceFormat] = None) -> Program:
    lines = normalize(source, fmt)
    prog = Program(program_id=_find_program_id(lines))

    if any(re.search(r"\bPROCEDURE\s+DIVISION\b", cl.text, re.I) for cl in lines):
        prog.has_procedure_division = True
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

    # Group body lines into paragraphs by Area-A headers.
    current = Paragraph(name="_ENTRY_", line=body[0].line)
    section: Optional[str] = None
    buckets: List[Paragraph] = [current]
    bucket_lines: List[List[CodeLine]] = [[]]
    for cl in body:
        name = _header_name(cl)
        if name is not None:
            if cl.text.strip().upper().endswith("SECTION ."):
                section = name
            is_section = bool(re.search(r"\bSECTION\b", cl.text, re.I))
            section = name if is_section else section
            current = Paragraph(name=name, line=cl.line,
                                section=None if is_section else section)
            buckets.append(current)
            bucket_lines.append([])
            if is_section:
                # A SECTION header is itself a state boundary but has no body of its
                # own beyond following paragraphs; keep it as an empty paragraph.
                continue
        else:
            bucket_lines[-1].append(cl)

    for para, plines in zip(buckets, bucket_lines):
        toks = tokenize(plines)
        para.statements = StmtParser(toks).parse_paragraph()

    # Drop the synthetic entry bucket if it carried no statements.
    if buckets and buckets[0].name == "_ENTRY_" and not buckets[0].statements:
        buckets.pop(0)
    prog.paragraphs = buckets
    return prog


# --------------------------------------------------------------------------- #
# Pass 2: statement parser (recursive descent over a paragraph's tokens)
# --------------------------------------------------------------------------- #

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
        if v == "IF":
            return self.parse_if()
        if v == "EVALUATE":
            return self.parse_evaluate()
        if v == "PERFORM":
            return self.parse_perform()
        if v == "GO":
            return self.parse_goto()
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
            return self.parse_opaque_scoped(OPAQUE_SCOPED[v])
        return self.parse_action(stops)

    # -- opaque action -----------------------------------------------------
    def parse_action(self, stops: Set[str]) -> Stmt:
        start = self._next()
        verb = start.up
        parts = [start.text]
        line = start.line
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
                                 and self._peek(1).up in {"AT", "INVALID", "ON"}):
                    break
                if u == "INVALID":
                    break
            parts.append(self._next().text)
        return Action(line=line, text=" ".join(parts), verb=verb)

    def parse_opaque_scoped(self, endword: str) -> Stmt:
        start = self._next()
        parts = [start.text]
        line = start.line
        depth = 1
        while not self._eof():
            t = self._peek()
            if t.kind == "period":
                break
            if t.kind == "word" and t.up == endword:
                parts.append(self._next().text)
                depth -= 1
                if depth == 0:
                    break
                continue
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
        on_exc = False
        while not self._eof():
            t = self._peek()
            if t.kind == "period":
                break
            if t.kind == "word":
                if t.up == "END-CALL":
                    self._next()
                    break
                if t.up in {"EXCEPTION", "OVERFLOW"}:
                    on_exc = True
                if t.up in STARTERS and t.up not in {"CALL"}:
                    break
            self._next()
        return CallStmt(line=line, target=target, dynamic=dynamic, on_exception=on_exc)

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

    # -- I/O ---------------------------------------------------------------
    def parse_io(self) -> Stmt:
        verb_tok = self._next()
        verb = verb_tok.up
        line = verb_tok.line
        endword = "END-" + verb
        file_name = None
        if self._peek() and self._peek().kind == "word" and self._peek().up not in STARTERS:
            file_name = self._next().up
        handlers = {}
        # Skip clause noise (INTO/FROM/KEY/...) until a handler intro, end, or period.
        while not self._eof():
            t = self._peek()
            if t.kind == "period":
                break
            if t.kind == "word":
                u = t.up
                if u == endword:
                    self._next()
                    break
                if u == "AT":
                    self._next()
                    key = "AT_END"
                    if self._peek() and self._peek().is_word("END"):
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
                    if self._peek() and self._peek().is_word("AT"):
                        self._next()
                        if self._peek() and self._peek().is_word("END"):
                            self._next()
                        handlers["NOT_AT_END"] = self.parse_block(stops={"AT", "INVALID"})
                        continue
                    if self._peek() and self._peek().is_word("INVALID"):
                        self._next()
                        if self._peek() and self._peek().is_word("KEY"):
                            self._next()
                        handlers["NOT_INVALID_KEY"] = self.parse_block(stops={"AT", "INVALID"})
                        continue
                    continue
                if u in STARTERS:
                    break
            self._next()
        return IoStmt(line=line, verb=verb, file=file_name, handlers=handlers)


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
