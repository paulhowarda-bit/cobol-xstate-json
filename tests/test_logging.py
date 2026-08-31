"""Error handling and logging.

Covers the contract added when the tool grew a real logging layer:

* the exception hierarchy (one CobolXstateError base; old bases preserved),
* the library logging contract (a NullHandler on the package logger; nothing configured on
  import; the CLI logger stays inside the package hierarchy),
* CLI verbosity (-v / -q / -qq) and that a default run stays as chatty as before,
* the top-level error boundary: an unexpected exception becomes a clean message + exit 1,
  with the raw traceback shown only under --debug; an expected CobolXstateError is reported
  by message; and the historical exit codes (2 = bad input) still hold.
"""

import logging

import pytest

from cobol_xstate import api, cli
from cobol_xstate.cli import run
from cobol_xstate.errors import (
    CobolXstateError,
    CopybookError,
    ParseError,
    ReactiveLoweringError,
    SourceFormatError,
)
from cobol_xstate import PACKAGE_LOGGER
from cobol_parser import PACKAGE_LOGGER as PARSE_LOGGER
from mainframe_artifacts.artifact_service import ServiceUnavailable
from mainframe_artifacts.logging_setup import PACKAGE_LOGGER as CORE_LOGGER
from mainframe_artifacts.logging_setup import configure_logging, level_for
from cobol_xstate.runtime_assets import RuntimeAssetMissing

#: Every top-level logger root the CLI can emit from. A root nobody configures does not
#: merely lose messages - it propagates to the root logger, or falls back to logging's
#: lastResort and prints straight to stderr. These are asserted together on purpose.
LOGGER_ROOTS = (CORE_LOGGER, PARSE_LOGGER, PACKAGE_LOGGER)

_PROG = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. HELLO.\n"
    "       PROCEDURE DIVISION.\n"
    "       0000-MAIN.\n"
    "           DISPLAY 'HI'.\n"
    "           STOP RUN.\n"
)


def _src(tmp_path):
    p = tmp_path / "hello.cbl"
    p.write_text(_PROG)
    return str(p)


@pytest.fixture
def pkg_logger():
    """Snapshot and restore EVERY logger root so a logging test cannot leak global
    handler/level/propagate state into the rest of the suite. Yields the front-end's
    logger, which is the one most tests poke at."""
    saved = {}
    for name in LOGGER_ROOTS:
        lg = logging.getLogger(name)
        saved[name] = (lg.level, lg.propagate, list(lg.handlers))
    try:
        yield logging.getLogger(PACKAGE_LOGGER)
    finally:
        for name, (level, propagate, handlers) in saved.items():
            lg = logging.getLogger(name)
            lg.setLevel(level)
            lg.propagate = propagate
            lg.handlers[:] = handlers


# --------------------------------------------------------------------------- #
# exception hierarchy
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("exc", [
    SourceFormatError, ParseError, CopybookError,
    ReactiveLoweringError, RuntimeAssetMissing, ServiceUnavailable,
])
def test_every_domain_error_derives_from_the_one_base(exc):
    assert issubclass(exc, CobolXstateError)


def test_reactive_error_is_still_a_notimplementederror():
    # cli.py and callers historically catch NotImplementedError for the reactive refusal.
    assert issubclass(ReactiveLoweringError, NotImplementedError)


def test_runtime_asset_missing_is_still_a_runtimeerror():
    assert issubclass(RuntimeAssetMissing, RuntimeError)


# --------------------------------------------------------------------------- #
# library logging contract
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("root", LOGGER_ROOTS)
def test_package_logger_has_a_nullhandler(root):
    # Every package that logs attaches its own NullHandler on import; one that forgot
    # would write to stderr the moment a host application imported it.
    handlers = logging.getLogger(root).handlers
    assert any(isinstance(h, logging.NullHandler) for h in handlers)


def test_unconfigured_library_never_writes_to_stderr(pkg_logger, capsys):
    # With only the package NullHandler and no application/root handler, a library log call
    # must stay silent (the NullHandler suppresses logging's lastResort stderr fallback).
    root = logging.getLogger()
    root_saved = root.handlers[:]
    root.handlers[:] = []
    try:
        pkg_logger.handlers[:] = [logging.NullHandler()]
        pkg_logger.setLevel(logging.NOTSET)
        pkg_logger.propagate = True
        logging.getLogger("cobol_xstate.demo").warning("must-not-appear")
        captured = capsys.readouterr()
        assert "must-not-appear" not in captured.err
        assert "must-not-appear" not in captured.out
    finally:
        root.handlers[:] = root_saved


def test_cli_logger_stays_in_the_package_hierarchy():
    # Regression guard: `python -m cobol_xstate.cli` makes __name__ == "__main__", which
    # would put the logger outside configure_logging's reach and silently drop progress.
    assert cli._log.name.startswith("cobol_xstate")


