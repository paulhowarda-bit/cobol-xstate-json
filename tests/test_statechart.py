import json
from pathlib import Path

from cobol_xstate.parser import parse_program
from cobol_xstate.statechart import build_machine

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _machine(src: str):
    return build_machine(parse_program(src))


def test_custrpt_machine_shape():
    machine = _machine((EXAMPLES / "custrpt.cbl").read_text())
    cfg = machine.config
    assert cfg["id"] == "CUSTRPT"
    assert cfg["initial"] == "0000-MAIN"
    states = cfg["states"]
    assert set(states) == {"0000-MAIN", "1000-INIT", "2000-PROCESS", "3000-TERM"}
    # The driver performs the three phase paragraphs.
    targets = {e["target"] for e in states["0000-MAIN"]["always"]}
    assert {"1000-INIT", "2000-PROCESS", "3000-TERM"} <= targets


def test_no_invented_logic_guards_and_actions_are_strings():
    machine = _machine((EXAMPLES / "custrpt.cbl").read_text())
    for state in machine.config["states"].values():
        for a in state.get("entry", []):
            assert isinstance(a, str)
        for tr in state.get("always", []):
            assert isinstance(tr.get("guard", ""), str)
            for a in tr.get("actions", []):
                assert isinstance(a, str)


def test_every_referenced_name_has_provenance():
    machine = _machine((EXAMPLES / "custrpt.cbl").read_text())
    prov = machine.provenance
    for name, state in machine.config["states"].items():
        assert name in prov and prov[name]["kind"] == "state"
        for a in state.get("entry", []):
            assert a in prov, f"missing provenance for action {a}"
        for tr in state.get("always", []):
            if "guard" in tr:
                assert tr["guard"] in prov
            for a in tr.get("actions", []):
                assert a in prov


def test_terminator_marks_final():
    machine = _machine(
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           STOP RUN.\n"
    )
    assert machine.config["states"]["0000-MAIN"].get("type") == "final"


def test_banktran_flags_alter_and_dynamic_call():
    machine = _machine((EXAMPLES / "banktran.cbl").read_text())
    messages = " ".join(f["message"] for f in machine.flags)
    assert "ALTER" in messages
    assert "dynamic CALL" in messages


def test_evaluate_produces_guarded_transitions():
    machine = _machine((EXAMPLES / "banktran.cbl").read_text())
    dispatch = machine.config["states"]["2000-DISPATCH"]
    guards = [e.get("guard") for e in dispatch["always"] if "guard" in e]
    assert len(guards) >= 3  # WHEN 'D' / 'W' / 'I'


def test_bundle_is_json_serializable_and_well_formed():
    machine = _machine((EXAMPLES / "custrpt.cbl").read_text())
    text = machine.to_json()
    obj = json.loads(text)
    assert obj["format"] == "xstate-v5-config"
    assert "machine" in obj and "provenance" in obj and "flags" in obj
    # machine-only path is the bare config
    bare = json.loads(machine.to_json(machine_only=True))
    assert "states" in bare and "format" not in bare
