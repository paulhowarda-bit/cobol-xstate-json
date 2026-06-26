"""Stage 4 - map the recovered control flow onto an XState v5 statechart.

The output is a **bare ``createMachine`` config as serializable data** (the kind
``JSON.parse`` could feed straight into XState v5): states, ``entry`` action-name
lists, and eventless ``always`` transitions, with guards and actions referenced **by
name as strings only**. No guard or action body is invented (references/
cobol-to-statecharts.md).

Modeling decisions (stated, because honesty is the contract):

* **One state per paragraph/section** (OR-state siblings). This is the altitude of
  the skill's own ``cfg_extract.py`` reference skeleton.
* **Every control transfer becomes an ``always`` transition** - PERFORM (call-return),
  GO TO (no return), fall-through, and the transfers inside IF / EVALUATE / I-O
  handler branches (which carry **guards** recovered from the COBOL condition). Each
  transition records its ``meta.kind`` and the COBOL line.
* Because XState evaluates ``always`` edges in document order and a PERFORM's *return*
  edge cannot be inferred statically, the result is an honest **review skeleton**, not
  a guaranteed-equivalent machine. The disclaimer travels in the output.
* Constructs a static pass cannot resolve - ``ALTER``, ``GO TO ... DEPENDING ON``,
  dynamic ``CALL``, ``NEXT SENTENCE``, ``DECLARATIVES`` - are **flagged**, never
  smoothed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .analysis import CallAnalysis, analyze_calls
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
    walk_statements,
)
from .naming import NameRegistry, _slug

_IO_GUARD_KEY = {
    "AT_END": "atEnd",
    "NOT_AT_END": "notAtEnd",
    "INVALID_KEY": "invalidKey",
    "NOT_INVALID_KEY": "notInvalidKey",
}


@dataclass
class Machine:
    config: dict
    provenance: Dict[str, dict]
    flags: List[dict]
    notes: List[str]
    program_id: str
    source_name: str = "<source>"

    def bundle(self) -> dict:
        return {
            "format": "xstate-v5-config",
            "metadata": {
                "program": self.program_id,
                "source": self.source_name,
                "generator": "cobol-xstate 0.1.0",
                "disclaimer": (
                    "Heuristic control-flow recovery, not a conformant COBOL parse. "
                    "Guards/actions are names only (no invented logic); meaning lives "
                    "in 'provenance'. PERFORM return edges are not inferred and "
                    "'always' edges are document-ordered, so review this as a "
                    "skeleton. Items under 'flags' are not statically resolvable."
                ),
            },
            "machine": self.config,
            "provenance": self.provenance,
            "flags": self.flags,
            "notes": self.notes,
        }

    def to_json(self, machine_only: bool = False, indent: int = 2) -> str:
        obj = self.config if machine_only else self.bundle()
        return json.dumps(obj, indent=indent)


# --------------------------------------------------------------------------- #


@dataclass
class _BuildCtx:
    reg: NameRegistry
    calls: CallAnalysis
    # altered-paragraph -> ordered candidate exit targets (orig GO TO + PROCEED-TOs)
    alter_targets: Dict[str, List[str]] = field(default_factory=dict)
    context: Dict[str, object] = field(default_factory=dict)
    flags: List[dict] = field(default_factory=list)
    _seen_flags: set = field(default_factory=set)

    def flag(self, para: str, line: int, message: str) -> None:
        key = (para, message)
        if key in self._seen_flags:
            return
        self._seen_flags.add(key)
        self.flags.append({"paragraph": para, "line": line, "message": message})


def _call_action(st: CallStmt, ctx: _BuildCtx, para: str) -> str:
    """Action name for a CALL, resolving a dynamic target by constant propagation
    where the program proves it constant (else flag, don't guess)."""
    reg = ctx.reg
    if not st.dynamic:
        return reg.action_named("call_" + st.target, f"CALL '{st.target}'", st.line)
    res = ctx.calls.resolve(st.target)
    if res.confident and res.resolved:
        return reg.action_named(
            "call_" + res.resolved,
            f"CALL {st.target} -> resolved '{res.resolved}' ({res.reason})", st.line)
    # Unresolved or ambiguous: keep the identifier name and flag it.
    ctx.flag(para, st.line, f"dynamic CALL {st.target} - {res.reason}")
    return reg.action_named("call_" + st.target, f"CALL (dynamic) {st.target}", st.line)


def _summarize_transfer(body: List[Stmt], reg: NameRegistry,
                        ctx: _BuildCtx, para: str) -> Tuple[List[str], Optional[str], bool]:
    """Reduce a branch/handler body to (action-names, transfer-target, is_final).

    Looks one level deep: leading straight-line work becomes action names, the first
    PERFORM/GO TO sets the target, a terminator sets is_final.
    """
    actions: List[str] = []
    target: Optional[str] = None
    final = False
    for st in body:
        if isinstance(st, Action):
            actions.append(reg.action(st.text, st.line))
        elif isinstance(st, CallStmt):
            actions.append(_call_action(st, ctx, para))
        elif isinstance(st, IoStmt):
            actions.append(_io_action(st, reg))
        elif isinstance(st, PerformStmt):
            if st.target and target is None:
                target = st.target
            break
        elif isinstance(st, GoToStmt):
            if st.depending:
                ctx.flag(para, st.line, "GO TO ... DEPENDING ON - computed multi-target; verify")
            if st.targets and target is None:
                target = st.targets[0]
            break
        elif isinstance(st, TerminateStmt):
            final = True
            break
        elif isinstance(st, ContinueStmt):
            if st.next_sentence:
                ctx.flag(para, st.line, "NEXT SENTENCE - differs from CONTINUE; verify control flow")
    return actions, target, final


def _io_action(st: IoStmt, reg: NameRegistry) -> str:
    """A combined action name for an I/O verb incl. any set-flag handler bodies."""
    base = f"{st.verb.lower()}_{st.file or 'file'}"
    suffix = ""
    if "AT_END" in st.handlers:
        suffix += "_atEnd"
    if "INVALID_KEY" in st.handlers:
        suffix += "_invalidKey"
    cobol = f"{st.verb} {st.file or ''}".strip()
    if st.handlers:
        cobol += " [" + ", ".join(sorted(st.handlers.keys())) + " handlers]"
    return reg.action_named(base + suffix, cobol, st.line)


def _io_guard(st: IoStmt, key: str, reg: NameRegistry) -> str:
    base = f"{st.file or 'file'}_{_IO_GUARD_KEY.get(key, key.lower())}"
    return reg.guard_named(base, f"{st.verb} {st.file or ''} {key.replace('_', ' ')}".strip(), st.line)


def _build_state(para: Paragraph, next_name: Optional[str], ctx: _BuildCtx) -> dict:
    reg = ctx.reg
    reg.state(para.name, f"paragraph {para.name}"
              + (f" (section {para.section})" if para.section else ""), para.line)

    entry: List[str] = []
    always: List[dict] = []
    final = False
    final_line: Optional[int] = None
    ends_unconditionally = False

    def edge(target: Optional[str], kind: str, line: int,
             guard: Optional[str] = None, actions: Optional[List[str]] = None,
             note: str = "") -> None:
        e: dict = {}
        if guard:
            e["guard"] = guard
        if actions:
            e["actions"] = actions
        if target:
            e["target"] = target
        meta = {"kind": kind, "cobolLine": line}
        if note:
            meta["note"] = note
        e["meta"] = meta
        always.append(e)

    for st in para.statements:
        if isinstance(st, Action):
            entry.append(reg.action(st.text, st.line))

        elif isinstance(st, CallStmt):
            entry.append(_call_action(st, ctx, para.name))

        elif isinstance(st, IoStmt):
            entry.append(_io_action(st, reg))
            for key, body in st.handlers.items():
                acts, tgt, fin = _summarize_transfer(body, reg, ctx, para.name)
                if tgt or fin or acts:
                    g = _io_guard(st, key, reg)
                    if fin:
                        edge(None, "io-handler", st.line, guard=g, actions=acts,
                             note=f"{key} handler reaches termination")
                        final = True
                        final_line = st.line
                    elif tgt:
                        edge(tgt, "io-handler", st.line, guard=g, actions=acts,
                             note=f"{key} handler transfers")
                    # acts-only handlers fold into the combined I/O action name above.

        elif isinstance(st, PerformStmt):
            if st.target:
                note = "PERFORM call-return - add explicit return edge"
                if st.kind in ("until", "varying", "times"):
                    g = reg.guard(st.control_text or f"{st.kind} clause", st.line)
                    note = (f"PERFORM {st.kind} ({st.control_text}); loop body, "
                            f"exits when guard '{g}' holds - add return edge")
                    edge(st.target, "perform-loop", st.line, guard=None,
                         note=note)
                else:
                    edge(st.target, "perform", st.line, note=note)
            elif st.inline_body:
                acts, tgt, fin = _summarize_transfer(st.inline_body, reg, ctx, para.name)
                entry.extend(acts)
                if st.control_text:
                    reg.guard(st.control_text, st.line)
                if tgt:
                    edge(tgt, "perform-inline", st.line, note="inline PERFORM body transfer")
                if fin:
                    final = True
                    final_line = st.line

        elif isinstance(st, GoToStmt):
            if para.name in ctx.alter_targets:
                # This paragraph's head GO TO is ALTER-switched: emit a context-driven
                # guard set over every candidate exit (the skill's encoding).
                slug_p = _slug(para.name)
                for t in ctx.alter_targets[para.name]:
                    g = reg.guard_named(f"alt_{slug_p}_is_{_slug(t)}",
                                        f"ALTER-switched exit of {para.name} -> {t} "
                                        f"(context.alt_{slug_p})", st.line)
                    edge(t, "alter-switch", st.line, guard=g)
                ctx.flag(para.name, st.line,
                         f"ALTER-switched exit: target of {para.name} is set at runtime; "
                         f"verify context.alt_{slug_p}")
                ends_unconditionally = True
            elif st.depending:
                ctx.flag(para.name, st.line, "GO TO ... DEPENDING ON - computed multi-target; verify")
                for idx, t in enumerate(st.targets, start=1):
                    g = reg.guard_named(f"depending_eq_{idx}",
                                        f"GO TO DEPENDING ON selects target {idx} ({t})", st.line)
                    edge(t, "goto-depending", st.line, guard=g)
                ends_unconditionally = True
            else:
                for t in st.targets:
                    edge(t, "goto", st.line, note="GO TO - no return")
                if st.targets:
                    ends_unconditionally = True

        elif isinstance(st, TerminateStmt):
            final = True
            final_line = st.line
            ends_unconditionally = True

        elif isinstance(st, IfStmt):
            _emit_selection(
                [(st.cond_text, st.then_body)], st.else_body, st.line,
                para, ctx, entry, edge)

        elif isinstance(st, EvaluateStmt):
            whens = [(f"{st.subject} = {c}" if st.subject and c else (c or st.subject), b)
                     for c, b in st.whens]
            _emit_selection(whens, st.other_body, st.line, para, ctx, entry, edge)

        elif isinstance(st, AlterStmt):
            # The ALTER itself is the switch-flip: a set-action on this state that
            # rewrites the altered paragraph's exit (which is drawn as a guard set).
            for altered, target in st.pairs:
                slug_p = _slug(altered)
                act = reg.action_named(
                    f"set_alt_{slug_p}_to_{_slug(target)}",
                    f"ALTER {altered} TO PROCEED TO {target}", st.line)
                entry.append(act)
                if altered not in ctx.alter_targets:
                    ctx.flag(para.name, st.line,
                             f"ALTER {altered} TO PROCEED TO {target} - altered paragraph "
                             f"has no head GO TO; non-idiomatic, switch not modeled")

        elif isinstance(st, ContinueStmt):
            if st.next_sentence:
                ctx.flag(para.name, st.line, "NEXT SENTENCE - differs from CONTINUE; verify control flow")

        elif isinstance(st, ExitStmt):
            if st.kind in ("PERFORM", "PERFORM_CYCLE"):
                ctx.flag(para.name, st.line, f"EXIT {st.kind.replace('_', ' ')} - inline-PERFORM break/continue")

    # Fall-through to the next paragraph unless this one ends with an unconditional
    # transfer or termination (category 8: falling off the end is easy to forget).
    if not ends_unconditionally and next_name is not None:
        edge(next_name, "fallthrough", para.line, note="fall-through to next paragraph")

    state: dict = {}
    if entry:
        state["entry"] = entry
    if always:
        state["always"] = always
    if final:
        if not always:
            state["type"] = "final"
        else:
            state.setdefault("meta", {})["reachesTermination"] = True
            state["meta"]["terminationLine"] = final_line
    if not state:
        state["meta"] = {"note": "no recovered control flow"}
    return state


def _emit_selection(branches: List[Tuple[str, List[Stmt]]],
                    other_body: Optional[List[Stmt]],
                    line: int, para: Paragraph, ctx: _BuildCtx,
                    entry: List[str], edge) -> None:
    """Emit guarded edges for IF/EVALUATE branches that transfer; fold pure-action
    branches into the state entry (conditional work has no separate state)."""
    reg = ctx.reg
    for cond, body in branches:
        acts, tgt, fin = _summarize_transfer(body, reg, ctx, para.name)
        g = reg.guard(cond, line) if cond.strip() else None
        if fin:
            edge(None, "select", line, guard=g, actions=acts, note="branch reaches termination")
        elif tgt:
            edge(tgt, "select", line, guard=g, actions=acts)
        else:
            # Pure-action branch: fold, but note the guard for traceability.
            if cond.strip():
                reg.guard(cond, line)
            entry.extend(acts)
    if other_body is not None:
        acts, tgt, fin = _summarize_transfer(other_body, reg, ctx, para.name)
        if fin:
            edge(None, "select-other", line, actions=acts, note="WHEN OTHER reaches termination")
        elif tgt:
            edge(tgt, "select-other", line, actions=acts, note="WHEN OTHER / ELSE")
        else:
            entry.extend(acts)


def _compute_alter_targets(program: Program, ctx: _BuildCtx) -> None:
    """Map each ALTER'd paragraph to its ordered candidate exit targets and seed the
    machine context with the initial (head GO TO) target."""
    by_name = {p.name: p for p in program.paragraphs}
    proceeds: Dict[str, List[str]] = {}
    for para in program.paragraphs:
        for st in walk_statements(para.statements):
            if isinstance(st, AlterStmt):
                for altered, target in st.pairs:
                    proceeds.setdefault(altered, [])
                    if target not in proceeds[altered]:
                        proceeds[altered].append(target)
    for altered, targets in proceeds.items():
        para = by_name.get(altered)
        orig = None
        if para is not None:
            for st in para.statements:
                if isinstance(st, GoToStmt) and st.targets:
                    orig = st.targets[0]
                    break
        if orig is None:
            # No head GO TO to switch - non-idiomatic; leave unmodeled (flagged at use).
            continue
        ordered = [orig] + [t for t in targets if t != orig]
        ctx.alter_targets[altered] = ordered
        ctx.context[f"alt_{_slug(altered)}"] = orig


def build_machine(program: Program, source_name: str = "<source>") -> Machine:
    ctx = _BuildCtx(reg=NameRegistry(), calls=analyze_calls(program))
    _compute_alter_targets(program, ctx)

    states: Dict[str, dict] = {}
    paras = program.paragraphs
    names = [p.name for p in paras]

    for idx, para in enumerate(paras):
        next_name = names[idx + 1] if idx + 1 < len(names) else None
        states[para.name] = _build_state(para, next_name, ctx)

    config: dict = {
        "id": program.program_id,
        "context": ctx.context,
        "states": states,
    }
    if names:
        config["initial"] = names[0]

    notes = list(program.notes)
    if not program.has_procedure_division:
        notes.append("No PROCEDURE DIVISION found - no control flow to recover.")
    notes.append(
        "Step semantics: one record cycle = one macrostep; flags set in one cycle are "
        "sensed next cycle (STATEMATE next-step sensing). See cobol-to-statecharts.md."
    )

    return Machine(
        config=config,
        provenance=ctx.reg.provenance_dict(),
        flags=ctx.flags,
        notes=notes,
        program_id=program.program_id,
        source_name=source_name,
    )
