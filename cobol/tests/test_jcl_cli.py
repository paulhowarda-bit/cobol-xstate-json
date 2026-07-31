"""The JCL front-end's own command line.

Two entry points reach the JCL views: this package's ``cobol-xstate-jcl``, and the COBOL
command's auto-fork (``cobol-xstate job.jcl``), kept for one release so existing scripts
keep working. They must not drift, so the first test here runs every JCL example through
both and compares bytes - the auto-fork delegates rather than reimplementing, and this is
what says so.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cobol_xstate.cli import run as cobol_run                      # noqa: E402
from cobol_xstate_jcl.cli import run as jcl_run                    # noqa: E402

REPO = Path(__file__).resolve().parents[1]
JCL = REPO.parent / "jcl" / "examples"
FAKE = "fakes.estate:fetch_artifact"

JOBS = sorted(p for p in JCL.iterdir()
              if p.suffix.lower() in (".jcl", ".prc", ".proc"))


def _files(d: Path):
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(d.glob("*.json"))}


@pytest.mark.parametrize("job", JOBS, ids=lambda p: p.name)
def test_the_dedicated_cli_and_the_cobol_auto_fork_agree_byte_for_byte(job, tmp_path):
    a, b = tmp_path / "jcl", tmp_path / "fork"
    assert jcl_run([str(job), "--outdir", str(a), "--fetcher", FAKE,
                    "--jobs", "1", "-q"]) == 0
    assert cobol_run([str(job), "--outdir", str(b), "--fetcher", FAKE,
                      "--jobs", "1", "-q"]) == 0
    assert _files(a) == _files(b)


def test_it_writes_both_views_and_both_reports(tmp_path):
    out = tmp_path / "o"
    assert jcl_run([str(JCL / "acctunld.jcl"), "--outdir", str(out),
                    "--fetcher", FAKE, "--jobs", "1", "-q"]) == 0
    assert {p.name.split(".", 1)[1] for p in out.glob("*.json")} == {
        "jcl.artifacts.json", "jcl.lineage.json",
        "jcl.prefetch.json", "jcl.fetch.json"}


@pytest.mark.parametrize("target,expected", [
    ("artifacts", {"jcl.artifacts.json", "jcl.prefetch.json", "jcl.fetch.json"}),
    ("lineage", {"jcl.lineage.json", "jcl.prefetch.json", "jcl.fetch.json"}),
])
def test_target_selects_views_but_never_drops_the_retrieval_account(tmp_path, target,
                                                                    expected):
    """The reports are not a view you can opt out of: what was retrieved decides whether
    the model is right."""
    out = tmp_path / "o"
    assert jcl_run([str(JCL / "acctunld.jcl"), "--outdir", str(out), "--target", target,
                    "--fetcher", FAKE, "--jobs", "1", "-q"]) == 0
    assert {p.name.split(".", 1)[1] for p in out.glob("*.json")} == expected


def test_a_cobol_source_is_flagged_rather_than_parsed_as_a_job(tmp_path, capsys):
    out = tmp_path / "o"
    jcl_run([str(REPO / "examples" / "accum.cbl"), "--outdir", str(out), "-q"])
    assert "does not look like JCL" in capsys.readouterr().err


def test_max_rounds_is_exposed_and_the_bound_is_reported(tmp_path):
    """The closure bound was hard-coded at 12; hitting it must stay visible, because a
    silently truncated closure looks exactly like a job with no more members."""
    out = tmp_path / "o"
    assert jcl_run([str(JCL / "dailypost.jcl"), "--outdir", str(out),
                    "--max-rounds", "1", "--fetcher", FAKE, "--jobs", "1", "-q"]) == 0
    pre = json.loads(next(out.glob("*.jcl.prefetch.json")).read_text())
    closure = [r for r in pre["members"] if r["member"] == "<closure>"]
    assert closure and "1 resolution rounds" in closure[0]["reason"]


def test_gather_then_replay_reproduces_the_views(tmp_path):
    bundle, live, offline = tmp_path / "b", tmp_path / "live", tmp_path / "off"
    job = str(JCL / "acctunld.jcl")
    assert jcl_run([job, "--outdir", str(tmp_path / "g"), "--fetcher", FAKE,
                    "--gather-only", str(bundle), "--jobs", "1", "-q"]) == 0
    assert jcl_run([job, "--outdir", str(live), "--fetcher", FAKE,
                    "--jobs", "1", "-q"]) == 0
    # No --fetcher at all on the replay: the bundle is the service.
    assert jcl_run([job, "--outdir", str(offline), "--from-bundle", str(bundle),
                    "--jobs", "1", "-q"]) == 0
    for name in ("acctunld.jcl.artifacts.json", "acctunld.jcl.lineage.json"):
        assert (offline / name).read_text() == (live / name).read_text()


def test_python_dash_m_works():
    import os
    import subprocess
    proc = subprocess.run(
        [sys.executable, "-m", "cobol_xstate_jcl", "--help"],
        capture_output=True, text=True, cwd=str(REPO.parent),
        env={**__import__("os").environ, "PYTHONPATH": os.pathsep.join(
            str(REPO.parent / t) for t in ("core/src", "cobol/src", "jcl/src"))})
    assert proc.returncode == 0
    assert "cobol-xstate-jcl" in proc.stdout
