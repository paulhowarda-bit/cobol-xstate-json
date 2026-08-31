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


def test_cics_link_program_data_name_resolves_via_value_clause():
    # LINK PROGRAM(WS-PGM) where WS-PGM has VALUE 'POSTLOG': the endpoint is the
    # MODULE name, not the working-storage identifier that held it.
    iface = _iface(
        "       0000-MAIN.\n"
        "           EXEC CICS LINK PROGRAM(WS-PGM) COMMAREA(WS-AREA) END-EXEC.\n",
        data_body="       01 WS-PGM PIC X(8) VALUE 'POSTLOG'.\n"
                  "       01 WS-AREA PIC X(100).\n",
    )
    endpoints = {e["endpoint"]: e for e in iface["endpoints"]}
    assert "POSTLOG" in endpoints and "WS-PGM" not in endpoints
    assert endpoints["POSTLOG"]["via"] == "WS-PGM"


def test_cics_link_unresolved_dynamic_target_is_marked_dynamic():
    iface = _iface(
        "       0000-MAIN.\n"
        "           MOVE WS-OTHER TO WS-PGM\n"
        "           EXEC CICS LINK PROGRAM(WS-PGM) END-EXEC.\n",
        data_body="       01 WS-PGM PIC X(8).\n"
                  "       01 WS-OTHER PIC X(8).\n",
    )
    endpoints = {e["endpoint"]: e for e in iface["endpoints"]}
    assert endpoints["WS-PGM"]["dynamic"] is True


def test_dynamic_batch_call_unresolved_is_marked_dynamic():
    iface = _iface(
        "       0000-MAIN.\n"
        "           MOVE WS-OTHER TO WS-PGM\n"
        "           CALL WS-PGM.\n",
        data_body="       01 WS-PGM PIC X(8).\n"
                  "       01 WS-OTHER PIC X(8).\n",
    )
    endpoints = {e["endpoint"]: e for e in iface["endpoints"]}
    assert endpoints["WS-PGM"]["dynamic"] is True


def test_dynamic_batch_call_resolved_records_the_via_item():
    iface = _iface(
        "       0000-MAIN.\n"
        "           CALL WS-PGM.\n",
        data_body="       01 WS-PGM PIC X(8) VALUE 'POSTLOG'.\n",
    )
    endpoints = {e["endpoint"]: e for e in iface["endpoints"]}
    assert "POSTLOG" in endpoints and "WS-PGM" not in endpoints
    assert endpoints["POSTLOG"]["via"] == "WS-PGM"


def test_dynamic_transid_queue_file_map_operands_resolve():
    # The same resolution PROGRAM gets applies to EVERY resource-name operand.
    iface = _iface(
        "       0000-MAIN.\n"
        "           EXEC CICS START TRANSID(WS-TRAN) END-EXEC\n"
        "           EXEC CICS WRITEQ TD QUEUE(WS-Q) FROM(WS-MSG) END-EXEC\n"
        "           EXEC CICS READ FILE(WS-F) INTO(WS-REC) END-EXEC\n"
        "           EXEC CICS SEND MAP(WS-MAP) MAPSET('MSETX') END-EXEC.\n",
        data_body="       01 WS-TRAN PIC X(4) VALUE 'AB12'.\n"
                  "       01 WS-Q PIC X(8) VALUE 'ERRQ'.\n"
                  "       01 WS-F PIC X(8) VALUE 'ACCTFILE'.\n"
                  "       01 WS-MAP PIC X(7) VALUE 'MENUMAP'.\n"
                  "       01 WS-MSG PIC X(80).\n"
                  "       01 WS-REC PIC X(80).\n",
    )
    endpoints = {e["endpoint"]: e for e in iface["endpoints"]}
    assert endpoints["AB12"]["type"] == "transaction"
    assert endpoints["AB12"]["via"] == "WS-TRAN"
    assert endpoints["ERRQ"]["type"] == "queue"
    assert endpoints["ERRQ"]["via"] == "WS-Q"
    assert endpoints["ACCTFILE"]["type"] == "file"
    assert endpoints["ACCTFILE"]["via"] == "WS-F"
    assert endpoints["MENUMAP"]["type"] == "terminal"
    assert endpoints["MENUMAP"]["via"] == "WS-MAP"
    for ws in ("WS-TRAN", "WS-Q", "WS-F", "WS-MAP"):
        assert ws not in endpoints


