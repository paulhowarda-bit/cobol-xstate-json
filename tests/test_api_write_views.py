"""The published write half: `api.write_views` leaves what a run leaves.

Upstream ledger item 35. `api.py` published the ANALYSIS half of a run only, so a program
embedding this package reached every view in memory and then had to reimplement the
writing - the base name, the suffix per view, and the per-view error boundary - to produce
the files a run produces. The CLI now goes through the same function, which is the half of
the fix that matters: a copy in cli.py would diverge from the published one silently.

The collision guard the CLI used to carry (rename a companion to `.view<suffix>` when it
would land on the artifact just written) is GONE, and `test_every_target_has_a_distinct
_suffix` is why: every artifact of a run is one shared base plus a suffix from `_SUFFIX`,
so two of them can only collide if two targets share a suffix. That was already
unreachable in the CLI - the branch had no test and no reachable input - and it is
unreachable here by construction. The invariant is asserted instead of defended.
"""

import json
import logging
from pathlib import Path

import pytest

import cobol_xstate.api
from cobol_xstate.api import DEFAULT_TARGETS, analyze, artifact_base, write_views

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
SOURCE = (EXAMPLES / "custrpt.cbl").read_text()
#: Its reactive lowering refuses - a CICS handler region lowers to type:parallel.
REFUSED_SOURCE = (EXAMPLES / "cicsinq.cbl").read_text()


def _analysis(source: str = SOURCE, name: str = "custrpt.cbl"):
    return analyze(source, source_name=name, retrieve=False)


def test_a_default_run_s_artifacts_are_all_written(tmp_path):
    written = write_views(_analysis(), tmp_path)
    assert tuple(written) == DEFAULT_TARGETS          # a fixed order, not a set's
    for name, path in written.items():
        assert path.exists(), name
        json.loads(path.read_text(encoding="utf-8"))  # every one of them is readable
    assert written["bundle"] == tmp_path / "custrpt.json"
    assert written["dynamic-calls"] == tmp_path / "custrpt.dynamic-calls.json"


def test_every_target_has_a_distinct_suffix():
    """What makes one artifact of a run landing on another's path impossible.

    One base plus a distinct suffix per target. Add a target sharing a suffix and this
    fails here rather than by silently overwriting a bundle in someone's run.
    """
    suffixes = cobol_xstate.api._SUFFIX
    assert sorted(suffixes) == sorted(DEFAULT_TARGETS)
    assert len(set(suffixes.values())) == len(suffixes)


def test_a_source_named_like_a_companion_still_leaves_a_readable_bundle(tmp_path):
    """Their test 1: a source already ending in `.lineage.cbl`.

    The base is the source STEM, so the bundle is `cust.lineage.json` and the lineage
    companion `cust.lineage.lineage.json` - two paths, both written, neither renamed.
    Deriving the base by chopping at the first dot is what would collide, and is exactly
    what `artifact_base` does not do.
    """
    written = write_views(_analysis(name="cust.lineage.cbl"), tmp_path)
    assert written["bundle"] == tmp_path / "cust.lineage.json"
    assert written["lineage"] == tmp_path / "cust.lineage.lineage.json"
    assert json.loads(written["bundle"].read_text(encoding="utf-8"))["machine"]
    assert len(set(written.values())) == len(written)


def test_a_refused_reactive_view_still_leaves_the_other_artifacts(tmp_path, caplog):
    """Their test 2: `reactive()` refusing is a fact about the program, not a failure."""
    with caplog.at_level(logging.INFO, logger="cobol_xstate.api"):
        written = write_views(_analysis(REFUSED_SOURCE, "cicsinq.cbl"), tmp_path)
    assert "reactive" not in written
    assert not (tmp_path / "cicsinq.reactive.json").exists()
    assert written["artifacts"].exists()
    assert written["bundle"].exists()
    assert any("no reactive view" in r.message for r in caplog.records)


def test_targets_bundle_only_does_not_compute_business(tmp_path, monkeypatch):
    """Their test 3: `targets` decides what is COMPUTED, not just what is written."""
    calls = []
    real = cobol_xstate.api.build_business_view
    monkeypatch.setattr(cobol_xstate.api, "build_business_view",
                        lambda m: (calls.append(1), real(m))[1])
    written = write_views(_analysis(), tmp_path, targets=("bundle",))
    assert calls == []
    assert list(written) == ["bundle"]
    assert [p.name for p in tmp_path.iterdir()] == ["custrpt.json"]


def test_a_crashing_companion_is_a_warning_and_the_rest_still_land(tmp_path, monkeypatch,
                                                                   caplog):
    def _boom(machine):
        raise RuntimeError("view exploded")

    monkeypatch.setattr(cobol_xstate.api, "build_business_view", _boom)
    with caplog.at_level(logging.WARNING, logger="cobol_xstate.api"):
        written = write_views(_analysis(), tmp_path)
    assert "business" not in written
    assert not (tmp_path / "custrpt.business.json").exists()
    assert written["lineage"].exists() and written["artifacts"].exists()
    assert any("business view failed (RuntimeError: view exploded)" in r.message
               for r in caplog.records)


