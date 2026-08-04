"""The deprecated JCL auto-fork: `cobol-xstate job.jcl` delegates to jcl-dependencies.

Kept for one release so existing scripts keep working. What these tests pin is the
DELEGATION: the auto-fork must produce byte-for-byte what the real `jcl-dependencies`
command produces, because two implementations of the same path is exactly what the
repository split removed. The pure-JCL CLI tests live in the jcl-dependencies
repository, beside the code they test.

Skipped entirely when the JCL package is not installed - a COBOL install without the
[jcl] extra is a complete install, and its suite should say so rather than fail.
"""

import sys
from pathlib import Path

import pytest

pytest.importorskip(
    "jcl_dependencies",
    reason="the JCL half is an optional extra: pip install cobol-xstate[jcl]")

sys.path.insert(0, str(Path(__file__).resolve().parent))   # so --fetcher fakes.… loads

from cobol_xstate.cli import run as cobol_run                       # noqa: E402
from jcl_dependencies.cli import run as jcl_run                     # noqa: E402

FAKE = "fakes.estate:fetch_artifact"

# Self-contained jobs, written to tmp_path per test: this repository no longer carries
# JCL example files - the jcl-dependencies repository owns those - and these tests only
# need SOME job to run through both entry points.
PLAIN_JOB = (
    "//AGREEJOB JOB (ACCT),'AGREEMENT'\n"
    "//STEP01   EXEC PGM=IEBGENER\n"
    "//SYSUT1   DD DSN=PROD.IN.FILE,DISP=SHR\n"
    "//SYSUT2   DD DSN=PROD.OUT.FILE,DISP=(NEW,CATLG,DELETE)\n"
    "//SYSIN    DD DUMMY\n"
)
INSTREAM_JOB = (
    "//SORTJOB  JOB (ACCT),'SORT'\n"
    "//S1       EXEC PGM=SORT\n"
    "//SORTIN   DD DSN=PROD.RAW,DISP=SHR\n"
    "//SORTOUT  DD DSN=PROD.SORTED,DISP=(NEW,CATLG,DELETE)\n"
    "//SYSIN    DD *\n"
    "  SORT FIELDS=(1,5,CH,A)\n"
    "/*\n"
)


def _files(d: Path):
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(d.glob("*.json"))}


@pytest.mark.parametrize("name,text", [("plain.jcl", PLAIN_JOB),
                                       ("instream.jcl", INSTREAM_JOB)])
def test_the_auto_fork_and_the_real_command_agree_byte_for_byte(tmp_path, name, text):
    job = tmp_path / name
    job.write_text(text, encoding="utf-8")
    a, b = tmp_path / "real", tmp_path / "fork"
    assert jcl_run([str(job), "--outdir", str(a), "--fetcher", FAKE,
                    "--jobs", "1", "-q"]) == 0
    assert cobol_run([str(job), "--outdir", str(b), "--fetcher", FAKE,
                      "--jobs", "1", "-q"]) == 0
    assert _files(a) == _files(b)


def test_the_auto_fork_writes_all_four_files_with_the_new_format_names(tmp_path):
    import json
    job = tmp_path / "sort.jcl"
    job.write_text(INSTREAM_JOB, encoding="utf-8")
    out = tmp_path / "o"
    assert cobol_run([str(job), "--outdir", str(out), "-q"]) == 0
    assert {p.name.split(".", 1)[1] for p in out.glob("*.json")} == {
        "jcl.artifacts.json", "jcl.lineage.json",
        "jcl.prefetch.json", "jcl.fetch.json"}
    art = json.loads((out / "sort.jcl.artifacts.json").read_text())
    assert art["format"] == "jcl-dependencies-artifacts"
