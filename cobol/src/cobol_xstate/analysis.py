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
_SET_TRUE_RE = re.compile(r"^SET\s+(.+?)\s+TO\s+TRUE\b", re.I)
_NAME_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]*$", re.I)
# Runs for every MOVE / SET in the program - compiled once, like its neighbours.
_SPLIT_OPERANDS = re.compile(r"[\s,]+")


@dataclass
class CallResolution:
    confident: bool                 # exactly one literal reaches, no variable assign
    resolved: Optional[str]         # the literal target when confident
    candidates: List[str] = field(default_factory=list)  # all literal possibilities
    has_variable_assignment: bool = False
    reason: str = ""
    # HOW GOOD the candidates are, which is not the same question as how many there are:
    #   "assigned"    - a MOVE or a VALUE clause provably stores this literal
    #   "declared-88" - an 88-level names it as a possible value, but NO SET/MOVE in the
    #                   visible source proves it is ever stored
    # Collapsing the two lets a declared-but-never-stored name be reported with the same
    # confidence as one the program demonstrably moves, which is an overclaim: the first
    # is what the program was WRITTEN to allow, the second is what it DOES.
    evidence: Optional[str] = None


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
    # parent item -> the string literals its 88-level condition names carry. When no
    # assignment reaches the item at all, these are still the values the program was
    # WRITTEN to put there (via SET ... TO TRUE) - reported as candidates, not proof.
    condition_literals: Dict[str, List[str]] = field(default_factory=dict)

    def resolve(self, name: str) -> CallResolution:
        name = name.upper()
        lits = sorted(self.literal_assigns.get(name, set()))
        var = name in self.var_assigns
        if len(lits) == 1 and not var:
            return CallResolution(True, lits[0], lits, False,
                                  f"only literal reaching {name} is '{lits[0]}'",
                                  evidence="assigned")
        if lits and not var:
            return CallResolution(False, None, lits, False,
                                  f"{name} may be one of {lits}; verify reaching definition",
                                  evidence="assigned")
        if lits and var:
            return CallResolution(False, None, lits, True,
                                  f"{name} set to {lits} and also to a variable; runtime-determined",
                                  evidence="assigned")
        if var:
            return CallResolution(False, None, [], True,
                                  f"{name} set only from variables; target runtime-determined")
        c88 = sorted(set(self.condition_literals.get(name, [])))
        if c88:
            return CallResolution(False, None, c88, False,
                                  f"{name} carries 88-level condition value(s) {c88} "
                                  f"but no SET ... TO TRUE or MOVE in the visible "
                                  f"source proves which reaches; verify",
                                  evidence="declared-88")
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

    # 88-level condition names with string VALUEs: `SET <cond> TO TRUE` stores the
    # condition's (first) VALUE into its parent item - a literal-assignment channel on
    # a par with MOVE 'lit' (the `88 DCIOC104-MODULE VALUE 'DCIOC104'` idiom for
    # dynamic CALL targets). cond -> (parent, literal-it-SETs); parent -> all its
    # 88 string literals (candidate values even when no SET is visible).
    cond_lit: Dict[str, tuple] = {}
    cond_values: Dict[str, List[str]] = {}
    for name, it in (getattr(program, "data_by_name", None) or {}).items():
        parent = getattr(it, "cond_parent", None)
        vals = [str(v).strip("'\"") for v in (getattr(it, "condition_values", None) or [])
                if str(v)[:1] in ("'", '"')]
        if parent and vals:
            cond_lit[str(name).upper()] = (str(parent).upper(), vals[0])
            cond_values.setdefault(str(parent).upper(), []).extend(vals)

    # Fold in every MOVE and SET ... TO TRUE in the procedure division.
    for para in program.paragraphs:
        for st in walk_statements(para.statements):
            if not isinstance(st, Action):
                continue
            verb = st.verb.upper()
            if verb == "MOVE":
                m = _MOVE_RE.match(st.text.strip())
                if not m:
                    continue
                source = m.group(1).strip()
                targets = [t for t in _SPLIT_OPERANDS.split(m.group(2).strip())
                           if _NAME_RE.match(t)]
                if source[:1] in ("'", '"'):
                    lit = source.strip("'\"").rstrip()
                    for t in targets:
                        literal_assigns.setdefault(t.upper(), set()).add(lit)
                else:
                    for t in targets:
                        var_assigns.add(t.upper())
            elif verb == "SET":
                m = _SET_TRUE_RE.match(st.text.strip())
                if not m:
                    continue
                for t in _SPLIT_OPERANDS.split(m.group(1).strip()):
                    cl = cond_lit.get(t.upper())
                    if cl:
                        literal_assigns.setdefault(cl[0], set()).add(cl[1])
    declared = {str(n).upper() for n in (getattr(program, "data_by_name", None) or {})}
    missing = [str(cb.get("member", "")).upper()
               for cb in (getattr(program, "copybooks", None) or [])
               if cb.get("status") == "missing"]
    return CallAnalysis(literal_assigns=literal_assigns, var_assigns=var_assigns,
                        declared=declared, missing_copybooks=missing,
                        condition_literals=cond_values)
