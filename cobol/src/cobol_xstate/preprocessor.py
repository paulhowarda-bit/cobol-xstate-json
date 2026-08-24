"""Moved to the cobol-parser distribution (parser/src/cobol_parser/preprocessor.py);
re-exported here so existing imports keep working."""

from cobol_parser.preprocessor import (  # noqa: F401
    CopybookResolver,
    PreprocessResult,
    preprocess,
    scan_copy_members,
)