def test_debug_re_raises_a_companion_failure(tmp_path, monkeypatch):
    def _boom(machine):
        raise RuntimeError("view exploded")

    monkeypatch.setattr(cobol_xstate.api, "build_business_view", _boom)
    with pytest.raises(RuntimeError, match="view exploded"):
        write_views(_analysis(), tmp_path, debug=True)


def test_a_reactive_refusal_stays_a_note_even_under_debug(tmp_path, caplog):
    """A refusal is not a defect, so there is no traceback to want.

    `--debug` turns an isolated CRASH back into an exception; it must not turn a program
    the lowering legitimately refuses into one, which is what the CLI did before the write
    half moved here and what `debug+refused-reactive` in the A/B matrix pins.
    """
    with caplog.at_level(logging.INFO, logger="cobol_xstate.api"):
        written = write_views(_analysis(REFUSED_SOURCE, "cicsinq.cbl"), tmp_path,
                              debug=True)
    assert "reactive" not in written
    assert written["artifacts"].exists()
    assert any("no reactive view" in r.message for r in caplog.records)


def test_a_not_implemented_error_from_another_view_is_a_crash_not_a_refusal(tmp_path,
                                                                            monkeypatch,
                                                                            caplog):
    """Only `reactive` has a refusal. Elsewhere NotImplementedError is a defect, and takes
    the ordinary companion boundary rather than being reported as a refused view."""
    def _boom(machine):
        raise NotImplementedError("business is not done yet")

    monkeypatch.setattr(cobol_xstate.api, "build_business_view", _boom)
    with caplog.at_level(logging.WARNING, logger="cobol_xstate.api"):
        written = write_views(_analysis(), tmp_path)
    assert "business" not in written
    assert written["lineage"].exists()
    assert any("business view failed (NotImplementedError" in r.message
               for r in caplog.records)
    with pytest.raises(NotImplementedError):
        write_views(_analysis(), tmp_path, targets=("business",), debug=True)


def test_the_bundle_is_not_isolated(tmp_path):
    """The bundle is the run's product: a failure there is the run's failure.

    Isolating it would hand a caller an empty mapping and a zero exit code for a run that
    produced nothing - the SUMPGM01 false negative in reverse.
    """
    analysis = _analysis()
    analysis.machine_json = lambda **kw: (_ for _ in ()).throw(RuntimeError("no bundle"))
    with pytest.raises(RuntimeError, match="no bundle"):
        write_views(analysis, tmp_path, targets=("bundle",))


def test_an_unknown_target_is_refused(tmp_path):
    with pytest.raises(ValueError, match="unknown target"):
        write_views(_analysis(), tmp_path, targets=("bundle", "interface"))


def test_machine_only_trims_the_bundle_and_not_the_target_set(tmp_path):
    full = write_views(_analysis(), tmp_path / "full", targets=("bundle", "lineage"))
    trim = write_views(_analysis(), tmp_path / "trim", targets=("bundle", "lineage"),
                       machine_only=True)
    assert set(json.loads(trim["bundle"].read_text(encoding="utf-8"))) < \
        set(json.loads(full["bundle"].read_text(encoding="utf-8")))
    assert trim["lineage"].exists()     # machine_only is about the bundle, not the set


def test_a_source_with_no_filename_falls_back_to_the_program_id(tmp_path):
    """A run reading from a pipe has no stem: `<stdin>` is a placeholder, not a path."""
    written = write_views(analyze(SOURCE, source_name="<stdin>", retrieve=False),
                          tmp_path, targets=("bundle",))
    assert written["bundle"] == tmp_path / "CUSTRPT.json"
    assert artifact_base(None, "CUSTRPT") == "CUSTRPT"
    assert artifact_base("cust", "CUSTRPT") == "cust"
    assert artifact_base(None, None) == "machine"


def test_base_overrides_the_derivation(tmp_path):
    written = write_views(_analysis(), tmp_path, base="chosen", targets=("bundle",))
    assert written["bundle"] == tmp_path / "chosen.json"


def test_dest_is_created_if_it_does_not_exist(tmp_path):
    """Otherwise every view fails inside its own boundary and the caller gets {}."""
    written = write_views(_analysis(), tmp_path / "a" / "b", targets=("bundle",))
    assert written["bundle"].exists()


def test_an_embedding_caller_reproduces_a_cli_run_byte_for_byte(tmp_path):
    """The item's actual ask, end to end: same files, same bytes, no CLI.

    Compares the six views only - the two retrieval reports record HOW the run reached
    the estate (the `deps/` destination, the reason no client was available), which is
    the CLI's business to pass in and legitimately differs between the two callers.
    """
    from cobol_xstate.cli import run

    src = tmp_path / "custrpt.cbl"
    src.write_text(SOURCE)
    assert run([str(src), "--outdir", str(tmp_path / "cli"), "--no-fetch"]) == 0

    written = write_views(_analysis(), tmp_path / "api")
    views = ("bundle", "business", "lineage", "reactive", "artifacts", "dynamic-calls")
    for name in views:
        theirs = (tmp_path / "cli" / written[name].name).read_bytes()
        assert theirs == written[name].read_bytes(), name
