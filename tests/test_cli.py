"""CLI output-destination behavior: default same-name file, --outdir, -o overrides."""

import json
import os
from pathlib import Path

import pytest

from cobol_xstate.cli import run

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


def test_default_writes_same_name_json_in_cwd(tmp_path, monkeypatch):
    src = _write_src(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = run([src.name])
    assert rc == 0
    out = tmp_path / "hello.json"
    assert out.exists()
    json.loads(out.read_text())  # valid JSON


def test_outdir_relative_is_created(tmp_path, monkeypatch):
    src = _write_src(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = run([src.name, "--outdir", "build/charts"])
    assert rc == 0
    out = tmp_path / "build" / "charts" / "hello.json"
    assert out.exists()


def test_outdir_dot_is_current_directory(tmp_path, monkeypatch):
    src = _write_src(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = run([src.name, "--outdir", "."])
    assert rc == 0
    assert (tmp_path / "hello.json").exists()


def test_outdir_absolute_path_is_created(tmp_path):
    src = _write_src(tmp_path)
    target = tmp_path / "abs" / "out"
    rc = run([str(src), "--outdir", str(target)])
    assert rc == 0
    assert (target / "hello.json").exists()


def test_outdir_names_output_after_source_not_cwd(tmp_path, monkeypatch):
    # Running on a file in another directory still names the output after the source.
    srcdir = tmp_path / "src"
    srcdir.mkdir()
    src = _write_src(srcdir, "payroll.cbl")
    monkeypatch.chdir(tmp_path)
    rc = run([str(src), "--outdir", "out"])
    assert rc == 0
    assert (tmp_path / "out" / "payroll.json").exists()


def test_explicit_output_overrides_outdir(tmp_path):
    src = _write_src(tmp_path)
    exact = tmp_path / "somewhere" / "custom.json"
    rc = run([str(src), "-o", str(exact), "--outdir", str(tmp_path / "ignored")])
    assert rc == 0
    assert exact.exists()
    assert not (tmp_path / "ignored").exists()


def test_dash_output_writes_stdout(tmp_path, capsys):
    src = _write_src(tmp_path)
    rc = run([str(src), "-o", "-"])
    assert rc == 0
    captured = capsys.readouterr()
    json.loads(captured.out)  # machine JSON went to stdout
    # No stray file created next to the source.
    assert not (tmp_path / "hello.json").exists()


def test_js_target_default_name_is_mjs_with_runtime(tmp_path, monkeypatch):
    src = _write_src(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = run([src.name, "--target", "js"])
    assert rc == 0
    assert (tmp_path / "hello.mjs").exists()
    assert (tmp_path / "cobolRuntime.mjs").exists()


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
    out = tmp_path / "m.mjs"
    rc = run([str(src), "--target", "js", "-o", str(out)])
    assert rc == 0
    runtime = tmp_path / "cobolRuntime.mjs"
    assert runtime.exists(), "cobolRuntime.mjs must be written beside the module"
    # the module imports exactly that filename
    imported = re.search(r'from "(\./[^"]+\.mjs)"', out.read_text(encoding="utf-8"))
    assert imported and imported.group(1) == "./cobolRuntime.mjs"
    # and it is written as UTF-8 (the platform default cannot encode it)
    assert "→" in runtime.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# the default run: three views of the same program
# --------------------------------------------------------------------------- #

def _names(d):
    return {f.name for f in d.iterdir()}


def test_default_run_writes_all_four_views(tmp_path):
    src = Path(__file__).resolve().parents[1] / "examples" / "banktran.cbl"
    assert run([str(src), "--outdir", str(tmp_path)]) == 0
    assert _names(tmp_path) == {"banktran.json",            # the faithful machine
                                "banktran.business.json",   # the distillation
                                "banktran.lineage.json",    # the field table
                                "banktran.reactive.json"}   # what replaces it


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
    faithful = json.loads((tmp_path / "banktran.json").read_text(encoding="utf-8"))
    business = json.loads((tmp_path / "banktran.business.json").read_text(encoding="utf-8"))
    lineage = json.loads((tmp_path / "banktran.lineage.json").read_text(encoding="utf-8"))
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
                                "banktran.reactive.json"}


def test_both_opt_outs_leave_only_the_bundle(tmp_path):
    src = Path(__file__).resolve().parents[1] / "examples" / "banktran.cbl"
    assert run([str(src), "--no-business", "--no-lineage", "--no-reactive",
                "--outdir", str(tmp_path)]) == 0
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
                           "MY.PROG.lineage.json", "MY.PROG.reactive.json"}


def test_companion_never_overwrites_the_bundle(tmp_path):
    """A source whose name ends in a companion suffix used to have its bundle silently
    destroyed: the business view landed on the same path, leaving business+lineage only."""
    import json
    src = tmp_path / "ACCT.business.cbl"
    src.write_text(_PROG)
    out = tmp_path / "o"
    assert run([str(src), "--outdir", str(out)]) == 0
    names = _names(out)
    assert len(names) == 4, f"an artifact was clobbered: {names}"
    # the bundle survives and is still the FAITHFUL machine, not the distillation
    bundle = json.loads((out / "ACCT.business.json").read_text(encoding="utf-8"))
    assert bundle["metadata"].get("view") is None


def test_explicit_output_path_carries_matched_companions(tmp_path):
    assert run([str(EXAMPLES_CBL), "-o", str(tmp_path / "custom.json")]) == 0
    assert _names(tmp_path) == {"custom.json", "custom.business.json",
                                "custom.lineage.json", "custom.reactive.json"}