def test_unresolved_transid_and_queue_operands_marked_dynamic():
    iface = _iface(
        "       0000-MAIN.\n"
        "           MOVE WS-OTHER TO WS-TRAN\n"
        "           MOVE WS-OTHER TO WS-Q\n"
        "           EXEC CICS START TRANSID(WS-TRAN) END-EXEC\n"
        "           EXEC CICS READQ TS QUEUE(WS-Q) INTO(WS-REC) END-EXEC.\n",
        data_body="       01 WS-TRAN PIC X(4).\n"
                  "       01 WS-Q PIC X(8).\n"
                  "       01 WS-OTHER PIC X(8).\n"
                  "       01 WS-REC PIC X(80).\n",
    )
    endpoints = {e["endpoint"]: e for e in iface["endpoints"]}
    assert endpoints["WS-TRAN"]["dynamic"] is True
    assert endpoints["WS-Q"]["dynamic"] is True


def test_return_transid_data_name_resolves_in_the_verb():
    iface = _iface(
        "       0000-MAIN.\n"
        "           EXEC CICS RETURN TRANSID(WS-NEXT) END-EXEC.\n",
        data_body="       01 WS-NEXT PIC X(4) VALUE 'AB12'.\n",
    )
    ev = next(e for e in iface["events"] if e["endpointType"] == "caller")
    assert "TRANSID(AB12)" in ev["verb"]


def test_dynamic_sql_is_its_own_endpoint_kind_marked_dynamic():
    """Classified as "db2", every host variable of a PREPARE/EXECUTE read as a column-
    mapping failure downstream; "dynamic_sql" is the discriminator that says the mapping
    is inherently impossible here, not unrecovered (same move as db2_proc)."""
    iface = _iface(
        "       0000-MAIN.\n"
        "           EXEC SQL EXECUTE IMMEDIATE :WS-SQL END-EXEC.\n",
        data_body="       01 WS-SQL PIC X(200).\n",
    )
    endpoints = {e["endpoint"]: e for e in iface["endpoints"]}
    ep = endpoints["<dynamic-sql>"]
    assert ep["type"] == "dynamic_sql"
    assert ep["dynamic"] is True
    assert sorted(ep["directions"]) == ["create", "get"]
    ev = next(e for e in iface["events"] if e["endpoint"] == "<dynamic-sql>")
    assert ev["fields"] == ["WS-SQL"]
    assert ev["endpointType"] == "dynamic_sql"
    # The event name follows the type, exactly as *.DB2_PROC.* does.
    assert ev["event"] in ("GET.DYNAMIC_SQL.<dynamic-sql>",
                           "CREATE.DYNAMIC_SQL.<dynamic-sql>")
    assert "<dynamic-sql>" not in {
        e["endpoint"] for e in iface["events"] if e["endpointType"] == "db2"}


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


# --------------------------------------------------------------------------- #
# Field-level capture + previously-invisible channels
# --------------------------------------------------------------------------- #

def _iface_of(src: str) -> dict:
    from cobol_xstate.parser import parse_program
    from cobol_xstate.statechart import build_machine
    return build_machine(parse_program(src)).bundle()["interface"]


