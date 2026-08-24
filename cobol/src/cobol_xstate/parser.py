"""Moved to the cobol-parser distribution (parser/src/cobol_parser/parser.py);
re-exported here so existing imports keep working. ``CopybookResolver`` was always
reachable through this module (the real parser imports it), so it stays reachable."""

from cobol_parser.parser import (  # noqa: F401
    CopybookResolver,
    Paragraph,
    Program,
    parse_program,
)
