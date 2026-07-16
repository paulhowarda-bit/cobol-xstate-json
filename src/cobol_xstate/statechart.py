"""Stage 4 - map the recovered control flow onto an XState v5 statechart.

The output is a **bare ``createMachine`` config as serializable data** (the kind
``JSON.parse`` could feed straight into XState v5): states, ``entry`` action-name
lists, and eventless ``always`` transitions, with guards and actions referenced **by
name as strings only**. No guard or action body is invented (references/
cobol-to-statecharts.md).

The goal is to capture **all** of the program's logic - which is why the target is a
Harel statechart (XState), not a UML-subset flattening. Each paragraph's *entire*
statement tree is compiled recursively:

* A run of genuinely straight-line statements folds into one state's ``entry`` action
  list (the reduction principle) - this is the *only* thing collapsed.
* Every **conditional** or **order-bearing** construct becomes real structure that
  preserves the logic: ``IF``/``EVALUATE`` become guarded ``always`` branches that
  converge on the continuation; ``PERFORM UNTIL/VARYING/TIMES`` and inline ``PERFORM``
  become loop states; ``READ ... AT END`` becomes a guarded handler branch (so the
  conditional flag-set is conditional, not unconditional); ``GO TO`` is an exit
  transition; terminators reach a shared ``final`` state. Nested branches nest.
* Guards and actions are still **names only** - no invented bodies; meaning lives in
  the provenance table.
* Constructs whose behavior rides on runtime data - resolved where provable (dynamic
  ``CALL`` via constant propagation, ``ALTER`` as a context-driven exit switch) and
  otherwise **flagged** (``GO TO ... DEPENDING ON``, ``NEXT SENTENCE``, ``DECLARATIVES``)
  - are drawn *and* flagged, never silently smoothed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .analysis import CallAnalysis, analyze_calls
from .interface import build_interface
from .semantics import parse_condition, parse_operation
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
from .naming import NameRegistry, _slug

_IO_GUARD_KEY = {
    "AT_END": "atEnd",
    "NOT_AT_END": "notAtEnd",
    "INVALID_KEY": "invalidKey",
    "NOT_INVALID_KEY": "notInvalidKey",
    "AT_EOP": "atEop",
    "NOT_AT_EOP": "notAtEop",
}

# ON-condition handler keys (CALL / arithmetic / ACCEPT / DISPLAY) -> guard stems.
_HANDLED_GUARD_KEY = {
    "ON_SIZE_ERROR": "sizeError",
    "NOT_ON_SIZE_ERROR": "notSizeError",
    "ON_EXCEPTION": "exception",
    "NOT_ON_EXCEPTION": "notException",
    "ON_OVERFLOW": "overflow",
    "NOT_ON_OVERFLOW": "notOverflow",
}


@dataclass
class Machine:
    config: dict
    provenance: Dict[str, dict]
    flags: List[dict]
    notes: List[str]
    program_id: str
    source_name: str = "<source>"
    data: Dict[str, dict] = field(default_factory=dict)
    semantics: Dict[str, dict] = field(default_factory=dict)
    paragraph_order: List[str] = field(default_factory=list)  # source order (for THRU)
    # section header -> [header, member paragraphs...] so PERFORM section-name owns
    # the section's whole extent (not just the header pseudo-paragraph).
    sections: Dict[str, List[str]] = field(default_factory=dict)
    using: List[str] = field(default_factory=list)       # PROCEDURE DIVISION USING params
    returning: Optional[str] = None                      # PROCEDURE DIVISION RETURNING
    # FILE-CONTROL SELECT entries (file -> assign/organization/statusField/...).
    files: Dict[str, dict] = field(default_factory=dict)

    def bundle(self) -> dict:
        from .harel import to_harel
        # Build the interface FIRST: it tags meta.perimeter/gets/creates onto the flat
        # IR's state nodes, and the Harel view is derived from that IR - so it has to
        # run before the restructuring copies the nodes, or the boundary tags are lost.
        iface = build_interface(
            self.config, self.semantics, self.provenance,
            data=self.data, using=self.using, returning=self.returning,
            files=self.files)
        config, charts = to_harel(self)
        return {
            "format": "xstate-v5-config",
            "metadata": {
                "program": self.program_id,
                "source": self.source_name,
                "generator": "cobol-xstate 0.1.0",
                "disclaimer": (
                    "A Harel-DERIVED statechart, encoded in XState v5 - which is a "
                    "restricted subset of Harel: negated events (NOT AT END), durative "
                    "activities, and static reactions are encoded as guards/flags rather "
                    "than expressed directly. It models the program's LOGIC, not a "
                    "transcript of its "
                    "text: paragraphs are compound (OR) states, PERFORM is resolved to a "
                    "real call/return ('invoke' of the callee's chart in 'charts', "
                    "returning on onDone), and source-order fall-through that the program "
                    "never executes is pruned. Every state keeps its COBOL name as its "
                    "'id' and traces to source via 'provenance' - nothing is invented. "
                    "The data logic travels too: 'data' is the typed dictionary "
                    "(PIC/USAGE/sign), 'semantics.actions' give each action's "
                    "target := expression, 'semantics.guards' each guard's Boolean tree. "
                    "COBOL arithmetic is fixed-point DECIMAL: a rewrite must honor the "
                    "data types, not use binary float. Items under 'flags' need "
                    "verification against source."
                ),
            },
            "machine": config,
            # Each performed paragraph as its own chart - the statechart model of a
            # subroutine (a classical Harel chart has no call stack; a child chart that
            # runs to completion and returns is the faithful equivalent).
            "charts": charts,
            "data": self.data,
            "semantics": {"actions": self.semantics.get("actions", {}),
                          "guards": self.semantics.get("guards", {})},
            "interface": iface,
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
    data: Dict[str, object] = field(default_factory=dict)   # name -> DataItem
    states: Dict[str, dict] = field(default_factory=dict)
    counter: int = 0
    # altered-paragraph -> ordered candidate exit targets (orig GO TO + PROCEED-TOs)
    alter_targets: Dict[str, List[str]] = field(default_factory=dict)
    context: Dict[str, object] = field(default_factory=dict)
    action_sem: Dict[str, dict] = field(default_factory=dict)  # action name -> operation
    guard_sem: Dict[str, dict] = field(default_factory=dict)   # guard name  -> condition tree
    flags: List[dict] = field(default_factory=list)
    _seen_flags: set = field(default_factory=set)
    # Synthetic data items the compiler introduces (PERFORM n TIMES loop counters):
    # merged into the data dictionary so the emitter types and seeds them.
    synthetic_data: Dict[str, dict] = field(default_factory=dict)

    def new_times_counter(self, line: int) -> str:
        n = sum(1 for k in self.synthetic_data if k.startswith("TIMES-CTR-")) + 1
        name = f"TIMES-CTR-{n}"
        self.synthetic_data[name] = {
            "level": 77, "line": line, "section": "SYNTHETIC",
            "type": {"category": "numeric-display", "digits": 9, "scale": 0,
                     "signed": False, "pic": "9(9)"},
            "note": "synthetic loop counter introduced for PERFORM n TIMES",
        }
        self.context[name] = 0
        return name

    def flag(self, para: str, line: int, message: str) -> None:
        key = (para, message)
        if key in self._seen_flags:
            return
        self._seen_flags.add(key)
        self.flags.append({"paragraph": para, "line": line, "message": message})

    def record_action(self, name: str, text: str, line: int, para: str) -> None:
        if name in self.action_sem:
            return
        spec = parse_operation(text, self.data)
        if spec is None:
            return
        self.action_sem[name] = spec
        # A reference-modified store (MOVE x TO F(a:b)) is a substring write the data
        # model cannot express; the runnable machine fails loudly instead of writing a
        # phantom key - surface it here so a reviewer sees it without running.
        for a in spec.get("assignments", []):
            if ":" in a.get("target", ""):
                self.flag(para, line,
                          f"{spec.get('verb', '?')} writes reference-modified target "
                          f"{a['target']} - substring store is not modeled (routed to "
                          f"notModeled in the runnable machine); verify")
        # The global decimal-arithmetic caveat is a note; only flag genuine per-site
        # concerns: arithmetic writing a known non-numeric item, or an ON SIZE ERROR
        # overflow path the rewrite must replicate.
        if spec.get("kind") in ("arith", "compute"):
            for a in spec.get("assignments", []):
                di = self.data.get(a["target"].upper())
                cat = getattr(getattr(di, "type", None), "category", "") if di else ""
                if di is not None and not cat.startswith("numeric"):
                    self.flag(para, line,
                              f"{spec['verb']} writes non-numeric {a['target']} "
                              f"({cat or 'unknown'}) - verify type (S0C7 risk)")
            if spec.get("onSizeError"):
                self.flag(para, line,
                          f"{spec['verb']} ON SIZE ERROR - the overflow path must be "
                          f"replicated in the rewrite")

    def record_guard(self, name: str, cond_text: str, para: str = "", line: int = 0) -> None:
        if name in self.guard_sem or not cond_text.strip():
            return
        tree = parse_condition(cond_text, self.data)
        self.guard_sem[name] = tree
        # Iron rule for scale: a condition we could not model into a Boolean tree must be
        # surfaced as a flag, never left only in 'semantics' where a reviewer scanning
        # 'flags' would miss it.
        if tree.get("op") == "raw":
            self.flag(para or "?", line,
                      f"condition not fully modeled (left as raw): {cond_text.strip()} "
                      f"- routed to an external guard; verify")


def _call_args_suffix(st: CallStmt) -> str:
    """The `` USING a b RETURNING r`` tail for a CALL's provenance label, so the external
    interface can surface the arguments passed across the boundary."""
    parts = []
    if st.using:
        parts.append("USING " + " ".join(st.using))
    if st.returning:
        parts.append("RETURNING " + st.returning)
    return (" " + " ".join(parts)) if parts else ""


def _call_action(st: CallStmt, ctx: _BuildCtx, para: str) -> str:
    """Action name for a CALL, resolving a dynamic target by constant propagation
    where the program proves it constant (else flag, don't guess)."""
    reg = ctx.reg
    args = _call_args_suffix(st)
    if not st.dynamic:
        return reg.action_named("call_" + st.target, f"CALL '{st.target}'{args}", st.line)
    res = ctx.calls.resolve(st.target)
    if res.confident and res.resolved:
        return reg.action_named(
            "call_" + res.resolved,
            f"CALL {st.target} -> resolved '{res.resolved}' ({res.reason}){args}", st.line)
    # Unresolved or ambiguous: keep the identifier name and flag it.
    ctx.flag(para, st.line, f"dynamic CALL {st.target} - {res.reason}")
    return reg.action_named("call_" + st.target, f"CALL (dynamic) {st.target}{args}", st.line)


def _io_action(st: IoStmt, reg: NameRegistry) -> str:
    base = f"{st.verb.lower()}_{st.file or 'file'}"
    label = f"{st.verb} {st.file or ''}".strip()
    if st.into:
        label += f" INTO {st.into}"
    if st.from_:
        label += f" FROM {st.from_}"
    return reg.action_named(base, label, st.line)


def _io_guard(st: IoStmt, key: str, reg: NameRegistry) -> str:
    base = f"{st.file or 'file'}_{_IO_GUARD_KEY.get(key, key.lower())}"
    return reg.guard_named(base, f"{st.verb} {st.file or ''} {key.replace('_', ' ')}".strip(), st.line)


# Terminal-transfer node into the shared final state.
_END = "__END__"
_DECL_END = "__DECL_END__"


class _ParaCompiler:
    """Compile one paragraph's full statement tree into a hierarchy-free set of XState
    states. Every branch / loop / handler becomes real guarded structure - the only
    thing collapsed is a run of genuinely straight-line statements (the skill's
    reduction principle), which fold into one state's ``entry`` action list. Nothing
    conditional or order-bearing is folded away, so the whole program logic survives.
    """

    def __init__(self, ctx: _BuildCtx, para: Paragraph):
        self.ctx = ctx
        self.reg = ctx.reg
        self.para = para
        self.pname = para.name
        # The paragraph's own continuation (where EXIT PARAGRAPH lands) and the first
        # state after the enclosing section (where EXIT SECTION lands); set by compile().
        self.cont: str = _END
        self.section_exit: str = _END
        # Enclosing inline-loop targets: (break_target, cycle_target) per nesting level,
        # for EXIT PERFORM / EXIT PERFORM CYCLE.
        self._loops: List[tuple] = []

    # -- emit / name -------------------------------------------------------
    def _fresh(self, hint: str) -> str:
        self.ctx.counter += 1
        return f"{self.pname}__{hint}{self.ctx.counter}"

    def _emit(self, name: str, state: dict) -> str:
        if name not in self.reg.entries:
            self.reg.state(name, f"structural state in {self.pname}", self.para.line)
        self.ctx.states[name] = state
        return name

    @staticmethod
    def _edge(target: str, kind: str, line: int, guard: Optional[str] = None,
              note: str = "") -> dict:
        e: dict = {}
        if guard:
            e["guard"] = guard
        e["target"] = target
        meta = {"kind": kind, "cobolLine": line}
        if note:
            meta["note"] = note
        e["meta"] = meta
        return e

    # -- entry point -------------------------------------------------------
    def compile(self, cont: str, section_exit: Optional[str] = None) -> str:
        self.cont = cont
        self.section_exit = section_exit if section_exit is not None else cont
        return self.compile_block(self.para.statements, cont, root=self.pname)

    # -- a sequence of statements -----------------------------------------
    def compile_block(self, stmts: List[Stmt], cont: str, root: Optional[str] = None) -> str:
        if not stmts:
            if root is not None:
                return self._emit(root, {"always": [self._edge(
                    cont, "fallthrough", self.para.line, note="fall-through")]})
            return cont
        # Peel a leading run of straight-line statements into one state's entry list.
        run: List[Stmt] = []
        idx = 0
        while idx < len(stmts) and self._is_straightline(stmts[idx]):
            run.append(stmts[idx])
            idx += 1
        if run:
            rest = self.compile_block(stmts[idx:], cont)
            name = root if root is not None else self._fresh("seq")
            actions: List[str] = []
            for s in run:
                actions.extend(self._straight_actions(s))
            state: dict = {}
            if actions:
                state["entry"] = actions
            state["always"] = [self._edge(rest, "seq", run[0].line, note="continue")]
            return self._emit(name, state)
        first = stmts[0]
        rest = self.compile_block(stmts[1:], cont)
        return self.compile_control(first, rest, root)

    def _is_straightline(self, st: Stmt) -> bool:
        if isinstance(st, CallStmt):
            return not st.handlers
        if isinstance(st, (Action, AlterStmt)):
            return True
        if isinstance(st, HandledStmt):
            return False
        if isinstance(st, PerformStmt):
            return st.kind == "call" and st.target is not None  # simple PERFORM p [THRU q]
        if isinstance(st, IoStmt):
            return not st.handlers
        if isinstance(st, ExecStmt):
            return st.kind in ("effect", "call", "handle", "input")
        if isinstance(st, ContinueStmt):
            return not st.next_sentence
        if isinstance(st, ExitStmt):
            return st.kind == "PLAIN"
        return False

    def _straight_actions(self, st: Stmt) -> List[str]:
        reg = self.reg
        if isinstance(st, Action):
            name = reg.action(st.text, st.line)
            self.ctx.record_action(name, st.text, st.line, self.pname)
            # STRING/UNSTRING are consumed opaquely up to their END- terminator; an inner
            # ON OVERFLOW is a conditional branch we fold away - flag it so it is not
            # silently lost (SEARCH is modeled as real structure, see compile_search).
            if st.verb in ("STRING", "UNSTRING") and re.search(r"\bOVERFLOW\b", st.text, re.I):
                self.ctx.flag(self.pname, st.line,
                              f"{st.verb} ON OVERFLOW handler is folded into the opaque "
                              f"action; its conditional branch is not modeled - verify")
            # STRING/UNSTRING/INSPECT transform data (receivers, TALLYING counters) but
            # are modeled as opaque effects: their receivers stay UNCHANGED in the model.
            if st.verb in ("STRING", "UNSTRING", "INSPECT"):
                self.ctx.flag(self.pname, st.line,
                              f"{st.verb} is an opaque effect: its receiver/tally data "
                              f"changes are NOT modeled (receivers unchanged in the "
                              f"contract) - verify")
            return [name]
        if isinstance(st, CallStmt):
            return [_call_action(st, self.ctx, self.pname)]
        if isinstance(st, PerformStmt):
            return [self._perform_action(st)]
        if isinstance(st, IoStmt):
            name = _io_action(st, reg)
            self._io_sem(st, name)
            return [name]
        if isinstance(st, ExecStmt):
            return [self._exec_action(st)]
        if isinstance(st, AlterStmt):
            names = []
            for altered, target in st.pairs:
                name = reg.action_named(
                    f"set_alt_{_slug(altered)}_to_{_slug(target)}",
                    f"ALTER {altered} TO PROCEED TO {target}", st.line)
                names.append(name)
                if altered not in self.ctx.alter_targets:
                    self.ctx.flag(self.pname, st.line,
                                  f"ALTER {altered} TO PROCEED TO {target} - altered "
                                  f"paragraph has no head GO TO; switch not modeled")
                else:
                    # The switch is a real assignment to the synthetic ALT- field, so
                    # the runnable machine actually flips the exit at run time.
                    self.ctx.action_sem.setdefault(name, {
                        "verb": "ALTER", "kind": "assign",
                        "assignments": [{"target": f"ALT-{_slug(altered)}",
                                         "expr": f"'{target}'"}],
                        "raw": f"ALTER {altered} TO PROCEED TO {target}",
                    })
            return names
        return []  # ContinueStmt / ExitStmt PLAIN: no-op

    def _io_sem(self, st: IoStmt, name: str) -> None:
        """Record the I/O statement's data endpoints (file, INTO/FROM areas) so the
        external-interface overlay can surface the fields crossing the boundary."""
        spec = {"verb": st.verb, "kind": "io", "file": st.file}
        if st.into:
            spec["into"] = st.into
        if st.from_:
            spec["from"] = st.from_
        self.ctx.action_sem.setdefault(name, spec)

    def _perform_action(self, st: PerformStmt) -> str:
        thru = f" THRU {st.thru}" if st.thru else ""
        ctrl = f" {st.control_text}" if st.control_text else ""
        cobol = f"PERFORM {st.target}{thru}{ctrl}".strip()
        if st.thru:
            # Encode both ends so the emitter builds a range actor; register() preserves the
            # `__THRU__` separator (action_named would slug `__` down to `_`).
            return self.reg.register(
                "action", f"perform_{st.target}__THRU__{st.thru}", cobol, st.line)
        return self.reg.action_named(f"perform_{st.target}", cobol, st.line)

    def _exec_action(self, st: ExecStmt) -> str:
        base = f"link_{st.target}" if st.kind == "call" and st.target else \
            f"exec_{st.lang.lower()}_{st.verb.lower()}"
        name = self.reg.action_named(base, f"EXEC {st.lang} {st.text} END-EXEC", st.line)
        if st.kind == "input" and st.into_vars:
            # SELECT/FETCH ... INTO: the DB row populates the host variables - a real
            # (external-sourced) assignment to each, not an opaque effect.
            self.ctx.action_sem[name] = {
                "verb": st.verb, "kind": "input",
                "assignments": [{"target": v, "expr": "<external: SQL row>"}
                                for v in st.into_vars],
                "raw": f"EXEC {st.lang} {st.text} END-EXEC",
            }
        else:
            self.ctx.action_sem.setdefault(name, {
                "verb": st.verb, "kind": f"exec-{st.lang.lower()}",
                "hostVars": st.host_vars, "raw": f"EXEC {st.lang} {st.text} END-EXEC",
            })
        if st.kind == "handle":
            self.ctx.flag(self.pname, st.line,
                          f"EXEC {st.lang} {st.verb} registers implicit handler(s) "
                          f"{st.conditions or ''} - later transfer is invisible at this "
                          f"site; model as an orthogonal handler region and verify")
        return name

    # -- control constructs ------------------------------------------------
    def compile_control(self, st: Stmt, after: str, root: Optional[str]) -> str:
        if isinstance(st, IfStmt):
            return self.compile_if(st, after, root)
        if isinstance(st, EvaluateStmt):
            return self.compile_eval(st, after, root)
        if isinstance(st, PerformStmt):
            if st.kind == "inline":  # PERFORM ... END-PERFORM with no loop control
                return self.compile_block(st.inline_body, after, root=root)
            return self.compile_loop(st, after, root)
        if isinstance(st, GoToStmt):
            return self.compile_goto(st, after, root)
        if isinstance(st, TerminateStmt):
            name = root or self._fresh("end")
            self.reg.state(name, f"{st.kind} (terminator)", st.line)
            return self._emit(name, {"type": "final", "meta": {"kind": st.kind, "cobolLine": st.line}})
        if isinstance(st, SortStmt):
            return self.compile_sort(st, after, root)
        if isinstance(st, SearchStmt):
            return self.compile_search(st, after, root)
        if isinstance(st, IoStmt):
            return self.compile_io(st, after, root)
        if isinstance(st, HandledStmt):
            return self.compile_handled(st, st.inner, st.handlers, after, root)
        if isinstance(st, CallStmt):       # CALL with ON EXCEPTION/OVERFLOW handlers
            return self.compile_handled(st, st, st.handlers, after, root)
        if isinstance(st, ExecStmt):       # terminate (RETURN/ABEND) or transfer (XCTL)
            name = root or self._fresh("exec")
            self.reg.state(name, f"EXEC {st.lang} {st.verb} ({st.kind})", st.line)
            if st.kind == "transfer":
                self.ctx.flag(self.pname, st.line,
                              f"EXEC {st.lang} XCTL to {st.target or '?'} - control "
                              f"transfers out with no return")
            meta = {"kind": f"cics-{st.verb.lower()}", "cobolLine": st.line}
            if st.target:
                meta["target"] = st.target
            # The terminating command itself is a boundary crossing (CICS RETURN sends
            # the COMMAREA/TRANSID back to the caller; XCTL passes it on; ABEND raises
            # a condition) - carry it as the final state's entry action so the external
            # interface sees it. It was invisible here before.
            eff = self._exec_action(st)
            return self._emit(name, {"type": "final", "entry": [eff], "meta": meta})
        if isinstance(st, ContinueStmt):  # NEXT SENTENCE (plain CONTINUE is straight-line)
            self.ctx.flag(self.pname, st.line, "NEXT SENTENCE - differs from CONTINUE; verify control flow")
            name = root or self._fresh("next")
            return self._emit(name, {"always": [self._edge(after, "next-sentence", st.line)]})
        if isinstance(st, ExitStmt):
            name = root or self._fresh("exit")
            if st.kind == "PARAGRAPH":
                return self._emit(name, {"always": [self._edge(
                    self.cont, "exit-paragraph", st.line,
                    note="EXIT PARAGRAPH - skips the rest of the paragraph")]})
            if st.kind == "SECTION":
                return self._emit(name, {"always": [self._edge(
                    self.section_exit, "exit-section", st.line,
                    note="EXIT SECTION - skips the rest of the section")]})
            if st.kind in ("PERFORM", "PERFORM_CYCLE"):
                if self._loops:
                    brk, cyc = self._loops[-1]
                    tgt = cyc if st.kind == "PERFORM_CYCLE" else brk
                    return self._emit(name, {"always": [self._edge(
                        tgt, "exit-perform", st.line,
                        note=("EXIT PERFORM CYCLE - next iteration"
                              if st.kind == "PERFORM_CYCLE"
                              else "EXIT PERFORM - loop break"))]})
                self.ctx.flag(self.pname, st.line,
                              f"EXIT {st.kind.replace('_', ' ')} outside an inline "
                              f"PERFORM - modeled as fall-through; verify")
            return self._emit(name, {"always": [self._edge(after, "exit", st.line)]})
        name = root or self._fresh("stmt")
        return self._emit(name, {"always": [self._edge(after, "seq", getattr(st, "line", 0))]})

    def compile_if(self, st: IfStmt, after: str, root: Optional[str]) -> str:
        name = root or self._fresh("if")
        g = self.reg.guard(st.cond_text, st.line) if st.cond_text.strip() else None
        if g:
            self.ctx.record_guard(g, st.cond_text, self.pname, st.line)
        then_e = self.compile_block(st.then_body, after)
        else_e = self.compile_block(st.else_body, after) if st.else_body else after
        edges = [self._edge(then_e, "if-then", st.line, guard=g),
                 self._edge(else_e, "if-else", st.line)]
        return self._emit(name, {"always": edges})

    def compile_eval(self, st: EvaluateStmt, after: str, root: Optional[str]) -> str:
        name = root or self._fresh("eval")
        edges = []
        for cond, body in st.whens:
            full = _evaluate_when_condition(st.subject, cond)
            g = self.reg.guard(full, st.line) if full.strip() else None
            if g:
                self.ctx.record_guard(g, full, self.pname, st.line)
            tgt = self.compile_block(body, after) if body else after
            # A WHEN whose every object is ANY (no condition text) always matches.
            edges.append(self._edge(tgt, "when", st.line, guard=g))
        if st.other_body is not None:
            edges.append(self._edge(self.compile_block(st.other_body, after),
                                    "when-other", st.line))
        else:
            edges.append(self._edge(after, "when-fallthrough", st.line,
                                    note="no WHEN OTHER; falls through"))
        return self._emit(name, {"always": edges})

    def compile_loop(self, st: PerformStmt, after: str, root: Optional[str]) -> str:
        varying = _parse_varying(st.control_text) if st.kind == "varying" else []
        until = _until_text(st.control_text)
        if st.kind == "times" and not varying:
            # PERFORM n TIMES: the count is statically known - model it as a synthetic
            # counter stepped like a VARYING index (init 0, +1 each iteration, exit at n).
            m = re.match(r"\s*(\S+?)\s+TIMES\b", st.control_text or "", re.I)
            if m:
                ctr = self.ctx.new_times_counter(st.line)
                varying = [(ctr, "0", "1")]
                until = f"{ctr} >= {m.group(1)}"
        # Name the guard from the full control clause (stable name); model its semantics
        # from the bounded UNTIL condition (so a VARYING ... AFTER doesn't over-capture).
        g = self.reg.guard(st.control_text or f"{st.kind} clause", st.line)
        if until:
            self.ctx.record_guard(g, until, self.pname, st.line)

        if varying:
            # PERFORM VARYING is test-before: init the control variable, test, run body,
            # step (var := var + by), retest. The init state is the loop's entry (so
            # inbound transitions land on it); head/body/step are fresh.
            if st.test_after:
                self.ctx.flag(self.pname, st.line,
                              "PERFORM VARYING WITH TEST AFTER - modeled as test-before "
                              "init/step; verify the first-iteration semantics")
            if len(varying) > 1:
                inner = ", ".join(v[0] for v in varying[1:])
                self.ctx.flag(self.pname, st.line,
                              f"PERFORM VARYING ... AFTER ({inner}): only the primary index "
                              f"{varying[0][0]} is stepped; nested index iteration is not "
                              f"modeled - verify the inner loops")
            head = self._fresh("loop")
            var, frm, by = varying[0]
            step = self.reg.action(f"ADD {by} TO {var}", st.line)
            self.ctx.record_action(step, f"ADD {by} TO {var}", st.line, self.pname)
            loop_back = self._emit(self._fresh("vstep"),
                                   {"entry": [step],
                                    "always": [self._edge(head, "loop-step", st.line)]})
            if st.inline_body:
                self._loops.append((after, loop_back))
                body_entry = self.compile_block(st.inline_body, loop_back)
                self._loops.pop()
            else:
                state = {"always": [self._edge(loop_back, "loop-iter", st.line)]}
                if st.target:
                    state["entry"] = [self._perform_action(st)]
                body_entry = self._emit(self._fresh("iter"), state)
            self._emit(head, {"always": [
                self._edge(after, "loop-exit", st.line, guard=g),
                self._edge(body_entry, "loop-body", st.line),
            ]})
            init = self.reg.action(f"MOVE {frm} TO {var}", st.line)
            self.ctx.record_action(init, f"MOVE {frm} TO {var}", st.line, self.pname)
            return self._emit(root or self._fresh("vinit"),
                              {"entry": [init],
                               "always": [self._edge(head, "loop-init", st.line)]})

        if st.test_after:                      # do-while: run body, then test
            head = self._fresh("loop")
            body_root = root
        else:                                  # while: test, then body (default)
            head = root or self._fresh("loop")
            body_root = None
        if st.inline_body:
            self._loops.append((after, head))
            body_entry = self.compile_block(st.inline_body, head, root=body_root)
            self._loops.pop()
        else:
            nm = body_root or self._fresh("iter")
            state = {"always": [self._edge(head, "loop-iter", st.line)]}
            if st.target:
                state["entry"] = [self._perform_action(st)]
            body_entry = self._emit(nm, state)
        self._emit(head, {"always": [
            self._edge(after, "loop-exit", st.line, guard=g),
            self._edge(body_entry, "loop-body", st.line),
        ]})
        return body_entry if st.test_after else head

    def compile_goto(self, st: GoToStmt, after: str, root: Optional[str]) -> str:
        reg = self.reg
        name = root or self._fresh("goto")
        if self.pname in self.ctx.alter_targets:
            slug_p = _slug(self.pname)
            key = f"ALT-{slug_p}"
            edges = []
            for t in self.ctx.alter_targets[self.pname]:
                gg = reg.guard_named(f"alt_{slug_p}_is_{_slug(t)}",
                                     f"ALTER-switched exit of {self.pname} -> {t} "
                                     f"(context.{key})", st.line)
                # A real, evaluable guard over the synthetic switch field.
                self.ctx.record_guard(gg, f"{key} = '{t}'", self.pname, st.line)
                edges.append(self._edge(t, "alter-switch", st.line, guard=gg))
            self.ctx.flag(self.pname, st.line,
                          f"ALTER-switched exit: target of {self.pname} is set at runtime "
                          f"(modeled as guards over context.{key}); verify")
            return self._emit(name, {"always": edges})
        if st.depending:
            self.ctx.flag(self.pname, st.line,
                          "GO TO ... DEPENDING ON - computed multi-target"
                          + ("" if st.depending_on else " (index variable unknown)")
                          + "; verify")
            edges = []
            for idx, t in enumerate(st.targets, start=1):
                gg = reg.guard_named(f"depending_eq_{idx}",
                                     f"GO TO DEPENDING ON selects target {idx} ({t})", st.line)
                if st.depending_on:
                    # DEPENDING ON var: target i is taken exactly when var = i.
                    self.ctx.record_guard(gg, f"{st.depending_on} = {idx}",
                                          self.pname, st.line)
                edges.append(self._edge(t, "goto-depending", st.line, guard=gg))
            edges.append(self._edge(after, "goto-depending-oob", st.line,
                                    note="index out of range falls through"))
            return self._emit(name, {"always": edges})
        edges = [self._edge(t, "goto", st.line, note="GO TO - no return") for t in st.targets]
        if not edges:
            edges = [self._edge(after, "goto-empty", st.line)]
        return self._emit(name, {"always": edges})

    def compile_sort(self, st: SortStmt, after: str, root: Optional[str]) -> str:
        """SORT/MERGE as the control flow the compiler inserts: perform the INPUT
        PROCEDURE (call-return), run the sort (an opaque effect), then perform the OUTPUT
        PROCEDURE. USING/GIVING move records via compiler-managed file I/O (an effect)."""
        name = root or self._fresh("sort")
        entry: List[str] = []

        def perform_proc(target: str, thru: Optional[str], phase: str) -> None:
            thru_s = f" THRU {thru}" if thru else ""
            cobol = f"{st.verb} {phase} PROCEDURE {target}{thru_s}"
            if thru:
                entry.append(self.reg.register(
                    "action", f"perform_{target}__THRU__{thru}", cobol, st.line))
            else:
                entry.append(self.reg.action_named(f"perform_{target}", cobol, st.line))

        if st.input_proc:
            perform_proc(st.input_proc, st.input_thru, "INPUT")

        sort_eff = self.reg.action_named(
            f"{st.verb.lower()}_{_slug(st.file or 'FILE')}",
            st.raw or f"{st.verb} {st.file or ''}".strip(), st.line)
        entry.append(sort_eff)
        self.ctx.flag(self.pname, st.line,
                      f"{st.verb} on {st.file or '?'} is an opaque effect; record ordering "
                      f"(ASCENDING/DESCENDING KEY) is not modeled")

        if st.output_proc:
            perform_proc(st.output_proc, st.output_thru, "OUTPUT")
        if st.using:
            self.ctx.flag(self.pname, st.line,
                          f"{st.verb} USING {', '.join(st.using)}: records are read by the "
                          f"sort automatically; that file data flow is not modeled")
        if st.giving:
            self.ctx.flag(self.pname, st.line,
                          f"{st.verb} GIVING {', '.join(st.giving)}: sorted records are "
                          f"written automatically; that file data flow is not modeled")

        return self._emit(name, {"entry": entry,
                                 "always": [self._edge(after, "sort", st.line)]})

    def compile_search(self, st: SearchStmt, after: str, root: Optional[str]) -> str:
        """SEARCH / SEARCH ALL as real guarded structure: each WHEN is a guarded branch to
        its body, AT END is a guarded branch (table-exhausted is a runtime condition routed
        to an external guard), then fall-through. The serial index iteration itself is an
        opaque effect - flagged, not faked as a loop."""
        name = root or self._fresh("search")
        verb = "SEARCH ALL" if st.all else "SEARCH"
        effect = self.reg.action_named(
            f"search_{_slug(st.table)}", f"{verb} {st.table}", st.line)
        edges = []
        for cond, body in st.whens:
            g = self.reg.guard(cond, st.line) if cond.strip() else None
            if g:
                self.ctx.record_guard(g, cond, self.pname, st.line)
            tgt = self.compile_block(body, after) if body else after
            edges.append(self._edge(tgt, "search-when", st.line, guard=g))
        if st.at_end_body:
            gend = self.reg.guard_named(
                f"{_slug(st.table)}_searchAtEnd",
                f"{verb} {st.table} exhausted (AT END) - runtime", st.line)
            tgt = self.compile_block(st.at_end_body, after)
            edges.append(self._edge(tgt, "search-at-end", st.line, guard=gend))
        edges.append(self._edge(after, "search-continue", st.line,
                                note="no WHEN matched / fell through"))
        self.ctx.flag(self.pname, st.line,
                      f"{verb} on {st.table}: WHEN/AT END branches are modeled as guarded "
                      f"edges, but the index iteration (advance until match) is an opaque "
                      f"effect - verify the loop/index behavior")
        return self._emit(name, {"entry": [effect], "always": edges})

    def compile_handled(self, st: Stmt, inner: Stmt, handlers: Dict[str, List[Stmt]],
                        after: str, root: Optional[str]) -> str:
        """An imperative with [NOT] ON SIZE ERROR / EXCEPTION / OVERFLOW handlers:
        the action runs on entry, then each handler body is a guarded branch. The
        triggering condition is a runtime event (external guard) - flagged, never
        hoisted into the unconditional flow and never invented."""
        name = root or self._fresh("stmt")
        entry = self._straight_actions(inner)
        base = _slug(entry[0]) if entry else name
        edges = []
        for key, body in handlers.items():
            stem = _HANDLED_GUARD_KEY.get(key, key.lower())
            g = self.reg.guard_named(
                f"{base}_{stem}", f"{key.replace('_', ' ')} raised at runtime "
                f"by {base} - runtime condition", st.line)
            tgt = self.compile_block(body, after) if body else after
            edges.append(self._edge(tgt, "on-condition", st.line, guard=g, note=key))
        edges.append(self._edge(after, "on-continue", st.line,
                                note="normal (condition not raised)"))
        keys = ", ".join(k.replace("_", " ") for k in handlers)
        self.ctx.flag(self.pname, st.line,
                      f"{keys} handler(s) modeled as guarded branch(es); the trigger "
                      f"is a runtime condition (external guard) - verify")
        state: dict = {"always": edges}
        if entry:
            state["entry"] = entry
        return self._emit(name, state)

    def compile_io(self, st: IoStmt, after: str, root: Optional[str]) -> str:
        name = root or self._fresh("io")
        edges = []
        for key, body in st.handlers.items():
            g = _io_guard(st, key, self.reg)
            tgt = self.compile_block(body, after) if body else after
            edges.append(self._edge(tgt, "io-handler", st.line, guard=g, note=key))
        edges.append(self._edge(after, "io-continue", st.line, note="normal (no condition)"))
        io_name = _io_action(st, self.reg)
        self._io_sem(st, io_name)
        return self._emit(name, {"entry": [io_name], "always": edges})


_VARYING_RE = re.compile(
    r"\b(?:VARYING|AFTER)\s+([A-Z0-9][A-Z0-9-]*)\s+FROM\s+(\S+)\s+BY\s+(\S+)", re.I)


def _parse_varying(control: str):
    """Extract (var, from, by) for the primary VARYING and each AFTER clause."""
    return [(v.upper(), frm, by) for v, frm, by in _VARYING_RE.findall(control or "")]


def _until_text(control: str) -> str:
    """The primary UNTIL condition, bounded before any AFTER clause."""
    m = re.search(r"\bUNTIL\b\s+(.+?)(?:\s+AFTER\b|$)", control or "", re.I)
    return m.group(1).strip() if m else ""


def _redefines_compatible(item, target) -> bool:
    """True when a REDEFINES item has the same category/size as what it redefines, so it is
    a value alias rather than a genuine byte reinterpretation across different PICTUREs."""
    if target is None:
        return False
    a, b = getattr(item, "type", None), getattr(target, "type", None)
    if a is None or b is None:
        return getattr(item, "is_group", False) and getattr(target, "is_group", False)
    if a.category != b.category:
        return False
    if a.category.startswith("numeric"):
        return (a.digits, a.scale, a.usage) == (b.digits, b.scale, b.usage)
    return a.pic == b.pic  # alphanumeric/alphabetic: same picture => same length


def _evaluate_when_condition(subject: str, when: str) -> str:
    """Build the Boolean condition text for one EVALUATE WHEN, honoring multiple operands
    (``EVALUATE a ALSO b ... WHEN x ALSO y`` -> ``a = x AND b = y``), ``EVALUATE TRUE``
    (the objects are conditions), ``THRU`` ranges, abbreviated relations (``WHEN > 5``),
    and ``ANY`` (matches anything -> dropped from the conjunction)."""
    subjects = [s.strip() for s in re.split(r"\bALSO\b", subject or "", flags=re.I)]
    objects = [o.strip() for o in re.split(r"\bALSO\b", when or "", flags=re.I)]
    pieces: List[str] = []
    for idx, obj in enumerate(objects):
        subj = subjects[idx] if idx < len(subjects) else (subjects[-1] if subjects else "")
        piece = _evaluate_pair(subj, obj)
        if piece:
            pieces.append(piece)
    if not pieces:
        return ""  # all ANY (or empty) -> an unconditional WHEN
    return " AND ".join(f"({p})" for p in pieces) if len(pieces) > 1 else pieces[0]


def _evaluate_pair(subj: str, obj: str) -> str:
    obj = obj.strip()
    up = obj.upper()
    su = subj.strip().upper()
    if not obj or up == "ANY":
        return ""
    if su == "TRUE":     # EVALUATE TRUE: each object is itself a condition
        return obj
    if su == "FALSE":    # EVALUATE FALSE: the object is a condition that must be false
        return f"NOT ( {obj} )"
    m = re.match(r"(.+?)\s+(?:THRU|THROUGH)\s+(.+)$", obj, re.I)
    if m:               # WHEN lo THRU hi -> subj >= lo AND subj <= hi
        return f"{subj} >= {m.group(1).strip()} AND {subj} <= {m.group(2).strip()}"
    if re.match(r"^(NOT\s+)?(>=|<=|<>|[<>=])", up) or \
            re.match(r"^(NOT\s+)?(GREATER|LESS|EQUAL|EQUALS|EQ|GT|LT|GE|LE|NE)\b", up):
        return f"{subj} {obj}"   # abbreviated relation: WHEN > 5  ->  subj > 5
    return f"{subj} = {obj}"


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
        # The switch variable is a real (synthetic, typed) context field, so the
        # alter-exit guards and set-actions are executable data, not external stubs.
        key = f"ALT-{_slug(altered)}"
        ctx.context[key] = orig
        # No PIC on purpose: the value is a paragraph-name token, compared and stored
        # unpadded (a PIC X(n) would space-pad it on every store).
        ctx.synthetic_data[key] = {
            "level": 77, "line": 0, "section": "SYNTHETIC",
            "type": {"category": "alphanumeric"},
            "note": f"ALTER switch: the active exit target of {altered}",
        }


def _initial_value(item) -> object:
    """The start-of-run value of an elementary item: its VALUE clause, else a typed
    default (0 for numeric, '' for text)."""
    t = getattr(item, "type", None)
    if item.value is not None:
        v = item.value
        if v[:1] in ("'", '"'):
            return v.strip("'\"")
        up = v.upper()
        if up in ("ZERO", "ZEROS", "ZEROES"):
            return 0
        if up in ("SPACE", "SPACES"):
            return ""
        if re.match(r"^[+-]?\d+(\.\d+)?$", v):
            return float(v) if "." in v else int(v)
        return v
    if t is not None and t.category.startswith("numeric"):
        return 0
    if t is not None and t.category == "group":
        return None
    return ""


def _data_dictionary(program: Program) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for it in program.data_items:
        if it.level == 88:
            entry88 = {"kind": "condition-name", "of": it.cond_parent,
                       "values": it.condition_values, "line": it.line}
            if it.condition_ranges:
                entry88["ranges"] = it.condition_ranges
            if it.origin:
                entry88["member"] = it.origin
            out[it.name] = entry88
            continue
        entry = {"level": it.level, "line": it.line}
        if it.origin:
            entry["member"] = it.origin
        if it.section:
            entry["section"] = it.section
        if it.parent:
            entry["parent"] = it.parent
        if it.redefines:
            entry["redefines"] = it.redefines
        if getattr(it, "file", None):
            entry["file"] = it.file
        if it.occurs:
            entry["occurs"] = it.occurs
        if getattr(it, "occurs_depending", None):
            entry["occursDependingOn"] = it.occurs_depending
        if it.value is not None:
            entry["value"] = it.value
        if it.is_group:
            entry["type"] = {"category": "group"}
        elif it.type is not None:
            entry["type"] = it.type.to_dict()
        out[it.name] = entry
    return out


def _build_handlers_region(program: Program, ctx: "_BuildCtx"):
    """The orthogonal error-handler region for DECLARATIVES USE procedures and CICS HANDLE
    CONDITION. Returns an XState region {initial, states} watching for trigger events and
    dispatching to the handler paragraph, or None when there is nothing to model.

    The triggering errors are *runtime* events, so the watch->handler edges are reactive
    (they are not driven by the autonomous run); this is the faithful Harel shape, flagged
    as such. Declarative handler paragraphs are compiled here and lifted out of the main
    flow; CICS handler targets are ordinary main paragraphs."""
    decl = program.declaratives
    cics = program.cics_handlers
    if not decl and not cics:
        return None

    # Compile the declarative paragraphs into ctx.states, then lift them out so they live
    # in the handler region rather than the main PROGRAM flow.
    before = set(ctx.states)
    dnames = [p.name for p in decl]
    for idx, para in enumerate(decl):
        ctx.reg.state(para.name, f"declarative {para.name}"
                      + (f" (section {para.section})" if para.section else ""),
                      para.line, member=para.origin)
        cont = dnames[idx + 1] if idx + 1 < len(dnames) else _DECL_END
        _ParaCompiler(ctx, para).compile(cont)
    decl_states = {k: ctx.states.pop(k) for k in (set(ctx.states) - before)}
    if any(tr.get("target") == _DECL_END
           for s in decl_states.values() for tr in s.get("always", [])):
        decl_states[_DECL_END] = {"type": "final"}

    handler_states: dict = {}
    watch_on: dict = {}

    def add_handler(event: str, target: str) -> None:
        hkey = f"__H_{_slug(target)}"
        watch_on[event] = hkey
        handler_states[hkey] = {
            "entry": [ctx.reg.action_named(f"perform_{target}",
                                           f"PERFORM {target} (error handler)", 0)],
            "always": [{"target": "__WATCH__"}],
        }

    # DECLARATIVES USE sections: each section's trigger -> its first body paragraph.
    i = 0
    while i < len(decl):
        p = decl[i]
        if p.use_trigger:
            target = None
            j = i
            while j < len(decl) and (j == i or not decl[j].use_trigger):
                if decl[j].statements:
                    target = decl[j].name
                    break
                j += 1
            if target:
                for f in (p.use_files or ["*"]):
                    ev = f"IO.{p.use_trigger}" + ("" if f == "*" else f".{f}")
                    add_handler(ev, target)
        i += 1

    # CICS HANDLE CONDITION: condition -> target paragraph (a main-flow paragraph).
    for cond, target in cics:
        add_handler(f"CICS.{cond}", target)

    if not watch_on:                       # nothing modelable; restore and bail
        ctx.states.update(decl_states)
        return None

    handler_states["__WATCH__"] = {"on": watch_on}
    handler_states.update(decl_states)
    ctx.flag("DECLARATIVES", 0,
             "DECLARATIVES / CICS HANDLE modeled as an orthogonal parallel handler region; "
             "the triggering errors are runtime events, so the watch->handler edges are not "
             "driven by the autonomous run - verify against the source")
    return {"initial": "__WATCH__", "states": handler_states}


def _section_map(program: Program) -> Dict[str, List[str]]:
    """Section header name -> [header, member paragraphs...] in source order. PERFORM of
    a section runs its whole extent, so the emitter's actor owner needs the members."""
    out: Dict[str, List[str]] = {}
    for plist in (program.paragraphs, program.declaratives):
        for p in plist:
            if p.section:
                out.setdefault(p.section, [p.section]).append(p.name)
    return out


def build_machine(program: Program, source_name: str = "<source>") -> Machine:
    ctx = _BuildCtx(reg=NameRegistry(), calls=analyze_calls(program),
                    data=program.data_by_name)
    _compute_alter_targets(program, ctx)

    # Seed the machine memory (context) with each elementary item's start-of-run value
    # - the data the actions assign to and the guards test (alter switches were already
    # added by _compute_alter_targets and are preserved). An elementary OCCURS item is an
    # n-element table -> seeded as an array.
    for it in program.data_items:
        if it.is_group and it.occurs:
            ctx.flag(it.section or "DATA", it.line,
                     f"OCCURS on group {it.name}: subscripting its subordinate items "
                     f"is not modeled (only elementary-item OCCURS is)")
        if getattr(it, "occurs_depending", None):
            ctx.flag(it.section or "DATA", it.line,
                     f"{it.name} OCCURS ... DEPENDING ON {it.occurs_depending}: the "
                     f"table is modeled at its MAXIMUM size ({it.occurs}); the dynamic "
                     f"length is not enforced - verify uses of the variable extent")
        if it.redefines:
            tgt = ctx.data.get(it.redefines)
            if _redefines_compatible(it, tgt):
                ctx.flag(it.section or "DATA", it.line,
                         f"{it.name} REDEFINES {it.redefines}: same category/size - it can "
                         f"be treated as a value ALIAS of {it.redefines}. Modeled as an "
                         f"independent context field; if the program writes one and reads "
                         f"the other, mirror the value")
            else:
                ctx.flag(it.section or "DATA", it.line,
                         f"{it.name} REDEFINES {it.redefines}: DIFFERENT PICTURE/USAGE - "
                         f"reading one layout's bytes through another (byte reinterpretation) "
                         f"is NOT modeled; {it.name} is an independent field - review manually")
        if it.level in (88, 66) or it.is_group or it.pic is None:
            continue
        val = _initial_value(it)
        if it.occurs:
            val = [val for _ in range(it.occurs)]
        ctx.context.setdefault(it.name, val)

    paras = program.paragraphs
    names = [p.name for p in paras]

    for idx, para in enumerate(paras):
        # Register the paragraph name with its own provenance before compiling so the
        # compiler's structural-state default does not overwrite it.
        ctx.reg.state(para.name, f"paragraph {para.name}"
                      + (f" (section {para.section})" if para.section else ""),
                      para.line, member=para.origin)
        if getattr(para, "parse_error", None):
            ctx.flag(para.name, para.line,
                     f"paragraph body did not parse ({para.parse_error}); recovered as one "
                     f"opaque action - logic here is NOT modeled; review manually")
        cont = names[idx + 1] if idx + 1 < len(names) else _END
        sec_exit = cont
        if para.section:
            # EXIT SECTION lands on the first paragraph after the section's extent.
            j = idx
            while (j + 1 < len(program.paragraphs)
                   and program.paragraphs[j + 1].section == para.section):
                j += 1
            sec_exit = names[j + 1] if j + 1 < len(names) else _END
        _ParaCompiler(ctx, para).compile(cont, section_exit=sec_exit)

    # Validate every transition target: a GO TO / PERFORM naming a paragraph that does
    # not exist would emit a dangling edge (a broken machine) - flag it and reroute to
    # the program end instead of failing silently at run time.
    known = set(ctx.states) | {_END, _DECL_END}
    for sname, sdict in ctx.states.items():
        for tr in sdict.get("always", []) or []:
            tgt = tr.get("target")
            if tgt and tgt not in known:
                ctx.flag(sname, (tr.get("meta") or {}).get("cobolLine", 0),
                         f"transition target {tgt} does not exist in the program "
                         f"(GO TO / reference to an unknown paragraph) - rerouted to "
                         f"program end; verify")
                tr["target"] = _END

    # The shared final state (reached by falling off the physical end, or by any
    # paragraph whose continuation is end-of-program).
    if any(tr.get("target") == _END
           for s in ctx.states.values() for tr in s.get("always", [])):
        ctx.reg.state(_END, "end of program (fall-off / STOP RUN target)", 0)
        ctx.states[_END] = {"type": "final"}

    program_states = ctx.states
    program_initial = names[0] if names else None

    # DECLARATIVES + CICS HANDLE: an orthogonal error-handler region (Harel parallel
    # state). Built only when handlers exist, so ordinary programs keep their flat shape.
    handlers_region = _build_handlers_region(program, ctx)

    if handlers_region is not None and program_initial is not None:
        config: dict = {
            "id": program.program_id,
            "context": ctx.context,
            "type": "parallel",
            "states": {
                "PROGRAM": {"initial": program_initial, "states": program_states},
                "HANDLERS": handlers_region,
            },
        }
    else:
        config = {
            "id": program.program_id,
            "context": ctx.context,
            "states": program_states,
        }
        if program_initial:
            config["initial"] = program_initial

    notes = list(program.notes)
    if not program.has_procedure_division:
        notes.append("No PROCEDURE DIVISION found - no control flow to recover.")
    notes.append(
        "Step semantics: one record cycle = one macrostep; flags set in one cycle are "
        "sensed next cycle (STATEMATE next-step sensing). See cobol-to-statecharts.md."
    )
    if program.data_items:
        notes.append(
            "Data items are typed in 'data' (PIC/USAGE/sign). COBOL numerics are "
            "fixed-point decimal (COMP-3 packed, DISPLAY zoned, COMP binary); guard/"
            "action expressions in 'semantics' must be evaluated with decimal, not "
            "binary-float, arithmetic to stay faithful."
        )

    return Machine(
        config=config,
        provenance=ctx.reg.provenance_dict(),
        flags=ctx.flags,
        notes=notes,
        program_id=program.program_id,
        source_name=source_name,
        data={**_data_dictionary(program), **ctx.synthetic_data},
        semantics={"actions": ctx.action_sem, "guards": ctx.guard_sem},
        paragraph_order=[p.name for p in program.paragraphs]
        + [p.name for p in program.declaratives],
        sections=_section_map(program),
        using=program.using,
        returning=program.returning,
        files=program.files,
    )
