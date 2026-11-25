"""Stage 6 (projection) - field lineage across the program's external boundary.

Answers, for every external event and every field crossing it: *which event is
responsible for this field's state?* An input event's fields are filled BY that event;
an output event's fields are traced BACK to the event(s) whose data ultimately reached
them. The result is one row per ``(external event, field)``:

    event  direction  field     changedByProgram  changedBy        origins
    WRITE  output     OUT-FEE   true              COMPUTE @line 25  [GET.CALLER.CALLER,
                                                                    GET.CONSOLE.SYSIN]

Each row also carries a ``conditionSetId`` - the conditions under which its event happens
at all, interned into document-level pools rather than written out inline. A row's
``changedBy`` write sites are likewise stated once per field, in the document's
``changedBy`` table under the row's ``field``. Origins are
only half a business rule - "DAILYPOST changes the balance" is a dependency, "DAILYPOST
changes the balance WHEN the transaction is a deposit" is the rule - and the when-clause
is the half a requirements reader actually needs.

"Changed by a LINKAGE item" is not a separate column: reading a linkage field already
*is* a ``GET.CALLER.CALLER`` event, so it appears in ``origins`` like any other source.

The analysis is a **flow-sensitive** reaching-origins fixpoint over the emitted state
graph: only origins that actually reach an event are reported (an assignment on a branch
that cannot reach this event is excluded). PERFORM is followed as a call and returned
from, using the same target resolution as the runnable emitter, so a value produced
inside a performed paragraph is traced correctly.

Honest limits, all surfaced in ``flags`` rather than guessed:

* **Context-insensitive.** A paragraph performed from two sites is analyzed once with
  the *merged* incoming state, so an origin from call site A can appear at an event
  reached only via call site B. This over-approximates (never misses a real origin, may
  name an extra one) - the safe direction for provenance.
* **CALL ... USING** passes BY REFERENCE by default, and the callee is a different
  program: it *may* rewrite any argument. Those arguments get the CALL as a ``maybe``
  origin naming the program that would resolve it.
* **REDEFINES byte-aliasing, unresolved/multi-dimension subscripts, and paragraphs that
  failed to parse** break a chain. The field is marked ``unknown`` rather than reported
  as having no origin - "we cannot trace this" and "nothing external feeds this" are
  very different answers.
"""

from __future__ import annotations

import functools
import heapq
import json
import re
import weakref
from collections import deque
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from . import VIEW_SCHEMA_VERSION
from . import interface as _iface
from .semantics import mask_literals
from .business import _is_control_guard
from .emitter import (
    _para_of, _target_owner, edge_target, iter_transitions, order_index,
    perform_target as _perform_target, segment_entry,
)
from .statechart import Machine
from .storage import children_index, item_size, record_layout

# An origin: (event name, maybe?, program that would resolve a `maybe`).
Origin = Tuple[str, bool, Optional[str]]
OriginSet = FrozenSet[Origin]
# field -> the origins reaching it at a program point.
State = Dict[str, OriginSet]


class _Tracked(dict):
    """A state's working map that remembers what each written key held before its
    first write (``None``: absent), so the keys a state changed fall out of `_apply`
    without comparing the whole map. Only ``cur[k] = v`` is tracked - which is the one
    way `_apply` and its helpers write - and reads stay plain C-level dict reads."""
    __slots__ = ("old",)

    @classmethod
    def of(cls, origins: State) -> "_Tracked":
        """A private, tracked copy of ``origins``."""
        out = cls(origins)
        out.old = {}
        return out

    def __setitem__(self, key, value):
        old = self.old
        if key not in old:
            old[key] = dict.get(self, key)
        dict.__setitem__(self, key, value)

    def many(self, keys: List[str]) -> List[Optional[OriginSet]]:
        """``[cur.get(k) for k in keys]``, in one pass: what a group MOVE or a group read
        wants, every field of a record at once."""
        return list(map(self.get, keys))

    def put(self, values: State) -> None:
        """``cur[k] = v`` for every item, in one C-level update."""
        old, get = self.old, dict.get
        for k in values:
            if k not in old:
                old[k] = get(self, k)
        dict.update(self, values)

    def changes(self, canon: Dict[OriginSet, OriginSet]) -> Dict[str, OriginSet]:
        """The written keys whose value really changed, with their new values. Every
        value left in the map is a shared one: a changed value is replaced by its
        ``canon`` copy, and a key rewritten with an equal set keeps the object it held."""
        new = {}
        for k, was in self.old.items():
            now = dict.__getitem__(self, k)
            if now is was:
                continue
            if now == was:
                dict.__setitem__(self, k, was)
                continue
            now = canon.setdefault(now, now)
            dict.__setitem__(self, k, now)
            new[k] = now
        return new


class _Overlay(_Tracked):
    """A visit's working map laid over a stored map (a `_Node`) without copying it: the
    visit's writes are held here, and a key it has not written is read from the node.
    A wide map is thousands of entries while most visits touch a few dozen, so copying
    it per visit (as `_Tracked` is) was most of what a revisit cost. A visit that reads
    a lot (a group MOVE reads every field of its source) lays the node out whole after
    `_LAYOUT_AFTER` reads instead of walking its chain for each. Supports exactly what
    `_apply` and its helpers use: ``get``, ``in``, ``[k]``, ``[k] = v`` and ``put``."""
    __slots__ = ("node", "below", "reads")

    def __init__(self, node: "_Node"):
        dict.__init__(self)
        self.old = {}
        self.node = node
        self.reads = 0
        self.below = (node.full.get if node.full is not None
                      else functools.partial(_lookup, node))

    def _below(self, key):
        self.reads += 1
        if self.reads == _LAYOUT_AFTER and self.node.full is None:
            self.below = _materialize(self.node).get
        return self.below(key)

    def many(self, keys: List[str]) -> List[Optional[OriginSet]]:
        """``[cur.get(k) for k in keys]`` without a method call per key: a group MOVE
        reads every field of its source, and every field of its target for the old
        value, and per-key reads through the overlay were most of what it cost."""
        r = self.reads
        self.reads = r + len(keys)
        if r < _LAYOUT_AFTER <= r + len(keys) and self.node.full is None:
            self.below = _materialize(self.node).get
        below = self.below
        if not dict.__len__(self):         # nothing written yet this visit
            return list(map(below, keys))
        get = dict.get
        return [v if (v := get(self, k)) is not None else below(k) for k in keys]

    def get(self, key, default=None):
        v = dict.get(self, key)
        if v is None:
            v = self._below(key)
        return default if v is None else v

    def __contains__(self, key):
        return dict.__contains__(self, key) or self._below(key) is not None

    def __getitem__(self, key):
        v = self.get(key)
        if v is None:
            raise KeyError(key)
        return v

    def __setitem__(self, key, value):
        old = self.old
        if key not in old:
            old[key] = self.get(key)
        dict.__setitem__(self, key, value)

    def put(self, values: State) -> None:
        old = self.old
        new = [k for k in values if k not in old]
        if new:
            old.update(zip(new, self.many(new)))
        dict.update(self, values)


_LAYOUT_AFTER = 48   # reads after which a visit's `_Overlay` lays its node out whole


class _Node:
    """One program point's origin map, stored as the keys it changed from ``base``.

    The fixpoint used to keep a full field -> origins dict per state; on a wide record
    layout that is thousands of entries per state while a state changes about a dozen
    (median 12 of 2,285 on an 11k-line program), so copying, merging and comparing
    whole maps was the cost of the pass and a full map per state its memory. A node
    keeps only ``delta``, until it is ``_HOPS`` nodes from the last ``full`` snapshot:
    then it becomes one, so a value is never more than ``_HOPS`` lookups away. A
    snapshot keeps only a weak link (``up``) to the node it grew from, so a chain holds
    one snapshot alive, not every map ever built above it (which grew without bound
    on programs whose states are re-solved many times). Nodes never change what they
    answer once built, so they can be shared freely."""
    __slots__ = ("base", "delta", "hops", "load", "width", "full", "up", "__weakref__")

    def __init__(self, base: Optional["_Node"], delta: Optional[Dict[str, OriginSet]],
                 full: Optional[dict] = None):
        self.base = base
        self.delta = delta
        self.full = full
        self.up = None
        if full is not None:
            self.hops, self.load, self.width = 0, 0, len(full)
        else:
            self.hops, self.load = base.hops + 1, base.load + len(delta)
            self.width = base.width


_HOPS = 8           # a full snapshot at least every this many nodes down a chain
_WALK = 32          # how far up `_since_common` looks for a shared ancestor
_LOAD = 4           # ...or once the changes above a snapshot exceed 1/_LOAD of the map
_NO_ORIGINS: OriginSet = frozenset()
# The change count at which a state's convergence is first checked (`_check_height`),
# then at every doubling. Any value is sound - the inequality checked holds at every
# count - so this only decides the cost: real programs change a state's output a few
# times, and never pay for a check.
_CHECK_FROM = 16


# -- origin maps as change chains (see `_Node`) ------------------------------------


def _extend(base: _Node, delta: Dict[str, OriginSet],
            full: Optional[dict] = None) -> _Node:
    """``base`` with ``delta`` applied - or ``base`` itself when nothing changed.
    ``full``, when the caller already holds the whole map, becomes the snapshot if
    this node is due one."""
    if not delta:
        return base
    node = _Node(base, delta)
    # Due a snapshot after `_HOPS` nodes, or sooner once the changes stacked above the
    # last one add up to a fair part of the map: then a lookup or a layout would spend
    # more walking deltas than a snapshot costs to keep.
    if node.hops >= _HOPS or node.load * _LOAD > node.width:
        if full is None:
            full = _materialize(node)
        full.old = None
        # The snapshot answers every lookup itself, so the chain above it need not
        # be kept: only a WEAK link stays, for finding the maps it shares an
        # ancestor with while that ancestor is still in use (`_since_common`).
        node.full, node.up, node.base = full, weakref.ref(node.base), None
        node.hops, node.load, node.width = 0, 0, len(full)
    return node


def _materialize(node: _Node) -> _Tracked:
    """The whole map at ``node``, as a fresh, private, write-tracked dict."""
    chain = []
    while node.full is None:
        chain.append(node.delta)
        node = node.base
    out = _Tracked.of(node.full)
    for delta in reversed(chain):
        dict.update(out, delta)
    return out


def _whole(node: _Node) -> dict:
    """The whole map at ``node``, to read only: its snapshot itself when it is one."""
    return node.full if node.full is not None else _materialize(node)


def _lookup(node: _Node, key: str) -> Optional[OriginSet]:
    while node.full is None:
        delta = node.delta
        if key in delta:
            return delta[key]
        node = node.base
    return node.full.get(key)


def _differ(a: _Node, b: _Node, keys) -> List[str]:
    """The keys among ``keys`` on which two maps disagree."""
    keys = list(keys)
    ga, gb = _getter(a, len(keys)), _getter(b, len(keys))
    return [k for k, va, vb in zip(keys, map(ga, keys), map(gb, keys))
            if va is not vb and va != vb]


def _since_common(nodes: List[_Node]) -> Tuple[Optional[_Node], Set[str]]:
    """The nearest map every one of ``nodes`` grew from (``None`` when they share
    no snapshot), and every key changed between it and any of them: the only keys
    on which they can differ from it."""
    lines = []
    for n in nodes:
        line = [n]
        while len(line) <= _WALK:
            if n.full is None:
                n = n.base
            else:
                n = n.up() if n.up is not None else None
                if n is None:
                    break
            line.append(n)
        lines.append(line)
    others = [set(map(id, line)) for line in lines[1:]]
    top = next((a for a in lines[0] if all(id(a) in o for o in others)), None)
    keys: Set[str] = set()
    if top is not None:
        for line in lines:
            for n in line:
                if n is top:
                    break
                keys.update(n.delta)
    return top, keys


def _snapshot(node: _Node) -> dict:
    """The snapshot ``node`` grew from: its size is the map's width, near enough."""
    while node.full is None:
        node = node.base
    return node.full


def _getter(node: _Node, lookups: int):
    """A lookup function for ``node``: down its chain for a few keys, or into the
    map laid out whole when that is cheaper than ``lookups`` chain walks."""
    if node.full is not None:
        return node.full.get
    if 8 * lookups * node.hops > len(_snapshot(node)):
        return _materialize(node).get
    return functools.partial(_lookup, node)


def _differ_all(a: _Node, b: _Node) -> List[str]:
    """Every key on which two maps disagree."""
    top, keys = _since_common([a, b])
    if top is not None:
        return _differ(a, b, keys)
    ma, mb = _materialize(a), _materialize(b)
    out = [k for k, v in ma.items() if mb.get(k) is not v and mb.get(k) != v]
    out.extend(k for k in mb if k not in ma)
    return out

# A condition: (guard name, is it the NEGATED sense?). Negation is first-class because
# the else / WHEN OTHER branch carries no guard of its own - its condition is precisely
# the negation of the branches before it, and that negation is usually the business rule
# worth reading ("when the transaction is none of D/W/I").
Cond = Tuple[str, bool]
# Readable form. The fixpoint carries the same sets packed into ints - see
# `_Lineage._intern_conditions` - and `_conds` unpacks one back to this.
CondSet = FrozenSet[Cond]

