"""Byte layout of a record: field sizes, offsets, and when we refuse to state them.

Naming the field a dynamic CALL target is read from (``CTL-PGM-NAME``) is only half an
instruction. To actually go and read ``PROD.PARM.CNTL`` someone needs the field's
POSITION - "bytes 5-12 of an 80-byte record" - because a flat mainframe dataset has no
column headers to look it up by.

So this module computes the layout. What makes it worth its own module rather than a
helper is the part that refuses:

    **A wrong offset looks exactly like a right one.**

There is no way for a reader to tell that byte 5 should have been byte 7, and they will
find garbage and blame the data. Silence is recoverable; a confident wrong number is not.
So an offset is emitted ONLY when the arithmetic is fully determined, and every case that
is not gets the ordered field layout plus the reason the position was withheld:

* **OCCURS DEPENDING ON** - the record's length varies at run time, so every field after
  the table moves.
* **REDEFINES** among the fields - two names occupy the same bytes, so "the" offset of a
  later field depends on which view you mean.
* **SYNCHRONIZED** - the compiler inserts slack bytes to align COMP items, and how many
  depends on where the record itself starts. Detected only because ``data_division``
  captures the clause (nothing else reads it).
* **an unknown PICTURE or USAGE** - anything this tool did not fully classify.

Sizes follow IBM Enterprise COBOL:

| usage | bytes |
|---|---|
| DISPLAY | one per PICTURE character position (``V``/``P``/``S`` occupy none) |
| COMP-3 (packed) | ``digits // 2 + 1`` - digits plus a sign nibble, rounded up |
| COMP / COMP-4 / COMP-5 (binary) | 2 (1-4 digits), 4 (5-9), 8 (10-18) |
| COMP-1 / COMP-2 | 4 / 8 (single/double float) |
| group | sum of its children, times OCCURS |

``SIGN IS SEPARATE`` adds one byte to a signed DISPLAY number - exact, so it stays
provable rather than blocking the offset.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# PICTURE symbols that occupy no storage: the implied decimal point, scaling positions,
# and the sign (unless SIGN IS SEPARATE, handled by the caller).
_NO_BYTE = set("VPS")
# `X(8)` / `9(5)` - a repeat count to expand before counting positions.
_REPEAT = re.compile(r"([A-Z9])\((\d+)\)", re.I)
_BINARY_USAGES = {"COMP", "COMP-4", "COMP-5", "BINARY"}


def _expand_pic(pic: str) -> str:
    """``S9(5)V99`` -> ``S999 99`` style expansion: every symbol written out once."""
    out = pic.upper()
    while True:
        m = _REPEAT.search(out)
        if not m:
            return out
        out = out[:m.start()] + m.group(1) * int(m.group(2)) + out[m.end():]


def pic_positions(pic: Optional[str]) -> Optional[int]:
    """Character positions a PICTURE occupies, or ``None`` if it cannot be read."""
    if not pic:
        return None
    expanded = _expand_pic(str(pic).strip().rstrip("."))
    if not expanded:
        return None
    return sum(1 for ch in expanded if ch not in _NO_BYTE)


def item_size(entry: dict) -> Tuple[Optional[int], Optional[str]]:
    """Bytes one ELEMENTARY item occupies: ``(size, None)`` or ``(None, why_not)``."""
    typ = entry.get("type") or {}
    category = str(typ.get("category") or "")
    usage = str(typ.get("usage") or "DISPLAY").upper()
    digits = int(typ.get("digits") or 0)

    if category == "group":
        return None, "group items are sized from their children"
    if usage == "COMP-1":
        return 4, None
    if usage == "COMP-2":
        return 8, None
    if usage in ("INDEX", "POINTER"):
        return 4, None
    if usage == "COMP-3":
        if not digits:
            return None, f"packed-decimal item with no digit count ({typ.get('pic')})"
        return digits // 2 + 1, None
    if usage in _BINARY_USAGES:
        if not digits:
            return None, f"binary item with no digit count ({typ.get('pic')})"
        return (2 if digits <= 4 else 4 if digits <= 9 else 8), None

    positions = pic_positions(typ.get("pic"))
    if positions is None:
        return None, f"PICTURE {typ.get('pic')!r} could not be read as a byte count"
    if entry.get("signSeparate") and typ.get("signed"):
        # An exact, knowable +1 - so this does NOT make the layout unprovable.
        positions += 1
    return positions, None


def _children(data: Dict[str, dict], parent: str) -> List[Tuple[str, dict]]:
    """Immediate children of a group, in source order (the dict preserves it)."""
    return [(name, entry) for name, entry in data.items()
            if isinstance(entry, dict)
            and str(entry.get("parent") or "").upper() == parent.upper()]


def _sized(data: Dict[str, dict], name: str, entry: dict,
           blockers: List[str]) -> Optional[int]:
    """Total bytes for one item including OCCURS, appending any reason it is unknown."""
    if entry.get("occursDependingOn"):
        blockers.append(
            f"{name} OCCURS DEPENDING ON {entry['occursDependingOn']} - the record "
            f"length varies at run time, so every field after it moves")
    if entry.get("sync"):
        blockers.append(
            f"{name} is SYNCHRONIZED - the compiler inserts slack bytes to align it, "
            f"and how many depends on where the record itself starts")

    kids = _children(data, name)
    if kids:
        total = 0
        for kid_name, kid in kids:
            if kid.get("redefines"):
                # A redefinition overlays a sibling; it adds no bytes of its own, but it
                # does mean a later field's position depends on which view is meant.
                blockers.append(
                    f"{kid_name} REDEFINES {kid['redefines']} - two names occupy the "
                    f"same bytes, so a single offset would not describe both")
                continue
            size = _sized(data, kid_name, kid, blockers)
            if size is None:
                return None
            total += size
    else:
        total, why = item_size(entry)
        if total is None:
            blockers.append(f"{name}: {why}")
            return None
    occurs = entry.get("occurs")
    return total * int(occurs) if occurs else total


def record_layout(data: Dict[str, dict], record: str) -> dict:
    """The byte layout of ``record``: its fields in order, with offsets when provable.

    Returns ``{record, provable, fields: [{name, offset?, length?, pic, level}],
    size?, reason?}``. ``offset`` is 1-based, the way a mainframe reader counts columns
    ("bytes 5-12"), and every field carries its PICTURE either way - the layout alone is
    still enough to count by hand, which is the point of emitting it when the arithmetic
    is refused."""
    entry = data.get(record.upper())
    if not isinstance(entry, dict):
        return {"record": record, "provable": False, "fields": [],
                "reason": f"{record} is not a declared record in this program"}

    blockers: List[str] = []
    fields: List[dict] = []

    def walk(name: str, ent: dict, offset: int) -> Optional[int]:
        """Append leaves under `name`, returning the offset just past it."""
        kids = _children(data, name)
        if ent.get("redefines"):
            blockers.append(
                f"{name} REDEFINES {ent['redefines']} - two names occupy the same "
                f"bytes, so a single offset would not describe both")
        if not kids:
            size, why = item_size(ent)
            row = {"name": name, "level": ent.get("level"),
                   "pic": (ent.get("type") or {}).get("pic")}
            if size is None:
                blockers.append(f"{name}: {why}")
            else:
                row.update({"offset": offset, "length": size})
            fields.append(row)
            return None if size is None else offset + size
        cur = offset
        for kid_name, kid in kids:
            nxt = walk(kid_name, kid, cur)
            if nxt is None:
                return None
            cur = nxt
        return cur

    end = walk(record.upper(), entry, 1)
    total = _sized(data, record.upper(), entry, blockers)

    out: dict = {"record": record.upper(), "fields": fields}
    if blockers or end is None or total is None:
        out["provable"] = False
        out["reason"] = (
            "byte positions are NOT stated because: " + "; ".join(_dedup(blockers))
            if blockers else
            "byte positions could not be computed for every field of this record")
        out["note"] = ("the fields are listed in declaration order with their PICTUREs, "
                       "so the positions can still be counted by hand - they are simply "
                       "not asserted here, because a wrong offset is indistinguishable "
                       "from a right one to whoever reads the data")
        # An offset computed under a blocker is not trustworthy - drop them all rather
        # than emit a mix a reader cannot tell apart.
        for row in fields:
            row.pop("offset", None)
    else:
        out["provable"] = True
        out["size"] = total
    return out


def _dedup(items: List[str]) -> List[str]:
    seen: set = set()
    out: List[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def field_position(data: Dict[str, dict], field: str) -> dict:
    """Where one field sits in its own record - the form the dynamic-calls view wants.

    Returns ``{record, field, offset?, length?, provable, reason?, layout}``. The record
    is the field's topmost ancestor, since that is the unit a dataset is read in."""
    entry = data.get(field.upper())
    if not isinstance(entry, dict):
        return {"field": field, "provable": False,
                "reason": f"{field} is not a declared item in this program"}
    record = field.upper()
    while True:
        parent = data.get(record, {}).get("parent")
        if not parent:
            break
        record = str(parent).upper()

    layout = record_layout(data, record)
    out = {"record": record, "field": field.upper(),
           "provable": bool(layout.get("provable")), "layout": layout}
    if layout.get("size"):
        out["recordLength"] = layout["size"]
    hit = next((f for f in layout["fields"] if f["name"] == field.upper()), None)
    if hit and hit.get("offset"):
        out.update({"offset": hit["offset"], "length": hit["length"],
                    # "the N-byte record", never "a N-byte record": the article would be
                    # wrong for 8/11/18/80... and this string is read by humans.
                    "readAt": f"bytes {hit['offset']}-"
                              f"{hit['offset'] + hit['length'] - 1}"
                              + (f" of the {layout['size']}-byte record"
                                 if layout.get("size") else "")})
    elif layout.get("reason"):
        out["reason"] = layout["reason"]
    return out
