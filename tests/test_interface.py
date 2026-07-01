"""The external-interface / perimeter overlay: which states get or create external events."""

from cobol_xstate.parser import parse_program
from cobol_xstate.statechart import build_machine


def _iface(proc_body: str, data_body: str = "") -> dict:
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. T.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n" + data_body +
        "       PROCEDURE DIVISION.\n" + proc_body
    )
    return build_machine(parse_program(src)).bundle()["interface"]


def _events_at(iface, state):
    d = iface["perimeterStates"].get(state, {"gets": [], "creates": []})
    return d["gets"], d["creates"]


def test_file_read_is_a_get_and_write_is_a_create():
    iface = _iface(
        "       0000-MAIN.\n"
        "           READ TRAN-FILE AT END CONTINUE END-READ\n"
        "           WRITE REPORT-REC.\n"
    )
    endpoints = {e["endpoint"]: e for e in iface["endpoints"]}
    assert endpoints["TRAN-FILE"]["type"] == "file"
    assert "get" in endpoints["TRAN-FILE"]["directions"]
    # some perimeter state gets TRAN-FILE and some creates the report record
    gets = [ev for d in iface["perimeterStates"].values() for ev in d["gets"]]
    creates = [ev for d in iface["perimeterStates"].values() for ev in d["creates"]]
    assert "GET.FILE.TRAN-FILE" in gets
    assert any(ev.startswith("CREATE.FILE.") for ev in creates)


def test_display_is_a_create_to_console():
    iface = _iface(
        "       0000-MAIN.\n"
        "           DISPLAY 'HELLO'.\n"
    )
    creates = [ev for d in iface["perimeterStates"].values() for ev in d["creates"]]
    assert "CREATE.CONSOLE.SYSOUT" in creates


def test_call_is_a_create_to_a_program():
    iface = _iface(
        "       0000-MAIN.\n"
        "           CALL 'POSTLOG'.\n"
    )
    creates = [ev for d in iface["perimeterStates"].values() for ev in d["creates"]]
    assert "CREATE.PROGRAM.POSTLOG" in creates


def test_sql_select_is_a_get_from_db2_with_fields():
    iface = _iface(
        "       0000-MAIN.\n"
        "           EXEC SQL SELECT NAME, BAL INTO :WS-NAME, :WS-BAL\n"
        "               FROM CUSTOMER WHERE ID = :WS-ID END-EXEC.\n",
        data_body=(
            "       01 WS-NAME PIC X(20).\n"
            "       01 WS-BAL  PIC 9(7)V99.\n"
            "       01 WS-ID   PIC 9(6).\n"
        ),
    )
    ev = next(e for e in iface["events"] if e["endpointType"] == "db2")
    assert ev["direction"] == "get"
    assert ev["endpoint"] == "CUSTOMER"
    assert set(ev["fields"]) == {"WS-NAME", "WS-BAL"}


def test_internal_moves_and_computes_are_not_perimeter():
    iface = _iface(
        "       0000-MAIN.\n"
        "           MOVE 1 TO WS-A\n"
        "           ADD WS-A TO WS-B.\n",
        data_body="       01 WS-A PIC 9. \n       01 WS-B PIC 99.\n",
    )
    assert iface["perimeterStates"] == {}
    assert iface["events"] == []


def test_program_parameter_interface_using_returning_linkage():
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. SUBPGM.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01 WS-RC PIC 9(4).\n"
        "       LINKAGE SECTION.\n"
        "       01 LK-REQUEST PIC X(80).\n"
        "       01 LK-REPLY   PIC X(80).\n"
        "       PROCEDURE DIVISION USING LK-REQUEST LK-REPLY RETURNING WS-RC.\n"
        "       0000-MAIN.\n"
        "           MOVE 'OK' TO LK-REPLY\n"
        "           GOBACK.\n"
    )
    iface = build_machine(parse_program(src)).bundle()["interface"]
    p = iface["parameters"]
    assert p["using"] == ["LK-REQUEST", "LK-REPLY"]
    assert p["returning"] == "WS-RC"
    assert set(p["linkage"]) == {"LK-REQUEST", "LK-REPLY"}
    # The entry gets the caller's parameters and creates a reply back to the caller.
    caller_get = [e for e in iface["events"]
                  if e["endpointType"] == "caller" and e["direction"] == "get"]
    assert caller_get and set(caller_get[0]["fields"]) == {"LK-REQUEST", "LK-REPLY"}
    caller_create = [e for e in iface["events"]
                     if e["endpointType"] == "caller" and e["direction"] == "create"]
    assert any("WS-RC" in e["fields"] for e in caller_create)


