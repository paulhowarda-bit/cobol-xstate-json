"""CLI output-destination behavior: default same-name file, --outdir, -o overrides."""

import json
import os
from pathlib import Path

import pytest

from cobol_xstate.cli import run


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
