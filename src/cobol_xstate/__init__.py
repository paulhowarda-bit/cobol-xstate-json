"""cobol_xstate - recover IBM Enterprise COBOL control flow as an XState v5 JSON
Harel statechart.

Pipeline (see references in the ibm-cobol skill):

    raw source
      -> normalizer   : fixed/free format, column-7 handling, continuation, comments
      -> lexer        : tokens carrying source-line provenance
      -> parser       : PROCEDURE DIVISION sections/paragraphs + a control-flow
                        statement AST (IF / EVALUATE / PERFORM / GO TO / I/O /
                        terminators / CALL / ALTER)
      -> cfg          : paragraph/section control-flow graph
      -> statechart   : bare XState v5 createMachine *config* as serializable JSON,
                        with guards/actions as named strings and a provenance map

Design rule (modernization contract): NO invented guard/action logic. Every state,
guard, and action name traces back to its COBOL origin via the provenance table, and
constructs a static pass cannot resolve (ALTER, computed GO TO, dynamic CALL, CICS
HANDLE) are flagged rather than smoothed over.
"""

from .normalizer import normalize, CodeLine, SourceFormat
from .lexer import tokenize, Token
from .parser import parse_program, Program, Paragraph
from .runtime_assets import RUNTIME_FILES, read_runtime_asset, runtime_asset_path
from .statechart import build_machine, Machine

__all__ = [
    "normalize",
    "CodeLine",
    "SourceFormat",
    "tokenize",
    "Token",
    "parse_program",
    "Program",
    "Paragraph",
    "build_machine",
    "Machine",
    "RUNTIME_FILES",
    "read_runtime_asset",
    "runtime_asset_path",
]

__version__ = "0.1.0"
