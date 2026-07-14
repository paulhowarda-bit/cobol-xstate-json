"""DATA DIVISION recovery - the typed data dictionary.

In a Harel/STATEMATE statechart the data items are first-class: actions assign to
them and conditions test them, and the items are *typed*. For COBOL that type is the
``PICTURE`` + ``USAGE`` + sign, which is exactly what determines numeric behavior
(packed vs. zoned vs. binary, digit count, decimal scale, signedness - the things
that cause S0C7s). This module recovers that dictionary so the emitted statechart can
carry the real types, not just names.

It parses level-numbered data description entries (handling multi-line entries and the
decimal point inside a ``VALUE`` literal by splitting on the *next level number*, not
on periods), derives a type descriptor from each PICTURE/USAGE, and resolves 88-level
condition-names to the (parent == value) test they stand for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .normalizer import CodeLine

_LEVEL_START = re.compile(r"^\s*(\d{1,2})[\s.]", )
_SECTIONS = ("FILE", "WORKING-STORAGE", "LOCAL-STORAGE", "LINKAGE", "REPORT", "SCREEN")


@dataclass
class PicType:
    category: str            # 'numeric' | 'numeric-edited' | 'alphanumeric' |
                             # 'alphabetic' | 'alphanumeric-edited' | 'group' | 'unknown'
    digits: int = 0          # number of digit positions (numeric)
    scale: int = 0           # digits after the implied decimal point (V)
    signed: bool = False
    usage: str = "DISPLAY"   # DISPLAY | COMP | COMP-3 | COMP-1 | COMP-2 | ...
    pic: Optional[str] = None

    def describe(self) -> str:
        if self.category == "group":
            return "group"
        if self.category.startswith("numeric"):
            s = f"{'signed ' if self.signed else ''}numeric({self.digits}"
            if self.scale:
                s += f",{self.scale}"
            return s + f") usage {self.usage}"
        return f"{self.category} usage {self.usage}"

    def to_dict(self) -> dict:
        d = {"category": self.category, "usage": self.usage}
        if self.pic:
            d["pic"] = self.pic
        if self.category.startswith("numeric"):
            d.update({"digits": self.digits, "scale": self.scale, "signed": self.signed})
        return d


@dataclass
class DataItem:
    level: int
    name: str
    line: int
    origin: Optional[str] = None   # copybook member name if from a COPY-expanded line
    section: Optional[str] = None
    pic: Optional[str] = None
    usage: Optional[str] = None
    value: Optional[str] = None
    occurs: Optional[int] = None
    # OCCURS min TO max DEPENDING ON var: `occurs` holds the MAXIMUM (the table is
    # seeded at full size); the dynamic length variable is kept here and flagged.
    occurs_depending: Optional[str] = None
    redefines: Optional[str] = None
    parent: Optional[str] = None
    is_group: bool = False
    condition_values: List[str] = field(default_factory=list)  # 88-level singleton VALUE(s)
    condition_ranges: List[List[str]] = field(default_factory=list)  # 88-level [lo, hi] THRU
    cond_parent: Optional[str] = None                          # 88-level's data item
    type: Optional[PicType] = None


_USAGES = {
    "COMP-3": "COMP-3", "COMPUTATIONAL-3": "COMP-3", "PACKED-DECIMAL": "COMP-3",
    "COMP-1": "COMP-1", "COMPUTATIONAL-1": "COMP-1",
    "COMP-2": "COMP-2", "COMPUTATIONAL-2": "COMP-2",
    "COMP-4": "COMP-4", "COMPUTATIONAL-4": "COMP-4",
    "COMP-5": "COMP-5", "COMPUTATIONAL-5": "COMP-5",
    "COMP": "COMP", "COMPUTATIONAL": "COMP", "BINARY": "COMP-4",
    "DISPLAY": "DISPLAY", "INDEX": "INDEX", "POINTER": "POINTER",
}


def _find_usage(text: str) -> Optional[str]:
    up = text.upper()
    for kw, norm in _USAGES.items():
        if re.search(rf"\b{re.escape(kw)}\b", up):
            return norm
    return None


def expand_pic(pic: str) -> str:
    """Expand repetition counts: 9(3)V99 -> 999V99, X(4) -> XXXX."""
    def rep(m):
        return m.group(1) * int(m.group(2))
    return re.sub(r"([9AXZSPV0BspnN.,/$+\-*])\((\d+)\)", rep, pic, flags=re.I)


def parse_pic(pic: Optional[str], usage: Optional[str]) -> PicType:
    u = usage or "DISPLAY"
    if not pic:
        return PicType(category="group", usage=u, pic=None)
    raw = pic
    exp = expand_pic(pic).upper()
    signed = exp.startswith("S") or "S" in exp[:1] or exp.endswith(("CR", "DB")) or "+" in exp or "-" in exp
    body = exp[1:] if exp.startswith("S") else exp
    # Editing characters (display-only numeric or alphanumeric edited).
    has_edit = any(c in exp for c in "ZB*$,/CRDB") or exp.count(".") > 0 and "V" not in exp
    if "X" in exp:
        cat = "alphanumeric-edited" if has_edit else "alphanumeric"
        return PicType(category=cat, usage=u, pic=raw, signed=False)
    if "A" in exp and "9" not in exp:
        return PicType(category="alphabetic", usage=u, pic=raw)
    if "9" in exp or "V" in exp or "P" in exp:
        digit_part = body
        # scale = digits to the right of V (or of an actual decimal point in edited)
        scale = 0
        if "V" in digit_part:
            after = digit_part.split("V", 1)[1]
            scale = sum(1 for c in after if c in "9P")
            digits = sum(1 for c in digit_part if c in "9P")
        else:
            digits = sum(1 for c in digit_part if c in "9P")
        cat = "numeric-edited" if any(c in exp for c in "ZB*$,/") or (
            "." in exp and "V" not in exp) else "numeric"
        return PicType(category=cat, digits=digits, scale=scale,
                       signed=bool(re.match(r"^S", exp)) or signed and cat == "numeric",
                       usage=u, pic=raw)
    return PicType(category="unknown", usage=u, pic=raw)


def _data_region(lines: List[CodeLine]) -> List[CodeLine]:
    start = end = None
    for i, cl in enumerate(lines):
        if start is None and re.search(r"\bDATA\s+DIVISION\b", cl.text, re.I):
            start = i
        elif start is not None and re.search(r"\bPROCEDURE\s+DIVISION\b", cl.text, re.I):
            end = i
            break
    if start is None:
        return []
    return lines[start + 1:end if end is not None else len(lines)]


def _entries(region: List[CodeLine]):
    """Yield (text, line, section, origin) for each level-numbered data entry."""
    section = None
    buf: List[str] = []
    first_line = 0
    first_origin = None
    for cl in region:
        t = cl.text.strip()
        up = t.upper()
        sec = next((s for s in _SECTIONS if up.startswith(s + " SECTION")), None)
        if sec:
            if buf:
                yield " ".join(buf), first_line, section, first_origin
                buf = []
            section = sec
            continue
        if up.startswith(("FD ", "SD ", "RD ", "FD.", "01 FD")) or up in ("FD", "SD"):
            # File/sort descriptions - skip the FD line itself; its 01 follows.
            continue
        if _LEVEL_START.match(cl.text) and re.match(r"^\s*\d", cl.text):
            if buf:
                yield " ".join(buf), first_line, section, first_origin
            buf = [t]
            first_line = cl.line
            first_origin = cl.origin
        else:
            if buf:
                buf.append(t)
    if buf:
        yield " ".join(buf), first_line, section, first_origin


def parse_data_division(lines: List[CodeLine]):
    """Return (items, by_name) recovered from the DATA DIVISION."""
    items: List[DataItem] = []
    region = _data_region(lines)
    parent_stack: List[DataItem] = []  # (group items by level)
    last_elementary: Dict[int, DataItem] = {}

    for text, line, section, origin in _entries(region):
        m = re.match(r"^(\d{1,2})\s+([A-Z0-9][A-Z0-9-]*|FILLER)\b(.*)$", text, re.I)
        if not m:
            continue
        level = int(m.group(1))
        name = m.group(2).upper()
        rest = m.group(3)
        item = DataItem(level=level, name=name, line=line, section=section, origin=origin)

        pm = re.search(r"\bPIC(?:TURE)?\b\s+(?:IS\s+)?(\S+)", rest, re.I)
        if pm:
            item.pic = pm.group(1).rstrip(".")
        item.usage = _find_usage(rest)
        vm = re.search(r"\bVALUE\b\s+(?:IS\s+)?('(?:[^']*)'|\"(?:[^\"]*)\"|\S+)", rest, re.I)
        if vm:
            item.value = vm.group(1).rstrip(".")
        om = re.search(r"\bOCCURS\b\s+(\d+)(?:\s+TO\s+(\d+))?", rest, re.I)
        if om:
            # A variable-length table (OCCURS min TO max) is sized at its MAXIMUM.
            item.occurs = int(om.group(2) or om.group(1))
            dm = re.search(r"\bDEPENDING\s+(?:ON\s+)?([A-Z0-9][A-Z0-9-]*)", rest, re.I)
            if dm:
                item.occurs_depending = dm.group(1).upper()
        rm = re.search(r"\bREDEFINES\b\s+([A-Z0-9-]+)", rest, re.I)
        if rm:
            item.redefines = rm.group(1).upper()

        if level == 88:
            # condition-name: collect its VALUE(s); parent is the last elementary item.
            # `lo THRU hi` is a range (kept as a pair, not flattened into two singletons).
            toks = re.findall(
                r"'[^']*'|\"[^\"]*\"|[+-]?\d+(?:\.\d+)?|THRU|THROUGH|[A-Za-z][A-Za-z0-9-]*",
                re.sub(r"^88\s+[A-Z0-9-]+\s+VALUES?\s+(?:ARE\s+|IS\s+)?", "",
                       text, flags=re.I))
            toks = [t.rstrip(".") for t in toks if t.rstrip(".")]
            singles: List[str] = []
            ranges: List[List[str]] = []
            i = 0
            while i < len(toks):
                if (i + 2 < len(toks) and toks[i + 1].upper() in ("THRU", "THROUGH")):
                    ranges.append([toks[i], toks[i + 2]])
                    i += 3
                elif toks[i].upper() in ("THRU", "THROUGH"):
                    i += 1  # stray keyword, skip
                else:
                    singles.append(toks[i])
                    i += 1
            item.condition_values = singles
            item.condition_ranges = ranges
            if parent_stack:
                item.cond_parent = parent_stack[-1].name
            items.append(item)
            continue

        # Maintain the group/parent stack by level number.
        while parent_stack and parent_stack[-1].level >= level:
            parent_stack.pop()
        item.parent = parent_stack[-1].name if parent_stack else None
        item.is_group = item.pic is None and level not in (66, 77)
        item.type = parse_pic(item.pic, item.usage)
        items.append(item)
        parent_stack.append(item)
        last_elementary[level] = item

    by_name: Dict[str, DataItem] = {}
    for it in items:
        by_name.setdefault(it.name, it)  # first wins on duplicate (qualified) names
    # Resolve 88 parents' types onto the condition for convenience.
    return items, by_name
