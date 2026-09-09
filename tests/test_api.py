"""The `Analysis` view surface: every builder is asked at most once.

The class docstring promises "the view builders are memoized because several of them are
genuinely expensive and a default run asks for the same object more than once." Three of
the five did not follow it (upstream ledger batch 10, item 29). These tests pin the promise
so the next view added inherits it.

Worth recording what measurement showed, because the ledger's stated reason was wrong and
someone will otherwise re-derive it: `lineage()` was ALREADY returning the same object,
memoized one layer down on the Machine (`statechart._lineage_cache`, `_Lineage._view`), and
`reactive()`'s expensive lowering was already cached too (`_lowered_cache`). Only
`business()` genuinely recomputed end to end. The guards here buy object identity, which is
what a caller holding a view both to write it and to load it back needs; they do not buy
back a fixpoint that was never being recomputed.
"""

from pathlib import Path

import pytest

import cobol_xstate.api
from cobol_xstate.api import analyze
from cobol_xstate.errors import ReactiveLoweringError

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
SOURCE = (EXAMPLES / "custrpt.cbl").read_text()
#: Its reactive lowering refuses - a CICS handler region lowers to type:parallel.
REFUSED_SOURCE = (EXAMPLES / "cicsinq.cbl").read_text()


def _analysis(source: str = SOURCE, name: str = "custrpt.cbl"):
    return analyze(source, source_name=name, retrieve=False)


def test_each_view_is_built_once():
    """Asking twice returns the same object, so an expensive builder runs once."""
    a = _analysis()
    assert a.lineage() is a.lineage()
    assert a.business() is a.business()
    assert a.reactive() is a.reactive()
    assert a.artifacts() is a.artifacts()          # already true; pinned so it stays true
    assert a.dynamic_calls() is a.dynamic_calls()  # likewise


def test_the_lineage_builder_is_called_once(monkeypatch):
    calls = []
    real = cobol_xstate.api.build_lineage
    monkeypatch.setattr(cobol_xstate.api, "build_lineage",
                        lambda m: (calls.append(1), real(m))[1])
    a = _analysis()
    a.lineage()
    a.lineage()
    assert len(calls) == 1


def test_the_business_builder_is_called_once(monkeypatch):
    """The one that genuinely recomputed: _BusinessView is rebuilt per call."""
    calls = []
    real = cobol_xstate.api.build_business_view
    monkeypatch.setattr(cobol_xstate.api, "build_business_view",
                        lambda m: (calls.append(1), real(m))[1])
    a = _analysis()
    a.business()
    a.business()
    assert len(calls) == 1


def test_a_refused_reactive_lowering_raises_every_time():
    """A refusal is a fact about the program, not a cached failure.

    Caching on success only is what makes this correct without special-casing: the field
    stays None when the builder raises, so the next call re-raises rather than returning a
    remembered failure - or, worse, None."""
    a = _analysis(REFUSED_SOURCE, "cicsinq.cbl")
    with pytest.raises(ReactiveLoweringError):
        a.reactive()
    with pytest.raises(ReactiveLoweringError):
        a.reactive()


def test_a_memoized_view_is_not_shared_between_analyses():
    """The cache is per-Analysis. Two runs of the same source are two objects, because a
    caller that mutates one must not reach into the other."""
    assert _analysis().lineage() is not _analysis().lineage()
