"""Moved to the cobol-parse distribution (parser/src/cobol_parse/preprocessor.py);
re-exported here so existing imports keep working."""

from cobol_parse.preprocessor import (  # noqa: F401
    CopybookResolver,
    PreprocessResult,
    preprocess,
    scan_copy_members,
)
