"""Moved to the cobol-parser distribution (parser/src/cobol_parser/normalizer.py);
re-exported here so existing imports keep working."""

from cobol_parser.normalizer import (  # noqa: F401
    CodeLine,
    FormatDetection,
    SourceFormat,
    detect_source_format,
    normalize,
)