def test_call_using_arguments_become_event_fields():
    iface = _iface(
        "       0000-MAIN.\n"
        "           CALL 'AUDIT' USING WS-REQ WS-RESP.\n",
        data_body="       01 WS-REQ PIC X(10).\n       01 WS-RESP PIC X(10).\n",
    )
    ev = next(e for e in iface["events"] if e["endpoint"] == "AUDIT")
    assert ev["direction"] == "create"
    assert ev["endpointType"] == "program"
    assert ev["fields"] == ["WS-REQ", "WS-RESP"]


def test_linkage_moves_are_receive_request_and_send_response():
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. LKSUB.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01 WS-NAME PIC X(20).\n"
        "       LINKAGE SECTION.\n"
        "       01 LK-REQ-AREA.\n"
        "          05 LK-CUST-ID PIC 9(6).\n"
        "          05 LK-REPLY   PIC X(20).\n"
        "       PROCEDURE DIVISION USING LK-REQ-AREA.\n"
        "       0000-MAIN.\n"
        "           MOVE LK-CUST-ID TO WS-NAME\n"
        "           MOVE WS-NAME TO LK-REPLY\n"
        "           GOBACK.\n"
    )
    iface = build_machine(parse_program(src)).bundle()["interface"]
    caller = [e for e in iface["events"] if e["endpointType"] == "caller"]
    # reading a linkage field is a get (receive request); writing one is a create (send)
    reads = [e for e in caller if e["direction"] == "get" and "LK-CUST-ID" in e["fields"]]
    writes = [e for e in caller if e["direction"] == "create" and "LK-REPLY" in e["fields"]]
    assert reads, "MOVE from a linkage field should be a receive-request get"
    assert writes, "MOVE to a linkage field should be a send-response create"


def test_sqlcode_branch_is_a_db2_response_event():
    iface = _iface(
        "       0000-MAIN.\n"
        "           EXEC SQL SELECT NAME INTO :WS-N FROM CUST END-EXEC\n"
        "           EVALUATE SQLCODE\n"
        "             WHEN 0 MOVE 'OK' TO WS-N\n"
        "             WHEN OTHER MOVE 'NG' TO WS-N\n"
        "           END-EVALUATE.\n",
        data_body="       01 WS-N PIC X(4).\n",
    )
    resp = [e for e in iface["events"] if e["endpointType"] == "response"]
    assert resp and resp[0]["direction"] == "get"
    assert resp[0]["fields"] == ["SQLCODE"]


def test_cics_link_commarea_is_a_field():
    iface = _iface(
        "       0000-MAIN.\n"
        "           EXEC CICS LINK PROGRAM('POSTLOG') COMMAREA(WS-AREA) END-EXEC.\n",
        data_body="       01 WS-AREA PIC X(100).\n",
    )
    ev = next(e for e in iface["events"] if e["endpoint"] == "POSTLOG")
    assert ev["fields"] == ["WS-AREA"]


def test_perimeter_states_are_tagged_on_the_machine_nodes():
    prog = parse_program(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. T.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           DISPLAY 'HI'.\n"
    )
    m = build_machine(prog)
    bundle = m.bundle()
    # the state that DISPLAYs is tagged meta.perimeter = output on the machine itself
    def find(states):
        for n, st in (states or {}).items():
            if st.get("meta", {}).get("perimeter"):
                return st["meta"]["perimeter"]
            got = find(st.get("states"))
            if got:
                return got
        return None
    assert find(bundle["machine"]["states"]) == "output"


def test_cics_handle_condition_is_a_get_in_the_handlers_region():
    iface = _iface(
        "       DECLARATIVES.\n"
        "       ERR-SECTION SECTION.\n"
        "           USE AFTER STANDARD ERROR PROCEDURE ON CUST-FILE.\n"
        "       ERR-PARA.\n"
        "           DISPLAY 'IO ERR'.\n"
        "       END DECLARATIVES.\n"
        "       0000-MAIN.\n"
        "           READ CUST-FILE END-READ.\n"
    )
    # the watch state in the HANDLERS region gets an external error condition
    conds = [ev for d in iface["perimeterStates"].values() for ev in d["gets"]
             if ev.startswith("GET.CONDITION.")]
    assert conds, "an external error/exception condition should be a 'get'"
