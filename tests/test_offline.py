"""Gather on the estate-connected box, model on one with no network.

This is the end-to-end form of the claim bundle.py makes in the small: a run driven from
a bundle produces the same MODEL and the same retrieval ACCOUNT as the run that gathered
it. If that is not true, the whole point of separating the two halves is lost, because
the offline answer would silently be a different analysis.

The estate here is tests/fakes/estate.py, which answers from a fixed table and covers
every outcome the reports distinguish - including a member the request FAILS on, which
must stay distinguishable from one the estate simply does not have.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cobol_xstate.cli import run                                    # noqa: E402

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples"
FAKE = "fakes.estate:fetch_artifact"

# lineage.cbl reaches the widest set of outcomes in the fake estate: a copybook that is
# local, one that must be fetched, a program found only by the second probe, and calls
# that are not fetchable at all.
SUBJECT = "lineage.cbl"


def _views(outdir: Path):
    return {p.name: p.read_text(encoding="utf-8")
            for p in sorted(outdir.glob("*.json"))}


def _no_paths(report: dict) -> dict:
    """Every reported fact except the ones naming a local filesystem path.

    `copiedTo` and a locally-resolved member's `source` describe where a file is on THIS
    machine; a bundle reproduces the estate's answers, not the layout of the box that
    made them. Everything else must match exactly.
    """
    out = json.loads(json.dumps(report))
    for row in out.get("members", []) + out.get("artifacts", []):
        row.pop("copiedTo", None)
        row.pop("source", None)
    return out


@pytest.fixture
def gathered(tmp_path):
    """A bundle for SUBJECT, gathered against the fake estate."""
    bundle = tmp_path / "bundle"
    rc = run([str(EXAMPLES / SUBJECT), "--outdir", str(tmp_path / "gather-out"),
              "--fetcher", FAKE, "--gather-only", str(bundle), "-I", str(EXAMPLES),
              "--jobs", "1", "-q"])
    assert rc == 0
    return bundle


def test_gather_writes_a_bundle_and_no_views(gathered):
    assert (gathered / "estate-bundle.json").is_file()
    assert (gathered / "source" / SUBJECT).is_file()
    # The product of this mode is the bundle; the two reports travel with it for reading,
    # but none of the machine views are built.
    assert not list(gathered.glob("*.business.json"))
    assert not list(gathered.glob("*.reactive.json"))


def test_the_offline_run_needs_no_estate_client_at_all(gathered, tmp_path):
    out = tmp_path / "offline"
    # No --fetcher: the bundle IS the service. A default run would otherwise warn that
    # the estate is unreachable and resolve nothing.
    rc = run([str(EXAMPLES / SUBJECT), "--outdir", str(out),
              "--from-bundle", str(gathered), "-I", str(EXAMPLES), "--jobs", "1", "-q"])
    assert rc == 0
    assert (out / "lineage.json").is_file()


def test_offline_reproduces_the_live_model_byte_for_byte(gathered, tmp_path):
    live, offline = tmp_path / "live", tmp_path / "offline"
    assert run([str(EXAMPLES / SUBJECT), "--outdir", str(live), "--fetcher", FAKE,
                "-I", str(EXAMPLES), "--jobs", "1", "-q"]) == 0
    assert run([str(EXAMPLES / SUBJECT), "--outdir", str(offline),
                "--from-bundle", str(gathered), "-I", str(EXAMPLES),
                "--jobs", "1", "-q"]) == 0

    live_views, offline_views = _views(live), _views(offline)
    assert set(live_views) == set(offline_views)

    # The machine and every view projected from it must be identical bytes - these carry
    # no filesystem paths at all, so there is nothing to normalize away.
    for name in sorted(set(live_views) - {"lineage.prefetch.json", "lineage.fetch.json"}):
        assert offline_views[name] == live_views[name], f"{name} differs offline"


def test_offline_reproduces_the_retrieval_ACCOUNT_too(gathered, tmp_path):
    """Not just the model: every status, reason, count and ordering in both reports."""
    live, offline = tmp_path / "live", tmp_path / "offline"
    assert run([str(EXAMPLES / SUBJECT), "--outdir", str(live), "--fetcher", FAKE,
                "-I", str(EXAMPLES), "--jobs", "1", "-q"]) == 0
    assert run([str(EXAMPLES / SUBJECT), "--outdir", str(offline),
                "--from-bundle", str(gathered), "-I", str(EXAMPLES),
                "--jobs", "1", "-q"]) == 0

    for report in ("lineage.prefetch.json", "lineage.fetch.json"):
        a = json.loads((live / report).read_text(encoding="utf-8"))
        b = json.loads((offline / report).read_text(encoding="utf-8"))
        assert _no_paths(b) == _no_paths(a), f"{report} differs offline"


def test_a_failed_request_is_still_an_error_offline_not_an_absence(gathered, tmp_path):
    """ABENDL RAISES in the fake estate. If a bundle turned that into 'not found', the
    offline report would claim the estate was asked and had nothing - the one confusion
    this tool's reporting exists to prevent."""
    out = tmp_path / "offline"
    assert run([str(EXAMPLES / SUBJECT), "--outdir", str(out),
                "--from-bundle", str(gathered), "-I", str(EXAMPLES),
                "--jobs", "1", "-q"]) == 0
    rows = json.loads((out / "lineage.fetch.json").read_text())["artifacts"]
    statuses = {r["artifact"]: r["status"] for r in rows}
    # SUBFEE is found only by the second probe; that it is `fetched` proves the probe
    # chain replayed its miss as well as its hit.
    assert statuses.get("SUBFEE") == "fetched"


def test_no_fetch_says_it_was_switched_off_not_that_the_estate_was_empty(tmp_path):
    # db2diag.cbl, because it EXEC SQL INCLUDEs SQLCA - a member that is NOT on the
    # local search path, so the run genuinely has to decide what to say about it.
    out = tmp_path / "o"
    assert run([str(EXAMPLES / "db2diag.cbl"), "--outdir", str(out), "--fetcher", FAKE,
                "--no-fetch", "-I", str(EXAMPLES), "-q"]) == 0
    pre = json.loads((out / "db2diag.prefetch.json").read_text())
    reasons = {r.get("reason", "") for r in pre["members"] if r["status"] == "no-service"}
    assert any("disabled for this run" in r for r in reasons), reasons
    # ...and it must not be reported as the estate having been asked.
    assert not [r for r in pre["members"] if r["status"] == "not-found"]


def test_gather_and_from_bundle_together_is_refused(tmp_path):
    rc = run([str(EXAMPLES / SUBJECT), "--outdir", str(tmp_path / "o"),
              "--gather-only", str(tmp_path / "b"),
              "--from-bundle", str(tmp_path / "b"), "-q"])
    assert rc == 2


def test_an_unreadable_bundle_exits_cleanly(tmp_path):
    rc = run([str(EXAMPLES / SUBJECT), "--outdir", str(tmp_path / "o"),
              "--from-bundle", str(tmp_path / "nowhere"), "-q"])
    assert rc == 2


def test_replaying_a_bundle_gathered_for_another_program_warns(gathered, tmp_path,
                                                               capsys):
    run([str(EXAMPLES / "accum.cbl"), "--outdir", str(tmp_path / "o"),
         "--from-bundle", str(gathered), "-I", str(EXAMPLES)])
    assert "differs from the one the bundle was gathered for" in capsys.readouterr().err
