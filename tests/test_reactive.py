"""Stage 5b reactive target: faithful machine -> event-driven XState v5 module.

Pure-Python tests assert the boundary rewrite (inbound gets become `on` waits, the SQLCODE
branch moves behind a response-event wait, entry reads are dropped). The Node integration
test emits the SQL-SELECT slice, drives it under real XState by *sending events*, and checks
that the row event assigns the host variables and the SQLCODE response event branches - the
end-to-end proof of the push / response-event model. It skips when node / xstate are absent.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from cobol_xstate.parser import parse_program
from cobol_xstate.reactive import _event_slug, emit_reactive_module
from cobol_xstate.statechart import build_machine

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples"
RUNTIME = REPO / "runtime" / "cobolRuntime.mjs"


def _machine(name):
    src = (EXAMPLES / name).read_text()
    return build_machine(parse_program(src), source_name=name)


def _extract(module: str, name: str) -> dict:
    """Pull an ``export const <name> = { ... };`` JSON object out of the emitted module."""
    head = f"export const {name} = "
    body = module.split(head, 1)[1]
    # the object runs to the line that is exactly `};` (json.dumps indent=2 output)
    end = body.index("\n};") + len("\n}")
    return json.loads(body[:end])


# --------------------------------------------------------------------------- #
# the boundary rewrite (pure Python)
# --------------------------------------------------------------------------- #

def test_event_slug_is_identifier_safe():
    assert _event_slug("GET.DB2.CUSTOMER") == "GET_DB2_CUSTOMER"
    assert _event_slug("GET.RESPONSE.DB2") == "GET_RESPONSE_DB2"


def test_inbound_get_becomes_an_on_wait():
    mod = emit_reactive_module(_machine("sqlsel.cbl"))
    cfg = _extract(mod, "machineConfig")
    main = cfg["states"]["0000-MAIN"]
    # the synchronous SELECT is dropped; the preceding MOVE stays
    assert main.get("entry") == ["MOVE_12345_TO_WS-CUST-ID"]
    assert "always" not in main
    # the state now waits for the row event and assigns the INTO host vars
    assert "GET.DB2.CUSTOMER" in main["on"]
    handler = main["on"]["GET.DB2.CUSTOMER"]
    assert handler["actions"] == ["recv_GET_DB2_CUSTOMER"]
    assert handler["target"] == "0000-MAIN__if2"


def test_response_branch_waits_then_evaluates_existing_guards():
    mod = emit_reactive_module(_machine("sqlsel.cbl"))
    cfg = _extract(mod, "machineConfig")
    branch = cfg["states"]["0000-MAIN__if2"]
    # the SQLCODE branch no longer fires eventlessly; it waits for the response event
    assert "always" not in branch
    resp = branch["on"]["GET.RESPONSE.DB2"]
    assert resp["actions"] == ["recv_GET_RESPONSE_DB2"]
    ready = resp["target"]
    assert ready == "0000-MAIN__if2__ready"
    # the original guarded edges are parked in the synthetic __ready state, unchanged
    edges = cfg["states"][ready]["always"]
    assert edges[0]["guard"] == "SQLCODE_eq_0"
    assert edges[0]["target"] == "0000-MAIN__seq3"
    assert edges[1]["target"] == "0000-MAIN__seq4"


def test_recv_ops_assign_event_fields():
    mod = emit_reactive_module(_machine("sqlsel.cbl"))
    assert ('"recv_GET_DB2_CUSTOMER": (context, event) => '
            '({ "WS-NAME": event["WS-NAME"], "WS-BALANCE": event["WS-BALANCE"] })') in mod
    assert ('"recv_GET_RESPONSE_DB2": (context, event) => '
            '({ "SQLCODE": event["SQLCODE"] })') in mod


def test_manifest_lists_inbound_events_and_no_flags():
    mod = emit_reactive_module(_machine("sqlsel.cbl"))
    manifest = _extract(mod, "manifest")
    assert set(manifest["inbound"]) == {"GET.DB2.CUSTOMER", "GET.RESPONSE.DB2"}
    assert manifest["outbound"] == []
    assert manifest["flags"] == []  # flat SELECT slice is fully lowered


def test_parallel_machine_is_refused_not_faked():
    # cicsinq has EXEC CICS HANDLE CONDITION -> a type:parallel handler-region machine,
    # which the slice does not lower. It must refuse loudly, not emit something wrong.
    with pytest.raises(NotImplementedError):
        emit_reactive_module(_machine("cicsinq.cbl"))


# --------------------------------------------------------------------------- #
# Node integration (skipped when node / xstate are unavailable)
# --------------------------------------------------------------------------- #

NODE = shutil.which("node")
HAS_XSTATE = (REPO / "node_modules" / "xstate" / "package.json").exists()


@pytest.fixture
def repo_tmp():
    """A temp dir *inside* the repo so Node's upward node_modules lookup finds xstate."""
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="react_", dir=str(REPO)))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.mark.skipif(not NODE, reason="node not available")
def test_reactive_module_passes_node_syntax_check(tmp_path):
    mod_path = tmp_path / "machine.mjs"
    mod_path.write_text(emit_reactive_module(_machine("sqlsel.cbl")))
    r = subprocess.run([NODE, "--check", str(mod_path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(not (NODE and HAS_XSTATE), reason="node+xstate not available")
def test_reactive_slice_runs_by_sending_events(repo_tmp):
    """The SELECT slice: start (blocks on the row), send the row event (host vars assigned),
    send the SQLCODE response event (branch fires). Proven for both FOUND and MISSING."""
    (repo_tmp / "machine.mjs").write_text(emit_reactive_module(_machine("sqlsel.cbl")))
    (repo_tmp / "cobolRuntime.mjs").write_text(RUNTIME.read_text())
    driver = repo_tmp / "drive.mjs"
    driver.write_text(
        "import { createActor } from 'xstate';\n"
        "import machine from './machine.mjs';\n"
        "function run(sqlcode) {\n"
        "  const a = createActor(machine); a.start();\n"
        "  let s = a.getSnapshot();\n"
        "  if (s.context['WS-CUST-ID'] !== '12345') "
        "{ console.error('cust-id', s.context['WS-CUST-ID']); process.exit(1); }\n"
        "  a.send({ type: 'GET.DB2.CUSTOMER', 'WS-NAME': 'ACME CORP', 'WS-BALANCE': '250.00' });\n"
        "  s = a.getSnapshot();\n"
        "  if (s.context['WS-NAME'] !== 'ACME CORP') "
        "{ console.error('name', JSON.stringify(s.context['WS-NAME'])); process.exit(1); }\n"
        "  if (s.context['WS-BALANCE'] !== '250.00') "
        "{ console.error('bal', s.context['WS-BALANCE']); process.exit(1); }\n"
        "  a.send({ type: 'GET.RESPONSE.DB2', 'SQLCODE': sqlcode });\n"
        "  return a.getSnapshot();\n"
        "}\n"
        "const found = run('0');\n"
        "if (found.status !== 'done') { console.error('found status', found.status); process.exit(1); }\n"
        "if (found.context['WS-STATUS'] !== 'FOUND     ') "
        "{ console.error('found', JSON.stringify(found.context['WS-STATUS'])); process.exit(1); }\n"
        "const missing = run('100');\n"
        "if (missing.status !== 'done') { console.error('missing status', missing.status); process.exit(1); }\n"
        "if (missing.context['WS-STATUS'] !== 'MISSING   ') "
        "{ console.error('missing', JSON.stringify(missing.context['WS-STATUS'])); process.exit(1); }\n"
        "process.exit(0);\n"
    )
    r = subprocess.run([NODE, str(driver)], capture_output=True, text=True,
                       cwd=str(repo_tmp), timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
