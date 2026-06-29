from cobol_xstate.normalizer import normalize
from cobol_xstate.lexer import tokenize
from cobol_xstate.preprocessor import preprocess, CopybookResolver
from cobol_xstate.parser import parse_program
from cobol_xstate.statechart import build_machine
from cobol_xstate.model import ExecStmt


def _write(tmp_path, name, text):
    (tmp_path / name).write_text(text)
    return CopybookResolver(paths=[str(tmp_path)])


# -- COPY / REPLACE --------------------------------------------------------

def test_copy_brings_in_copybook_data_items(tmp_path):
    resolver = _write(tmp_path, "REC.cpy",
                      "       01  REC.\n"
                      "           05  REC-AMT  PIC S9(5)V99 COMP-3.\n")
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. T.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       COPY REC.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-X.\n"
        "           ADD 1 TO REC-AMT.\n"
    )
    prog = parse_program(src, resolver=resolver)
    assert "REC-AMT" in prog.data_by_name
    t = prog.data_by_name["REC-AMT"].type
    assert t.usage == "COMP-3" and t.signed and t.scale == 2


def test_copy_replacing_pseudo_text(tmp_path):
    resolver = _write(tmp_path, "REC.cpy",
                      "       01  :PFX:-REC.\n"
                      "           05  :PFX:-ID  PIC 9(4).\n")
    src = (
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       COPY REC REPLACING ==:PFX:== BY ==CUST==.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-X.\n"
        "           MOVE 1 TO CUST-ID.\n"
    )
    prog = parse_program(src, resolver=resolver)
    assert "CUST-ID" in prog.data_by_name
    assert "CUST-REC" in prog.data_by_name


def test_missing_copybook_is_reported_not_silent(tmp_path):
    src = (
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       COPY NOSUCH.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-X.\n"
        "           CONTINUE.\n"
    )
    prog = parse_program(src, resolver=CopybookResolver(paths=[str(tmp_path)]))
    assert any("NOSUCH" in n and "missing" in n for n in prog.notes)


def test_preprocess_unit_expands_and_records(tmp_path):
    resolver = _write(tmp_path, "A.cpy", "       01  A-FIELD PIC X.\n")
    lines = normalize("       COPY A.\n")
    res = preprocess(lines, resolver)
    assert "A" in res.expanded
    assert any("A-FIELD" in cl.text for cl in res.lines)
    assert res.lines[0].origin == "A"


# -- copybook provenance (origin threaded to tokens / data / paragraphs) ----

def test_tokens_carry_copybook_origin(tmp_path):
    resolver = _write(tmp_path, "A.cpy", "       01  A-FIELD PIC X.\n")
    res = preprocess(normalize("       COPY A.\n"), resolver)
    toks = tokenize(res.lines)
    a_field = next(t for t in toks if t.up == "A-FIELD")
    assert a_field.origin == "A"


def test_copybook_data_items_carry_member(tmp_path):
    resolver = _write(tmp_path, "REC.cpy",
                      "       01  REC.\n"
                      "           05  REC-AMT  PIC 9(5) COMP-3.\n"
                      "           05  REC-FLG  PIC X.\n"
                      "               88  REC-OK  VALUE 'Y'.\n")
    src = (
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       COPY REC.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-X.\n"
        "           ADD 1 TO REC-AMT.\n"
    )
    machine = build_machine(parse_program(src, resolver=resolver))
    assert machine.data["REC-AMT"]["member"] == "REC"
    assert machine.data["REC-OK"]["member"] == "REC"   # 88-level too


def test_copybook_paragraph_shows_member_in_provenance(tmp_path):
    # A procedure copybook: the performed paragraph's provenance names its member.
    resolver = _write(tmp_path, "PROC.cpy",
                      "       1000-LOG.\n"
                      "           ADD 1 TO WS-N.\n")
    src = (
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01  WS-N PIC 9(3) VALUE 0.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           PERFORM 1000-LOG\n"
        "           STOP RUN.\n"
        "       COPY PROC.\n"
    )
    machine = build_machine(parse_program(src, resolver=resolver))
    assert machine.provenance["1000-LOG"]["member"] == "PROC"
    # a non-copybook paragraph has no member key
    assert "member" not in machine.provenance["0000-MAIN"]


# -- EXEC SQL / CICS / DLI extraction --------------------------------------

def _stmts(proc_body):
    prog = parse_program(
        "       PROCEDURE DIVISION.\n"
        "       0000-X.\n" + proc_body)
    return prog.paragraphs[0].statements


def test_exec_sql_host_variables_captured():
    st = _stmts("           EXEC SQL\n"
                "               SELECT NAME INTO :WS-NAME FROM CUST WHERE ID = :WS-ID\n"
                "           END-EXEC.\n")[0]
    assert isinstance(st, ExecStmt) and st.lang == "SQL"
    assert ":WS-NAME" in st.host_vars and ":WS-ID" in st.host_vars


def test_exec_cics_link_is_call_and_xctl_is_transfer():
    link = _stmts("           EXEC CICS LINK PROGRAM('POSTLOG') END-EXEC.\n")[0]
    assert link.kind == "call" and link.target == "POSTLOG"
    xctl = _stmts("           EXEC CICS XCTL PROGRAM('NEXTPGM') END-EXEC.\n")[0]
    assert xctl.kind == "transfer" and xctl.target == "NEXTPGM"


def test_exec_cics_return_terminates():
    st = _stmts("           EXEC CICS RETURN END-EXEC.\n")[0]
    assert st.kind == "terminate"


def test_cics_program_flags_handle_and_xctl():
    src = (
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           EXEC CICS HANDLE CONDITION NOTFND(9000-NF) END-EXEC\n"
        "           EXEC CICS XCTL PROGRAM('OTHER') END-EXEC.\n"
        "       9000-NF.\n"
        "           EXEC CICS RETURN END-EXEC.\n"
    )
    machine = build_machine(parse_program(src))
    msgs = " ".join(f["message"] for f in machine.flags)
    assert "HANDLE" in msgs and "XCTL" in msgs
    # CICS HANDLE makes the machine parallel: PROGRAM flow + an orthogonal HANDLERS region.
    assert machine.config["type"] == "parallel"
    program = machine.config["states"]["PROGRAM"]["states"]
    assert any(s.get("type") == "final" for s in program.values())  # RETURN -> final
    # the HANDLE target is dispatched from the watcher.
    assert "CICS.NOTFND" in machine.config["states"]["HANDLERS"]["states"]["__WATCH__"]["on"]
