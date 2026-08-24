"""Moved to the cobol-parser distribution (cobol-parser/src/cobol_parser/data_division.py);
re-exported here so existing imports keep working."""

from cobol_parser.data_division import (  # noqa: F401
    DataItem,
    PicType,
    expand_pic,
    parse_data_division,
    parse_pic,
)