_CICS_SRC = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. CQ.\n"
    "       DATA DIVISION.\n"
    "       WORKING-STORAGE SECTION.\n"
    "       01  WS-BUF        PIC X(80).\n"
    "       01  WS-REC        PIC X(80).\n"
    "       LINKAGE SECTION.\n"
    "       01  DFHCOMMAREA.\n"
    "           05  CA-ID     PIC 9(6).\n"
    "       PROCEDURE DIVISION.\n"
    "       0000-MAIN.\n"
    "           IF EIBCALEN = 0\n"
    "               EXEC CICS ABEND ABCODE('NOCA') END-EXEC\n"
    "           END-IF\n"
    "           EXEC CICS READQ TS QUEUE('MYTSQ') INTO(WS-BUF) END-EXEC\n"
    "           EXEC CICS READ DATASET('ACCT') INTO(WS-REC) RIDFLD(CA-ID)\n"
    "           END-EXEC\n"
    "           EXEC CICS WRITEQ TD QUEUE('MYTDQ') FROM(WS-BUF) END-EXEC\n"
    "           EXEC CICS RETURN TRANSID('CQ02') COMMAREA(DFHCOMMAREA)\n"
    "           END-EXEC.\n"
)


def test_cics_queues_are_visible_with_fields():
    iface = _iface_of(_CICS_SRC)
    evs = {(e["verb"], e["endpoint"]): e for e in iface["events"]}
    rq = evs[("CICS READQ TS", "MYTSQ")]
    assert rq["direction"] == "get" and rq["fields"] == ["WS-BUF"]
    wq = evs[("CICS WRITEQ TD", "MYTDQ")]
    assert wq["direction"] == "create" and wq["fields"] == ["WS-BUF"]


def test_cics_return_with_commarea_and_transid_is_visible():
    iface = _iface_of(_CICS_SRC)
    ret = next(e for e in iface["events"] if e["verb"].startswith("CICS RETURN"))
    assert "TRANSID(CQ02)" in ret["verb"]        # the pseudo-conversational contract
    assert ret["fields"] == ["DFHCOMMAREA"]      # the returned COMMAREA
    assert ret["direction"] == "create" and ret["endpointType"] == "caller"


def test_cics_read_carries_into_and_ridfld_key():
    iface = _iface_of(_CICS_SRC)
    rd = next(e for e in iface["events"] if e["verb"] == "CICS READ")
    assert rd["fields"] == ["WS-REC"]            # landing area
    assert rd.get("params") == ["CA-ID"]         # outbound key (from LINKAGE!)


def test_eibcalen_branch_is_a_cics_input_and_abend_visible():
    iface = _iface_of(_CICS_SRC)
    assert any(e["endpoint"] == "CICS-EIB" and "EIBCALEN" in e["fields"]
               for e in iface["events"])
    assert any(e["verb"] == "CICS ABEND" and e["endpoint"] == "NOCA"
               for e in iface["events"])


def test_file_status_branch_is_a_response_event():
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. FS.\n"
        "       ENVIRONMENT DIVISION.\n"
        "       INPUT-OUTPUT SECTION.\n"
        "       FILE-CONTROL.\n"
        "           SELECT MAST-FILE ASSIGN TO MASTDD\n"
        "               ORGANIZATION IS INDEXED\n"
        "               RECORD KEY IS M-KEY\n"
        "               FILE STATUS IS WS-FSTAT.\n"
        "       DATA DIVISION.\n"
        "       FILE SECTION.\n"
        "       FD  MAST-FILE.\n"
        "       01  MAST-REC.\n"
        "           05  M-KEY   PIC X(8).\n"
        "           05  M-AMT   PIC 9(5).\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01  WS-FSTAT    PIC XX.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           OPEN INPUT MAST-FILE\n"
        "           READ MAST-FILE\n"
        "               AT END CONTINUE\n"
        "           END-READ\n"
        "           IF WS-FSTAT NOT = '00'\n"
        "               DISPLAY 'BAD ' WS-FSTAT\n"
        "           END-IF\n"
        "           STOP RUN.\n"
    )
    iface = _iface_of(src)
    # branching on the FILE STATUS field is a response event from that file
    assert any(e["endpointType"] == "response" and e["endpoint"] == "MAST-FILE"
               and e["fields"] == ["WS-FSTAT"] for e in iface["events"])
    # the file endpoint carries its external binding from FILE-CONTROL
    ep = next(p for p in iface["endpoints"] if p["endpoint"] == "MAST-FILE")
    assert ep["assign"] == "MASTDD" and ep["organization"] == "INDEXED"
    assert ep["statusField"] == "WS-FSTAT" and ep["recordKey"] == "M-KEY"
    # READ with no INTO lists the FD record's field layout
    rd = next(e for e in iface["events"] if e["verb"] == "READ")
    assert set(rd["fields"]) >= {"MAST-REC", "M-KEY", "M-AMT"}
    # DISPLAY of a variable carries it as a field
    disp = next(e for e in iface["events"] if e["verb"] == "DISPLAY")
    assert disp["fields"] == ["WS-FSTAT"]


