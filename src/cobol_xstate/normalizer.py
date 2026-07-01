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


# Anchors every real COBOL program contains. If normalization picked the WRONG
# format it slices the wrong columns and these get mangled, so the count of anchors
# recovered under a candidate format is a reliable tie-breaker for ambiguous source.
_COBOL_ANCHORS = re.compile(
    r"\b(IDENTIFICATION\s+DIVISION|PROGRAM-ID|ENVIRONMENT\s+DIVISION|"
    r"DATA\s+DIVISION|WORKING-STORAGE|LINKAGE\s+SECTION|PROCEDURE\s+DIVISION)\b",
    re.I,
)


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


def _score_columns(raw_lines: List[str]) -> Tuple[int, int, bool]:
    """Weighted column-signal vote. Returns ``(free, fixed, saw_strong_signal)``.

    Only signals that are genuinely diagnostic get weight; the one ambiguous layout
    (blank/numeric seq area + blank indicator + code from col 8) is deliberately a
    *weak* fixed vote, because a free-format file merely indented to col 8 is
    byte-identical to it - that case is resolved later by a shape check, not here.
    """
    free = fixed = 0
    strong = False
    for raw in raw_lines:
        if not raw.strip():
            continue
        seq = raw[_SEQ]
        ind = raw[_IND] if len(raw) > _IND else " "
        stripped = raw.strip()
        indent = len(raw) - len(raw.lstrip())
        if len(raw.rstrip()) > 80:
            free += 5; strong = True            # past the 80-col card boundary: can't be fixed
        elif seq.strip().isdigit():
            fixed += 3; strong = True            # all-digit sequence numbers: classic fixed
        elif seq.strip():
            free += 2; strong = True             # letters in cols 1-6: left-margin free code
        elif stripped.startswith((">>", "*>")) and indent < _IND:
            free += 2; strong = True             # free directive / inline comment at the margin
        elif ind in ("*", "/", "-", "D", "d"):
            fixed += 2; strong = True            # comment / continuation / debug indicator in col 7
        elif ind == " ":
            fixed += 1                           # normal fixed line - but also free indented to col 8
        else:
            free += 2; strong = True             # a code character sits in the indicator column
    return free, fixed, strong


def _shape_score(source: str, fmt: SourceFormat) -> int:
    """How many distinct COBOL structural anchors survive normalization under ``fmt``.

    A correct format recovers several (IDENTIFICATION/PROGRAM-ID/PROCEDURE ...); the
    wrong format mangles them, so this cheaply verifies an otherwise-ambiguous guess.
    """
    joined = "\n".join(cl.text for cl in normalize(source, fmt))
    return len({m.group(1).split()[0].upper() for m in _COBOL_ANCHORS.finditer(joined)})


def detect_source_format(source: str) -> FormatDetection:
    """Auto-detect fixed vs. free format, with a confidence and a human-readable reason.

    Layered so that certainty degrades gracefully: an explicit directive wins; else
    clear column signals; else a shape check that normalizes both ways and keeps the
    one yielding recognizable COBOL; else the z/OS default (fixed) at low confidence.
    """
    raw_lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    directive = _directive_format(raw_lines)
    if directive is not None:
        return FormatDetection(directive, 1.0, "explicit source-format directive")

    free, fixed, strong = _score_columns(raw_lines)
    total = free + fixed
    if strong and total:
        margin = abs(free - fixed) / total
        if margin >= 0.2:
            fmt = SourceFormat.FREE if free > fixed else SourceFormat.FIXED
            return FormatDetection(fmt, min(0.97, 0.65 + margin * 0.3),
                                   f"column signals (free={free}, fixed={fixed})")

    # Ambiguous columns: let each format prove itself by recovering COBOL structure.
    fixed_shape = _shape_score(source, SourceFormat.FIXED)
    free_shape = _shape_score(source, SourceFormat.FREE)
    if fixed_shape != free_shape:
        fmt = SourceFormat.FIXED if fixed_shape > free_shape else SourceFormat.FREE
        lead = abs(fixed_shape - free_shape)
        return FormatDetection(fmt, 0.8 if lead >= 2 else 0.65,
                               f"shape check (fixed={fixed_shape}, free={free_shape})")

    # Genuinely indistinguishable: default to the z/OS norm, low confidence.
    fmt = SourceFormat.FREE if free > fixed else SourceFormat.FIXED
    return FormatDetection(
        fmt, 0.3,
        f"ambiguous - no directive or diagnostic signals; defaulted to {fmt.value} "
        "(z/OS norm). Pass --format to override.")


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
