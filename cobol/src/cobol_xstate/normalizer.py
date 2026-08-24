"""Moved to the cobol-parse distribution (parser/src/cobol_parse/normalizer.py);
re-exported here so existing imports keep working."""

from cobol_parse.normalizer import (  # noqa: F401
    CodeLine,
    FormatDetection,
    SourceFormat,
    detect_source_format,
    normalize,
)
