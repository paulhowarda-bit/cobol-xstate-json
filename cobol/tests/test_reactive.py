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
RUNTIME = REPO / "src" / "cobol_xstate" / "runtime" / "cobolRuntime.mjs"


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


def test_retarget_namespaces_bare_string_handler_targets():
    """Flattening an actor body must namespace bare-string handler targets
    (``on: {EVENT: "__H_x"}`` from statechart._build_handlers_region) as well as the dict
    form. The dict-only version dropped the bare one - the same latent bug harel._retarget
    was fixed for, now shared via emitter.retarget_on. (Reactive refuses parallel machines,
    where these edges live, so this guards the rewriter directly rather than via an example.)"""
    from cobol_xstate.reactive import _retarget
    node = {"on": {"IO.ERROR.F": "__H_2000", "OTHER": {"target": "s1"}}}
    _retarget(node, "ACT")
    assert node["on"]["IO.ERROR.F"] == {"target": "ACT____H_2000"}  # was silently dropped
    assert node["on"]["OTHER"] == {"target": "ACT__s1"}


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


def test_post_read_processing_runs_when_the_record_arrives_not_on_entry():
    """A read-process-write paragraph folds into one state whose entry is
    ``[read, process..., write]``. The push rewrite dropped the read but LEFT the
    processing and the write on ``entry`` - so they ran the moment the state was entered,
    before any record existed, deriving output from an empty record every cycle. The
    statements that consume the record must ride in the ``on`` handler, after ``recv``."""
    mod = emit_reactive_module(_machine("readproc.cbl"))
    cfg = _extract(mod, "machineConfig")
    main = cfg["states"]["0000-MAIN"]
    # nothing pre-read here, so entry is gone entirely - no processing runs early
    assert "entry" not in main
    handler = main["on"]["GET.DB2.CUST"]
    acts = handler["actions"]
    assert acts[0] == "recv_GET_DB2_CUST"               # the row is assigned first
    # ...then everything that consumes it, in source order
    assert acts[1:] == ["MOVE_WS-NAME_TO_OUT-NAME",
                        "COMPUTE_OUT-DBL_eq_WS-AMT_2"]


def test_pre_read_actions_stay_on_entry():
    """Only the statements AFTER the read consume the record. Anything before it (a MOVE
    that sets up the read) must still run on the way in, before the wait."""
    mod = emit_reactive_module(_machine("sqlsel.cbl"))
    cfg = _extract(mod, "machineConfig")
    main = cfg["states"]["0000-MAIN"]
    # the MOVE that precedes the SELECT is a pre-read action: it stays on entry
    assert main.get("entry") == ["MOVE_12345_TO_WS-CUST-ID"]
    assert main["on"]["GET.DB2.CUSTOMER"]["actions"] == ["recv_GET_DB2_CUSTOMER"]


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


def test_recv_ops_store_arriving_fields_through_their_picture():
    """A record does not become exempt from COBOL data semantics by arriving as an event:
    an inbound field is stored through the same PICTURE rules as any internal MOVE."""
    mod = emit_reactive_module(_machine("sqlsel.cbl"))
    # alphanumeric -> storeStr with the field's spec (so PIC X(20) pads, as a MOVE would)
    assert ('"WS-NAME": event["WS-NAME"] !== undefined ? '
            'storeStr(event["WS-NAME"], FIELDS["WS-NAME"]) : context["WS-NAME"]') in mod
    # numeric -> decimal store, quantized to digits/scale
    assert ('"WS-BALANCE": event["WS-BALANCE"] !== undefined ? '
            'store(D(String(event["WS-BALANCE"])), FIELDS["WS-BALANCE"]) '
            ': context["WS-BALANCE"]') in mod
    assert ('"SQLCODE": event["SQLCODE"] !== undefined ? '
            'store(D(String(event["SQLCODE"])), FIELDS["SQLCODE"])') in mod


def test_recv_op_leaves_context_alone_for_a_field_the_event_omits():
    # D(undefined) would throw; a missing field must simply not be assigned.
    mod = emit_reactive_module(_machine("sqlsel.cbl"))
    assert 'event["WS-NAME"] !== undefined ?' in mod
    assert ': context["WS-NAME"]' in mod


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
        # WS-NAME is PIC X(20): the arriving value is stored through its PICTURE, so it
        # is space-padded exactly as an internal MOVE would leave it.
        "  if (s.context['WS-NAME'] !== 'ACME CORP'.padEnd(20)) "
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


