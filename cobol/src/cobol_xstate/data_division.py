"""Moved to the cobol-parse distribution (parser/src/cobol_parse/data_division.py);
re-exported here so existing imports keep working."""

from cobol_parse.data_division import (  # noqa: F401
    DataItem,
    PicType,
    expand_pic,
    parse_data_division,
    parse_pic,
)
