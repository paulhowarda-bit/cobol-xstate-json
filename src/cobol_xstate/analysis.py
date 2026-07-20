"""Whole-program analyses over the recovered AST.

Currently: **constant propagation for dynamic CALL targets.** A `CALL identifier`
has a runtime-determined target in general, but in the common case the identifier is
only ever set to a literal in this program - a `WORKING-STORAGE VALUE 'POSTLOG'`
clause or a `MOVE 'POSTLOG' TO WS-SUBPGM`. When a single literal is the *only*
reaching value, the target resolves and the "unknown target" flag can be dropped.

This is a *may*-analysis, not flow-sensitive reaching-definitions: it is honest about
that by staying flagged whenever a non-literal assignment can also reach the call, or
when more than one literal can. The skill's rule holds - resolve when provably
constant, flag (don't guess) otherwise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .model import Action, Program, walk_statements

_MOVE_RE = re.compile(r"^MOVE\s+(.+?)\s+TO\s+(.+)$", re.I)
_NAME_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]*$", re.I)


@dataclass
class CallResolution:
    confident: bool                 # exactly one literal reaches, no variable assign
    resolved: Optional[str]         # the literal target when confident
    candidates: List[str] = field(default_factory=list)  # all literal possibilities
    has_variable_assignment: bool = False
    reason: str = ""


@dataclass
class CallAnalysis:
    literal_assigns: Dict[str, Set[str]]
    var_assigns: Set[str]
    # All data-item names visible to the parse, so an unresolvable name can be
    # diagnosed honestly: "declared but never assigned" is a different situation from
    # "not declared at all" - the latter usually means the item (and its VALUE) lives
    # in a copybook that was not found, which `missing_copybooks` names.
    declared: Set[str] = field(default_factory=set)
    missing_copybooks: List[str] = field(default_factory=list)

    def resolve(self, name: str) -> CallResolution:
        name = name.upper()
        lits = sorted(self.literal_assigns.get(name, set()))
        var = name in self.var_assigns
        if len(lits) == 1 and not var:
            return CallResolution(True, lits[0], lits, False,
                                  f"only literal reaching {name} is '{lits[0]}'")
        if lits and not var:
            return CallResolution(False, None, lits, False,
                                  f"{name} may be one of {lits}; verify reaching definition")
        if lits and var:
            return CallResolution(False, None, lits, True,
                                  f"{name} set to {lits} and also to a variable; runtime-determined")
        if var:
            return CallResolution(False, None, [], True,
                                  f"{name} set only from variables; target runtime-determined")
        if name not in self.declared:
            hint = (f" - likely defined (with its VALUE) in a missing copybook "
                    f"({', '.join(self.missing_copybooks)})"
                    if self.missing_copybooks else "")
            return CallResolution(False, None, [], False,
                                  f"{name} is not declared in the visible source{hint}; "
                                  f"target runtime-determined")
        return CallResolution(False, None, [], False,
                              f"{name} is declared but never assigned a literal; "
                              f"target runtime-determined")


def analyze_calls(program: Program) -> CallAnalysis:
    literal_assigns: Dict[str, Set[str]] = {}
    var_assigns: Set[str] = set()

    # Seed from WORKING-STORAGE VALUE clauses (an initial literal value).
    for name, lit in program.working_values.items():
        literal_assigns.setdefault(name.upper(), set()).add(lit)

    # Fold in every MOVE in the procedure division.
    for para in program.paragraphs:
        for st in walk_statements(para.statements):
            if isinstance(st, Action) and st.verb.upper() == "MOVE":
                m = _MOVE_RE.match(st.text.strip())
                if not m:
                    continue
                source = m.group(1).strip()
                targets = [t for t in re.split(r"[\s,]+", m.group(2).strip())
                           if _NAME_RE.match(t)]
                if source[:1] in ("'", '"'):
                    lit = source.strip("'\"").rstrip()
                    for t in targets:
                        literal_assigns.setdefault(t.upper(), set()).add(lit)
                else:
                    for t in targets:
                        var_assigns.add(t.upper())
    declared = {str(n).upper() for n in (getattr(program, "data_by_name", None) or {})}
    missing = [str(cb.get("member", "")).upper()
               for cb in (getattr(program, "copybooks", None) or [])
               if cb.get("status") == "missing"]
    return CallAnalysis(literal_assigns=literal_assigns, var_assigns=var_assigns,
                        declared=declared, missing_copybooks=missing)
