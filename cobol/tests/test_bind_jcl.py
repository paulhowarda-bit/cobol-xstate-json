"""Where the COBOL command meets JCL: --bind-jcl, and the deprecated auto-fork.

These live with the COBOL package because they drive ITS command line, even though the
parsing they rely on is the JCL package's. Each is skipped when that package is absent -
a COBOL install without the [jcl] extra is a complete install, and its test suite should
say so rather than fail.
"""

import json
from pathlib import Path

import pytest

pytest.importorskip(
    "cobol_xstate_jcl",
    reason="the JCL half is an optional extra: pip install cobol-xstate[jcl]")

from cobol_xstate.cli import run                                    # noqa: E402

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
JCL_EXAMPLES = Path(__file__).resolve().parents[2] / "jcl" / "examples"


def test_cli_bind_jcl_enriches_the_artifacts_companion(tmp_path):
    """The join both sides were built for: SQLUNLD's OUT-FILE row said 'ddname OUTDD, DSN
    in the JCL'; ACCTUNLD's STEP01 says OUTDD -> PROD.ACCT.UNLOAD."""
    assert run([str(EXAMPLES / "sqlunld.cbl"), "--target", "artifacts",
                "--bind-jcl", str(JCL_EXAMPLES / "acctunld.jcl"),
                "--outdir", str(tmp_path)]) == 0
    art = json.loads((tmp_path / "sqlunld.artifacts.json").read_text())
    row = next(a for a in art["artifacts"] if a.get("ddname") == "OUTDD")
    assert row["dataset"] == "PROD.ACCT.UNLOAD"


def test_cli_bind_jcl_missing_file_is_a_clean_error(tmp_path, capsys):
    assert run([str(EXAMPLES / "sqlunld.cbl"), "--bind-jcl", str(tmp_path / "nope.jcl"),
                "--outdir", str(tmp_path)]) == 2
    assert "no such file" in capsys.readouterr().err


def test_cli_autodetects_jcl_and_writes_both_views(tmp_path):
    """The deprecated auto-fork: `cobol-xstate job.jcl` delegates to the JCL package
    rather than carrying its own copy of that path."""
    assert run([str(JCL_EXAMPLES / "acctunld.jcl"), "--outdir", str(tmp_path)]) == 0
    names = {f.name for f in tmp_path.iterdir()}
    assert names == {"acctunld.jcl.artifacts.json", "acctunld.jcl.lineage.json",
                     # the JCL path retrieves its dependencies too, and must: a
                     # cataloged PROC carries EXEC PGM= steps that are in no other file
                     "acctunld.jcl.prefetch.json", "acctunld.jcl.fetch.json"}
    art = json.loads((tmp_path / "acctunld.jcl.artifacts.json").read_text())
    assert art["format"] == "cobol-xstate-jcl-artifacts"


def test_cli_jcl_detection_does_not_misfire_on_cobol():
    from cobol_xstate_core.detect import looks_like_jcl
    cobol = "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. T.\n"
    assert looks_like_jcl("t.cbl", cobol) is False
    assert looks_like_jcl("t.jcl", "//J JOB\n//S EXEC PGM=P\n") is True