def test_file_read_into_recv_assigns_the_into_records_leaves():
    """A file ``READ f INTO ws-rec`` event must carry the INTO record's ELEMENTARY fields,
    so the reactive ``recv`` assigns them through their PICTURE - exactly as an SQL SELECT's
    host variables are. Before the fix the event carried only the group record name (not a
    context key), so ``recv`` was ``=> ({})``: a runtime NO-OP that processed an empty
    record. This is why readproc.cbl had to use SQL, not a file READ, to be drivable."""
    mod = emit_reactive_module(_machine("readinto.cbl"))
    assert '"recv_GET_FILE_IN_FILE": (context, event) => ({})' not in mod   # was a no-op
    assert ('"WS-KEY": event["WS-KEY"] !== undefined ? '
            'storeStr(event["WS-KEY"], FIELDS["WS-KEY"]) : context["WS-KEY"]') in mod
    assert ('"WS-AMT": event["WS-AMT"] !== undefined ? '
            'store(D(String(event["WS-AMT"])), FIELDS["WS-AMT"])') in mod


@pytest.mark.skipif(not (NODE and HAS_XSTATE), reason="node+xstate not available")
def test_file_read_into_derives_from_the_arriving_record(repo_tmp):
    """The file analogue of the readproc SQL proof, drivable only now that a file READ
    event carries the INTO record's leaves. The FD record is opaque X(13); WS-REC's leaves
    (WS-KEY / WS-AMT) arrive on the event and drive per-record processing. Pre-fix the recv
    was a no-op: the loop still counted records and reached ``done``, but WS-SUM stayed 0
    and OUT-KEY stayed blank - a machine that looked finished while every derived value was
    wrong."""
    body = (
        "for (const [k, amt] of "
        "[['AAAA1111','00021'],['BBBB2222','00100'],['CCCC3333','00007']]) "
        "a.send({ type: 'GET.FILE.IN-FILE', 'WS-KEY': k, 'WS-AMT': amt });\n"
        "a.send({ type: 'END.FILE.IN-FILE' });\n"
        + _expect({"WS-CNT": "3", "WS-SUM": "128", "OUT-KEY": "CCCC3333"}))
    r = _run_reactive(repo_tmp, "readinto.cbl", body)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(not (NODE and HAS_XSTATE), reason="node+xstate not available")
