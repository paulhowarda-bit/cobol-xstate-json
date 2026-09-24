"""The shared Node session must fail as loudly as one process per test did (ledger item 54).

Batching the XState drivers into one process is only worth its ~37 s if a failure still
names its own case: a driver's exit code, stdout and stderr must come back as its own,
and one case failing, throwing or hanging must not change what the next one reports.
"""

import shutil

import pytest

import node_session

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node not available")


def _driver(d, body, name="drive.mjs"):
    d.mkdir(exist_ok=True)
    (d / name).write_text(body)
    return d / name


def test_a_failing_case_reports_its_own_output_and_the_next_case_passes(tmp_path):
    bad = node_session.run(_driver(tmp_path / "a", (
        "console.error('WS-SUM', 'got', 10, 'want', 11);\n"
        "process.exit(1);\n")))
    assert bad.returncode == 1
    assert bad.stderr == "WS-SUM got 10 want 11\n"
    good = node_session.run(_driver(tmp_path / "b", (
        "process.stdout.write(JSON.stringify({ok: true}));\n")))
    assert (good.returncode, good.stdout, good.stderr) == (0, '{"ok":true}', "")


def test_an_exit_code_other_than_one_is_kept(tmp_path):
    assert node_session.run(_driver(tmp_path, "process.exit(3);\n")).returncode == 3


def test_an_uncaught_throw_is_exit_one_with_its_stack(tmp_path):
    r = node_session.run(_driver(tmp_path, "throw new Error('boom in case');\n"))
    assert r.returncode == 1
    assert "Error: boom in case" in r.stderr


def test_a_module_rewritten_in_place_is_not_served_from_the_cache(tmp_path):
    """Node caches modules per URL, and several tests rewrite machine.mjs in the same
    directory for a second case - which must run the NEW machine, not the first one."""
    d = tmp_path / "m"
    drive = "import v from './machine.mjs';\nprocess.stdout.write(String(v));\n"
    _driver(d, "export default 'first';\n", "machine.mjs")
    assert node_session.run(_driver(d, drive)).stdout == "first"
    _driver(d, "export default 'second';\n", "machine.mjs")
    assert node_session.run(_driver(d, drive)).stdout == "second"


def test_a_hung_case_times_out_alone_and_the_session_restarts(tmp_path):
    hung = node_session.run(_driver(tmp_path / "a", "for (;;) {}\n"), timeout=2)
    assert hung.returncode == 1
    assert "timed out" in hung.stderr
    after = node_session.run(_driver(tmp_path / "b", "process.exit(0);\n"))
    assert after.returncode == 0, after.stderr


def test_check_parses_without_running(tmp_path):
    ok = _driver(tmp_path, "import x from 'no-such-package';\nprocess.exit(7);\n", "ok.mjs")
    assert node_session.check(ok).returncode == 0          # never linked, never run
    bad = _driver(tmp_path, "export default {;\n", "bad.mjs")
    r = node_session.check(bad)
    assert r.returncode == 1
    assert "SyntaxError" in r.stderr
