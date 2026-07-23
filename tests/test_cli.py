"""CLI output destination: everything a run produces goes into --outdir, as given.

There is exactly ONE placement mechanism, and it is literal - the path you pass is the
path files appear in, with nothing appended. Bundle, all six views, both retrieval
reports, and the artifacts fetched from the estate all land there. No second flag can put
a file anywhere else, which is the property these tests exist to hold.
"""

import json
import os
from pathlib import Path

import pytest

from cobol_xstate.cli import run
from cobol_xstate.profiling import StageTimer

EXAMPLES_CBL = str(
    Path(__file__).resolve().parents[1] / "examples" / "banktran.cbl")


_PROG = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. HELLO.\n"
    "       PROCEDURE DIVISION.\n"
    "       0000-MAIN.\n"
    "           DISPLAY 'HI'.\n"
    "           STOP RUN.\n"
)


def _write_src(dir_: Path, name: str = "hello.cbl") -> Path:
    p = dir_ / name
    p.write_text(_PROG)
    return p


def _run_dir(root):
    """Where a run writes: --outdir itself, taken literally with nothing appended."""
    return Path(root)


def test_default_outdir_is_out(tmp_path, monkeypatch):
    src = _write_src(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert run([src.name]) == 0
    out = tmp_path / "out" / "hello.json"
    assert out.exists()
    json.loads(out.read_text())  # valid JSON


def test_nothing_is_written_to_the_working_directory(tmp_path, monkeypatch):
    """The reason the default is ./out and not '.': a bare run must never scatter files
    into whatever directory it happened to be invoked from."""
    src = _write_src(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert run([src.name]) == 0
    assert {p.name for p in tmp_path.iterdir()} == {"hello.cbl", "out"}


def test_outdir_relative_is_created(tmp_path, monkeypatch):
    src = _write_src(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert run([src.name, "--outdir", "build/charts"]) == 0
    assert (tmp_path / "build" / "charts" / "hello.json").exists()


def test_outdir_is_taken_literally(tmp_path, monkeypatch):
    """--outdir names the directory files appear in - nothing is appended to it, so
    `--outdir .` writes into the current directory itself."""
    src = _write_src(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert run([src.name, "--outdir", "."]) == 0
    assert (tmp_path / "hello.json").exists()


def test_outdir_absolute_path_is_created(tmp_path):
    src = _write_src(tmp_path)
    target = tmp_path / "abs" / "out"
    assert run([str(src), "--outdir", str(target)]) == 0
    assert (target / "hello.json").exists()


def test_files_are_named_after_the_source_wherever_it_lives(tmp_path, monkeypatch):
    """Running on a file in another directory still names the output after the source,
    and still writes it into --outdir rather than beside the source."""
    srcdir = tmp_path / "src"
    srcdir.mkdir()
    src = _write_src(srcdir, "payroll.cbl")
    monkeypatch.chdir(tmp_path)
    assert run([str(src), "--outdir", "out"]) == 0
    assert (tmp_path / "out" / "payroll.json").exists()
    assert not (srcdir / "payroll.json").exists()


def test_js_target_default_name_is_mjs_with_runtime(tmp_path, monkeypatch):
    src = _write_src(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = run([src.name, "--target", "js"])
    assert rc == 0
    run_dir = tmp_path / "out"
    assert (run_dir / "hello.mjs").exists()
    # The runtime lands in the SAME directory: the module imports './cobolRuntime.mjs',
    # so a relative import that does not resolve on disk is a broken deliverable.
    assert (run_dir / "cobolRuntime.mjs").exists()


# --------------------------------------------------------------------------- #
# packaging: the JS runtime must ship INSIDE the package
# --------------------------------------------------------------------------- #

def test_runtime_ships_as_package_data():
    """`--target js` emits `import ... from './cobolRuntime.mjs'`, so the runtime must
    be readable from the installed package - not located by walking up to a repo root
    that only exists in a source checkout (that shipped a dangling import)."""
    from cobol_xstate.runtime_assets import RUNTIME_FILES, read_runtime_asset
    for name in RUNTIME_FILES:
        text = read_runtime_asset(name)
        assert text.strip(), f"{name} is empty"
    assert "export function store" in read_runtime_asset("cobolRuntime.mjs")


def test_unknown_runtime_asset_rejected():
    from cobol_xstate.runtime_assets import read_runtime_asset
    with pytest.raises(ValueError):
        read_runtime_asset("nope.mjs")


def test_js_target_writes_runtime_next_to_module(tmp_path):
    """The emitted module's relative import must resolve on disk."""
    import re
    src = (Path(__file__).resolve().parents[1] / "examples" / "custrpt.cbl")
    assert run([str(src), "--target", "js", "--outdir", str(tmp_path)]) == 0
    run_dir = _run_dir(tmp_path)
    out = run_dir / "custrpt.mjs"
    runtime = run_dir / "cobolRuntime.mjs"
    assert runtime.exists(), "cobolRuntime.mjs must be written beside the module"
    # the module imports exactly that filename
    imported = re.search(r'from "(\./[^"]+\.mjs)"', out.read_text(encoding="utf-8"))
    assert imported and imported.group(1) == "./cobolRuntime.mjs"
    # and it is written as UTF-8 (the platform default cannot encode it)
    assert "→" in runtime.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# the default run: three views of the same program
# --------------------------------------------------------------------------- #

# The two dependency-retrieval reports are written by EVERY run (see
# test_every_run_writes_both_retrieval_reports) and are not views of the program, so the
# view-naming tests below would otherwise all have to repeat them.
_REPORTS = (".prefetch.json", ".fetch.json", ".jcl.prefetch.json", ".jcl.fetch.json")


def _names(d):
    """View files inside the run directory under ``d`` (an --outdir root)."""
    return {f.name for f in _run_dir(d).iterdir()
            if not f.name.endswith(_REPORTS) and not f.is_dir()}


def _all_names(d):
    """Everything inside the run directory under ``d``, reports included."""
    return {f.name for f in _run_dir(d).iterdir()}


def test_every_run_writes_both_retrieval_reports(tmp_path):
    """Prefetch and fetch are the pipeline, not a mode of it: no flag turns them on, and
    a plain run accounts for both stages. A run that quietly skipped them would produce a
    model with unexplained holes - an unresolved dynamic CALL looks identical to a
    program that genuinely has none."""
    src = Path(__file__).resolve().parents[1] / "examples" / "banktran.cbl"
    assert run([str(src), "--outdir", str(tmp_path)]) == 0
    names = _all_names(tmp_path)
    assert "banktran.prefetch.json" in names
    assert "banktran.fetch.json" in names

    import json
    d = _run_dir(tmp_path)
    pre = json.loads((d / "banktran.prefetch.json").read_text(encoding="utf-8"))
    fetched = json.loads((d / "banktran.fetch.json").read_text(encoding="utf-8"))
    assert pre["format"] == "cobol-xstate-prefetch"
    assert fetched["format"] == "cobol-xstate-fetch"
    # No estate client on a test machine: that must be stated, never left to look like
    # an estate that was asked and had nothing.
    assert pre["serviceAvailable"] is False
    assert "cast_clients" in pre["serviceUnavailable"]


def test_there_is_no_second_way_to_place_output(tmp_path):
    """--outdir is the only placement mechanism. The flags that used to compete with it
    (-o for an exact path, --deps-dir for retrieved members) are gone, so nothing can
    put a file outside the run directory."""
    src = _write_src(tmp_path)
    for flag in ("-o", "--output", "--deps-dir"):
        with pytest.raises(SystemExit):
            run([str(src), flag, str(tmp_path / "elsewhere")])


def test_machine_only_still_retrieves_it_just_stops_reporting(tmp_path):
    """--machine-only suppresses the reports, never the retrieval. What was fetched
    decides whether the machine is right, so skipping it to save two files would change
    the answer rather than the output format."""
    src = Path(__file__).resolve().parents[1] / "examples" / "banktran.cbl"
    assert run([str(src), "--machine-only", "--outdir", str(tmp_path)]) == 0
    assert _all_names(tmp_path) == {"banktran.json"}


def test_default_run_writes_all_views(tmp_path):
    src = Path(__file__).resolve().parents[1] / "examples" / "banktran.cbl"
    assert run([str(src), "--outdir", str(tmp_path)]) == 0
    assert _names(tmp_path) == {"banktran.json",             # the faithful machine
                                "banktran.business.json",    # the distillation
                                "banktran.lineage.json",     # the field table
                                "banktran.reactive.json",    # what replaces it
                                "banktran.artifacts.json",   # the related artifacts
                                "banktran.dynamic-calls.json"}  # targets it cannot name


def test_a_refused_reactive_view_does_not_take_the_run_down(tmp_path):
    """The reactive lowering refuses CICS handler regions. On a default run that is a
    fact about the program, not a failure of the run: the other views must still land."""
    src = Path(__file__).resolve().parents[1] / "examples" / "cicsinq.cbl"
    assert run([str(src), "--outdir", str(tmp_path)]) == 0
    names = _names(tmp_path)
    assert "cicsinq.json" in names and "cicsinq.business.json" in names
    assert "cicsinq.reactive.json" not in names       # refused, and said so


def test_explicit_reactive_target_on_a_refused_program_errors_cleanly(tmp_path, capsys):
    src = Path(__file__).resolve().parents[1] / "examples" / "cicsinq.cbl"
    assert run([str(src), "--target", "reactive", "--outdir", str(tmp_path)]) == 3
    assert "type:parallel" in capsys.readouterr().err   # the reason, not a traceback


def test_no_reactive_opts_out(tmp_path):
    src = Path(__file__).resolve().parents[1] / "examples" / "banktran.cbl"
    assert run([str(src), "--no-reactive", "--outdir", str(tmp_path)]) == 0
    assert "banktran.reactive.json" not in _names(tmp_path)
    assert "banktran.json" in _names(tmp_path)


def test_the_three_views_are_each_well_formed(tmp_path):
    import json
    src = Path(__file__).resolve().parents[1] / "examples" / "banktran.cbl"
    assert run([str(src), "--outdir", str(tmp_path)]) == 0
    d = _run_dir(tmp_path)
    faithful = json.loads((d / "banktran.json").read_text(encoding="utf-8"))
    business = json.loads((d / "banktran.business.json").read_text(encoding="utf-8"))
    lineage = json.loads((d / "banktran.lineage.json").read_text(encoding="utf-8"))
    assert faithful["format"] == "xstate-v5-config"
    assert faithful["metadata"].get("view") is None
    assert business["format"] == "xstate-v5-config"     # both are renderable machines
    assert business["metadata"]["view"] == "business"
    assert lineage["format"] == "cobol-xstate-lineage"
    # the distillation is smaller than what it distils. Count LEAVES: the faithful
    # machine is hierarchical (paragraphs are compound states), so comparing top-level
    # keys would compare 1 compound against 12 flat states.
    def leaves(states):
        n = 0
        for s in states.values():
            n += leaves(s["states"]) if "states" in s else 1
        return n
    faithful_n = leaves(faithful["machine"]["states"]) + sum(
        leaves(c["states"]) for c in faithful["charts"].values())
    assert leaves(business["machine"]["states"]) < faithful_n


def test_no_business_opts_out_of_just_that_view(tmp_path):
    src = Path(__file__).resolve().parents[1] / "examples" / "banktran.cbl"
    assert run([str(src), "--no-business", "--outdir", str(tmp_path)]) == 0
    assert _names(tmp_path) == {"banktran.json", "banktran.lineage.json",
                                "banktran.reactive.json", "banktran.artifacts.json",
                                "banktran.dynamic-calls.json"}


def test_no_artifacts_opts_out_of_just_that_view(tmp_path):
    src = Path(__file__).resolve().parents[1] / "examples" / "banktran.cbl"
    assert run([str(src), "--no-artifacts", "--outdir", str(tmp_path)]) == 0
    names = _names(tmp_path)
    assert "banktran.artifacts.json" not in names
    assert {"banktran.json", "banktran.business.json", "banktran.lineage.json",
            "banktran.reactive.json"} <= names


def test_both_opt_outs_leave_only_the_bundle(tmp_path):
    src = Path(__file__).resolve().parents[1] / "examples" / "banktran.cbl"
    assert run([str(src), "--no-business", "--no-lineage", "--no-reactive",
                "--no-artifacts", "--no-dynamic-calls", "--outdir", str(tmp_path)]) == 0
    assert _names(tmp_path) == {"banktran.json"}


def test_machine_only_suppresses_both_companions(tmp_path):
    src = Path(__file__).resolve().parents[1] / "examples" / "banktran.cbl"
    assert run([str(src), "--machine-only", "--outdir", str(tmp_path)]) == 0
    assert _names(tmp_path) == {"banktran.json"}


# --------------------------------------------------------------------------- #
# artifact naming: a companion must never land on another artifact's path
# --------------------------------------------------------------------------- #

def test_dotted_source_name_keeps_companions_matched(tmp_path):
    """A source called MY.PROG.cbl must give MY.PROG.{json,business.json,lineage.json}.
    Deriving the base by chopping at the FIRST dot produced companions named MY.* that
    did not match their bundle."""
    src = tmp_path / "MY.PROG.cbl"
    src.write_text(_PROG)
    out = tmp_path / "o"
    assert run([str(src), "--outdir", str(out)]) == 0
    assert _names(out) == {"MY.PROG.json", "MY.PROG.business.json",
                           "MY.PROG.lineage.json", "MY.PROG.reactive.json",
                           "MY.PROG.artifacts.json", "MY.PROG.dynamic-calls.json"}


def test_companion_never_overwrites_the_bundle(tmp_path):
    """A source whose name ends in a companion suffix used to have its bundle silently
    destroyed: the business view landed on the same path, leaving business+lineage only."""
    import json
    src = tmp_path / "ACCT.business.cbl"
    src.write_text(_PROG)
    out = tmp_path / "o"
    assert run([str(src), "--outdir", str(out)]) == 0
    names = _names(out)
    assert len(names) == 6, f"an artifact was clobbered: {names}"
    # the bundle survives and is still the FAITHFUL machine, not the distillation
    bundle = json.loads((_run_dir(out) / "ACCT.business.json").read_text(encoding="utf-8"))
    assert bundle["metadata"].get("view") is None


# --- --timing instrumentation: a diagnostic that must never perturb output ---


class _FakeLog:
    """Captures .info() lines so the timer can be tested without the logging stack."""

    def __init__(self):
        self.msgs = []

    def info(self, m):
        self.msgs.append(m)


def test_stage_timer_records_and_reports_when_enabled():
    log = _FakeLog()
    t = StageTimer(log, enabled=True, source_name="x.cbl")
    with t.stage("parse"):
        pass
    tok = t.start()
    t.since("views", tok)
    t.report()
    text = "\n".join(log.msgs)
    assert "timing (ms):" in text
    assert "parse" in text and "views" in text and "measured" in text


def test_stage_timer_is_a_no_op_when_disabled():
    """Disabled, it must record nothing and emit nothing - the normal-run code path is
    then byte-for-byte the same as before the flag existed."""
    log = _FakeLog()
    t = StageTimer(log, enabled=False, source_name="x.cbl")
    with t.stage("parse"):
        pass
    assert t.start() is None
    t.since("views", None)
    t.report()
    assert log.msgs == []


def _all_files(root):
    """Every file under an --outdir root, keyed by its path relative to that root."""
    root = Path(root)
    return {str(p.relative_to(root)): p.read_bytes()
            for p in root.rglob("*") if p.is_file()}


def test_timing_does_not_change_any_output_byte(tmp_path):
    """--timing writes only to stderr: the FILES a run produces must be byte-identical
    with or without it. This is the guarantee that the diagnostic cannot corrupt the
    tool's byte-stable output contract."""
    src = EXAMPLES_CBL
    assert run([src, "--outdir", str(tmp_path / "plain")]) == 0
    assert run([src, "--timing", "--outdir", str(tmp_path / "timed")]) == 0
    assert _all_files(tmp_path / "plain") == _all_files(tmp_path / "timed")


def test_timing_prints_a_per_stage_breakdown_to_stderr(tmp_path, capsys):
    src = EXAMPLES_CBL
    assert run([src, "--timing", "--outdir", str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert "timing (ms):" in err
    for stage in ("prefetch", "parse", "build_machine", "lineage-fixpoint",
                  "fetch", "views", "measured"):
        assert stage in err, f"missing stage {stage!r} in stderr:\n{err}"


# --- timing_sink: an embedding program routing timings into its own log ---


def test_timing_sink_receives_structured_rows_without_the_flag(tmp_path, capsys):
    """An embedding program supplies a sink to route timings into its own timing log: it
    gets the data WITHOUT --timing, and stderr stays quiet."""
    got = []
    assert run([EXAMPLES_CBL, "--outdir", str(tmp_path)], timing_sink=got.append) == 0
    assert len(got) == 1, "the sink is called exactly once, on a completed run"
    rows = got[0]
    assert rows and all(set(r) == {"stage", "ms"} for r in rows)
    assert all(isinstance(r["ms"], float) for r in rows)
    names = [r["stage"] for r in rows]
    # A sink-only caller measures the same stages the flag reports - including the two
    # pre-warmed analyses, which are the numbers worth logging.
    for stage in ("prefetch", "parse", "build_machine", "interface",
                  "lineage-fixpoint", "fetch", "views"):
        assert stage in names, f"missing stage {stage!r} in {names}"
    assert "timing (ms)" not in capsys.readouterr().err


def test_timing_sink_and_flag_work_together(tmp_path, capsys):
    got = []
    assert run([EXAMPLES_CBL, "--timing", "--outdir", str(tmp_path)],
               timing_sink=got.append) == 0
    assert len(got) == 1 and got[0]
    assert "timing (ms)" in capsys.readouterr().err


def test_a_raising_timing_sink_never_fails_the_run(tmp_path, capsys):
    """The sink is a diagnostic hook: a bug in the caller's own log must not fail a
    conversion whose output files are already written."""
    def boom(rows):
        raise ValueError("caller bug")
    assert run([EXAMPLES_CBL, "--outdir", str(tmp_path)], timing_sink=boom) == 0
    assert "timing sink raised" in capsys.readouterr().err


def test_timing_sink_does_not_change_any_output_byte(tmp_path):
    assert run([EXAMPLES_CBL, "--outdir", str(tmp_path / "plain")]) == 0
    assert run([EXAMPLES_CBL, "--outdir", str(tmp_path / "sunk")],
               timing_sink=lambda rows: None) == 0
    assert _all_files(tmp_path / "plain") == _all_files(tmp_path / "sunk")