# --------------------------------------------------------------------------- #
# level mapping + configuration
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("verbose,quiet,expected", [
    (0, 0, logging.INFO),
    (1, 0, logging.DEBUG),
    (2, 0, logging.DEBUG),
    (0, 1, logging.WARNING),
    (0, 2, logging.ERROR),
    (0, 3, logging.ERROR),
    (1, 1, logging.WARNING),   # quiet wins over verbose
])
def test_level_for(verbose, quiet, expected):
    assert level_for(verbose, quiet) == expected


@pytest.mark.parametrize("root", LOGGER_ROOTS)
def test_configure_logging_is_idempotent(pkg_logger, root):
    configure_logging(0, 0, loggers=LOGGER_ROOTS)
    configure_logging(0, 0, loggers=LOGGER_ROOTS)
    configure_logging(1, 0, loggers=LOGGER_ROOTS)
    streams = [h for h in logging.getLogger(root).handlers
               if isinstance(h, logging.StreamHandler)
               and not isinstance(h, logging.NullHandler)]
    assert len(streams) == 1   # replaced each call, never stacked


def test_configure_logging_configures_every_root_it_is_given(pkg_logger):
    """The regression this whole parameter exists for: core and the front-end are
    separate logger trees, and a root left unconfigured propagates to the root logger or
    prints via logging's lastResort."""
    configured = configure_logging(0, 0, loggers=LOGGER_ROOTS)
    assert [lg.name for lg in configured] == list(LOGGER_ROOTS)
    for lg in configured:
        assert lg.propagate is False
        assert lg.level == logging.INFO


# --------------------------------------------------------------------------- #
# CLI verbosity, end to end
# --------------------------------------------------------------------------- #

def test_default_run_still_prints_progress(pkg_logger, tmp_path, capsys):
    assert run([_src(tmp_path), "--outdir", str(tmp_path / "o")]) == 0
    err = capsys.readouterr().err
    assert "wrote" in err
    assert "detected source format" in err


def test_quiet_hides_progress_but_keeps_warnings(pkg_logger, tmp_path, capsys):
    """`-q` is a LEVEL, not a mute button. The warning it must keep is forced into
    existence here rather than inherited from whatever estate client the machine has:
    asserting `"WARNING" in err` against an ambient condition passes only on a box with
    no estate access, and this needs SOME warning to be guaranteed."""
    from fakes.estate import NO_ESTATE_CLIENT
    assert run([_src(tmp_path), "--outdir", str(tmp_path / "o"), "-q",
                "--fetcher", NO_ESTATE_CLIENT]) == 0
    err = capsys.readouterr().err
    assert "wrote" not in err                 # INFO progress suppressed
    assert "WARNING" in err                    # the estate-unavailable warning still shows


def test_double_quiet_silences_everything_but_errors(pkg_logger, tmp_path, capsys):
    """Forced for the same reason, and here it is what gives the test its meaning: the
    warning whose absence is asserted is one the run WOULD have emitted, so this now
    tells `-qq` silenced it apart from nothing having been logged at all. Against an
    ambient estate it passed vacuously."""
    from fakes.estate import NO_ESTATE_CLIENT
    assert run([_src(tmp_path), "--outdir", str(tmp_path / "o"), "-qq",
                "--fetcher", NO_ESTATE_CLIENT]) == 0
    assert capsys.readouterr().err.strip() == ""


# --------------------------------------------------------------------------- #
# error boundary
# --------------------------------------------------------------------------- #

def _raise(exc):
    def boom(*a, **k):
        raise exc
    return boom


def test_unexpected_error_is_reported_without_a_traceback(pkg_logger, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(api, "build_machine", _raise(ValueError("kaboom")))
    rc = run([_src(tmp_path), "--outdir", str(tmp_path / "o")])
    err = capsys.readouterr().err
    assert rc == 1
    assert "Traceback" not in err
    assert "internal error" in err
    # The one-liner carries the ACTUAL failure: a caller that captures stderr (a batch
    # tracer, CI) must record the reason, not just a pointer at a --debug re-run it
    # will never do.
    assert "ValueError" in err and "kaboom" in err


def test_debug_flag_reraises_the_full_traceback(pkg_logger, tmp_path, monkeypatch):
    monkeypatch.setattr(api, "build_machine", _raise(ValueError("kaboom")))
    with pytest.raises(ValueError):
        run([_src(tmp_path), "--outdir", str(tmp_path / "o"), "--debug"])


def test_expected_error_reports_its_message_not_a_traceback(pkg_logger, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(api, "build_machine",
                        _raise(CobolXstateError("no PROCEDURE DIVISION found")))
    rc = run([_src(tmp_path), "--outdir", str(tmp_path / "o")])
    err = capsys.readouterr().err
    assert rc == 1
    assert "Traceback" not in err
    assert "no PROCEDURE DIVISION found" in err


def test_missing_source_file_still_exits_2(pkg_logger, tmp_path, capsys):
    rc = run([str(tmp_path / "does_not_exist.cbl"), "--outdir", str(tmp_path / "o")])
    assert rc == 2
