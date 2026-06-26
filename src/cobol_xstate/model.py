"""Control-flow AST for the PROCEDURE DIVISION.

Only constructs that can alter the order of execution get their own node; a run of
straight-line data manipulation (MOVE/COMPUTE/ADD ...) collapses into ``Action``
nodes that later fold into a state's action list (the reduction principle from
references/cobol-to-statecharts.md). Every node carries the source line for
provenance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class Stmt:
    line: int


@dataclass
class Action(Stmt):
    """Opaque straight-line statement (MOVE, ADD, OPEN, DISPLAY, SET, ...).

    ``text`` is the original spelling; ``verb`` is the leading word. The body is not
    interpreted - it becomes a *named* action reference in the statechart, never an
    inferred implementation.
    """

    text: str
    verb: str


@dataclass
class IfStmt(Stmt):
    cond_text: str
    then_body: List[Stmt] = field(default_factory=list)
    else_body: List[Stmt] = field(default_factory=list)


@dataclass
class EvaluateStmt(Stmt):
    subject: str
    whens: List[Tuple[str, List[Stmt]]] = field(default_factory=list)  # (cond, body)
    other_body: Optional[List[Stmt]] = None


@dataclass
class PerformStmt(Stmt):
    """PERFORM in all its forms.

    kind:
      'call'    - PERFORM p [THRU q]                (call-return into a range)
      'until'   - PERFORM p UNTIL c                 (out-of-line loop)
      'times'   - PERFORM p N TIMES
      'varying' - PERFORM p VARYING ... UNTIL c
      'inline'  - PERFORM ... END-PERFORM           (body is inline_body)
    """

    kind: str
    target: Optional[str] = None
    thru: Optional[str] = None
    control_text: str = ""      # raw UNTIL/VARYING/TIMES clause, for provenance
    test_after: bool = False
    inline_body: List[Stmt] = field(default_factory=list)


@dataclass
class GoToStmt(Stmt):
    targets: List[str]
    depending: bool = False     # GO TO ... DEPENDING ON  -> computed multi-target


@dataclass
class AlterStmt(Stmt):
    text: str                   # original spelling
    # (altered-paragraph, new-proceed-to-target) pairs. ALTER rewrites the GO TO at
    # the *head* of `altered` so it proceeds to `target` - i.e. a switchable exit.
    pairs: List[Tuple[str, str]] = field(default_factory=list)


@dataclass
class CallStmt(Stmt):
    target: str
    dynamic: bool               # CALL identifier (not a literal) -> target unknown
    on_exception: bool = False


@dataclass
class IoStmt(Stmt):
    """READ / WRITE / REWRITE / DELETE / START with their implicit handlers.

    handlers maps a handler key to the statements guarded by it:
      'AT_END', 'NOT_AT_END', 'INVALID_KEY', 'NOT_INVALID_KEY'
    The handler edges are control flow that is invisible at the I/O site.
    """

    verb: str
    file: Optional[str]
    handlers: Dict[str, List[Stmt]] = field(default_factory=dict)


@dataclass
class TerminateStmt(Stmt):
    kind: str                   # 'STOP_RUN' | 'GOBACK' | 'EXIT_PROGRAM'


@dataclass
class ExitStmt(Stmt):
    kind: str                   # 'PARAGRAPH'|'SECTION'|'PERFORM'|'PERFORM_CYCLE'|'PLAIN'


@dataclass
class ContinueStmt(Stmt):
    next_sentence: bool = False  # NEXT SENTENCE differs from CONTINUE - flagged


@dataclass
class Paragraph:
    name: str
    line: int
    section: Optional[str] = None
    statements: List[Stmt] = field(default_factory=list)


@dataclass
class Program:
    program_id: str
    paragraphs: List[Paragraph] = field(default_factory=list)
    has_procedure_division: bool = False
    notes: List[str] = field(default_factory=list)  # parser-level remarks
    # data-name -> initial literal from a WORKING-STORAGE `VALUE 'lit'` clause, used
    # by constant propagation to resolve dynamic CALL targets.
    working_values: Dict[str, str] = field(default_factory=dict)
    # DATA DIVISION recovery (data_division.DataItem); duck-typed to avoid coupling.
    data_items: List = field(default_factory=list)
    data_by_name: Dict[str, object] = field(default_factory=dict)


def walk_statements(stmts: List[Stmt]):
    """Yield every statement, descending into IF / EVALUATE / PERFORM-inline / I-O
    handler bodies (needed by whole-program analyses like constant propagation)."""
    for st in stmts:
        yield st
        if isinstance(st, IfStmt):
            yield from walk_statements(st.then_body)
            yield from walk_statements(st.else_body)
        elif isinstance(st, EvaluateStmt):
            for _cond, body in st.whens:
                yield from walk_statements(body)
            if st.other_body:
                yield from walk_statements(st.other_body)
        elif isinstance(st, PerformStmt):
            yield from walk_statements(st.inline_body)
        elif isinstance(st, IoStmt):
            for body in st.handlers.values():
                yield from walk_statements(body)
