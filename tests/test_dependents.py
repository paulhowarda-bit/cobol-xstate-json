"""The reverse direction: what the estate says calls this program.

The one view here whose rows are not a reading of the COBOL. Every other view traces to
the source its provenance names; these rows trace to a host index, so they stay their own
view and the three answers a lookup can give - nobody asked, asked and nothing, the
lookup broke - stay three answers all the way out to the written file.
"""

import json
from pathlib import Path

from mainframe_artifacts.dependents import DependentsLookup

from cobol_xstate.api import analyze, write_views
from cobol_xstate.dependents import FORMAT, build_dependents
from cobol_xstate.parser import parse_program
from cobol_xstate.statechart import build_machine

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
SOURCE = (EXAMPLES / "custrpt.cbl").read_text()

#: What a host index holds: a job runs CUSTRPT, and a module calls it.
_INDEX = {
    "CUSTRPT|program": [
        {"name": "RPTJOB", "kind": "JOB", "via": "EXEC PGM=CUSTRPT",
         "match_strength": "qualified", "detail": "step RPT runs it"},
        {"name": "BILLDRV", "kind": "MODULE", "via": "CALL",
         "match_strength": "bare"},
    ],
}


def _machine():
    return build_machine(parse_program(SOURCE), source_name="custrpt.cbl")


def _view(mapping=None, resolver=None):
    return build_dependents(_machine(), DependentsLookup(mapping, resolver))


# --- the view ------------------------------------------------------------------------

def test_dependents_attach_to_the_program_id():
    view = _view(_INDEX)
    row = view["provides"][0]
    assert (row["name"], row["kind"], row["provides"]) == ("CUSTRPT", "program",
                                                           "the PROGRAM-ID")
    assert [d["name"] for d in row["dependents"]] == ["BILLDRV", "RPTJOB"]
    assert row["count"] == 2


def test_the_view_has_the_family_keys_and_states_its_own_format():
    view = _view(_INDEX)
    assert list(view) == ["format", "formatVersion", "program", "source", "note",
                          "suppliedBy", "provides", "unanswered", "flags"]
    assert view["format"] == FORMAT
    assert view["formatVersion"] >= 3
    assert view["program"] == "CUSTRPT"


def test_a_row_carries_both_kinds_and_the_strength_as_its_own_field():
    rows = _view(_INDEX)["provides"][0]["dependents"]
    billdrv = next(r for r in rows if r["name"] == "BILLDRV")
    assert billdrv == {"name": "BILLDRV", "kind": "MODULE", "manifestKind": "program",
                       "via": "CALL", "matchStrength": "bare"}


def test_rows_are_sorted_so_the_hosts_order_cannot_change_the_bytes():
    flipped = {"CUSTRPT|program": list(reversed(_INDEX["CUSTRPT|program"]))}
    assert json.dumps(_view(_INDEX)) == json.dumps(_view(flipped))


def test_the_manifests_caller_row_still_points_at_this_answer():
    """The row this view exists to answer is left exactly as it was.

    `artifacts.py` emits a `caller` row saying the answer is not a retrievable member.
    Filling it in from a host index would put a row with no provenance into a manifest
    where every other row has one, so the two stay apart deliberately.
    """
    analysis = analyze(SOURCE, source_name="custrpt.cbl", retrieve=False,
                       dependents=_INDEX)
    manifest = analysis.artifacts()
    callers = [r for r in manifest["artifacts"] if r["kind"] == "caller"]
    excluded = [r for r in manifest.get("excluded", []) if r.get("kind") == "caller"]
    for row in callers + excluded:
        assert "dependents" not in row
    assert analysis.dependents()["provides"][0]["count"] == 2


# --- the three answers ----------------------------------------------------------------

def test_no_lookup_supplied_is_no_view_at_all():
    machine = _machine()
    assert build_dependents(machine, None) is None
    assert build_dependents(machine, DependentsLookup()) is None
    assert analyze(SOURCE, source_name="custrpt.cbl", retrieve=False).dependents() is None


def test_a_name_the_index_does_not_cover_is_unanswered_not_empty():
    view = _view(resolver=lambda name, kind=None: None)
    assert view["provides"] == []
    assert view["unanswered"] == [
        {"name": "CUSTRPT", "provides": "the PROGRAM-ID",
         "reason": "the lookup does not cover this name"}]


def test_asked_and_nothing_calls_it_is_said_with_an_empty_list():
    view = _view(resolver=lambda name, kind=None: [])
    assert view["provides"][0]["dependents"] == []
    assert view["unanswered"] == []


def test_a_lookup_that_breaks_is_flagged_and_the_run_completes():
    def boom(name, kind=None):
        raise RuntimeError("index unreachable")

    view = _view(resolver=boom)
    assert view["provides"] == []
    assert view["unanswered"][0]["reason"].startswith("the lookup failed earlier")
    assert "index unreachable" in view["flags"][0]
    assert "fix the lookup and re-run" in view["flags"][0]


def test_a_reported_fan_out_cap_reaches_the_view():
    def capped(name, kind=None):
        return {"rows": _INDEX["CUSTRPT|program"], "truncated": True, "total": 1206}

    row = _view(resolver=capped)["provides"][0]
    assert (row["truncated"], row["total"], row["count"]) == (True, 1206, 2)


def test_a_shape_the_contract_disallows_is_refused_rather_than_reported():
    view = _view(resolver=lambda name, kind=None: [{"name": "X", "kind": "TRIGGER"}])
    assert view["provides"] == []
    assert "not an artifact kind" in view["flags"][0]


# --- what a run writes ----------------------------------------------------------------

def test_an_unsupplied_run_writes_no_dependents_file(tmp_path):
    analysis = analyze(SOURCE, source_name="custrpt.cbl", retrieve=False)
    written = write_views(analysis, tmp_path)
    assert "dependents" not in written
    assert not (tmp_path / "custrpt.dependents.json").exists()


def test_a_supplied_run_writes_it_beside_the_others(tmp_path):
    analysis = analyze(SOURCE, source_name="custrpt.cbl", retrieve=False,
                       dependents=_INDEX)
    written = write_views(analysis, tmp_path)
    assert written["dependents"] == tmp_path / "custrpt.dependents.json"
    view = json.loads(written["dependents"].read_text(encoding="utf-8"))
    assert view["format"] == FORMAT
    # ...and no other view gained anything.
    bundle = json.loads(written["bundle"].read_text(encoding="utf-8"))
    assert "dependents" not in bundle


# --- the bundle -----------------------------------------------------------------------

def test_a_gathered_bundle_replays_the_reverse_direction(tmp_path):
    from mainframe_artifacts.bundle import open_bundle

    from cobol_xstate.api import gather

    def index(name, kind=None):
        return _INDEX.get("{0}|{1}".format(name, kind))

    live = analyze(SOURCE, source_name="custrpt.cbl", retrieve=False,
                   dependents_resolver=index).dependents()
    root = tmp_path / "bundle"
    gather(SOURCE, source_name="custrpt.cbl", dest=str(root),
           dependents_resolver=index)

    bundle = open_bundle(root)
    assert bundle.has_dependents()
    replayed = analyze(bundle.source(), source_name="custrpt.cbl",
                       bundle=bundle).dependents()
    assert replayed == live
