"""Exception hierarchy for cobol_xstate.

One base — :class:`CobolXstateError` — so the command-line boundary can catch every
*expected* failure and report it cleanly (a one-line message + a non-zero exit code),
while anything NOT derived from it is treated as an internal bug and shown with a full
traceback only under ``--debug``.

Two historical sentinel types keep a secondary base via multiple inheritance so existing
``except`` sites keep working unchanged:

  * :class:`ReactiveLoweringError` is also a ``NotImplementedError`` — the reactive
    lowering has always signalled "I refuse this program" with ``NotImplementedError``,
    and callers (e.g. the CLI) that catch that continue to catch this.
  * ``RuntimeAssetMissing`` (defined in :mod:`.runtime_assets`) is also a ``RuntimeError``.
  * ``ServiceUnavailable`` (defined in :mod:`.artifact_service`) derives from this base so
    a missing estate service is caught alongside every other expected failure.

This module imports nothing from the package, so it is safe to import from anywhere.
"""
from __future__ import annotations

# RE-EXPORTED, never redefined. The base lives at the lowest layer both front-ends
# depend on (mainframe_artifacts.errors) because ServiceUnavailable — raised by the estate
# boundary, which is core's — must derive from the SAME class this package's CLI catches.
# Defining a second CobolXstateError here would look harmless and would silently stop
# `except CobolXstateError` in cli.run from catching retrieval failures, turning an
# expected, explainable error into an "internal error" traceback.
# tests/test_logging.py::test_every_domain_error_derives_from_the_one_base is the guard.
from mainframe_artifacts.errors import CobolXstateError

# The parse-stage errors moved with the parse front-end to the cobol-parser
# distribution; re-exported here so existing imports and `except` sites keep catching
# the same classes.
from cobol_parser.errors import (CopybookError, ParseError,  # noqa: F401
                                SourceFormatError)


class ReactiveLoweringError(CobolXstateError, NotImplementedError):
    """The reactive lowering refuses this program (CICS handler regions, recursive
    PERFORM, ...). Kept a ``NotImplementedError`` as well, so pre-existing
    ``except NotImplementedError`` handlers continue to catch it."""
