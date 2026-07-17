"""Stage 2 - the preprocessor: COPY / REPLACE / EXEC SQL INCLUDE.

A COBOL "parser" is really a pipeline, and most of the COBOL-specific work lives
here, before the grammar runs (references/parsing-cobol.md, Stage 2). Without it the
parser cannot see copybook-defined data items or procedure code - they are silently
missing, which is worse than flagged. This stage runs after format normalization and
rewrites the ``CodeLine`` stream:

* **COPY** member [(OF|IN) library] [REPLACING ==a== BY ==b== ...] - textual inclusion
  with a configurable resolver (search paths, extension list, missing-copybook policy)
  and pseudo-text / word substitution.
* **EXEC SQL INCLUDE** member END-EXEC - behaves like COPY.
* standalone **REPLACE ==a== BY ==b==** ... **REPLACE OFF**.

Copybooks are expanded recursively (with a cycle guard); expanded lines carry their
``origin`` member name for provenance, and the set of expanded/missing members is
reported so coverage is measurable, never silently dropped.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .normalizer import CodeLine, SourceFormat, normalize


@dataclass
class CopybookResolver:
    """Locate copybooks. ``paths`` are searched in order, each combined with every
    extension in ``exts`` (plus the bare name)."""

    paths: List[str] = field(default_factory=list)
    exts: Tuple[str, ...] = ("", ".cpy", ".CPY", ".cbl", ".cob", ".copy", ".CBL")
    missing: str = "continue"  # 'continue' (stub + note) | 'error'

    def resolve(self, name: str) -> Optional[Tuple[str, str]]:
        name = name.strip().strip("'\"")
        for base in self.paths or ["."]:
            for ext in self.exts:
                candidate = os.path.join(base, name + ext)
                if os.path.isfile(candidate):
                    with open(candidate, "r", errors="replace") as f:
                        return f.read(), candidate
        return None


@dataclass
class PreprocessResult:
    lines: List[CodeLine]
    expanded: List[str] = field(default_factory=list)  # members successfully copied
    missing: List[str] = field(default_factory=list)   # members not found
    notes: List[str] = field(default_factory=list)
    # Structured record of every COPY / EXEC SQL INCLUDE seen, in source order:
    # {member, status: expanded|missing|skipped-cyclic, via: COPY|EXEC SQL INCLUDE,
    #  replacing: bool}. The lists above stay for the notes they already feed; this
    # carries the copybook dependency out as data (the related-artifact manifest reads it).
    copybooks: List[dict] = field(default_factory=list)


_COPY_RE = re.compile(
    r"\bCOPY\b\s+([A-Z0-9$#@_.-]+|'[^']*'|\"[^\"]*\")"
    r"(?:\s+(?:OF|IN)\s+[A-Z0-9$#@_-]+)?"
    r"(?:\s+REPLACING\s+(?P<rep>.*?))?\s*\.\s*$",
    re.I | re.S,
)
_SQL_INCLUDE_RE = re.compile(
    r"\bEXEC\s+SQL\s+INCLUDE\s+([A-Z0-9$#@_-]+)\s+END-EXEC\s*\.?\s*$", re.I | re.S)


def _parse_replacing(clause: str) -> List[Tuple[str, str]]:
    """Parse REPLACING pairs: ==a== BY ==b==  or  word BY word."""
    pairs: List[Tuple[str, str]] = []
    # Pseudo-text pairs first.
    for m in re.finditer(r"==(.*?)==\s+BY\s+==(.*?)==", clause, re.I | re.S):
        pairs.append((m.group(1).strip(), m.group(2).strip()))
    if pairs:
        return pairs
    for m in re.finditer(r"(\S+)\s+BY\s+(\S+)", clause, re.I):
        pairs.append((m.group(1), m.group(2)))
    return pairs


def _apply_replacing(text: str, pairs: List[Tuple[str, str]]) -> str:
    for a, b in pairs:
        if not a:
            continue
        # Whitespace-tolerant, case-insensitive textual substitution.
        pat = re.compile(re.escape(a).replace(r"\ ", r"\s+"), re.I)
        text = pat.sub(b.replace("\\", r"\\"), text)
    return text


def _gather_statement(lines: List[CodeLine], i: int):
    """From line i, collect lines until one carries a period; return (text, next_i)."""
    parts = [lines[i].text]
    j = i
    while "." not in lines[j].text and j + 1 < len(lines):
        j += 1
        parts.append(lines[j].text)
    return " ".join(parts), j + 1


def preprocess(lines: List[CodeLine], resolver: Optional[CopybookResolver] = None,
               _seen: Optional[set] = None,
               fmt: Optional[SourceFormat] = None) -> PreprocessResult:
    resolver = resolver or CopybookResolver()
    _seen = _seen if _seen is not None else set()
    out: List[CodeLine] = []
    res = PreprocessResult(lines=out)

    active_replace: List[Tuple[str, str]] = []

    def emit(cl: CodeLine) -> None:
        if active_replace:
            cl = CodeLine(text=_apply_replacing(cl.text, active_replace), line=cl.line,
                          area_a=cl.area_a, origin=cl.origin)
        out.append(cl)

    i = 0
    while i < len(lines):
        line = lines[i]
        up = line.text.upper()
        if re.match(r"\s*REPLACE\b", up):
            # standalone REPLACE ==a== BY ==b== ... / REPLACE OFF: text substitution
            # active on every following line until turned off.
            stmt, nxt = _gather_statement(lines, i)
            if re.search(r"\bREPLACE\s+(?:OFF|LAST\s+OFF)\b", stmt, re.I):
                active_replace = []
                i = nxt
                continue
            prs = _parse_replacing(re.sub(r"^\s*REPLACE\b", "", stmt, flags=re.I))
            if prs:
                active_replace = prs
                i = nxt
                continue
        if "COPY" in up or re.search(r"\bEXEC\s+SQL\s+INCLUDE\b", up):
            stmt, nxt = _gather_statement(lines, i)
            copy_m = _COPY_RE.search(stmt)
            m = copy_m or _SQL_INCLUDE_RE.search(stmt)
            if m:
                # Code preceding the COPY in the same gathered sentence (e.g.
                # ``MOVE 1 TO WS-IDX. COPY FOO.``) is real code - keep it.
                prefix = stmt[:m.start()].strip()
                if prefix:
                    emit(CodeLine(text=prefix, line=line.line,
                                  area_a=line.area_a, origin=line.origin))
                member = m.group(1)
                rep = m.groupdict().get("rep") if copy_m else None
                pairs = _parse_replacing(rep or "")
                _expand_member(member, pairs + active_replace, resolver, res, _seen, fmt,
                               via="COPY" if copy_m else "EXEC SQL INCLUDE",
                               replacing=bool(rep))
                i = nxt
                continue
        emit(line)
        i += 1
    return res


def _expand_member(member, pairs, resolver, res: PreprocessResult, seen: set,
                   fmt: Optional[SourceFormat] = None, via: str = "COPY",
                   replacing: bool = False) -> None:
    key = member.strip().strip("'\"").upper()

    def record(status: str) -> None:
        res.copybooks.append({"member": key, "status": status, "via": via,
                              "replacing": replacing})

    if key in seen:
        record("skipped-cyclic")
        res.notes.append(f"COPY {key}: recursive/cyclic include skipped")
        return
    found = resolver.resolve(member)
    if found is None:
        record("missing")
        res.missing.append(key)
        res.notes.append(f"COPY {key}: copybook not found on search path - "
                         f"members/logic it defines are NOT in the model")
        return
    text, path = found
    record("expanded")
    res.expanded.append(key)
    # A copybook inherits the including program's source format - it is a fragment,
    # far too small to auto-detect reliably on its own.
    sub = normalize(text, fmt)
    seen = seen | {key}
    # Recursively preprocess the copybook (nested COPY), then apply REPLACING.
    inner = preprocess(sub, resolver, seen, fmt)
    res.expanded.extend(x for x in inner.expanded if x not in res.expanded)
    res.missing.extend(inner.missing)
    res.notes.extend(inner.notes)
    res.copybooks.extend(inner.copybooks)   # nested COPY inside this member
    for cl in inner.lines:
        new_text = _apply_replacing(cl.text, pairs) if pairs else cl.text
        res.lines.append(CodeLine(text=new_text, line=cl.line,
                                  area_a=cl.area_a, origin=cl.origin or key))