def test_read_into_carries_the_into_records_leaves_not_just_the_group():
    """``READ f INTO ws-rec`` == ``READ f; MOVE fd-record TO ws-rec``: subsequent
    statements address ``ws-rec``'s ELEMENTARY fields, so the crossing must carry them -
    the file analogue of ``SELECT ... INTO :a :b``. Here the FD record is one opaque
    ``X(13)`` field while the INTO target reinterprets those bytes as key + amount, so the
    event must carry the INTO record's leaves (WS-KEY / WS-AMT), NOT the FD record. Listing
    only the group record name left the reactive ``recv`` with no context key to assign."""
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. RI.\n"
        "       ENVIRONMENT DIVISION.\n"
        "       INPUT-OUTPUT SECTION.\n"
        "       FILE-CONTROL.\n"
        "           SELECT IN-FILE ASSIGN TO INDD.\n"
        "       DATA DIVISION.\n"
        "       FILE SECTION.\n"
        "       FD  IN-FILE.\n"
        "       01  IN-REC        PIC X(13).\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01  WS-REC.\n"
        "           05  WS-KEY    PIC X(8).\n"
        "           05  WS-AMT    PIC 9(5).\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           READ IN-FILE INTO WS-REC\n"
        "               AT END CONTINUE\n"
        "           END-READ\n"
        "           STOP RUN.\n"
    )
    iface = _iface_of(src)
    rd = next(e for e in iface["events"] if e["verb"] == "READ")
    assert set(rd["fields"]) >= {"WS-KEY", "WS-AMT"}   # the leaves the machine addresses
    assert "IN-REC" not in rd["fields"]                # not the opaque FD record


# --------------------------------------------------------------------------- #
# a CALL names the program the COBOL names - never the name registry's spelling of it
#
# naming.NameRegistry keys a registered name on (kind, COBOL text) and appends _2, _3, ...
# to keep two STATEMENTS apart. Two CALLs to one program differing only in their USING
# operands are two statements, so the second registered as `call_MQINQ_2` - and the endpoint
# was recovered by splitting that name, which produced a program called "MQINQ_2".
#
# It exists nowhere. It was classified `unresolved` (the MQI verb list holds "MQINQ"), which
# made it FETCHABLE, so the estate was asked for it - twice, cobol then asm - came back
# not-found, and a downstream impact analysis read that as a missing program. It also
# emitted a lineage event CREATE.PROGRAM.MQINQ_2 into the message contract.
#
# That two IDENTICAL CALLs have always collapsed to one endpoint is what makes this a bug
# rather than a modelling choice: only differing text split them, and only the split was
# phantom.
# --------------------------------------------------------------------------- #

_MQ_TWICE = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. MQTWICE.\n"
    "       DATA DIVISION.\n"
    "       WORKING-STORAGE SECTION.\n"
    "       01 WS-A PIC S9(9) COMP.\n"
    "       01 WS-B PIC S9(9) COMP.\n"
    "       PROCEDURE DIVISION.\n"
    "       0000-MAIN.\n"
    "           CALL 'MQINQ' USING WS-A\n"
    "           CALL 'MQINQ' USING WS-B\n"
    "           STOP RUN.\n"
)


