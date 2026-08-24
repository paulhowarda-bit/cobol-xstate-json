"""--from-parse at the CLI: the two-step run, its refusals, and the offline combo."""

import json
from pathlib import Path

from cobol_parse.cli import run as parse_run
from cobol_xstate.cli import run as model_run

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

_PROG = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. TWOSTEP.\n"
    "       DATA DIVISION.\n"
    "       WORKING-STORAGE SECTION.\n"
    "       01  WS-N PIC S9(4) COMP-3 VALUE 0.\n"
    "       PROCEDURE DIVISION.\n"
    "       0000-MAIN.\n"
    "           PERFORM 1000-A\n"
    "           STOP RUN.\n"
    "       1000-A.\n"
    "           ADD 1 TO WS-N.\n"
)


def _produce(tmp_path, source=_PROG, name="twostep.cbl"):
    src = tmp_path / name
    src.write_text(source, encoding="utf-8")
    bundle = tmp_path / (src.stem + ".parse.json")
    rc = parse_run([str(src), "-o", str(bundle), "--no-fetch", "-qq"])
    assert rc == 0
    return src, bundle


def test_from_parse_writes_the_same_eight_files_a_direct_run_does(tmp_path):
    src, bundle = _produce(tmp_path)
    rc = model_run([str(src), "--from-parse", str(bundle), "--no-fetch",
                    "--outdir", str(tmp_path / "two"), "-qq"])
    assert rc == 0
    rc = model_run([str(src), "--no-fetch", "--outdir", str(tmp_path / "one"), "-qq"])
    assert rc == 0
    one = sorted(p.name for p in (tmp_path / "one").glob("*.json"))
    two = sorted(p.name for p in (tmp_path / "two").glob("*.json"))
    assert one == two and len(one) == 8
    for name in one:
        assert ((tmp_path / "one" / name).read_bytes()
                == (tmp_path / "two" / name).read_bytes()), name


def test_from_parse_refuses_a_stale_source(tmp_path):
    src, bundle = _produce(tmp_path)
    src.write_text(_PROG + "       2000-B.\n           STOP RUN.\n", encoding="utf-8")
    rc = model_run([str(src), "--from-parse", str(bundle), "--no-fetch",
                    "--outdir", str(tmp_path / "o"), "-qq"])
    assert rc != 0  # a stale Program is silently wrong; the run must not proceed


def test_from_parse_refuses_a_conflicting_format_override(tmp_path):
    src, bundle = _produce(tmp_path)
    rc = model_run([str(src), "--from-parse", str(bundle), "--format", "free",
                    "--no-fetch", "--outdir", str(tmp_path / "o"), "-qq"])
    assert rc != 0


def test_from_parse_conflicts_with_gather_only(tmp_path):
    src, bundle = _produce(tmp_path)
    rc = model_run([str(src), "--from-parse", str(bundle),
                    "--gather-only", str(tmp_path / "b"), "-qq"])
    assert rc == 2


def test_from_parse_with_a_missing_bundle_is_exit_2(tmp_path):
    src = tmp_path / "twostep.cbl"
    src.write_text(_PROG, encoding="utf-8")
    rc = model_run([str(src), "--from-parse", str(tmp_path / "absent.json"),
                    "--outdir", str(tmp_path / "o"), "-qq"])
    assert rc == 2


def test_from_parse_composes_with_from_bundle_fully_offline(tmp_path):
    """The maximum-speedup path: estate answers from the estate bundle, the Program
    from the parse bundle - no network, no parse."""
    example = EXAMPLES / "custrpt.cbl"
    estate = tmp_path / "estate"
    rc = model_run([str(example), "--gather-only", str(estate), "-qq"])
    assert rc == 0
    parse = tmp_path / "custrpt.parse.json"
    rc = parse_run([str(example), "-o", str(parse),
                    "--from-bundle", str(estate), "-qq"])
    assert rc == 0
    out = tmp_path / "offline"
    rc = model_run([str(example), "--from-bundle", str(estate),
                    "--from-parse", str(parse), "--outdir", str(out), "-qq"])
    assert rc == 0
    assert len(list(out.glob("*.json"))) == 8
    # And the bundle it wrote is a real machine for the right program.
    doc = json.loads((out / "custrpt.json").read_text(encoding="utf-8"))
    assert doc["metadata"]["program"] == "CUSTRPT"