_WORD = re.compile(r"[A-Z][A-Z0-9-]*")
# Verbs whose data effect is opaque (the value semantics are not modeled) but whose
# *dependency* is plain from the operands - which is all lineage needs.
_DEP_ONLY = {"STRING", "UNSTRING", "INSPECT"}
_UNKNOWN = "<unknown>"
# `IN-FILE_atEnd` / `IN-FILE_notAtEnd`: the READ lowering's synthetic end-of-stream guards.
_ATEND = re.compile(r"^(.*?)_(not)?[Aa]t[Ee]nd$")


# --------------------------------------------------------------------------- #
# operand extraction
# --------------------------------------------------------------------------- #

def _operands(expr: str, known: Set[str]) -> List[str]:
    """Field names referenced by an expression (literals/keywords dropped).

    Masked first (ledger item 156): a literal that SPELLS a data name (``MOVE 'TBL-GRP'
    TO T`` beside a DCLGEN group TBL-GRP) is a constant, not a read of that field. A word
    after OF/IN (ledger item 155) qualifies the word before it - ``A OF G`` reads A,
    not the whole of G - and the object of LENGTH OF / ADDRESS OF is no value source
    either."""
    out = []
    words = _WORD.findall(mask_literals((expr or "").upper()))
    for i, w in enumerate(words):
        if i and words[i - 1] in ("OF", "IN"):
            continue
        if w in known and w not in out:
            out.append(w)
    return out


_STRING_RE = re.compile(r"\bINTO\s+([A-Z0-9-]+)((?:\s*\([^()]*\))*)", re.I)
_REFMOD = re.compile(r"\(([^():]*:[^():]*)\)\s*$")


def _refmod(ref: str) -> Optional[str]:
    """The reference-modification range of a store target (``X(1:10)``,
    ``T(IX)(12:)``) as written, blanks dropped - ``"1:10"`` - or None."""
    m = _REFMOD.search(ref or "")
    return re.sub(r"\s+", "", m.group(1)) if m else None
_UNSTRING_INTO = re.compile(r"\bINTO\b(.*?)(?:\bWITH\b|\bTALLYING\b|\bON\b|$)", re.I | re.S)
_INSPECT_TALLY = re.compile(r"\bTALLYING\s+([A-Z0-9-]+)", re.I)


def _dep_only_flow(verb: str, text: str, known: Set[str]) -> Tuple[List[str], List[str]]:
    """``(receivers, sources)`` for STRING / UNSTRING / INSPECT.

    Lineage needs only *which fields feed which* - not the concatenation/split/count
    semantics - so the operands are enough to keep the chain intact.

    Every scan runs over the MASKED text, because these verbs exist to build and take
    apart message strings, so their literals are practically guaranteed to contain
    English: ``STRING 'PUT INTO QUEUE' ... INTO WS-MSG`` found its INTO inside the
    literal (naming the phantom receiver QUEUE and losing WS-MSG's write entirely),
    and ``INSPECT ... FOR ALL 'REPLACING'`` made the subject a receiver. Masking also
    keeps a literal that happens to SPELL a field's name out of the source list, since
    a masked span yields no words at all.
    """
    up = mask_literals((text or "").upper())
    if verb == "STRING":            # STRING a b ... INTO c
        m = _STRING_RE.search(up)
        recv = [m.group(1)] if m and m.group(1) in known else []
        head = up[:m.start()] if m else up
        return recv, [w for w in _operands(head, known) if w not in recv]
    if verb == "UNSTRING":          # UNSTRING src ... INTO a b c
        m = _UNSTRING_INTO.search(up)
        recv = _operands(m.group(1), known) if m else []
        head = up[:m.start()] if m else up
        return recv, [w for w in _operands(head, known) if w not in recv]
    if verb == "INSPECT":           # INSPECT x TALLYING n ... / REPLACING ...
        m = _INSPECT_TALLY.search(up)
        recv = [m.group(1)] if m and m.group(1) in known else []
        src = _operands(up, known)
        subject = src[0] if src else None
        if subject and "REPLACING" in up:
            # REPLACING rewrites the subject in place: it is a receiver AND its own
            # source, so its origins survive the rewrite (`X <- X` in the flow).
            if subject not in recv:
                recv = recv + [subject]
            return recv, [w for w in src if w not in recv or w == subject]
        return recv, [w for w in src if w not in recv]
    return [], []


# --------------------------------------------------------------------------- #
# condition rendering
# --------------------------------------------------------------------------- #

def _cond_text(name: str, tree: Optional[dict], negated: bool) -> Optional[str]:
    """A guard as COBOL-ish source text, or None if its test is not recoverable.

    The negated sense is rendered as ``NOT (...)`` rather than by flipping the operator.
    Inverting ``=`` to ``NOT =`` is safe, but inverting an ordering test is not always
    the identity a reader assumes once COBOL's figurative constants and class tests are
    in play - and this table exists to be trusted, not to look tidy. The same reasoning
    leaves ``NOT (F NOT AT END)`` un-simplified: clumsy to read, impossible to misread.
    """
    body: Optional[str] = None
    if isinstance(tree, dict):
        op = tree.get("op")
        if op == "rel":
            left, rel, right = tree.get("left"), tree.get("rel"), tree.get("right")
            if left is not None and rel is not None and right is not None:
                body = f"{left} {rel} {right}"
        elif op == "cond-name":
            body = tree.get("name") or None
        # {op:'raw'} and anything new fall through: say nothing rather than guess.
    elif tree is None:
        # A file's end-of-stream guard is synthesized by the READ lowering, so it has no
        # expression tree - but its meaning is not in doubt and it is not a mystery to be
        # flagged. `_atEnd` is the AT END arm, `_notAtEnd` the NOT AT END one.
        m = _ATEND.match(name)
        if m:
            body = f"{m.group(1)} {'NOT AT END' if m.group(2) else 'AT END'}"
    if not body:
        return None
    return f"NOT ({body})" if negated else body


# --------------------------------------------------------------------------- #
# the analysis
# --------------------------------------------------------------------------- #

def _entry_key(entry: dict) -> str:
    """A canonical, hashable form of a write-site entry, for order-preserving dedup.

    The entry is `{"action", "line"}` plus `_write_entry`'s condition keys, which since
    the conditions were interned are all SCALARS - the set is one `conditionSetId`.
    `json.dumps(..., sort_keys=True)` is stable for these (str/int/bool/None leaves
    only), which is what lets a `seen` set replace an `x not in list` scan without
    changing which entry survives or the order they come out in. It used to have to
    walk a nested condition list per comparison; it no longer does.
    """
    return json.dumps(entry, sort_keys=True, default=str)


_FILLER = "FILLER"
_FILLER_KEY = "#FILLER"      # a group's FILLER bytes: `<group>#FILLER` (never a COBOL name)