def _mq_twice_machine():
    return build_machine(parse_program(_MQ_TWICE), source_name="mqtwice.cbl")


def test_calling_one_program_twice_names_it_once():
    """The registry still allocates two names - it must, they are two statements - but
    both name the same program, so the perimeter has one endpoint."""
    m = _mq_twice_machine()
    assert "call_MQINQ_2" in m.provenance, \
        "two CALLs with different operands must still be two registered statements"
    programs = [e["endpoint"] for e in m.interface()["endpoints"]
                if e.get("type") == "program"]
    assert programs == ["MQINQ"], f"expected one MQINQ endpoint, got {programs}"


def test_the_phantom_program_is_not_classified_fetched_or_published():
    """Every consequence in one test, because fixing only the classification would leave
    the other three: the manifest would still list a program that exists nowhere, the
    estate would still be asked for it, and the message contract would still carry it."""
    from cobol_xstate.artifacts import build_artifacts
    from mainframe_artifacts.fetch import build_fetch_plan
    from cobol_xstate.lineage import build_lineage
    m = _mq_twice_machine()
    art = build_artifacts(m)

    rows = [r for r in art["artifacts"] if r["kind"] == "program"]
    assert [r["artifact"] for r in rows] == ["MQINQ"]
    assert rows[0]["classification"] == "ibm-runtime"
    assert rows[0]["subsystem"] == "ibm-mq"

    # ibm-runtime is NON_FETCHABLE, so nothing is requested from the estate at all
    planned = [e for e in build_fetch_plan(art) if e["status"] == "planned"]
    assert planned == [], f"the estate would be asked for {[e['request'] for e in planned]}"

    events = {r["event"] for r in build_lineage(m)["rows"]}
    assert not [e for e in events if e.endswith("_2")], \
        f"a phantom program reached the message contract: {sorted(events)}"


def _call_endpoints(proc_body: str, data_body: str = ""):
    return [e for e in _iface(proc_body, data_body)["endpoints"]
            if e.get("type") == "program"]


def test_a_static_call_names_the_literal():
    """`CALL 'DSNTIAC'` - the label carries the target in quotes."""
    eps = _call_endpoints("           CALL 'DSNTIAC' USING WS-A\n"
                          "           STOP RUN.\n",
                          "       01 WS-A PIC X(4).\n")
    assert [e["endpoint"] for e in eps] == ["DSNTIAC"]
    assert "via" not in eps[0] and "dynamic" not in eps[0]


def test_a_resolved_dynamic_call_names_the_literal_and_keeps_the_item_it_came_via():
    """The at-risk spelling: `CALL WS-PGM -> resolved 'POSTLOG' (only literal reaching
    WS-PGM is 'POSTLOG')` holds TWO quoted strings, and the endpoint is the first. The
    identifier it was proved through has to survive as `via` - it is the evidence."""
    eps = _call_endpoints("           CALL WS-PGM USING WS-A\n"
                          "           STOP RUN.\n",
                          "       01 WS-A PIC X(4).\n"
                          "       01 WS-PGM PIC X(8) VALUE 'POSTLOG'.\n")
    assert [e["endpoint"] for e in eps] == ["POSTLOG"]
    assert eps[0]["via"] == "WS-PGM"


def test_an_unresolved_dynamic_call_names_the_data_item_and_stays_marked_dynamic():
    """Nothing proves the target, so the honest endpoint is the ITEM, not a guess - and it
    must keep saying so, or a data item reads downstream as a load-module name."""
    eps = _call_endpoints("           CALL WS-PGM USING WS-A\n"
                          "           STOP RUN.\n",
                          "       01 WS-A PIC X(4).\n"
                          "       01 WS-PGM PIC X(8).\n")
    assert [e["endpoint"] for e in eps] == ["WS-PGM"]
    assert eps[0]["dynamic"] is True


