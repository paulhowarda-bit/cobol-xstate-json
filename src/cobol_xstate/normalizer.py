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

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional


class SourceFormat(Enum):
    FIXED = "fixed"  # z/OS default: cols 1-6 seq, 7 indicator, 8-72 code, 73-80 id
    FREE = "free"    # SOURCEFORMAT(FREE): the whole line is code


@dataclass
class CodeLine:
    """One logical line of COBOL code with provenance back to the source file."""

    text: str          # code only (no sequence area, indicator, or id area)
    line: int          # 1-based physical line number where this line *starts*
    area_a: bool = False  # first token begins in Area A (cols 8-11) -> header candidate

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


def _detect_format(raw_lines: List[str]) -> SourceFormat:
    """Heuristic: honor an explicit ``SOURCEFORMAT`` directive, else sniff columns.

    Fixed-format source overwhelmingly keeps cols 1-6 blank or numeric and reserves
    col 7 for indicators; free-format source routinely puts code in those columns.
    """
    for raw in raw_lines[:200]:
        up = raw.upper()
        if "SOURCEFORMAT" in up:
            if "FREE" in up:
                return SourceFormat.FREE
            if "FIXED" in up:
                return SourceFormat.FIXED
    votes_free = 0
    votes_fixed = 0
    for raw in raw_lines:
        if not raw.strip():
            continue
        seq = raw[_SEQ] if len(raw) >= 6 else raw
        ind = raw[_IND] if len(raw) > _IND else " "
        # A non-space, non-numeric sequence area or an alpha indicator column is a
        # strong signal of free format.
        if seq.strip() and not seq.strip().isdigit():
            votes_free += 1
        elif ind in ("*", "/", "-", "D", "d", " "):
            votes_fixed += 1
        else:
            votes_free += 1
    return SourceFormat.FREE if votes_free > votes_fixed else SourceFormat.FIXED


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
        fmt = _detect_format(raw_lines)

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
            # Area A is cols 8-11; the normalized code starts at col 8 (index 0), so
            # a first token within the first 4 chars sits in Area A. Free format has
            # no areas - treat a near-left-margin token as a header candidate.
            limit = 4 if fmt is SourceFormat.FIXED else 4
            out.append(CodeLine(text=code, line=idx, area_a=indent < limit))

    return [cl for cl in out if cl.text.strip()]
