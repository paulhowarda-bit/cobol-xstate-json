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


class CobolXstateError(Exception):
    """Base for every error this package raises deliberately.

    Code at the boundary (the CLI, or any embedding application) can print ``str(exc)``
    as the whole user-facing message: these carry human-readable explanations, not
    developer diagnostics. Unexpected exceptions — the ones that are NOT a
    ``CobolXstateError`` — signal a bug in the tool and warrant a traceback.
    """


class SourceFormatError(CobolXstateError):
    """The source format (fixed / free) could not be determined, or is invalid."""


class ParseError(CobolXstateError):
    """The COBOL or JCL source could not be parsed into a model."""


class CopybookError(CobolXstateError):
    """A COPY member / copybook could not be resolved or expanded."""


class ReactiveLoweringError(CobolXstateError, NotImplementedError):
    """The reactive lowering refuses this program (CICS handler regions, recursive
    PERFORM, ...). Kept a ``NotImplementedError`` as well, so pre-existing
    ``except NotImplementedError`` handlers continue to catch it."""