def test_the_endpoint_keeps_the_casing_the_cobol_used():
    """Read from the original text, not an uppercased copy. A lower-case CALL literal has
    always been reported as written; this fix removes a phantom name, it does not license
    re-casing real ones."""
    eps = _call_endpoints("           CALL 'mixedCase' USING WS-A\n"
                          "           STOP RUN.\n",
                          "       01 WS-A PIC X(4).\n")
    assert [e["endpoint"] for e in eps] == ["mixedCase"]


def test_an_unrecognised_call_spelling_falls_back_rather_than_vanishing():
    """A provenance spelling this does not know must degrade to the old derivation. A
    suffixed name is wrong; no name at all would drop the dependency entirely."""
    from cobol_xstate.interface import _call_endpoint
    assert _call_endpoint("CALL 'POSTLOG' USING X", "call_POSTLOG") == "POSTLOG"
    assert _call_endpoint("SOMETHING ELSE ENTIRELY", "call_POSTLOG_2") == "POSTLOG_2"
    assert _call_endpoint("", "call_POSTLOG") == "POSTLOG"


# -- literals are data, not keywords (audit findings #8, #9) ----------------

def test_display_literal_containing_upon_keeps_the_real_operands():
    """The mask sat one line BELOW the UPON split, so `DISPLAY 'REPORT UPON REQUEST'
    WS-CODE` cut the operand list inside its own literal - naming the phantom field
    REPORT and dropping WS-CODE."""
    from cobol_xstate.interface import _display_fields
    data = {"REPORT": {}, "WS-CODE": {}}
    assert _display_fields("DISPLAY 'REPORT UPON REQUEST' WS-CODE", data) == ["WS-CODE"]
    # A real UPON clause is still stripped.
    assert _display_fields("DISPLAY WS-CODE UPON CONSOLE", data) == ["WS-CODE"]


def test_sql_string_constant_containing_from_is_not_the_table():
    """`SELECT 'FROM AUDIT', COL1 ... FROM CUSTMAST` named the phantom Db2 table AUDIT
    - which the artifact manifest then listed, and the fetch stage requested from the
    estate by name."""
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. T.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01  WS-TAG   PIC X(12).\n"
        "       01  WS-COL   PIC X(10).\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           EXEC SQL\n"
        "             SELECT 'FROM AUDIT', COL1\n"
        "               INTO :WS-TAG, :WS-COL\n"
        "               FROM CUSTMAST\n"
        "           END-EXEC\n"
        "           STOP RUN.\n")
    iface = build_machine(parse_program(src)).bundle()["interface"]
    endpoints = {e["endpoint"] for e in iface["endpoints"]}
    assert "CUSTMAST" in endpoints
    assert "AUDIT" not in endpoints


def test_time_literal_colons_are_not_host_variables():
    """'12:30:45' in a WHERE clause read as the host variables :30 and :45 whenever the
    parser supplied no real ones - and DELETE is exactly the verb whose spec carries
    none, so the fallback scan is the one that fires. End-to-end through classify, so
    the CALL SITE is pinned, not just the helper."""
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. T.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01  WS-X   PIC X(4).\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           EXEC SQL\n"
        "             DELETE FROM AUDITLOG WHERE TS = '12:30:45'\n"
        "           END-EXEC\n"
        "           STOP RUN.\n")
    iface = build_machine(parse_program(src)).bundle()["interface"]
    fields = {f for s in iface["perimeterStates"].values()
              for f in s.get("creates", []) + s.get("gets", [])}
    assert "30" not in fields and "45" not in fields
    endpoints = {e["endpoint"] for e in iface["endpoints"]}
    assert "AUDITLOG" in endpoints


def test_cursor_table_binds_past_a_from_inside_the_select_list():
    from cobol_xstate.interface import _cursor_tables
    prov = {"a": {"cobol": "EXEC SQL DECLARE C1 CURSOR FOR SELECT 'FROM AUDIT', COL1 "
                            "FROM CUSTMAST END-EXEC"}}
    assert _cursor_tables(prov) == {"C1": "CUSTMAST"}


