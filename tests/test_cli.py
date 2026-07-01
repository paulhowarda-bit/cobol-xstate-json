"""CLI output-destination behavior: default same-name file, --outdir, -o overrides."""

import json
import os
from pathlib import Path

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