class _Lineage:
    def __init__(self, machine: Machine, timer=None):
        # `timer`: a StageTimer, only from the --timing run that builds this - it splits
        # the condition fixpoints out of `lineage:build`. Nothing here depends on it.
        self.m = machine
        self.config = machine.config
        self.actions = machine.semantics.get("actions", {})
        self.provenance = machine.provenance
        self.data = machine.data or {}
        self.known: Set[str] = {n.upper() for n, it in self.data.items()
                                if isinstance(it, dict)
                                and it.get("kind") != "condition-name"}
        # LINKAGE items (the caller's parameter records / COMMAREA). A write into one is
        # a caller-visible OUTPUT the event classifier does not recognize - see `_apply`.
        # Less the LINKAGE the program addresses itself (SET ADDRESS OF / CICS SET):
        # that storage is not the caller's, so it is neither a request nor a response.
        self.own_linkage: Set[str] = _iface.own_linkage(
            self.data, machine.semantics, machine.provenance,
            getattr(machine, "using", None))
        self.linkage: Set[str] = {n.upper() for n, it in self.data.items()
                                  if isinstance(it, dict)
                                  and it.get("section") == "LINKAGE"} - self.own_linkage
        self.ordered = machine.paragraph_order
        self.sections = getattr(machine, "sections", {}) or {}
        # Built once: PERFORM targets are resolved through it, never by list scans.
        self._index = order_index(self.ordered)
        self.files = getattr(machine, "files", {}) or {}
        self.dv = _iface._DataView(self.data, machine.fd_records)
        # group -> its elementary fields. A group's value IS those fields' bytes, so
        # every read and write of a group goes through them (`_read` / `_store`).
        self.groups: Dict[str, List[str]] = {}
        for n in self.known:
            if n == _FILLER:
                continue
            leaves = [l for l in self.dv.leaves(n) if l != n]
            if leaves or (self.data.get(n) or {}).get("fillers"):
                self.groups[n] = [l for l in leaves if l != _FILLER]
        # FILLER bytes. Every FILLER shares the one dictionary key, so a FILLER's bytes
        # are kept per GROUP instead, under `<group>#FILLER` - a FILLER cannot be named,
        # so it is only ever written or read through a group. A store into a group writes
        # its own and every nested group's filler key (`sub_fillers`); a read of a group
        # sees those and its enclosing groups' (`group_reads`), whose stores may have
        # laid bytes over its FILLERs.
        self.sub_fillers: Dict[str, List[str]] = {}
        anc: Dict[str, List[str]] = {}
        for n in self.groups:
            sub = [g for g in self._subgroups(n) if g in self.groups]
            self.sub_fillers[n] = [g + _FILLER_KEY for g in sub]
            for g in sub:
                if g != n:
                    anc.setdefault(g, []).append(n + _FILLER_KEY)
        self.group_reads: Dict[str, List[str]] = {
            n: leaves + self.sub_fillers[n] + anc.get(n, [])
            for n, leaves in self.groups.items()}
        # The dictionary is fixed for the whole run, so a record's byte layout, and which
        # source bytes each target field of a group MOVE overlaps, are computed once -
        # not at every visit of every group MOVE (`_place_bytes` / `_placement`).
        self._kids = None
        # name -> the items sharing its bytes through a REDEFINES (ledger item 159): a
        # store under one name is a store under the other. Coarse - every item of one
        # alternative against every item of the other - unless both alternatives can
        # be laid out, when the byte overlap says which.
        self.aliases: Dict[str, Tuple[str, ...]] = self._redefines_aliases()
        self._layouts: Dict[str, dict] = {}
        self._placements: Dict[Tuple[str, str], tuple] = {}
        self._canon: Dict[OriginSet, OriginSet] = {}
        self.cursors = _iface._cursor_tables(self.provenance)
        # A FETCH's host-variable <-> Db2 COLUMN correlation, recovered from the cursor's
        # DECLARE. Only the interface build passed this to `_classify`, so every FETCH row
        # here carried an EMPTY `columns` map - the dynamic-call view, which reads it to
        # say a value comes from `TABLE.COLUMN`, lost that entirely for cursor programs.
        self.cursor_cols = _iface._cursor_columns(machine.semantics, self.provenance)
        self.cursor_derivs = _iface._cursor_derivations(machine.semantics)
        self.cursor_refs = _iface._cursor_table_refs(self.provenance)
        # ...and the DECLAREs that never became actions at all (data division /
        # copybook), which provenance and semantics cannot see. Without this the row's
        # endpoint is a phantom `<cursor X>` while the bundle event names the table.
        _iface.seed_cursor_maps(self.cursors, self.cursor_cols, self.cursor_derivs,
                                getattr(machine, "sql_cursors", None), self.cursor_refs)
        # action name -> its classification. Every argument `_apply` passes to
        # `_classify` other than the name is fixed for this instance's lifetime, so the
        # result is invariant across visits - but the worklist calls `_apply` once per
        # state PER VISIT, and once more in the final rows pass, and `_classify` does
        # `mask_literals`, several regex scans and a `_DataView.leaves` walk each time.
        #
        # SCOPED TO THE INSTANCE, and that is not a stylistic choice. `_classify` is a
        # module-level function, but it is NOT invariant across CALLERS: its answer
        # depends on the cursor maps handed to it, and reactive builds two
        # provenance-only maps deliberately left unseeded (they consume booleans only).
        # `examples/sqlwscsr.cbl` proves it - action `exec_sql_fetch` classifies as
        # `T_DMAA_ACC_ANAL` here and as `<cursor ACCT_CSR>` there. A module-level
        # `lru_cache` on `_classify` would return whichever answer was computed first
        # and silently corrupt the other view, with no test failure, because those two
        # sites read only `h["direction"]`.
        self._classify_memo: Dict[str, List[dict]] = {}
        self._exec_writes_memo: Dict[str, List[Tuple[str, List[str]]]] = {}
        self.flags: List[str] = []
        self._flag_set: Set[str] = set()      # `flags` as a set: the dedup test
        # Primitive provenance facts, recorded during the final (row-emitting) pass so a
        # backward query can answer "where did THIS item's value come from" for an item
        # that never crosses the boundary itself - a dynamic CALL target being the case
        # that matters. Recorded HERE, from the fixpoint's own steps, rather than
        # re-derived elsewhere: a second traversal would drift from this one, and the
        # drift would be invisible (a plausible chain that is not the real one).
        self.fills: List[dict] = []       # field <- an external input event
        self.flow: List[dict] = []        # target <- source operands, at one write site
        self.dynamic_sites: List[dict] = []   # unresolved dynamic targets + their origins
        self._view: Optional[dict] = None     # `run`'s result, computed at most once
        raw: Dict[str, dict] = {}
        for name, _region, st in _iface._iter_states(self.config):
            raw[name] = st
        self.states, self.origin_state = self._split(raw)
        # (owner-exit node -> the edges that leaving/replaced it), filled by _successors
        # and read by _edge_conditions so a return edge keeps the condition of the branch
        # it stands in for.
        self.fold_src: Dict[str, List[str]] = {}
        self.fold_dst: Dict[str, List[str]] = {}
        # PERFORM target -> why it did not resolve, filled by _successors. An unresolved
        # call is already flagged there; this keeps WHICH target failed and HOW, so a
        # state left unreachable behind it can be given a cause instead of a shrug.
        self._unresolved: Dict[str, str] = {}
        self.succs = self._successors()
        self.guards = machine.semantics.get("guards", {}) or {}
        self.guard_kinds = machine.semantics.get("guardKinds", {}) or {}
        self.guard_line: Dict[str, int] = {}
        self.edge_cond = self._edge_conditions()
        self.entries = self._entries()
        self.cond_list, self.edge_bits = self._intern_conditions()
        self.pair_lo = self._pair_mask(self.cond_list)
        # Two passes over the same graph. MUST (meet = intersection) is what we report:
        # a condition that holds on EVERY path to the event, so stating it is always
        # true. MAY (meet = union) is only used to decide whether an empty MUST is
        # honest - see `_conditions_of`.
        t_cond = timer.start() if timer is not None else None
        self.must = self._cond_flow(lambda a, b: a & b)
        self.may = self._cond_flow(lambda a, b: a | b)
        if timer is not None:
            timer.since("lineage:conditions", t_cond)
        # state -> `_conditions_of`'s answer. Asked once per state by the write-site
        # pass and once more per ROW; the answer is a property of the state.
        self._cond_memo: Dict[str, Tuple[Optional[int], bool]] = {}
        self._changed_memo: Dict[str, List[dict]] = {}     # field -> `_changed_by`
        self._changed_fields: Set[str] = set()           # fields some row says changed
        # The OUTPUT's condition pools. Not to be confused with `_intern_conditions`
        # above, which packs the same vocabulary into BITMASKS for the fixpoint's own
        # meet/join; these two carry those predicates out to the document.
        #
        # A program point's conditions are a SET drawn from a small vocabulary - one
        # program had 296 distinct predicates - but the set is re-derived per row AND
        # per write site, and there are hundreds of write sites per row. Written inline
        # at each, that cost 2.04 GB to encode those 296 predicates: 27,429 copies
        # apiece, against a vocabulary of ~18 KB. Both levels are interned here instead.
        # Ids are assigned in first-encounter order, which is deterministic: every walk
        # that fills them iterates `self.states`, and `_cond_set_id` sorts.
        self._cond_ids: Dict[Tuple[str, bool], int] = {}
        self._cond_defs: List[dict] = []
        self._set_ids: Dict[Tuple[int, ...], int] = {}
        self._set_defs: List[List[int]] = []

    # -- split states at PERFORM boundaries ---------------------------------
    #
    # A state's `entry` is a folded run of straight-line actions, and a PERFORM can sit
    # in the MIDDLE of it: ``[ACCEPT, MOVE, perform_X, WRITE]``. Analyzing that as one
    # node would run the WRITE with pre-call origins. Split each run into a chain so the
    # call happens between the actions either side of it - the same shape the runnable
    # emitter builds with its invoke nodes.

    def _split(self, raw: Dict[str, dict]) -> Tuple[Dict[str, dict], Dict[str, str]]:
        out: Dict[str, dict] = {}
        origin: Dict[str, str] = {}      # split node -> the real state it came from
        for name, st in raw.items():
            entry = list(st.get("entry", []) or [])
            control = {k: v for k, v in st.items() if k != "entry"}
            if not any(a.startswith("perform_") for a in entry):
                out[name] = dict(st)
                origin[name] = name
                continue
            # Isolate each PERFORM into its own segment (shared with the emitter's split);
            # here each segment becomes an `__L{i}` node that keeps the PERFORM as an action.
            segs = segment_entry(entry, lambda a: a.startswith("perform_"), isolate=True)
            ids = [name] + [f"{name}__L{i}" for i in range(1, len(segs))]
            for i, seg in enumerate(segs):
                nid = ids[i]
                origin[nid] = name
                if i + 1 < len(segs):
                    out[nid] = {"entry": seg,
                                "always": [{"target": ids[i + 1]}]}
                else:
                    out[nid] = {"entry": seg, **control}
            if segs and not segs[-1][0].startswith("perform_"):
                continue
            # a trailing PERFORM: the control edges ride a final empty node
            tail = f"{name}__Lend"
            out[ids[-1]] = {"entry": segs[-1], "always": [{"target": tail}]}
            out[tail] = dict(control)
            origin[tail] = name
        return out, origin

    def _flag(self, msg: str) -> None:
        if msg not in self._flag_set:
            self._flag_set.add(msg)
            self.flags.append(msg)

    # -- path conditions ----------------------------------------------------
    #
    # "Where did this value come from?" is only half a business rule. The other half is
    # "under what condition?" - DAILYPOST changes the balance *when the transaction is a
    # deposit*, and the when-clause IS the rule. These two passes recover it.

    def _entries(self) -> List[str]:
        initial = self.config.get("initial")
        entries = [initial] if initial else []
        if self.config.get("type") == "parallel":
            entries = [r.get("initial") for r in self.config["states"].values()
                       if r.get("initial")]
        return [e for e in entries if e in self.states]

    def _branches(self, st: dict) -> List[list]:
        """The state's transition lists. Each list is one first-match-wins group: the
        `always` list is one, and each event key under `on` is its own."""
        out: List[list] = []
        al = st.get("always")
        if al:
            out.append(al if isinstance(al, list) else [al])
        for v in (st.get("on") or {}).values():
            out.append(v if isinstance(v, list) else [v])
        return out

    def _edge_conditions(self) -> Dict[Tuple[str, str], CondSet]:
        """``(src, target)`` -> the conditions under which that edge is taken.

        A transition list is FIRST-MATCH-WINS, which is exactly how COBOL's IF/ELSE and
        EVALUATE/WHEN lower here: branch *i* is taken when its own guard holds AND every
        guard before it failed. So the condition of an unguarded trailing branch (the
        ELSE, the WHEN OTHER, the loop body) is the conjunction of those negations -
        recovered exactly, not guessed.

        Two edges from one state to one target mean the target is reached under a
        DISJUNCTION, which this conjunctive lattice cannot express; they meet, dropping
        to what both agree on. Anything not recorded here defaults to no condition, which
        under-claims - the safe direction for a table that gets read as fact.
        """
        raw: Dict[Tuple[str, str], CondSet] = {}
        for name, st in self.states.items():
            for group in self._branches(st):
                seen: List[str] = []
                for item in group:
                    if isinstance(item, str):
                        tgt, guard, meta = item, None, {}
                    elif isinstance(item, dict):
                        tgt = item.get("target")
                        guard = item.get("guard")
                        meta = item.get("meta") or {}
                    else:
                        continue
                    cond: CondSet = frozenset((g, True) for g in seen)
                    if isinstance(guard, str) and guard:
                        cond = cond | {(guard, False)}
                        if guard not in self.guard_line and meta.get("cobolLine"):
                            self.guard_line[guard] = meta["cobolLine"]
                        seen.append(guard)
                    if not tgt:
                        continue
                    key = (name, tgt)
                    raw[key] = cond if key not in raw else (raw[key] & cond)
        out = {k: v for k, v in raw.items() if k[1] in self.states}
        # A PERFORM's return edge is a synthetic edge standing in for the real ones that
        # ran off the end of the paragraph - so it inherits their condition. Without
        # this, `IF X ... END-IF` at a paragraph's tail loses the `NOT X` on the way out,
        # the negation never cancels its positive downstream, and the merge below the
        # call looks conditional when it is not.
        for s2, leaving in self.fold_src.items():
            acc: Optional[CondSet] = None
            for lt in leaving:
                c = raw.get((s2, lt), frozenset())
                acc = c if acc is None else (acc & c)
            for cont in self.fold_dst.get(s2, []):
                out[(s2, cont)] = acc if acc is not None else frozenset()
        return out

    def _intern_conditions(self) -> Tuple[List[Cond], Dict[Tuple[str, str], int]]:
        """Pack every condition set into an integer, one bit per ``(guard, polarity)``.

        The fixpoint below meets and joins these sets once per graph edge per worklist
        step, and on a large program a single set holds thousands of conditions. As
        frozensets of tuples, one union re-hashed every element of both operands; as
        integers, ``&`` and ``|`` ARE meet and join, at a word per 64 conditions.
        Measured on a 4,330-state program: 2.11s of set algebra became 0.045s, with the
        step count and the answer both unchanged.

        Bits are handed out in SORTED order, never iteration order, so the packing
        cannot make the emitted view depend on PYTHONHASHSEED. Sorting also puts a
        guard's two polarities side by side - ``(g, False)`` immediately before
        ``(g, True)``, nothing can sort between them - which `_pair_mask` relies on.
        """
        cond_list = sorted({c for cs in self.edge_cond.values() for c in cs})
        bit = {c: 1 << i for i, c in enumerate(cond_list)}
        edge_bits: Dict[Tuple[str, str], int] = {}
        for key, conds in self.edge_cond.items():
            mask = 0
            for c in conds:
                mask |= bit[c]
            edge_bits[key] = mask
        return cond_list, edge_bits

    @staticmethod
    def _pair_mask(cond_list: List[Cond]) -> int:
        """Bit ``i`` set where ``cond_list[i]`` is ``(g, False)`` and ``cond_list[i+1]``
        is ``(g, True)``: the guards present in BOTH polarities, marked on their positive
        bit. A guard seen in one polarity only gets no bit."""
        mask = 0
        for i in range(len(cond_list) - 1):
            g, neg = cond_list[i]
            if not neg and cond_list[i + 1] == (g, True):
                mask |= 1 << i
        return mask

    @staticmethod
    def _partial_bits(must: int, may: int, pair_lo: int) -> bool:
        """Does some condition in MAY but not MUST belong to a guard that MAY does not
        hold in both polarities? Pure mask algebra - see `_conditions_of` for what the
        answer means. ``both`` marks the positive bit of every guard whose positive AND
        negative bits are both in MAY; widened to both bits, its complement leaves the
        one-sided guards, and any of those in ``MAY - MUST`` makes the point partial."""
        both = may & (may >> 1) & pair_lo
        both |= both << 1
        return bool(may & ~must & ~both)

    def _conds(self, bits: Optional[int]) -> CondSet:
        """A packed bitmask back to the condition set it stands for.

        Costs one step per condition actually present, not one per bit in the universe,
        so decoding a row is no dearer than the frozenset it replaced.
        """
        out: List[Cond] = []
        while bits:
            low = bits & -bits                      # lowest set bit
            out.append(self.cond_list[low.bit_length() - 1])
            bits ^= low
        return frozenset(out)

    def _cond_flow(self, join) -> Dict[str, Optional[int]]:
        """Forward fixpoint of path conditions, meeting at merge points with ``join``.

        Sets are the bitmasks built by ``_intern_conditions``; ``join`` is ``&`` for
        MUST and ``|`` for MAY. ``None`` means *never reached from any entry*, which is
        a different answer from ``0`` (*reached under no condition*) and stays distinct.

        The worklist is a QUEUE, and that is the whole difference between linear and
        quadratic. Popping the most recently pushed state dives down one branch, carrying
        a set that later merges will cut back, and then re-walks everything downstream of
        it once per revision - measured at 1.9 MILLION pops on a 2400-state program,
        against 4,810 for the same program in queue order, which is barely two per state.
        Breadth-first reaches a state after most of its predecessors have settled, so it
        usually settles on the first visit. The fixpoint is unique and order-independent,
        so this is purely about how much work is wasted getting there. A state already
        queued is not queued again, for the same reason.

        The iteration bound is a PROOF, not a guess, which is what makes tripping it
        meaningful. A state's set is assigned once and thereafter only shrinks (under
        intersection) or only grows (under union), so it can change at most once per
        condition plus once for that first assignment; only a change re-queues it; so
        the queue can be popped at most ``states x (conditions + 1)`` times plus the
        entries. The previous bound, ``max(50_000, states * 500)``, grew LINEARLY in the
        state count while the work grew quadratically, so a large program tripped it and
        silently emitted a half-computed ``conditions`` column behind a generic flag.
        """
        IN: Dict[str, Optional[int]] = {s: None for s in self.states}
        for e in self.entries:
            IN[e] = 0
        work = deque(self.entries)
        queued = set(self.entries)
        steps = 0
        limit = len(self.entries) + len(self.states) * (len(self.cond_list) + 1)
        while work:
            steps += 1
            if steps > limit:
                # Unreachable unless the lattice above stops being monotone. Kept as a
                # guard against a future change silently turning this into a hang.
                self._flag("condition fixpoint exceeded its proven iteration bound; "
                           "'conditions' may be incomplete - this is a defect in the "
                           "analysis, please report this program")
                break
            s = work.popleft()
            queued.discard(s)
            base = IN[s]
            if base is None:
                continue
            for t in self.succs.get(s, []):
                if t not in self.states:
                    continue
                out = base | self.edge_bits.get((s, t), 0)
                cur = IN[t]
                new = out if cur is None else join(cur, out)
                if new != cur:
                    IN[t] = new
                    if t not in queued:
                        work.append(t)
                        queued.add(t)
        return IN

    def _reached(self, state: str) -> bool:
        return self.must.get(state) is not None

    def _conditions_of(self, state: str) -> Tuple[Optional[int], bool]:
        """``(conditionSetId, partial)`` for a program point.

        The id indexes ``conditionSets`` in the emitted document; ``None`` means the
        MUST set is empty, and the caller then omits the key entirely - so
        ``"conditionSetId" in row`` is exactly the old ``bool(row.get("conditions"))``.

        The conditions are the MUST set, which is always sound: every one of them holds
        whenever this point is reached. ``partial`` warns that they are not the WHOLE
        condition - that something else also governs this point which a conjunction
        cannot express.

        The test is which guards appear in MAY but not MUST, and in what polarity:

        * both polarities => it says nothing about this point. Either the branches
          reconverged (after ``IF A ... ELSE ...`` every later state has both `A` and
          `NOT A` behind it, and the event really is unconditional), or it is a guard
          inside a loop whose earlier iterations went the other way - which is history,
          not a condition on this arrival.
        * one polarity only => something really does constrain this point that MUST could
          not keep. ``IF A: PERFORM W`` plus ``IF B: PERFORM W`` reaches W under ``A OR B``:
          no single guard is necessary, yet W plainly does not always happen. `B` shows
          up positive-only, and that is the tell.

        Known limit, and the reason ``partial``'s ABSENCE is not a guarantee: the same
        loop-history effect that correctly cancels a reconverged branch will also cancel
        a genuine disjunction *inside* a loop, so a two-IF disjunction in a loop body
        reports its loop guard and stays silent about the rest. The conditions listed
        stay true; the claim "these are all of them" is best-effort. Stated in the
        output's own note rather than left for a reader to discover.

        The test runs on the packed masks (`_partial_bits`) and MAY is never decoded.
        MUST is an intersection and stays small, but MAY is a union: deep in a program
        it holds most of the guard vocabulary, and unpacking it into a set once per
        state and once per row - 35 million bits on an 8,400-state program, to answer a
        yes/no - was most of the lineage stage's time. The answer is memoised per state.
        """
        got = self._cond_memo.get(state)
        if got is not None:
            return got
        must_bits = self.must.get(state) or 0
        may_bits = self.may.get(state) or 0
        got = (self._cond_set_id(self._conds(must_bits)),
               self._partial_bits(must_bits, may_bits, self.pair_lo))
        self._cond_memo[state] = got
        return got

    def _cond_id(self, name: str, negated: bool) -> int:
        """Intern one ``(guard, negated)`` predicate. Everything else about it - the
        rendered expression, its kind, its line - is derived from those two, so they
        are the whole identity."""
        key = (name, negated)
        got = self._cond_ids.get(key)
        if got is not None:
            return got
        tree = self.guards.get(name)
        d: dict = {"guard": name, "negated": negated}
        text = _cond_text(name, tree, negated)
        if text is None:
            # ALTER switches, GO TO DEPENDING ON and friends: the branch is real and
            # its existence is a fact, but no expression was recovered for it. Name
            # it and say so - never invent the test.
            d["unrecoverable"] = True
        else:
            d["expr"] = text
            d["kind"] = ("control" if _is_control_guard(name, tree, self.guard_kinds)
                         else "business")
        if name in self.guard_line:
            d["line"] = self.guard_line[name]
        cid = len(self._cond_defs)
        self._cond_ids[key] = cid
        self._cond_defs.append(d)
        return cid

    def _cond_set_id(self, conds: CondSet) -> Optional[int]:
        """Intern a whole condition set. ``None`` for the empty set: an absent key and
        an empty list say the same thing, and the absent one costs nothing."""
        if not conds:
            return None
        members = tuple(self._cond_id(name, negated) for name, negated in sorted(conds))
        got = self._set_ids.get(members)
        if got is not None:
            return got
        sid = len(self._set_defs)
        self._set_ids[members] = sid
        self._set_defs.append(list(members))
        return sid

    # -- control-flow graph (PERFORM followed as call + return) --------------
    def _perform_of(self, st: dict) -> Optional[str]:
        for a in st.get("entry", []) or []:
            if a.startswith("perform_"):
                return _perform_target(a, self.ordered, self.sections, self._index)
        return None

    def _edges(self, st: dict) -> List[str]:
        # always + on targets (the faithful config has no invoke here), bare-string
        # handler targets included - see emitter.iter_transitions / edge_target.
        return [t for _ev, e in iter_transitions(st) if (t := edge_target(e))]

    def _successors(self) -> Dict[str, List[str]]:
        """Successor map. A PERFORM node enters its target; every node that would leave
        the performed paragraph's extent returns to the call site's continuation.

        Returns merge across call sites (context-insensitive) - sound for provenance: it
        can name an extra origin, never miss one. Flagged when it actually happens.

        The exits of a performed extent are found ONCE per distinct target and replayed
        per call site. This used to rescan every state of the program for every PERFORM
        node - sites x states, with a target re-resolved through `ordered.index` each
        time - which is what made a program written in the ``PERFORM p THRU p-EXIT``
        house style, with a PERFORM per paragraph, quadratic in its own length: 8.4
        million extent tests on an 8,400-state program, for a call graph whose answer
        is linear in the states plus the sum of the extents.

        What must not move, because it is byte-visible downstream: the outer loop still
        walks `self.states` in order, so the unresolved flags, the `multi` counts and
        `succ`'s key order are as before; the exits of an extent are visited in
        `self.states` order (sorted by position, never by iterating the `owner` SET), so
        on overlapping extents the same call site is still the first to reach a shared
        exit - and `returns[s2]` / `fold_src[s2]` are seeded by whichever site gets
        there first, which `_edge_conditions` turns into conditions; and every `succ`
        list keeps its order, which drives the worklist order and with it the order the
        fixpoint's own flags are appended in.
        """
        succ: Dict[str, List[str]] = {}
        returns: Dict[str, List[str]] = {}   # owner-exit node -> continuations
        multi: Dict[str, int] = {}
        states = self.states
        # Once per state, in order: its PERFORM target (if it is a call node), its edges,
        # and the paragraph it belongs to (a `_split` segment belongs to its origin).
        target_of = {s: self._perform_of(st) for s, st in states.items()}
        edges_of = {s: self._edges(st) for s, st in states.items()}
        para_of = {s: _para_of(self.origin_state.get(s, s)) for s in states}
        by_para: Dict[str, List[str]] = {}
        for s in states:
            by_para.setdefault(para_of[s], []).append(s)
        pos = {s: i for i, s in enumerate(states)}
        owner_of: Dict[str, Tuple[Optional[set], Optional[str]]] = {}
        exits_of: Dict[str, List[Tuple[str, List[str], List[str]]]] = {}

        def exits(target: str, owner: Set[str]) -> List[Tuple[str, List[str], List[str]]]:
            """``(exit node, in-range edges, leaving edges)`` for every node of the
            extent that falls out of it or ends, in `self.states` order."""
            got = exits_of.get(target)
            if got is not None:
                return got
            got = []
            extent = sorted((s2 for p in owner for s2 in by_para.get(p, ())),
                            key=pos.__getitem__)
            for s2 in extent:
                if target_of[s2]:            # a call node inside the range is not an exit
                    continue
                edges = edges_of[s2]
                # Only the edges that LEAVE the performed range are returns. The ones
                # that stay are ordinary control flow inside the paragraph and must
                # survive: a node can do both at once - `IF X ... END-IF` as a
                # paragraph's last statement branches inward on X and falls out of the
                # range otherwise. Replacing the whole list (as this once did) deleted
                # the inward branch, and with it every event inside the IF.
                stay = [t for t in edges if t in states and para_of[t] in owner]
                leaves = [t for t in edges if t not in states or para_of[t] not in owner]
                if leaves or not edges:      # falls out of the range, or ends
                    got.append((s2, stay, leaves))
            exits_of[target] = got
            return got

        for name in states:
            target = target_of[name]
            if not target:
                succ[name] = [t for t in edges_of[name] if t in states]
                continue
            if target not in owner_of:
                owner_of[target] = _target_owner(target, self.ordered, self.sections,
                                                 self._index)
            owner, init = owner_of[target]
            cont = edges_of[name][0] if edges_of[name] else None
            if owner is None or init not in states:
                # WHY it failed matters to whoever reads the flag. A THRU range whose
                # endpoints both exist but run backwards is not a missing paragraph, and
                # reporting it as one sends a reader hunting for a name that is right
                # there in the source.
                kind, why = "perform-target-unresolved", "target unresolved"
                if "__THRU__" in target:
                    head, tail = target.split("__THRU__", 1)
                    if head in self.ordered and tail in self.ordered:
                        kind = "perform-range-inverted"
                        why = (f"range runs backwards - {head} appears after {tail} in "
                               f"the source, so the range is empty")
                self._unresolved[target] = kind
                self._flag(f"PERFORM {target}: {why}; the call is not "
                           f"followed - origins produced inside it are not traced")
                succ[name] = [t for t in edges_of[name] if t in states]
                continue
            succ[name] = [init]                      # enter the call
            multi[target] = multi.get(target, 0) + 1
            if cont is None:
                continue
            for s2, stay, leaves in exits(target, owner):    # wire the returns
                if s2 not in returns:
                    returns[s2] = list(stay)
                    self.fold_src[s2] = list(leaves)
                if cont not in returns[s2]:
                    returns[s2].append(cont)
                    self.fold_dst.setdefault(s2, []).append(cont)
        for s, conts in returns.items():
            succ[s] = list(conts)                    # in-range edges + the return
        for target, n in multi.items():
            if n > 1:
                self._flag(f"{target} is PERFORMed from {n} sites; it is analyzed with "
                           f"the merged incoming state (context-insensitive), so an "
                           f"origin from one call site may appear at an event reached "
                           f"only via another - over-approximate, verify")
        return succ

    # -- transfer -----------------------------------------------------------
    def _apply(self, name: str, st: dict, cur: State,
               rows: Optional[List[dict]]) -> State:
        """Run a state's entry actions over the origin map ``cur`` - the visit's own
        working map (an `_Overlay`, or a `_materialize`d copy), updated in place and
        returned. When
        ``rows`` is given, emit a row for every field of every event the state
        performs."""
        for aname in st.get("entry", []) or []:
            prov = self.provenance.get(aname, {})
            cobol = prov.get("cobol", "")
            line = prov.get("line", 0)
            spec = self.actions.get(aname)
            hits = self._classify_memo.get(aname)
            if hits is None:
                hits = _iface._classify(aname, cobol, spec, self.dv, self.files,
                                        self.cursors, self.cursor_cols,
                                        self.cursor_derivs, self.cursor_refs)
                self._classify_memo[aname] = hits
            got = [h for h in hits if h["direction"] == "get"]
            made = [h for h in hits if h["direction"] == "create"]
            # A MOVE / COMPUTE / SET into a LINKAGE (COMMAREA) field or RETURN-CODE is the
            # caller-visible OUTPUT of a subprogram, but the event classifier does not see
            # it as one - so a subprogram whose whole result IS its COMMAREA produced an
            # EMPTY lineage table, while the interface overlay (which runs exactly this
            # fallback) listed every field. Add the CREATE side here so those rows exist.
            # The GET side needs nothing: `_seed` already marks every linkage item
            # caller-originated at entry, so the data-movement pass threads it, and adding
            # per-action get hits would trip the `if got: continue` guard below and break
            # that threading.
            if not hits and spec:
                made = [h for h in _iface._classify_dataflow(spec, self.linkage)
                        if h["direction"] == "create"]
            # Every I/O statement on a file sets its FILE STATUS field (ledger item
            # 160): the file system's response, the same event the interface publishes
            # for a branch on it. Kept apart from `got`, which would skip a WRITE ...
            # FROM's store; CLOSE is classified as no crossing, so it is read here.
            status = self._status_fills(hits, cobol)
            # An EXEC statement's writes inside the program (FORMATTIME, Db2 SET /
            # VALUES INTO). Their sources' origins are read now, before step 1 fills
            # this statement's own targets: Db2 evaluates every expression first.
            writes = []
            if spec and spec.get("kind") in ("exec-cics", "exec-sql"):
                ex = self._exec_writes_memo.get(aname)
                if ex is None:
                    ex = _iface._exec_writes(aname, cobol, spec)
                    self._exec_writes_memo[aname] = ex
                for tgt, srcs in ex:
                    srcs = [s for s in srcs if s in self.known]
                    acc: Set[Origin] = set()
                    for s in srcs:
                        acc |= self._read(s, cur)
                    writes.append((tgt, srcs, acc))

            # 1. an INPUT event fills its fields: they originate HERE. A GROUP field
            # (a VARCHAR host structure, whose one host variable is the parent of a
            # length/text pair) fills its elementary children too: a MOVE out of the
            # text child must find the event, not "internally set". And its FILLER
            # bytes: a group reads as its leaves and FILLERs (`_read`), so a fill that
            # left `G#FILLER` holding an older `MOVE SPACES TO G` made `ACCEPT G` /
            # `MOVE G TO T` say T was "internally set".
            for h in got + status:
                ev = _iface._event("get", h["etype"], h["endpoint"])
                for f in h["fields"]:
                    fu = f.upper()
                    filled = frozenset({(ev, False, None)})
                    values = {fu: filled}
                    for leaf in self.dv.leaves(fu):
                        if leaf != fu:
                            values[leaf] = filled
                    for key in self.sub_fillers.get(fu, ()):
                        values[key] = filled
                    self._put(values, cur)
                if rows is not None:
                    for f in h["fields"]:
                        rows.append(self._row(name, h, "input", f, cur, aname, line))
                        self.fills.append({
                            "field": f.upper(), "event": ev, "endpoint": h["endpoint"],
                            "endpointType": h["etype"], "verb": h["verb"],
                            "state": name,
                            "baseState": self.origin_state.get(name, name),
                            "line": line, "cobol": cobol,
                            # host variable -> COLUMN. A host-variable name is
                            # program-local; the column is the database's, and it is the
                            # column a reader has to go and look at. A list of
                            # {table, column, hostVar} rows, and an empty LIST when there
                            # are none: a published key must not change JSON type.
                            "columns": h.get("columns") or [],
                        })

            # 2. data movement: the target inherits its operands' origins.
            if spec and spec.get("assignments"):
                for a in spec["assignments"]:
                    tgt = (a.get("target") or "").upper()
                    if not tgt:
                        continue
                    base = tgt.split("(")[0].strip()
                    refmod = _refmod(tgt)
                    srcs = _operands(a.get("expr", ""), self.known)
                    acc: Set[Origin] = set()
                    for s in srcs:
                        acc |= self._read(s, cur)
                    if got:      # e.g. SELECT ... INTO: already set by the event above
                        continue
                    if refmod:
                        # a partial write: the bytes outside the range keep what the
                        # field held, so its origins are the union, never a replacement
                        acc |= self._read(base, cur)
                        self._store(base, frozenset(acc), cur)
                    elif base in self.groups:
                        self._group_store(base, a.get("expr", ""), srcs, acc, cur,
                                          line)
                    else:
                        self._store(base, frozenset(acc), cur)
                    if rows is not None:
                        rec = {
                            "target": base, "sources": list(srcs), "state": name,
                            "baseState": self.origin_state.get(name, name),
                            "line": line, "cobol": cobol, "action": aname,
                        }
                        if refmod:
                            rec["refmod"] = refmod
                        self.flow.append(rec)

            # 2b. SET ADDRESS OF X TO P: X becomes the storage P points at. No value
            # moves, so X is an ALIAS of what P addresses - never a value flow from the
            # pointer, and never a caller output. Where P cannot be followed, say why.
            if spec and spec.get("kind") == "address"                     and spec.get("pointer") not in ("NULL", "NULLS"):
                ptr = spec.get("pointer") or ""
                aliased, why = self._address_target(ptr)
                acc = (set(self._origins_of(aliased, cur)) if aliased
                       else {(_UNKNOWN, True, None)})
                for x in spec.get("addressOf") or []:
                    if x not in self.known:
                        continue
                    cur[x] = frozenset(acc)
                    for leaf in self.dv.leaves(x):
                        cur[leaf] = cur[x]
                    for key in self.sub_fillers.get(x, ()):
                        cur[key] = cur[x]
                    if why:
                        self._flag(f"SET ADDRESS OF {x} TO {ptr}: the storage {x} "
                                   f"addresses cannot be followed - {why}")
                    if rows is not None:
                        rec = {
                            "target": x, "sources": [aliased] if aliased else [],
                            "alias": True, "state": name,
                            "baseState": self.origin_state.get(name, name),
                            "line": line, "cobol": cobol, "action": aname,
                        }
                        if why:
                            rec["unknownReason"] = why
                        self.flow.append(rec)

            # 3. opaque-but-traceable verbs: dependency only, value not modeled.
            if spec and spec.get("kind") == "effect":
                verb = (spec.get("verb") or "").upper()
                if verb in _DEP_ONLY:
                    recv, srcs = _dep_only_flow(verb, cobol, self.known)
                    if recv:
                        acc = set()
                        for s in srcs:
                            acc |= self._read(s, cur)
                        # STRING ... INTO X(a:b) is the same partial write a MOVE into
                        # a range is, and is published the same way
                        refmod = None
                        if verb == "STRING":
                            m = _STRING_RE.search(mask_literals(cobol.upper()))
                            refmod = _refmod(m.group(2)) if m else None
                        for r in recv:
                            held = self._read(r, cur) if refmod else set()
                            self._store(r, frozenset(acc | held), cur)
                            # the same record step 2 writes, so walking `flow` back
                            # from a field these verbs fill does not stop at it
                            if rows is not None:
                                rec = {
                                    "target": r, "sources": list(srcs), "state": name,
                                    "baseState": self.origin_state.get(name, name),
                                    "line": line, "cobol": cobol, "action": aname,
                                }
                                if refmod:
                                    rec["refmod"] = refmod
                                self.flow.append(rec)

            # 3b. the EXEC writes: dependency only, like step 3. A target this statement
            # also read from the clock in step 1 (`CURRENT DATE - :N DAYS`) keeps that
            # origin beside its sources'.
            if writes:
                filled = {f.upper() for h in got for f in h["fields"]}
                for tgt, srcs, acc in writes:
                    if tgt in filled:
                        acc = acc | self._read(tgt, cur)
                    self._store(tgt, frozenset(acc), cur)
                    if rows is not None:
                        self.flow.append({
                            "target": tgt, "sources": list(srcs), "state": name,
                            "baseState": self.origin_state.get(name, name),
                            "line": line, "cobol": cobol, "action": aname,
                        })

            # 4. CALL ... USING is BY REFERENCE: the callee may rewrite an argument.
            for h in made:
                if h["etype"] == "program" and h["fields"]:
                    ev = _iface._event("create", h["etype"], h["endpoint"])
                    for f in h["fields"]:
                        fu = f.upper()
                        if fu not in self.known:
                            continue
                        # every field of a group argument: the callee may rewrite any
                        for x in ([fu] + self.groups.get(fu, [])
                                  + self.sub_fillers.get(fu, [])):
                            cur[x] = frozenset(set(cur.get(x, frozenset()))
                                               | {(ev, True, h["endpoint"])})

            # 5. an OUTPUT event emits fields: report what reaches it.
            if rows is not None:
                for h in made:
                    for f in h["fields"]:
                        rows.append(self._row(name, h, "output", f, cur, aname, line))
                    # 6. an UNRESOLVED dynamic target. The "endpoint" here is the DATA
                    # ITEM whose run-time value names the real resource, so the question
                    # the manifest cannot answer - which program does this call? - turns
                    # into one this analysis can: where does that item's value come from?
                    # Snapshot the origins reaching it AT THE CALL, which is why this is
                    # recorded here and not from the state's entry map: an assignment
                    # earlier in the same state changes the answer.
                    if h.get("dynamic"):
                        self.dynamic_sites.append({
                            "item": str(h["endpoint"]).upper(),
                            "endpointType": h["etype"], "verb": h["verb"],
                            "state": name,
                            # Same synthetic-id problem as a lineage row: `name` is a
                            # `_split` segment whenever the paragraph had a mid-run
                            # PERFORM, and no other view splits. `fills` and `flow` carry
                            # it too now that the lineage view publishes them;
                            # `dynamic_calls` reads only named keys off those records.
                            "baseState": self.origin_state.get(name, name),
                            "line": line, "cobol": cobol,
                            "candidates": list(h.get("candidates") or []),
                            "origins": self._origins_of(str(h["endpoint"]), cur),
                        })
        return cur

    _CLOSE = re.compile(r"^\s*CLOSE\b(.*)$", re.I | re.S)

    def _status_fills(self, hits: List[dict], cobol: str) -> List[dict]:
        """A ``get`` RESPONSE hit filling the FILE STATUS field of each file this
        statement does I/O on - one per file, whatever its hits' directions."""
        named = [h["endpoint"] for h in hits if h["etype"] == _iface._FILE]
        m = self._CLOSE.match(cobol or "")
        if m:
            named += [w for w in _WORD.findall(mask_literals(m.group(1)).upper())
                      if w in self.files]
        out = []
        for f in dict.fromkeys(named):
            sf = str((self.files.get(f) or {}).get("statusField") or "").upper()
            if sf in self.known:
                out.append(_iface._hit("get", _iface._RESPONSE, f,
                                       f"file status ({sf})", [sf]))
        return out

    _ADDRESS_OF = _iface._ADDRESS_OF_EXPR

    def _address_target(self, ptr: str) -> Tuple[Optional[str], Optional[str]]:
        """``(item, None)`` when the pointer operand of a SET ADDRESS OF provably
        addresses one item of this program, else ``(None, reason)``. ``ptr`` is either
        ``ADDRESS OF Y`` itself or a pointer P; P is followed only when every write to
        it in the program is ``SET P TO ADDRESS OF`` one and the same item - the program
        as a whole, not the path, so two different addresses are reported, not chosen
        between."""
        m = self._ADDRESS_OF.match(ptr)
        if m:
            y = m.group(1)
            return (y, None) if y in self.known else (
                None, f"{y} is not a data item of this program")
        if getattr(self, "_ptr_index", None) is None:
            self._ptr_index = _iface.address_index(self.actions, self.provenance)
        addr, other = self._ptr_index
        p = ptr.split("(")[0].strip()
        ys, others = sorted(addr.get(p, ())), sorted(other.get(p, ()))
        if others:
            return None, f"{p} is set by {'; '.join(others)}"
        if len(ys) == 1:
            return (ys[0], None) if ys[0] in self.known else (
                None, f"{ys[0]} is not a data item of this program")
        if ys:
            return None, (f"{p} is set to the address of more than one item "
                          f"({', '.join(ys)})")
        if p in self.linkage or p in self.own_linkage:
            return None, f"{p} is a LINKAGE item: the address arrives from the caller"
        return None, f"{p} is never set to an address in this program"

    def _origins_of(self, field: str, cur: State) -> List[Origin]:
        """Origins reaching a field. A GROUP item has no assignments of its own - it is
        the union of its elementary children (moving a group moves all of them)."""
        return sorted(self._read(field, cur))

    def _read(self, name: str, cur: State) -> Set[Origin]:
        """The origins a read of ``name`` sees. A group's value is its fields' bytes, so
        a group reads as the union over its leaves and FILLER keys (``group_reads``),
        ALWAYS - never its own entry: every write to a group also writes those keys
        (``_store``, the input fill, SET ADDRESS OF, the LINKAGE seed), so a leaf
        rewritten since is never hidden behind an older entry for the group.

        It used to fall back to the group's own entry while none of those keys was
        set, and switch to their union once one arrived. That switch made the read -
        and so the fixpoint's transfer - non-monotone: an input fill wrote G but not
        ``G#FILLER``, a ``MOVE SPACES TO G`` on the back edge then brought an EMPTY
        ``G#FILLER`` in, and the read dropped the fill's origin (a published "T is
        internally set" after ``ACCEPT G`` / ``MOVE G TO T``). A union of keys is
        monotone in the map by construction, which is what `run`'s termination
        argument rests on. Callers only read the result: a field's own (immutable)
        entry is returned as it is, not copied."""
        fu = name.upper()
        keys = self.group_reads.get(fu)
        if keys is not None:
            acc: Set[Origin] = set()
            acc.update(*[v for v in cur.many(keys) if v is not None])
            return acc
        return cur.get(fu, _NO_ORIGINS)

    def _subgroups(self, name: str) -> List[str]:
        """``name`` and every item nested under it (FILLERs and 88-levels aside)."""
        out: List[str] = []
        seen: Set[str] = set()
        stack = [name]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            out.append(n)
            stack.extend(k for k in self.dv.children.get(n, ()) if k != _FILLER)
        return out

    def _store(self, name: str, origins: OriginSet, cur: State) -> None:
        """Write ``origins`` to ``name`` and, for a group, to every field under it - the
        coarse write, for a value whose bytes cannot be placed field by field."""
        values = {name: origins}
        for leaf in self.groups.get(name, ()):
            values[leaf] = origins
        for key in self.sub_fillers.get(name, ()):
            values[key] = origins
        self._put(values, cur)

    def _put(self, values: Dict[str, OriginSet], cur: State) -> None:
        """``cur.put(values)``, carried across every REDEFINES of a written item: the
        alias gains the store's origins as a UNION, never a replacement - it may cover
        bytes this store does not reach. A union keeps the transfer monotone."""
        if self.aliases:
            extra: Dict[str, Set[Origin]] = {}
            for written, origins in values.items():
                for a in self.aliases.get(written, ()):
                    if a not in values:
                        extra.setdefault(a, set()).update(origins)
            for a, origins in extra.items():
                values[a] = frozenset(origins.union(cur.get(a) or ()))
        cur.put(values)

    def _redefines_aliases(self) -> Dict[str, Tuple[str, ...]]:
        """Every elementary item -> the elementary items of the OTHER alternatives of
        the same bytes. An alternative is the redefined item itself, each named item
        that REDEFINES it, and each ``FILLER REDEFINES`` it (whose named items its
        ``members`` list, ledger item 159). Pairs are placed by byte offset within the
        alternative when every item of both can be sized; otherwise every item of one
        is an alias of every item of the other - a superset, the safe direction."""
        bases: Dict[str, List[Tuple[str, dict]]] = {}
        for n, e in self.data.items():
            if not isinstance(e, dict) or n == _FILLER:
                continue
            # A name declared more than once keeps each declaration (item 154), and
            # the REDEFINES may be on one that is not the entry's own: `15 X REDEFINES
            # Y.` holding `20 X PIC X(9).` keys the 20 (ledger item 169).
            for d in e.get("declarations") or (e,):
                if d.get("redefines"):
                    bases.setdefault(str(d["redefines"]).upper(), []).append((n, d))
        stack = [f for e in self.data.values() if isinstance(e, dict)
                 for f in e.get("fillers") or ()]
        while stack:
            f = stack.pop()
            stack.extend(f.get("fillers") or ())
            if f.get("redefines"):
                bases.setdefault(str(f["redefines"]).upper(), []).append((_FILLER, f))
        if not bases:
            return {}
        if self._kids is None:
            self._kids = children_index(self.data)
        out: Dict[str, Set[str]] = {}
        for base in sorted(bases):
            entry = self.data.get(base)
            if not isinstance(entry, dict) or base not in self.known:
                continue
            alts = [self._alternative(base, entry)] + [
                self._alternative(n, e) for n, e in
                sorted(bases[base], key=lambda p: int(p[1].get("line") or 0))]
            for i, (leaves_a, spans_a) in enumerate(alts):
                for leaves_b, spans_b in alts[i + 1:]:
                    placed = spans_a is not None and spans_b is not None
                    for a in leaves_a:
                        for b in leaves_b:
                            if a == b:
                                continue
                            if placed and not (spans_a[a][0] < spans_b[b][1]
                                               and spans_b[b][0] < spans_a[a][1]):
                                continue
                            out.setdefault(a, set()).add(b)
                            out.setdefault(b, set()).add(a)
        return {k: tuple(sorted(v)) for k, v in sorted(out.items())}

    def _alternative(self, name: str, entry: dict
                     ) -> Tuple[List[str], Optional[Dict[str, Tuple[int, int]]]]:
        """``(elementary items, {item: (start, end)} or None)`` of one alternative of a
        REDEFINES, offsets counted from the alternative's first byte. ``None`` when an
        item of it cannot be sized, or the alternative holds a REDEFINES of its own."""
        leaves: List[str] = []
        spans: Dict[str, Tuple[int, int]] = {}
        provable = [True]

        def kids(n: str, e: dict) -> List[Tuple[str, dict]]:
            if n == _FILLER:      # a FILLER object: its members and its own FILLERs
                named = [(k, d) for k, d in self._kids.get(_FILLER, ())
                         if k != _FILLER and d.get("parentLine") == e.get("line")]
            else:
                named = [(k, d) for k, d in self._kids.get(n.upper(), ()) if k != _FILLER]
            named = [(k, d) for k, d in named if d.get("kind") != "condition-name"]
            merged = named + [(_FILLER, f) for f in e.get("fillers") or ()]
            return sorted(merged, key=lambda p: int(p[1].get("line") or 0))

        def walk(n: str, e: dict, offset: int, root: bool,
                 above: FrozenSet[str] = frozenset()) -> int:
            if not root and e.get("redefines"):
                provable[0] = False
            if e.get("occursDependingOn") or e.get("sync"):
                provable[0] = False
            try:
                times = int(e.get("occurs") or 1)
            except (TypeError, ValueError):
                times, provable[0] = 1, False
            # Children are found by NAME, so an item named like a group it sits in
            # (`15 X REDEFINES Y. 20 X PIC X(9).`) would be handed that group's
            # children again, forever (ledger item 169): lay it out from its own
            # declaration. If that does not size it, the layout is unprovable and the
            # every-item-aliases-every-item fallback applies.
            key = n.upper()
            ks = [] if n != _FILLER and key in above else kids(n, e)
            if ks:
                cur = offset
                inner = above | {key} if n != _FILLER else above
                for k, d in ks:
                    nxt = walk(k, d, cur, False, inner)
                    if not d.get("redefines"):
                        cur = nxt
                return offset + (cur - offset) * times
            size, _why = item_size(e)
            if size is None:
                provable[0] = False
                size = 0
            if n != _FILLER:
                leaves.append(n.upper())
                spans[n.upper()] = (offset, offset + size * times)
            return offset + size * times

        walk(name, entry, 0, True)
        leaves = list(dict.fromkeys(leaves))
        return leaves, (spans if provable[0] else None)

    _BARE_REF = re.compile(r"^([A-Z0-9][A-Z0-9-]*)(?:\s+(?:OF|IN)\s+[A-Z0-9][A-Z0-9-]*)*$")

    def _group_store(self, tgt: str, expr: str, srcs: List[str], acc: Set[Origin],
                     cur: State, line: int) -> None:
        """A store into a GROUP. COBOL moves a group byte for byte, so each field of
        the target receives exactly the source bytes laid over it: placed through both
        byte layouts (``storage.record_layout``) when both are provable, and target bytes
        past the source's end are spaces - no origin. Where the bytes cannot be placed
        (a layout refused, or a source that is not one data item), every field of the
        target is given every origin of the source, and a flag says so: a superset is
        the safe direction for provenance, but it is not the answer, and must not look
        like one."""
        m = self._BARE_REF.match(re.sub(r"\s+", " ", (expr or "").strip().upper()))
        if not srcs:
            self._store(tgt, frozenset(), cur)        # a literal or figurative constant
            return
        why = None
        if m and m.group(1) == srcs[0] and len(srcs) == 1:
            placed, why = self._place_bytes(srcs[0], tgt, cur)
            if placed is not None:
                values = {tgt: frozenset(acc)}
                for leaf in self.groups[tgt]:
                    values[leaf] = placed.get(leaf, _NO_ORIGINS)
                filler = placed.get(_FILLER, _NO_ORIGINS)
                for key in self.sub_fillers[tgt]:
                    values[key] = filler
                self._put(values, cur)
                return
        else:
            why = "the source is an expression, not one data item"
        self._store(tgt, frozenset(acc), cur)
        self._flag(f"line {line}: the store into group {tgt} cannot be placed field by "
                   f"field - {why} - so every field of {tgt} is given every origin of "
                   f"{', '.join(srcs)}")

    def _place_bytes(self, src: str, tgt: str,
                     cur: State) -> Tuple[Optional[Dict[str, OriginSet]], Optional[str]]:
        """``({target leaf: origins}, None)`` from the two byte layouts, or ``(None,
        why)`` when either layout is refused."""
        plan = self._placements.get((src, tgt))
        if plan is None:
            plan = self._placements[(src, tgt)] = self._placement(src, tgt)
        names, groups, overlaps, why = plan
        if why is not None:
            return None, why
        # a field's own entry is already a frozenset; a group source reads its leaves
        origins = [_NO_ORIGINS if v is None else v for v in cur.many(names)]
        for i, n in groups:
            origins[i] = frozenset(self._read(n, cur))
        placed: Dict[str, OriginSet] = {}
        for name, hits in overlaps:
            if len(hits) == 1:                # the usual case: one source field
                acc = origins[hits[0]]
            else:
                acc = frozenset().union(*(origins[i] for i in hits))
            prev = placed.get(name)
            placed[name] = acc if prev is None else prev | acc
        return placed, None

    def _placement(self, src: str, tgt: str):
        """Which source fields' bytes each target field overlaps, by index into the
        source fields - ``(source names, [(i, group source name)], [(target name,
        [i, ...])], None)`` - or ``(None, None, None, why)`` when either layout is
        refused. The layouts and so the
        overlaps are fixed for the run: computed once per (source, target) pair, never
        at each visit of the store (re-deriving them per visit was a layout - a scan
        of the whole data dictionary per group node - at every group MOVE the fixpoint
        ran, which took a 2,172-line program's lineage from 0.05 s to 20 s)."""
        if self._kids is None:
            self._kids = children_index(self.data)
        for rec in (src, tgt):
            if rec not in self._layouts:
                self._layouts[rec] = record_layout(self.data, rec, children=self._kids)
        lay_s, lay_t = self._layouts[src], self._layouts[tgt]
        for lay in (lay_s, lay_t):
            if not lay.get("provable"):
                return None, None, None, (
                    f"the byte layout of {lay['record']} is not provable "
                    f"({lay.get('reason', 'no reason given')})")
        spans = [(f["offset"], f["offset"] + f["length"]) for f in lay_s["fields"]]
        overlaps = []
        for f in lay_t["fields"]:
            lo, hi = f["offset"], f["offset"] + f["length"]
            overlaps.append((f["name"], [i for i, (s_lo, s_hi) in enumerate(spans)
                                         if s_lo < hi and lo < s_hi]))
        names = [f["name"].upper() for f in lay_s["fields"]]
        groups = [(i, n) for i, n in enumerate(names) if n in self.group_reads]
        return names, groups, overlaps, None

    def _changed_by(self, field: str) -> List[dict]:
        """Assignments writing this field; for a group, those writing any child.

        Memoised per field: a field crosses the boundary once per event that carries
        it, and each row asked again - for a record-level group that is every child's
        write list, re-merged and re-keyed per row."""
        fu = field.upper()
        got = self._changed_memo.get(fu)
        if got is not None:
            return got
        out = list(self.changers.get(fu, []))
        if not out:
            # Same dedup as the `changers` build, and the same reason for a `seen` set
            # rather than `if e not in out`: a wide group merges every child's writes,
            # each comparison walking a nested `conditions` list.
            seen: Set[str] = set()
            for l in self.dv.leaves(fu):
                for e in self.changers.get(l, []):
                    k = _entry_key(e)
                    if k in seen:
                        continue
                    seen.add(k)
                    out.append(e)
        self._changed_memo[fu] = out
        return out

    def _row(self, state: str, hit: dict, direction: str, field: str,
             cur: State, aname: str, line: int) -> dict:
        fu = field.upper()
        item = self.data.get(fu) or {}
        typ = (item.get("type") or {})
        origins = self._origins_of(fu, cur)
        changed = self._changed_by(fu)
        row = {
            # `program` is on every row, not just at the top of the file: rows from many
            # programs get concatenated to answer "what touches this state?", and a
            # top-level field does not survive that.
            "program": self.m.program_id,
            "event": _iface._event(hit["direction"], hit["etype"], hit["endpoint"]),
            "direction": direction,
            "endpoint": hit["endpoint"],
            "endpointType": hit["etype"],
            "verb": hit["verb"],
            "state": state,
            # The REAL state this segment came from. `state` is a SYNTHETIC id whenever
            # `_split` fired - a paragraph with a mid-run PERFORM before an EXEC SQL
            # becomes `p__L1` / `p__L2` / `p__Lend` - and `build_interface` performs no
            # such split, so `state` alone cannot be joined to the interface event for
            # the SAME statement. `line` does not rescue it either: action names are
            # content-derived and globally deduplicated, so two textually identical
            # statements in different states share one provenance line.
            #
            # Always emitted, equal to `state` when `_split` did not fire, so a consumer
            # joins on one key unconditionally instead of special-casing the shape.
            "baseState": self.origin_state.get(state, state),
            "line": line,
            "field": fu,
            "pic": typ.get("pic") or typ.get("category"),
            "section": item.get("section"),
            "changedByProgram": bool(changed),
            "origins": [
                {"event": e, **({"maybe": True, "resolvedBy": r} if m else {})}
                for (e, m, r) in origins if e != _UNKNOWN
            ],
        }
        # The cross-program identity keys. `field` alone is a program-LOCAL name: A's
        # WS-BALANCE and B's CUST-BAL may be the same state, or unrelated. What proves
        # sameness is a shared declaration - the copybook the field came from, or the
        # file whose record it belongs to. Emitted only when the code actually proves
        # it; a field declared inline has neither, and is honestly unresolvable.
        if item.get("member"):
            row["member"] = item["member"]
        if item.get("file"):
            row["file"] = item["file"]
        # Under what condition does this event happen at all? For an output row that is
        # "when is this field written out", for an input row "when is it read" - and
        # that when-clause is the business rule the row is otherwise missing.
        sid, partial = self._conditions_of(state)
        if sid is not None:
            row["conditionSetId"] = sid
        if partial:
            row["conditionsPartial"] = True
            row["conditionsNote"] = (
                "reached under differing conditions on different paths; no single "
                "condition holds on all of them, so the full condition is a disjunction "
                "this table does not state - read the machine for the exact branches"
            )
        if changed:
            # The write sites are a fact about the FIELD, the same list in every row about
            # it - and repeated per row they were 70% of a large lineage file. Stated once
            # in the document's `changedBy` table, under `field`.
            self._changed_fields.add(fu)
        if any(e == _UNKNOWN for e, _m, _r in origins):
            row["unknown"] = True
            row["unknownReason"] = ("value passes through a construct whose data effect "
                                    "is not modeled; origin cannot be traced")
        if direction == "output" and not row["origins"] and not row.get("unknown"):
            row["note"] = "no external origin reaches this field (internally set)"
        return row

    # -- the fixpoint -------------------------------------------------------
    def _seed(self) -> State:
        """The origin map at program entry.

        LINKAGE items arrive already filled *by the caller* - that is the whole point of
        a parameter - so they start out originating from the caller. Everything else
        starts with no external origin (its VALUE clause is internal).
        """
        seed: State = {}
        caller = _iface._event("get", "caller", "CALLER")
        for n, it in self.data.items():
            if isinstance(it, dict) and it.get("section") == "LINKAGE" \
                    and it.get("kind") != "condition-name" \
                    and n.upper() not in self.own_linkage:
                seed[n.upper()] = frozenset({(caller, False, None)})
                # and its FILLER bytes, which the caller filled too: a group reads
                # as its leaves and FILLER keys only (`_read`)
                for key in self.sub_fillers.get(n.upper(), ()):
                    seed[key] = seed[n.upper()]
        return seed

    def _changers(self) -> Dict[str, List[dict]]:
        """field -> the assignments in THIS program that write it.

        An input event's own fill (ACCEPT / SELECT INTO) is NOT a change by this
        program - the value came from outside; the program only received it.

        Each write carries the conditions it happens under, which is what turns "this
        program writes the balance" into "this program writes the balance WHEN the
        transaction is a deposit" - the difference between a dependency and a rule.
        """
        out: Dict[str, List[dict]] = {}
        # `if e not in out[t]` was a linear scan of NESTED-dict comparisons - an entry
        # carries the site's whole `conditions` list - repeated per assignment per
        # state, against a list that accumulates across every state writing that target.
        # A `seen` set beside the list keeps the ORDER (first occurrence wins, which is
        # what the output contract is) while making the membership test O(1).
        # The document's proposed `(state, line, action)` key cannot be built: these
        # entries have no `state`, only `action` / `line` / the condition keys - so the
        # key is the canonical form of the whole entry.
        seen: Dict[str, Set[str]] = {}

        def _record(target: str, entry_dict: dict) -> None:
            k = _entry_key(entry_dict)
            if k in seen.setdefault(target, set()):
                return
            seen[target].add(k)
            out.setdefault(target, []).append(entry_dict)

        for name, st in self.states.items():
            entry = self._write_entry(name)
            for aname in st.get("entry", []) or []:
                spec = self.actions.get(aname)
                prov = self.provenance.get(aname, {})
                if spec and spec.get("kind") == "input":
                    continue
                if spec and spec.get("assignments"):
                    for a in spec["assignments"]:
                        t = (a.get("target") or "").upper().split("(")[0]
                        if not t:
                            continue
                        _record(t, {"action": aname,
                                    "line": prov.get("line", 0), **entry})
                if spec and spec.get("kind") == "effect":
                    verb = (spec.get("verb") or "").upper()
                    if verb in _DEP_ONLY:
                        recv, _ = _dep_only_flow(verb, prov.get("cobol", ""), self.known)
                        for t in recv:
                            _record(t, {"action": aname,
                                        "line": prov.get("line", 0), **entry})
        return out

    def _write_entry(self, state: str) -> dict:
        """The condition keys for a write site. A site that no path reaches is marked
        rather than annotated: it has no path condition BECAUSE it has no path, and an
        absent `conditionSetId` there would read as "this always happens"."""
        if not self._reached(state):
            return {"unreachable": True}
        sid, partial = self._conditions_of(state)
        e: dict = {}
        if sid is not None:
            e["conditionSetId"] = sid
        if partial:
            e["conditionsPartial"] = True
        return e

    # -- states no path reaches ---------------------------------------------
    #
    # The fixpoint walks forward from the entry points, so a state nothing reaches is
    # never visited and emits no row. That is correct - with no path there are no
    # origins, and naming one would be the guess this tool refuses to make. What was
    # wrong is that it happened in SILENCE, while `build_interface` walks the config
    # structurally and maps every statement's columns whether a path reaches it or not.
    # The two views then disagreed about whether a program touched a table at all, with
    # nothing in either file to say why.

    def _events_of(self, st: dict) -> List[dict]:
        """The external events a state's entry run performs, without running the
        transfer over an origin map.

        Deliberately NOT `_apply`. That one appends to `fills` / `flow` /
        `dynamic_sites` whenever it is emitting, and those are the primitive facts the
        dynamic-call view reads back. Feeding it states no path reaches would put
        unreachable events into a downstream answer - the opposite of the fix.
        """
        out: List[dict] = []
        for aname in st.get("entry", []) or []:
            prov = self.provenance.get(aname, {})
            spec = self.actions.get(aname)
            hits = self._classify_memo.get(aname)
            if hits is None:
                hits = _iface._classify(aname, prov.get("cobol", ""), spec, self.dv,
                                        self.files, self.cursors, self.cursor_cols,
                                        self.cursor_derivs, self.cursor_refs)
                self._classify_memo[aname] = hits
            # Same fallback `_apply` makes: a write into a LINKAGE item is a
            # caller-visible output the event classifier does not recognize on its own.
            if not hits and spec:
                hits = [h for h in _iface._classify_dataflow(spec, self.linkage)
                        if h["direction"] == "create"]
            for h in hits:
                fields = sorted({f.upper() for f in h["fields"]})
                if not fields:
                    continue
                out.append({
                    "event": _iface._event(h["direction"], h["etype"], h["endpoint"]),
                    "direction": "input" if h["direction"] == "get" else "output",
                    "endpoint": h["endpoint"],
                    "endpointType": h["etype"],
                    "verb": h["verb"],
                    "fields": fields,
                    # host variable -> COLUMN, the key a consumer joins the interface
                    # overlay on. Reporting it is the whole point of this block: it is
                    # exactly the pair that appears there and is missing from `rows`.
                    "columns": h.get("columns") or {},
                    "line": prov.get("line", 0),
                })
        return out

    def _unreached_reason(self, state: str, preds: Dict[str, List[str]],
                          reached: Set[str]) -> str:
        """Why a state was never entered. Two of these are gaps in THIS tool and two
        are properties of the program, and a consumer triaging a corpus needs to sort
        them apart before deciding whether anything is wrong."""
        base = self.origin_state.get(state, state)
        for target, kind in self._unresolved.items():
            head = target.split("__THRU__", 1)[0]
            if base == target or base == head or base.startswith(head + "__"):
                return kind
        ps = [p for p in preds.get(state, []) if p != state]
        if not ps:
            return "no-static-predecessor"
        if any(p in reached for p in ps):
            # Should not arise: preds is built from succs, so a reached predecessor
            # would have queued this state. Named rather than asserted, because being
            # wrong about that here must not silently look like dead code.
            return "reached-predecessor-not-propagated"
        return "cascade"

    # -- origin maps as change chains (see `_Node`) --------------------------------

    def _merge_all(self, nodes: List[_Node]) -> _Node:
        """The join of several maps, built whole: a key's origins at a join are every
        predecessor's. Used where nothing is known about what changed - a state's first
        visit, or a predecessor reached for the first time since its last one. Maps
        that grew from one snapshot can differ only on the keys changed since it."""
        top, keys = _since_common(nodes)
        if top is not None and 4 * len(keys) < len(_snapshot(top)):
            return _extend(top, self._rejoin(nodes, top, keys))
        canon = self._canon
        merged = _materialize(nodes[0])
        get = merged.get
        # Every write below makes a key's value strictly larger (a new key, or a union
        # that adds something), so the keys written ARE the delta from nodes[0]: kept
        # here directly rather than through the map's write tracking and a re-diff.
        delta: Dict[str, OriginSet] = {}
        for n in nodes[1:]:
            # equal sets are one object, so the pairs that differ are found in C
            for k, v in _whole(n).items() - merged.items():
                prev = get(k)
                if prev is None:
                    delta[k] = v
                    dict.__setitem__(merged, k, v)
                elif prev is not v and not v <= prev:
                    u = prev | v
                    u = delta[k] = canon.setdefault(u, u)
                    dict.__setitem__(merged, k, u)
        return _extend(nodes[0], delta, merged)

    def _rejoin(self, nodes: List[_Node], was: _Node, keys) -> Dict[str, OriginSet]:
        """The keys among ``keys`` whose join over ``nodes`` differs from ``was``, with
        their new value - every other key's join is unchanged, because no predecessor
        changed it since ``was`` was built."""
        canon = self._canon
        gets = [_getter(n, len(keys)) for n in nodes]
        was_get = _getter(was, len(keys))
        delta: Dict[str, OriginSet] = {}
        keys = list(keys)
        n = len(gets)
        for k, old, vals in zip(keys, map(was_get, keys),
                                zip(*[list(map(get, keys)) for get in gets])):
            u = vals[0]
            if vals.count(u) != n:
                u = None
                for v in vals:
                    if v is None or v is u:
                        continue
                    if u is None:
                        u = v
                    elif not v <= u:
                        w = u | v
                        u = canon.setdefault(w, w)
            if u is not old and u != old:
                delta[k] = u
        return delta

    def _check_height(self, state: str, n: int, out: _Node, steps: int) -> None:
        """Raise unless OUT[``state``], changed ``n`` times, has room for that many
        changes: ``n <= 1 + height``, where the height counts each key once and each
        origin under it once - what a monotone transfer must have added, one at least
        per change after the first. Failing means the fixpoint is not converging the way
        its termination rests on, so the run is refused: an unsettled origins map would
        publish "no external origin reaches this field" as a finding."""
        height = 0
        for v in _whole(out).values():
            height += 1 + len(v)
        if n > 1 + height:
            raise RuntimeError(
                f"lineage fixpoint is not converging: the origins leaving state {state} "
                f"changed {n} times but hold only {height} keys and origins, so the "
                f"transfer is not monotone and the iteration need not end ({steps:,} "
                f"visits so far). No lineage view is produced - a cut-off one would "
                f"report fields as internally set without proof. This is a defect in "
                f"the analysis; please report this program")

    def run(self, timer=None) -> dict:
        # Solved once per instance. The dynamic-call view wants the primitive facts this
        # pass records (`fills`/`flow`/`dynamic_sites`) and the lineage table wants the
        # rows; both call this, and re-solving would also append every flag twice.
        if self._view is not None:
            return self._view
        # `timer` (a StageTimer, from the --timing run that forces this pass) splits the
        # one `lineage-fixpoint` span the estate reported as 93% of all runtime into the
        # origins worklist and the row emission, so a per-program timing log can say
        # WHICH half a slow program is slow in. Output is untouched either way.
        t_origins = timer.start() if timer is not None else None
        self.changers = self._changers()
        entries = self.entries
        entryset = set(entries)

        preds: Dict[str, List[str]] = {s: [] for s in self.states}
        for s, ts in self.succs.items():
            for t in ts:
                if t in preds:
                    preds[t].append(s)

        # Equal origin sets are ONE object (`_canon`): a large program holds millions of
        # map entries but a few hundred distinct sets, and a fresh copy per entry was most
        # of its memory. It also makes "same set?" an identity test almost everywhere.
        canon = self._canon = {}
        empty = _Node(None, None, full={})
        seed_map = {k: canon.setdefault(v, v) for k, v in self._seed().items()}
        seed = _extend(empty, seed_map, _Tracked.of(seed_map))
        # None = not yet computed, which is NOT the same as "computed and empty" - the
        # distinction is what lets an all-empty prefix still propagate to successors.
        IN: Dict[str, Optional[_Node]] = {s: None for s in self.states}
        OUT: Dict[str, Optional[_Node]] = {s: None for s in self.states}
        # ORDER is what this pass costs. A state already queued is not queued again (the
        # reasons `_cond_flow` sets out above apply here with more force: a visit re-runs
        # `_apply` over a whole entry run), but dedup alone is not enough. A plain FIFO
        # queue, which took 1.0 visits per state on the synthetic it was measured on, took
        # 6.3 / 12.6 / 24.8 on real CICS programs of 1,459 / 2,098 / 4,236 lines - the
        # whole body re-solved ~25 times - because a routine PERFORMed from 80 sites gets
        # a NEW caller's map at every breadth-first depth, and each time its exit fans the
        # change out to all 80 continuations and everything downstream of them.
        #
        # So: reverse postorder, in PASSES. Within a pass, states run in RPO (predecessors
        # before successors along forward edges) from a heap; a state re-queued by a back
        # edge, or one this pass has already run, waits for the NEXT pass. That bounds
        # every state to one run per pass - a shared routine runs once with all its
        # callers' maps merged, not once per caller - and passes settle at about the loop
        # depth. A heap without the pass boundary was 4x WORSE than FIFO on a program whose
        # every paragraph performs one utility (the routine re-ran per caller); with it,
        # 4,236-line COACTUPC went 26.5 -> 3.0 pops per state in 5 passes, and the two
        # utility-heavy synthetics stayed level. Same rows either way: the answer is the
        # fixpoint, the order only decides how many wasted visits it takes to reach it.
        order: List[str] = []
        seen: set = set()
        for e in entries:
            if e in seen:
                continue
            seen.add(e)
            stack = [(e, iter(self.succs.get(e, ())))]
            while stack:
                node, it = stack[-1]
                nxt_node = None
                for t in it:
                    if t in self.states and t not in seen:
                        nxt_node = t
                        break
                if nxt_node is None:
                    stack.pop()
                    order.append(node)
                else:
                    seen.add(nxt_node)
                    stack.append((nxt_node, iter(self.succs.get(nxt_node, ()))))
        rpo: Dict[str, int] = {n: i for i, n in enumerate(reversed(order))}
        far = len(rpo)                       # anything the walk never reaches sorts last
        work: List[Tuple[int, str]] = [(rpo.get(e, far), e) for e in entries]
        heapq.heapify(work)
        later: List[Tuple[int, str]] = []    # the next pass
        queued = set(entries)
        steps = 0
        dirty: Dict[str, Set[str]] = {}      # keys a predecessor changed since IN[s]
        dirty_n: Dict[str, int] = {}         # ...in how many updates
        rebuild: Set[str] = set()            # states to re-join whole
        wrote: Dict[str, Dict[str, OriginSet]] = {}   # where OUT[s] differs from IN[s]
        # NO iteration limit: the loop runs until nothing is queued, which is exactly
        # when every state's IN is the join of its predecessors' OUTs and its OUT is its
        # transfer of that IN - the fixpoint. It used to stop after `max(50_000, states x
        # 500)` pops, a guess that scaled with the states only, while the work scales
        # with how many loop-carried hops an origin needs: a loop shifting a 502-field
        # history one field per iteration ran out of steps, and its DISPLAY row said
        # "internally set" about a field the ACCEPT reaches. Stopping early never
        # over-reports - origins only grow - so a cut-off answer is a confident "no".
        #
        # Termination is a proof, not a budget. Every map starts unset and the join is
        # union. The transfer is monotone (more origins in, never fewer out) because
        # every value `_apply` writes is a constant (an input event's own origin) or a
        # union of values it reads from the map, chosen by the statement alone, and
        # every read is a union over fixed keys - `_read` of a group used to switch from
        # the group's own entry to its leaf/FILLER keys once one arrived, which made an
        # OUT map SHRINK (`ACCEPT G` then `MOVE SPACES TO G` on the back edge), and no
        # longer does. That is an invariant of the transfer to keep, not a law: each
        # OUT[s] only grows while it holds, and a state is queued only when a
        # predecessor's OUT changed.
        # Each change of OUT[s] after its first adds at least one key or one origin to
        # it, so by the time OUT[s] has changed n times, n <= 1 + its height (the keys
        # it holds plus the origins under them), which the finite fields x origins bound
        # from above. That inequality is checked as the run goes (`_check_height`, at
        # doubling counts from `_CHECK_FROM`, so real programs never pay for it); it can
        # fail only if the transfer stops being monotone, and then the loop might never
        # end, so the solve RAISES rather than cut off and publish as if settled. Every
        # view that reads this solve (lineage, dynamic calls, the manifest annotated
        # from them) fails with it.
        changes: Dict[str, int] = {}         # how many times OUT[s] has changed
        check_at: Dict[str, int] = {}        # ...and the count to next check it at
        while work or later:
            steps += 1
            if not work:
                work, later = later, []
                heapq.heapify(work)
            cur, s = heapq.heappop(work)
            queued.discard(s)
            # Merging every predecessor's whole map, one `frozenset() | o` per field, was
            # the cost of the pass: 17.4M dict.get calls on a 4,236-line CICS program,
            # 8.7 of its 12 s, because a group input's fill seeds every leaf of a real
            # copybook record (mean map width 341 fields there). 81% of its states have
            # exactly ONE predecessor, whose map is simply shared. Since group stores
            # write every leaf (3ee1ee0) maps are wider still, and a program that
            # PERFORMs shared routines from many places re-solves each state 5-15 times
            # (every call of a routine placed earlier is a back edge, so a change travels
            # one round of calls per pass). So a revisit does work in proportion to what
            # CHANGED, not to the width: `dirty[s]` holds the keys a predecessor's output
            # has changed on since `IN[s]` was built, only those are re-joined
            # (`_rejoin`), and only the keys this state's input or own writes touched are
            # compared to decide whether its output changed. Every other key is equal by
            # construction - the answer is exactly the whole-map join's. A state is
            # re-joined whole (`rebuild`) on its first visit and when a predecessor
            # produces its first output, since nothing is known about what that adds.
            live = [OUT[p] for p in preds.get(s, []) if OUT[p] is not None]
            if s in entryset:
                live.insert(0, seed)
            was = IN[s]
            keys = dirty.pop(s, None)
            updates = dirty_n.pop(s, 0)
            if was is None or s in rebuild:
                rebuild.discard(s)
                if len(live) == 1:
                    merged = live[0]
                elif live:
                    merged = self._merge_all(live)
                else:
                    merged = empty
                in_changed = () if was is None else _differ_all(merged, was)
                if was is not None and not in_changed:
                    continue                  # input unchanged - nothing to redo
            elif not keys:
                continue
            elif len(live) == 1:
                merged = live[0]
                # One predecessor that changed once since this state last ran: IN[s]
                # equals its output from before that change, and `keys` is exactly where
                # the change landed - nothing to look up.
                in_changed = keys if updates == 1 else _differ(merged, was, keys)
                if not in_changed:
                    continue
            else:
                in_changed = self._rejoin(live, was, keys)
                if not in_changed:
                    continue
                merged = _extend(was, in_changed)
            IN[s] = merged
            st = self.states[s]
            if st.get("entry"):
                delta = self._apply(s, st, _Overlay(merged), None).changes(canon)
                new_out = _extend(merged, delta)
            else:                             # no actions: the output IS the input
                delta, new_out = {}, merged
            prev_out = OUT[s]
            if prev_out is None:
                out_changed = None            # a first output: successors re-join whole
            else:
                # A key neither this visit nor the last one wrote passes straight
                # through, so its output changed exactly where its input did; only the
                # written keys need comparing.
                written = set(delta)
                written.update(wrote.get(s, ()))
                if written:
                    out_changed = set(in_changed)
                    out_changed.difference_update(written)
                    out_changed.update(_differ(new_out, prev_out, written))
                else:
                    out_changed = in_changed
            # OUT[s] holds IN[s] plus these keys either way: when it is kept below, it is
            # equal to `new_out`
            wrote[s] = delta
            if out_changed is not None and not out_changed:
                continue
            OUT[s] = new_out
            n = changes[s] = changes.get(s, 0) + 1
            if n >= check_at.get(s, _CHECK_FROM):
                self._check_height(s, n, new_out, steps)
                check_at[s] = 2 * n
            for t in self.succs.get(s, []):
                if t not in self.states:
                    continue
                if IN[t] is not None:
                    if out_changed is None:
                        rebuild.add(t)
                    elif t not in rebuild:
                        dirty.setdefault(t, set()).update(out_changed)
                        dirty_n[t] = dirty_n.get(t, 0) + 1
                if t not in queued:
                    ti = rpo.get(t, far)
                    if ti > cur:
                        heapq.heappush(work, (ti, t))
                    else:
                        later.append((ti, t))
                    queued.add(t)

        if timer is not None:
            timer.since("lineage:origins", t_origins)
        t_rows = timer.start() if timer is not None else None
        rows: List[dict] = []
        unreached: List[dict] = []
        reached = {s for s in self.states if IN[s] is not None}
        for s in self.states:
            if IN[s] is None:                 # unreachable from any entry
                lost = self._events_of(self.states[s])
                if lost:
                    unreached.append({
                        "state": s,
                        # `_split` renames a segment; the interface overlay performs no
                        # such split, so the base name is what a consumer can join on.
                        "baseState": self.origin_state.get(s, s),
                        "reason": self._unreached_reason(s, preds, reached),
                        "events": lost,
                    })
                continue
            self._apply(s, self.states[s], _materialize(IN[s]), rows)

        for u in unreached:
            n = sum(len(e["fields"]) for e in u["events"])
            self._flag(f"{u['state']}: no path from an entry point reaches it "
                       f"({u['reason']}); the {len(u['events'])} external event(s) it "
                       f"performs and their {n} field(s) are absent from 'rows', while "
                       f"the interface overlay still lists them")

        self._view = {
            "format": "cobol-xstate-lineage",
            # Bumped when `conditions` moved out of the rows into the interned pools
            # below. Version 1 carried the full condition list inline at every row and
            # every write site; a reader that does not check this would silently see
            # every row as unconditional, so it is stated rather than left to be
            # inferred from a missing key.
            # ...and now the distribution-wide VIEW_SCHEMA_VERSION, which starts at 3
            # precisely so this view's published 2 never goes backwards.
            "formatVersion": VIEW_SCHEMA_VERSION,
            "program": self.m.program_id,
            "source": self.m.source_name,
            "note": (
                "One row per (external event, field). For an INPUT event the fields are "
                "the ones that event FILLS; for an OUTPUT event they are the ones that "
                "FILL it. 'changedByProgram' means this program assigns the field; "
                "its write sites are 'changedBy'[field] - one list per field, the same "
                "for every row about it; "
                "'origins' are the external events whose data reaches it here "
                "(flow-sensitive). A linkage-sourced value shows GET.CALLER.CALLER as "
                "an origin. 'maybe' origins name the program that would resolve them. "
                "'conditionSetId' indexes 'conditionSets' (a list of ids into "
                "'conditions'): the guards that hold on EVERY path to this event - a "
                "conjunction, always true when it fires; each also appears on the write "
                "sites in 'changedBy'. The key is ABSENT, never empty, when nothing "
                "governs the point, so 'conditionSetId' in row is exactly the v1 test "
                "bool(row.get('conditions')). They are necessary, not necessarily "
                "sufficient: "
                "'conditionsPartial' marks a point known to be governed by more than the "
                "conjunction listed, but its absence is best-effort, not a guarantee "
                "(a disjunction inside a loop body can evade it). Conditions "
                "are NOT attached to 'origins': an origin can reach a field through a "
                "chain of assignments, so its real condition is the conjunction along "
                "that whole chain, and reporting any one link's condition would look "
                "like the answer while not being it. Nothing is invented - see 'flags'."
            ),
            # The condition vocabulary, hoisted out of the rows. A point's conditions
            # are a set drawn from it, and the same set recurs at a row and at every
            # write site under it; stored once here, referenced by id there.
            "conditions": {str(i): d for i, d in enumerate(self._cond_defs)},
            "conditionSets": {str(i): s for i, s in enumerate(self._set_defs)},
            # field -> the write sites in this program that assign it (`_changed_by`),
            # for every field a row marks `changedByProgram`. Hoisted out of the rows:
            # every row about a field carried the same list.
            "changedBy": {f: self._changed_by(f) for f in sorted(self._changed_fields)},
            "rows": rows,
            # The internal dataflow behind those rows, recorded during the same pass:
            # `fills` is a field filled by a boundary crossing, `flow` is one write site
            # inside the program (target <- the operands it was computed from). Together
            # they are the chain `rows` gives only the two ends of. A flow record's
            # `action` resolves in the bundle's semantics, whose `kind` tells a copy
            # from a computation.
            "fills": self.fills,
            "flow": self.flow,
            # Sorted: `_flag` appends in visit order, so the list used to encode the
            # traversal - a scheduling change reordered it with every flag's text
            # unchanged. The answer must not depend on the order it was found in.
            "flags": sorted(self.flags),
        }
        # Present ONLY when something was actually lost. A program whose every
        # event-carrying state is reachable says nothing here rather than saying
        # nothing at length - which is also what keeps every existing golden
        # byte-identical across this change.
        if unreached:
            self._view["unreached"] = {
                "note": (
                    "States that perform an external event and that no path from an "
                    "entry point reaches. They contribute NO rows above: with no path "
                    "there are no origins, and naming one would be a guess. They are "
                    "listed because 'interface' walks the program structurally and "
                    "DOES map these statements' columns, so a consumer joining the two "
                    "views finds a (field, column) pair there and no row here - a "
                    "difference that used to be silent. 'reason' sorts the causes: "
                    "'perform-target-unresolved' and 'perform-range-inverted' are gaps "
                    "in THIS tool's control-flow recovery and mean rows are genuinely "
                    "missing; 'no-static-predecessor' and 'cascade' usually mean the "
                    "program itself never runs the code, in which case having no rows "
                    "is the correct answer."
                ),
                "states": unreached,
            }
        if timer is not None:
            timer.since("lineage:rows", t_rows)
        # Only the solve reads these, and the view above is all that is asked for again;
        # the Machine keeps this instance cached, so do not keep them alive with it.
        self._kids, self._layouts, self._placements, self._canon = None, {}, {}, {}
        return self._view


def build_lineage(machine: Machine) -> dict:
    """Field lineage across the external boundary. Pure read over the emitted machine."""
    return machine.lineage().run()