# --------------------------------------------------------------------------- #
# DML: the row selector is not the data written
# (docs/issues/unmapped-fields-v52.md, Issue 2)
# --------------------------------------------------------------------------- #

_DML_DATA = (
    "       01  WS-BAL          PIC S9(7)V99 COMP-3.\n"
    "       01  WS-NAME         PIC X(20).\n"
    "       01  WS-REF          PIC X(8).\n"
    "       01  WS-ID           PIC 9(9).\n"
    "       01  IND-BAL         PIC S9(4) COMP.\n"
)


def _sql_event(iface, verb):
    return next(e for e in iface["events"] if e["verb"] == verb)


def test_update_where_variable_is_a_param_not_a_field():
    iface = _iface(
        "       0000-MAIN.\n"
        "           EXEC SQL\n"
        "               UPDATE CUSTOMER SET BAL = :WS-BAL\n"
        "               WHERE ID = :WS-ID\n"
        "           END-EXEC.\n", _DML_DATA)
    upd = _sql_event(iface, "UPDATE")
    assert upd["fields"] == ["WS-BAL"]
    assert upd["params"] == ["WS-ID"]


def test_delete_writes_nothing_so_every_variable_is_a_param():
    iface = _iface(
        "       0000-MAIN.\n"
        "           EXEC SQL DELETE FROM CUSTOMER WHERE ID = :WS-ID END-EXEC.\n",
        _DML_DATA)
    dele = _sql_event(iface, "DELETE")
    assert dele["fields"] == []
    assert dele["params"] == ["WS-ID"]


def test_a_subselects_where_does_not_sweep_up_a_sibling_set_variable():
    """Scoping each WHERE to its own parentheses is what keeps the two apart.

    `:WS-REF` filters the subselect and `:WS-ID` picks the updated row - both are
    predicates. `:WS-NAME` sits in a SET clause AFTER the subselect closes, so reading
    from the first WHERE to the end of the statement would sweep the write in with the
    filters and lose its column mapping.
    """
    iface = _iface(
        "       0000-MAIN.\n"
        "           EXEC SQL\n"
        "               UPDATE CUSTOMER SET BAL =\n"
        "                   (SELECT TOT FROM LEDGER WHERE REF = :WS-REF),\n"
        "                   NAME = :WS-NAME\n"
        "               WHERE ID = :WS-ID\n"
        "           END-EXEC.\n", _DML_DATA)
    upd = _sql_event(iface, "UPDATE")
    assert upd["fields"] == ["WS-NAME"]
    assert upd["params"] == ["WS-REF", "WS-ID"]


def test_a_variable_on_both_sides_stays_a_field():
    """`SET LAST_ID = :WS-ID WHERE ID = :WS-ID` writes AND filters with one variable.

    The write is the stronger claim: the source proves which column it fills, and
    demoting it to a parameter would throw that mapping away.
    """
    iface = _iface(
        "       0000-MAIN.\n"
        "           EXEC SQL\n"
        "               UPDATE CUSTOMER SET LAST_ID = :WS-ID\n"
        "               WHERE ID = :WS-ID\n"
        "           END-EXEC.\n", _DML_DATA)
    upd = _sql_event(iface, "UPDATE")
    assert upd["fields"] == ["WS-ID"]
    assert "params" not in upd


def test_an_indicator_variable_does_not_demote_the_write_it_qualifies():
    """`SET BAL = :WS-BAL:IND-BAL` defeats the SET-pair matcher, so no column mapping
    is recovered for :WS-BAL - and a fields-minus-columns subtraction would call it a
    filter on that evidence alone. The split is structural instead: :WS-BAL is not in
    the WHERE clause, so it stays a write whatever the mapping did
    (docs/issues/conventions-indicator-variable-bug.md is the same failure class).
    """
    iface = _iface(
        "       0000-MAIN.\n"
        "           EXEC SQL\n"
        "               UPDATE CUSTOMER SET BAL = :WS-BAL:IND-BAL\n"
        "               WHERE ID = :WS-ID\n"
        "           END-EXEC.\n", _DML_DATA)
    upd = _sql_event(iface, "UPDATE")
    assert "WS-BAL" in upd["fields"]
    assert upd["params"] == ["WS-ID"]


