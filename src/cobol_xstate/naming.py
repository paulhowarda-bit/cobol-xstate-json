"""Deterministic name generation + a provenance registry.

Guards and actions in the emitted machine are **names only** - their meaning lives
here, in the provenance table, never in an invented function body (the no-fabrication
rule from references/cobol-to-statecharts.md). Every generated name is registered
with the exact COBOL it came from and its source line, so the statechart is traceable
back to the legacy source (the point of a rewrite contract).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

_OP_WORDS = {
    "=": "eq", ">": "gt", "<": "lt", ">=": "ge", "<=": "le", "<>": "ne",
}


def _slug(text: str, maxlen: int = 60) -> str:
    text = text.strip()
    # Map relational operators to words so names stay identifier-safe and readable.
    # LONGEST FIRST: iterating the table's own order let the single-character "=", ">"
    # and "<" fire first, so ">=" became "gt eq" and "<>" became "lt gt" - and the
    # "ge"/"le"/"ne" spellings the table declares could never be produced at all.
    for op in sorted(_OP_WORDS, key=len, reverse=True):
        text = text.replace(op, f" {_OP_WORDS[op]} ")
    text = text.replace("'", "").replace('"', "")
    text = re.sub(r"[^0-9A-Za-z_\- ]+", " ", text)
    parts = [p for p in text.split() if p]
    slug = "_".join(parts)
    slug = re.sub(r"_+", "_", slug).strip("_")
    if len(slug) > maxlen:
        slug = slug[:maxlen].rstrip("_")
    return slug or "x"


@dataclass
class ProvenanceEntry:
    name: str
    kind: str        # 'state' | 'guard' | 'action'
    cobol: str       # the exact COBOL text / condition / statement
    line: int
    member: Optional[str] = None  # copybook member, if this came from a COPY expansion


# A quoted alphanumeric literal, kept out of the case-folding below.
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")


def _signature(kind: str, cobol: str) -> str:
    """Reuse key for ``cobol``: case-folded OUTSIDE quoted literals only.

    COBOL is case-insensitive for keywords and data-names, so `MOVE A TO B` and
    `move a to b` are one statement and should share one action name. The CONTENTS of
    an alphanumeric literal are case-sensitive data, though: `MOVE 'Error'` and
    `MOVE 'ERROR'` store different bytes, and `IF C = 'y'` and `IF C = 'Y'` test
    different values. Folding the whole string collapsed those pairs onto one name, so
    whichever registered first silently won at both call sites - a wrong stored string,
    or a guard that sends the other branch the wrong way.
    """
    out: List[str] = []
    pos = 0
    for m in _QUOTED.finditer(cobol):
        out.append(cobol[pos:m.start()].upper())
        out.append(m.group(0))          # literal verbatim - its case is data
        pos = m.end()
    out.append(cobol[pos:].upper())
    return f"{kind}::{''.join(out)}"


@dataclass
class NameRegistry:
    """Allocates unique names and records their COBOL provenance."""

    entries: Dict[str, ProvenanceEntry] = field(default_factory=dict)
    _by_signature: Dict[str, str] = field(default_factory=dict)

    def _unique(self, base: str) -> str:
        if base not in self.entries:
            return base
        n = 2
        while f"{base}_{n}" in self.entries:
            n += 1
        return f"{base}_{n}"

    def register(self, kind: str, base: str, cobol: str, line: int) -> str:
        """Register a name for ``cobol``. Identical (kind, cobol) reuse one name so
        the same statement/condition maps to a single guard/action."""
        sig = _signature(kind, cobol.strip())
        if sig in self._by_signature:
            return self._by_signature[sig]
        name = self._unique(base)
        self.entries[name] = ProvenanceEntry(name=name, kind=kind, cobol=cobol.strip(), line=line)
        self._by_signature[sig] = name
        return name

    def action(self, cobol: str, line: int) -> str:
        return self.register("action", _slug(cobol), cobol, line)

    def action_named(self, base: str, cobol: str, line: int) -> str:
        return self.register("action", _slug(base), cobol, line)

    def guard(self, cobol: str, line: int) -> str:
        return self.register("guard", _slug(cobol), cobol, line)

    def guard_named(self, base: str, cobol: str, line: int) -> str:
        return self.register("guard", _slug(base), cobol, line)

    def state(self, name: str, cobol: str, line: int, member: Optional[str] = None) -> str:
        # State ids keep the COBOL paragraph/section name verbatim (XState keys may
        # contain hyphens); only register provenance.
        if name not in self.entries:
            self.entries[name] = ProvenanceEntry(name=name, kind="state", cobol=cobol,
                                                 line=line, member=member)
        return name

    def provenance_dict(self) -> Dict[str, Dict[str, object]]:
        out: Dict[str, Dict[str, object]] = {}
        for name, e in self.entries.items():
            entry: Dict[str, object] = {"kind": e.kind, "cobol": e.cobol, "line": e.line}
            if e.member:
                entry["member"] = e.member
            out[name] = entry
        return out

    def names_of(self, kind: str) -> List[str]:
        return [n for n, e in self.entries.items() if e.kind == kind]