def test_read_process_write_derives_from_the_arriving_record(repo_tmp):
    """The end-to-end proof of the J8 fix: start the machine (no record yet), send the
    record, and check the derived fields reflect THAT record - not the empty one that
    entry-time processing would have used."""
    (repo_tmp / "machine.mjs").write_text(emit_reactive_module(_machine("readproc.cbl")))
    (repo_tmp / "cobolRuntime.mjs").write_text(RUNTIME.read_text())
    driver = repo_tmp / "drive.mjs"
    driver.write_text(
        "import { createActor } from 'xstate';\n"
        "import machine from './machine.mjs';\n"
        "const a = createActor(machine); a.start();\n"
        # before the row arrives, the derived outputs must be untouched (empty host vars)
        "let s = a.getSnapshot();\n"
        "if (s.context['OUT-NAME'] && s.context['OUT-NAME'].trim() !== '') "
        "{ console.error('processed early:', JSON.stringify(s.context['OUT-NAME'])); "
        "process.exit(1); }\n"
        "a.send({ type: 'GET.DB2.CUST', 'WS-NAME': 'ACME', 'WS-AMT': '00021' });\n"
        "s = a.getSnapshot();\n"
        # WS-NAME is PIC X(20); moved to OUT-NAME (also X(20)), space-padded
        "if (s.context['OUT-NAME'] !== 'ACME'.padEnd(20)) "
        "{ console.error('OUT-NAME', JSON.stringify(s.context['OUT-NAME'])); process.exit(1); }\n"
        # OUT-DBL holds the decimal VALUE 21*2 = 42 (the runtime stores values, not the
        # PIC 9(6) display form); the point is it derived from the ARRIVING WS-AMT
        "if (String(s.context['OUT-DBL']) !== '42') "
        "{ console.error('OUT-DBL', JSON.stringify(s.context['OUT-DBL'])); process.exit(1); }\n"
        "process.exit(0);\n"
    )
    r = subprocess.run([NODE, str(driver)], capture_output=True, text=True,
                       cwd=str(repo_tmp), timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr


# --------------------------------------------------------------------------- #
# PERFORM: flattened into ONE machine, so queue events reach every state
# --------------------------------------------------------------------------- #

def _cfg(name):
    return _extract(emit_reactive_module(_machine(name)), "machineConfig")


def test_perform_becomes_set_return_then_jump():
    cfg = _cfg("custrpt.cbl")
    st = cfg["states"]["0000-MAIN"]
    assert st["entry"] == ["set_ret_1000-INIT_at_0000-MAIN"]      # record where to return
    assert st["always"][0]["target"] == "1000-INIT__1000-INIT"    # ...then enter the callee
    assert not any(a.startswith("perform_")                       # no marker survives
                   for s in cfg["states"].values()
                   for a in (s.get("entry", []) or []))


def test_return_dispatch_is_a_real_guard_not_an_external_stub():
    mod = emit_reactive_module(_machine("custrpt.cbl"))
    cfg = _extract(mod, "machineConfig")
    ret = cfg["states"]["1000-INIT__RET"]
    edge = ret["always"][0]
    assert edge["guard"] == "ret_1000-INIT_at_0000-MAIN"
    assert edge["target"] == "0000-MAIN__k1"                      # the call site's continuation
    # the guard must be evaluable, never left to the external channel
    assert '"ret_1000-INIT_at_0000-MAIN": (context) => rel(context["RET-1000-INIT"]' in mod
    assert "ret_1000-INIT_at_0000-MAIN" not in _extract_list(mod, "externalGuards")


def _extract_list(module: str, name: str):
    import json as _j
    import re as _re
    m = _re.search(rf"export const {name} = (\[.*?\]);", module, _re.S)
    return _j.loads(m.group(1))


def test_ret_field_is_typed_and_seeded():
    mod = emit_reactive_module(_machine("custrpt.cbl"))
    fields = _extract(mod, "FIELDS")
    assert fields["RET-1000-INIT"] == {"category": "alphanumeric"}   # no len: must not pad
    assert _extract(mod, "machineConfig")["context"]["RET-1000-INIT"] == ""


def test_open_input_is_not_lowered_to_a_wait():
    """OPEN INPUT classifies as a get/file but delivers no record. Lowering it would
    block forever - and swallow the first real record when one arrived."""
    cfg = _cfg("custrpt.cbl")
    opener = cfg["states"]["1000-INIT__1000-INIT"]
    assert opener["entry"] == ["OPEN_INPUT_CUST-FILE"]    # stays a plain effect
    assert "on" not in opener                             # ...and does not wait
    assert "GET.FILE.CUST-FILE" in cfg["states"]["1000-INIT__1000-INIT__io5"]["on"]


def test_read_wait_also_accepts_end_of_stream():
    cfg = _cfg("custrpt.cbl")
    on = cfg["states"]["1000-INIT__1000-INIT__io5"]["on"]
    assert set(on) == {"GET.FILE.CUST-FILE", "END.FILE.CUST-FILE"}
    # both land on the same state, where the AT END guard then branches
    assert on["GET.FILE.CUST-FILE"]["target"] == on["END.FILE.CUST-FILE"]["target"]


def test_end_recv_raises_the_at_end_flag_the_guards_read():
    mod = emit_reactive_module(_machine("custrpt.cbl"))
    assert '"recv_END_FILE_CUST_FILE"' in mod
    assert 'ext["CUST-FILE_atEnd"] = true;' in mod
    assert "CUST-FILE_atEnd" in _extract_list(mod, "externalGuards")


def test_negated_external_is_exported_so_not_at_end_is_the_record_path():
    mod = emit_reactive_module(_machine("notend.cbl"))
    assert '"IN-FILE_notAtEnd": "IN-FILE_atEnd"' in mod


def test_recursive_perform_is_refused_not_flattened_wrong():
    """One return-address field per paragraph cannot survive re-entrancy."""
    with pytest.raises(NotImplementedError, match="recursive PERFORM cycle"):
        emit_reactive_module(_machine("recur.cbl"))


def test_two_reads_in_one_state_become_two_waits():
    cfg = _cfg("twogets.cbl")
    assert "GET.CONSOLE.SYSIN" in cfg["states"]["0000-MAIN"]["on"]
    assert "GET.CONSOLE.SYSIN" in cfg["states"]["0000-MAIN__g1"]["on"]
    mod = emit_reactive_module(_machine("twogets.cbl"))
    # distinct recv actions: same event name, different fields
    assert '"recv_GET_CONSOLE_SYSIN": (context, event) => ({ "WS-A"' in mod
    assert '"recv_GET_CONSOLE_SYSIN_2": (context, event) => ({ "WS-B"' in mod


def test_no_perform_flag_once_performs_are_resolved():
    manifest = _extract(emit_reactive_module(_machine("custrpt.cbl")), "manifest")
    assert not any("PERFORM" in f for f in manifest["flags"])
    assert manifest["inbound"] == ["GET.FILE.CUST-FILE", "END.FILE.CUST-FILE"]
    # the inline map traces a flattened state id back to its paragraph
    assert manifest["inline"]["1000-INIT"]["states"]["1000-INIT__1000-INIT__io5"] \
        == "1000-INIT__io5"


@pytest.mark.skipif(not (NODE and HAS_XSTATE), reason="node+xstate not available")
def test_perform_structured_batch_runs_on_events_alone(repo_tmp):
    """THE headline: custrpt is PERFORM-structured (its READ lives inside a performed
    paragraph), so before flattening its logic never ran here at all. Drive it purely by
    sending record events + end-of-stream, and it must reach the same exact decimal total
    the synchronous golden master produces (test_golden_master.py: '113.20')."""
    (repo_tmp / "machine.mjs").write_text(emit_reactive_module(_machine("custrpt.cbl")))
    (repo_tmp / "cobolRuntime.mjs").write_text(RUNTIME.read_text())
    (repo_tmp / "drive.mjs").write_text(
        "import { createActor } from 'xstate';\n"
        "import machine from './machine.mjs';\n"
        "function run(amts) {\n"
        "  const a = createActor(machine); a.start();\n"
        "  for (const v of amts) a.send({ type: 'GET.FILE.CUST-FILE', 'CUST-AMT': v });\n"
        "  a.send({ type: 'END.FILE.CUST-FILE' });\n"
        "  return a.getSnapshot();\n"
        "}\n"
        "const s = run(['0.10','0.20','100.55','12.34','0.01']);\n"
        "if (s.status !== 'done') { console.error('status', s.status); process.exit(1); }\n"
        "if (s.context['WS-TOTAL'] !== '113.20') "
        "{ console.error('total', s.context['WS-TOTAL']); process.exit(1); }\n"
        "const e = run([]);\n"                      # empty stream: END arrives first
        "if (e.status !== 'done' || e.context['WS-TOTAL'] !== '0') "
        "{ console.error('empty', e.status, e.context['WS-TOTAL']); process.exit(1); }\n"
        "process.exit(0);\n"
    )
    r = subprocess.run([NODE, str(repo_tmp / "drive.mjs")], capture_output=True,
                       text=True, cwd=str(repo_tmp), timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(not (NODE and HAS_XSTATE), reason="node+xstate not available")
def test_not_at_end_body_runs_per_record_on_events(repo_tmp):
    """notend's NOT AT END path is the per-record path: it must fire for every record
    delivered and stop at end-of-stream. Matches the synchronous golden master."""
    (repo_tmp / "machine.mjs").write_text(emit_reactive_module(_machine("notend.cbl")))
    (repo_tmp / "cobolRuntime.mjs").write_text(RUNTIME.read_text())
    (repo_tmp / "drive.mjs").write_text(
        "import { createActor } from 'xstate';\n"
        "import machine from './machine.mjs';\n"
        "const a = createActor(machine); a.start();\n"
        "for (const v of ['1.50','2.25','3.00']) "
        "a.send({ type: 'GET.FILE.IN-FILE', 'IN-AMT': v });\n"
        "a.send({ type: 'END.FILE.IN-FILE' });\n"
        "const s = a.getSnapshot();\n"
        "const want = { 'WS-CNT': '3', 'WS-SUM': '6.75', 'WS-EOF': 'Y' };\n"
        "for (const k in want) if (String(s.context[k]) !== want[k]) "
        "{ console.error(k, s.context[k], 'want', want[k]); process.exit(1); }\n"
        "process.exit(0);\n"
    )
    r = subprocess.run([NODE, str(repo_tmp / "drive.mjs")], capture_output=True,
                       text=True, cwd=str(repo_tmp), timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr


def _run_reactive(tmp, name, driver_body):
    (tmp / "machine.mjs").write_text(emit_reactive_module(_machine(name)))
    (tmp / "cobolRuntime.mjs").write_text(RUNTIME.read_text())
    (tmp / "drive.mjs").write_text(
        "import { createActor } from 'xstate';\n"
        "import machine from './machine.mjs';\n"
        "const a = createActor(machine); a.start();\n" + driver_body)
    return subprocess.run([NODE, str(tmp / "drive.mjs")], capture_output=True,
                          text=True, cwd=str(tmp), timeout=30)


def _expect(want):
    return ("const s = a.getSnapshot();\n"
            f"const want = {json.dumps(want)};\n"
            "if (s.status !== 'done') { console.error('status', s.status); process.exit(1); }\n"
            "for (const k in want) if (String(s.context[k]) !== want[k]) "
            "{ console.error(k, s.context[k], 'want', want[k]); process.exit(1); }\n"
            "process.exit(0);\n")


@pytest.mark.skipif(not (NODE and HAS_XSTATE), reason="node+xstate not available")
def test_return_dispatch_sends_control_back_to_the_right_call_site(repo_tmp):
    """retdisp PERFORMs 9000-BUMP from three sites - two in the main flow and one from
    inside an inlined THRU range. A wrong return address gives a different total, so the
    arithmetic is the proof that the dispatch guards actually dispatch.
    0 +1 =1, x10 =10, +1 =11, +100 =111, +1 =112, +2 =114."""
    r = _run_reactive(repo_tmp, "retdisp.cbl", _expect({"WS-N": "114"}))
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(not (NODE and HAS_XSTATE), reason="node+xstate not available")
def test_perform_section_and_thru_range_are_inlined(repo_tmp):
    # no I/O at all: these must run to completion on microsteps alone
    assert _run_reactive(repo_tmp, "sectperf.cbl",
                         _expect({"WS-A": "12", "WS-B": "12"})).returncode == 0
    assert _run_reactive(repo_tmp, "thrurange.cbl",
                         _expect({"WS-N": "123"})).returncode == 0
    assert _run_reactive(repo_tmp, "accum.cbl",
                         _expect({"WS-I": "5", "WS-SUM": "15"})).returncode == 0


# --------------------------------------------------------------------------- #
# the drawable reactive JSON (the module's config, without needing to run JS)
# --------------------------------------------------------------------------- #

def test_reactive_view_is_a_drawable_machine_view():
    from cobol_xstate.reactive import build_reactive_view
    v = build_reactive_view(_machine("custrpt.cbl"))
    assert v["format"] == "xstate-v5-config"        # same shape as the other views
    assert v["metadata"]["view"] == "reactive"
    assert v["machine"]["initial"] in v["machine"]["states"]


def test_reactive_view_has_no_dangling_targets():
    from cobol_xstate.reactive import build_reactive_view
    st = build_reactive_view(_machine("custrpt.cbl"))["machine"]["states"]
    for name, s in st.items():
        for e in s.get("always", []) or []:
            if e.get("target"):
                assert e["target"] in st, f"{name} -> {e['target']}"
        for ev, h in (s.get("on") or {}).items():
            if isinstance(h, dict) and h.get("target"):
                assert h["target"] in st, f"{name} -{ev}-> {h['target']}"


def test_reactive_view_shows_the_message_contract():
    """The waits and publishes ARE the new system's message contract - a reader must be
    able to see them on the chart without running any JS."""
    from cobol_xstate.reactive import build_reactive_view
    v = build_reactive_view(_machine("custrpt.cbl"))
    waits = {ev for s in v["machine"]["states"].values() for ev in (s.get("on") or {})}
    assert waits == {"GET.FILE.CUST-FILE", "END.FILE.CUST-FILE"}
    publishes = {a for s in v["machine"]["states"].values()
                 for a in (s.get("entry") or []) if a.startswith("publish_")}
    assert publishes == {"publish_CREATE_CONSOLE_SYSOUT"}
    assert v["manifest"]["inbound"] == ["GET.FILE.CUST-FILE", "END.FILE.CUST-FILE"]
    assert v["manifest"]["outbound"] == ["CREATE.CONSOLE.SYSOUT"]


def test_reactive_view_and_module_are_the_same_machine():
    """Two encodings of one lowering - if they could drift, the drawing would stop
    describing the thing that runs."""
    from cobol_xstate.reactive import build_reactive_view
    m = _machine("custrpt.cbl")
    assert build_reactive_view(m)["machine"] == _extract(
        emit_reactive_module(m), "machineConfig")


def _run_dir(root):
    """Where a run writes: --outdir itself, taken literally with nothing appended."""
    return Path(root)


def test_cli_reactive_writes_both_the_module_and_the_drawable_json(tmp_path):
    from cobol_xstate.cli import run
    src = EXAMPLES / "custrpt.cbl"
    assert run([str(src), "--target", "reactive", "--outdir", str(tmp_path)]) == 0
    d = _run_dir(tmp_path)
    assert (d / "custrpt.reactive.mjs").exists()    # runnable
    assert (d / "custrpt.reactive.json").exists()   # drawable
    assert (d / "cobolRuntime.mjs").exists()        # beside it, so the import resolves