# -- CICS operands are blank-padded to their 8-byte field inside the quotes ----------
# `PROGRAM('ACTC000 ')` used to match nothing at all: the optional closing quote sat
# BEFORE the `\s*`, so it could only match a quote adjacent to the captured name. The
# failure then surfaced as a name-shaped sentinel (`<program>`), not as an error - and
# two padded targets in one program deduped into ONE `<program>` endpoint, dropping the
# second callee from the dependency graph entirely.
_PADDED_SRC = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. PADDED.\n"
    "       DATA DIVISION.\n"
    "       WORKING-STORAGE SECTION.\n"
    "       01  WS-FLAG   PIC X VALUE 'N'.\n"
    "       01  WS-REC    PIC X(80).\n"
    "       PROCEDURE DIVISION.\n"
    "       0000-MAIN.\n"
    "           IF WS-FLAG = 'Y'\n"
    "               EXEC CICS XCTL PROGRAM('ACTC000 ') END-EXEC\n"
    "           ELSE\n"
    "               EXEC CICS XCTL PROGRAM ('ACTC099 ') END-EXEC\n"
    "           END-IF\n"
    "           EXEC CICS READ FILE('CUSTFIL ') INTO(WS-REC) END-EXEC\n"
    "           EXEC CICS SEND MAP('MP1     ') MAPSET('MSET1   ') END-EXEC\n"
    "           EXEC CICS READQ TS QUEUE('QN1     ') INTO(WS-REC) END-EXEC\n"
    "           EXEC CICS START TRANSID('CQ03 ') END-EXEC\n"
    "           EXEC CICS ABEND ABCODE('NOCA ') END-EXEC.\n"
)


def test_quoted_blank_padded_program_operands_do_not_collapse_to_a_sentinel():
    iface = _iface_of(_PADDED_SRC)
    endpoints = {e["endpoint"]: e for e in iface["endpoints"]}
    # BOTH callees survive - they used to dedupe into a single `<program>` row.
    assert endpoints["ACTC000"]["type"] == "program"
    assert endpoints["ACTC099"]["type"] == "program"
    assert "<program>" not in endpoints


def test_padded_variants_for_every_resource_keyword():
    """FILE/MAP/MAPSET/QUEUE each degraded to their own sentinel, and TRANSID/ABCODE
    reach this through _CICS_OPT rather than _CICS_RESOURCE, so they need their own
    cases - fixing one pattern would not have fixed the other."""
    iface = _iface_of(_PADDED_SRC)
    endpoints = {e["endpoint"]: e for e in iface["endpoints"]}
    assert endpoints["CUSTFIL"]["type"] == "file"
    assert endpoints["MP1"]["type"] == "terminal"
    assert endpoints["QN1"]["type"] == "queue"
    assert endpoints["CQ03"]["type"] == "transaction"
    # ...and no name-shaped sentinel is left anywhere in the overlay.
    assert not [n for n in endpoints if n.startswith("<")]


def test_unpadded_and_unquoted_cics_operands_still_resolve():
    """The regression guard for the fix itself: the two forms that already worked."""
    iface = _iface_of(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. T.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01  WS-PGM   PIC X(8) VALUE 'ACTC150'.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           EXEC CICS LINK PROGRAM('ACTC099') END-EXEC\n"
        "           EXEC CICS XCTL PROGRAM(WS-PGM) END-EXEC.\n")
    endpoints = {e["endpoint"] for e in iface["endpoints"]}
    assert "ACTC099" in endpoints and "ACTC150" in endpoints
