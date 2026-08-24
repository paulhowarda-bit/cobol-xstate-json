"""The parse-bundle replay produces the machine a direct parse does - per example.

The gate (tools/gate.py) proves this byte-for-byte across every view; this is the fast
in-suite complement over the machine bundle, so a codec regression fails `pytest -q`
too, not only the release gate."""

import json
from pathlib import Path

import pytest

from cobol_parser.normalizer import detect_source_format
from cobol_parser.parse_bundle import program_from_dict, program_to_dict
from cobol_parser.parser import parse_program
from cobol_parser.preprocessor import CopybookResolver
from cobol_xstate.statechart import build_machine

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.mark.parametrize("src", sorted(EXAMPLES.glob("*.cbl")),
                         ids=lambda p: p.name)
def test_direct_and_roundtripped_programs_model_identically(src):
    source = src.read_text(encoding="utf-8", errors="replace")
    fmt = detect_source_format(source).format
    resolver = CopybookResolver(paths=[str(EXAMPLES)], fetcher=None, store={})
    program = parse_program(source, fmt, resolver=resolver)
    rehydrated = program_from_dict(json.loads(json.dumps(program_to_dict(program))))
    direct = build_machine(program, source_name=src.name)
    replayed = build_machine(rehydrated, source_name=src.name)
    assert replayed.to_json() == direct.to_json()
