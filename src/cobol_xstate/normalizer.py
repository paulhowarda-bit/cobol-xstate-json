"""Stage 1 - source-format normalization.

COBOL is not yet "COBOL the grammar can see": the same bytes mean different things
in fixed vs. free format, column 7 carries comment/continuation/debug indicators,
and continued literals are split mid-token. This stage turns raw source into a list
of ``CodeLine`` (code text + original 1-based line number) with comments removed and
continuation lines stitched, so every later stage keeps a source map back to the
original file.

This is the foundation and the most common source of silent corruption (see the
ibm-cobol skill, references/parsing-cobol.md, Stage 1), so it is kept small and
heavily tested in isolation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple


class SourceFormat(Enum):
    FIXED = "fixed"  # z/OS default: cols 1-6 seq, 7 indicator, 8-72 code, 73-80 id
    FREE = "free"    # SOURCEFORMAT(FREE): the whole line is code


@dataclass
class CodeLine:
    """One logical line of COBOL code with provenance back to the source file."""

    text: str          # code only (no sequence area, indicator, or id area)
    line: int          # 1-based physical line number where this line *starts*
    area_a: bool = False  # first token begins in Area A (cols 8-11) -> header candidate
    origin: Optional[str] = None  # copybook member name if expanded from a COPY

    def is_blank(self) -> bool:
        return not self.text.strip()


# Area boundaries for fixed format (1-based, inclusive) translated to 0-based slices.
_SEQ = slice(0, 6)        # cols 1-6  sequence number area
_IND = 6                  # col 7     indicator area
_CODE = slice(7, 72)      # cols 8-72 Area A (8-11) + Area B (12-72)


def _strip_inline_comment(code: str) -> str:
    """Remove a ``*>`` inline comment, but not one inside a string literal."""
    in_str: Optional[str] = None
    i = 0
    while i < len(code):
        ch = code[i]
        if in_str:
            if ch == in_str:
                # doubled quote == escaped quote inside the literal
                if i + 1 < len(code) and code[i + 1] == in_str:
                    i += 2
                    continue
                in_str = None
        elif ch in ("'", '"'):
            in_str = ch
        elif ch == "*" and i + 1 < len(code) and code[i + 1] == ">":
            return code[:i]
        i += 1
    return code


@dataclass
class FormatDetection:
    """Result of source-format auto-detection.

    ``confidence`` lets callers tell a firm classification (explicit directive,
    clear column signals, or a decisive shape check) from a guess, so an ambiguous
    file can be warned about / overridden instead of silently corrupted.
    """

    format: SourceFormat
    confidence: float          # 0.0 (pure default) .. 1.0 (explicit directive)
    reason: str

    @property
    def is_confident(self) -> bool:
        return self.confidence >= 0.6


# Characters valid in the fixed-format indicator area (column 7): blank = code line,
# * / = comment, - = continuation, D/d = debug, $ = directive. ANYTHING ELSE in column
# 7 means column 7 carries program text, which only happens in free format. Columns 1-6
# (sequence numbers AND alphanumeric change/revision markers) are format-independent -
# the compiler ignores them in fixed format - so they are never inspected here.
_FIXED_INDICATORS = frozenset(" */-Dd$")
_FIXED_COMMENT_INDICATORS = frozenset("*/-")

# A DIVISION header's column pins the format: in fixed format it sits in Area A
# (column 8); in free format it starts at the left margin.
_DIVISION_HEADER = re.compile(
    r"\b(IDENTIFICATION|ENVIRONMENT|DATA|PROCEDURE)\s+DIVISION\b", re.I)


def _directive_format(raw_lines: List[str]) -> Optional[SourceFormat]:
    """Honor an explicit source-format directive - the authoritative signal.

    Recognizes the IBM ``>>SOURCE FORMAT [IS] FREE|FIXED`` directive as well as the
    compiler-option / Micro Focus ``SOURCEFORMAT(FREE)`` / ``$SET SOURCEFORMAT"FREE"``
    forms. Collapsing spaces unifies the ``SOURCE FORMAT`` (spaced, standard) and
    ``SOURCEFORMAT`` (unspaced) spellings.
    """
    for raw in raw_lines[:200]:
        up = raw.upper()
        if "SOURCEFORMAT" in up.replace(" ", ""):
            if "FREE" in up:
                return SourceFormat.FREE
            if "FIXED" in up:
                return SourceFormat.FIXED
    return None


def _column7_scan(raw_lines: List[str]) -> Tuple[int, int, int]:
    """Inspect column 7 (the indicator area) of every non-blank line.

    Returns ``(n_lines, violations, comments)``. A *violation* is a line whose column
    7 holds program text (a character not valid in the fixed indicator area) - this
    only happens in free format and is the sole reliable free signal. *comments* counts
    lines with a ``*`` / ``/`` / ``-`` indicator, which is positive proof of fixed.
    Columns 1-6 are never looked at, so sequence numbers and change markers are moot.
    """
    n = violations = comments = 0
    for raw in raw_lines:
        if not raw.strip():
            continue
        n += 1
        col7 = raw[_IND] if len(raw) > _IND else " "
        if col7 in _FIXED_COMMENT_INDICATORS:
            comments += 1
        elif col7 not in _FIXED_INDICATORS:
            violations += 1
    return n, violations, comments


def _division_header_format(raw_lines: List[str]) -> Optional[SourceFormat]:
    """Decide by the column of the first DIVISION header: column 8 (Area A) => fixed,
    column 1-4 (left margin) => free. Returns ``None`` when no header pins it."""
    for raw in raw_lines:
        m = _DIVISION_HEADER.search(raw)
        if m is None:
            continue
        col = m.start() + 1  # 1-based column of the header word
        if col == _CODE.start + 1:      # column 8
            return SourceFormat.FIXED
        if col <= 4:
            return SourceFormat.FREE
        # A header at some other column is inconclusive; keep scanning for a clearer one.
    return None


def detect_source_format(source: str) -> FormatDetection:
    """Auto-detect fixed vs. free format, with a confidence and a human-readable reason.

    Priority order - definitive signals first, heuristic last - so certainty degrades
    gracefully and the change-marker problem never arises (column 7 is the discriminator,
    columns 1-6 are ignored):

    1. explicit ``>>SOURCE FORMAT`` / ``SOURCEFORMAT()`` directive  -> conclusive
    2. column-7 invariant: every line has a valid indicator          -> conclusive fixed
    3. first DIVISION header at column 8 vs. the left margin          -> conclusive
    4. a line past the 80-column card boundary                        -> free
    5. fallback: the column-7 violation rate                          -> heuristic
    """
    raw_lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    # 1. Explicit directive.
    directive = _directive_format(raw_lines)
    if directive is not None:
        return FormatDetection(directive, 1.0, "explicit source-format directive")

    n, violations, comments = _column7_scan(raw_lines)

    # 2. Column-7 invariant: not one line puts code in column 7 -> fixed. This alone
    #    classifies real fixed source correctly regardless of change markers in cols 1-6.
    if n and violations == 0:
        why = f"column 7 is a valid indicator on all {n} lines"
        if comments:
            why += f", incl. {comments} comment/continuation line(s)"
        return FormatDetection(SourceFormat.FIXED, 0.97, why)

    # 3. DIVISION header column.
    header_fmt = _division_header_format(raw_lines)
    if header_fmt is not None:
        return FormatDetection(header_fmt, 0.95, "DIVISION header column position")

    # 4. Anything past the 80-column card boundary cannot be strict fixed.
    if any(len(raw.rstrip()) > 80 for raw in raw_lines):
        return FormatDetection(SourceFormat.FREE, 0.85, "source line exceeds 80 columns")

    # 5. Fallback heuristic on how often column 7 carries code.
    ratio = violations / n if n else 0.0
    if ratio >= 0.15:
        return FormatDetection(SourceFormat.FREE, min(0.9, 0.6 + ratio),
                               f"program text in column 7 on {violations}/{n} line(s)")
    return FormatDetection(SourceFormat.FIXED, 0.6 if violations else 0.7,
                           "no free-format signal; defaulted to fixed (z/OS norm)")


def _detect_format(raw_lines: List[str]) -> SourceFormat:
    """Back-compat shim: return just the format enum (see ``detect_source_format``)."""
    return detect_source_format("\n".join(raw_lines)).format


def _fixed_code(raw: str) -> Optional[str]:
    """Return the code portion of a fixed-format line, or None if it is a comment.

    Honors column 7: ``*`` / ``/`` = full-line comment, ``D`` = debug line (treated
    as a comment unless WITH DEBUGGING MODE, which we do not model), ``-`` =
    continuation (handled by the caller).
    """
    if len(raw) <= _IND:
        return ""  # too short to hold code; treat as blank
    ind = raw[_IND]
    if ind in ("*", "/", "D", "d"):
        return None
    code = raw[_CODE] if len(raw) > 7 else ""
    return code.rstrip()


def _is_fixed_continuation(raw: str) -> bool:
    return len(raw) > _IND and raw[_IND] == "-"


def normalize(source: str, fmt: Optional[SourceFormat] = None) -> List[CodeLine]:
    """Normalize raw COBOL ``source`` into stitched, comment-free ``CodeLine``s.

    ``fmt`` forces a source format; when omitted it is auto-detected.
    """
    raw_lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if fmt is None:
        fmt = detect_source_format(source).format

    out: List[CodeLine] = []
    for idx, raw in enumerate(raw_lines, start=1):
        if fmt is SourceFormat.FIXED:
            code = _fixed_code(raw)
            if code is None:
                continue  # comment / debug line
            cont = _is_fixed_continuation(raw)
        else:
            stripped = raw.lstrip()
            if stripped.startswith("*"):
                continue  # free-format full-line comment
            code = raw.rstrip()
            cont = False  # free format uses no column-7 continuation

        code = _strip_inline_comment(code).rstrip()

        if cont and out:
            # Continuation: append to the previous logical line. A leading-quote
            # continuation stitches a split literal with no intervening space.
            prev = out[-1]
            joiner = "" if code.lstrip().startswith(("'", '"')) else " "
            prev.text = (prev.text.rstrip() + joiner + code.lstrip())
        else:
            indent = len(code) - len(code.lstrip())
            if fmt is SourceFormat.FIXED:
                # Area A is cols 8-11; the normalized fixed-format code starts at col 8
                # (index 0), so a first token within the first 4 chars sits in Area A.
                area_a = indent < 4
            else:
                # Free format has no reference areas: a header may sit at any indent,
                # so the strict header regex (parser._HEADER_RE) - not a column rule -
                # is the discriminator. Treat every line as an Area-A candidate.
                area_a = True
            out.append(CodeLine(text=code, line=idx, area_a=area_a))

    return [cl for cl in out if cl.text.strip()]
