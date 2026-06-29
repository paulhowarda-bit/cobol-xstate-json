from cobol_xstate.parser import parse_program
from cobol_xstate.model import (
    EvaluateStmt,
    GoToStmt,
    IfStmt,
    IoStmt,
    PerformStmt,
    SortStmt,
    TerminateStmt,
    AlterStmt,
    CallStmt,
)


def _wrap(proc_body: str) -> str:
    return (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. T.\n"
        "       PROCEDURE DIVISION.\n" + proc_body
    )


def test_program_id_and_paragraphs():
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           PERFORM 1000-A\n"
        "           STOP RUN.\n"
        "       1000-A.\n"
        "           DISPLAY 'HI'.\n"
    ))
    assert prog.program_id == "T"
    assert [p.name for p in prog.paragraphs] == ["0000-MAIN", "1000-A"]


def test_sort_captures_input_and_output_procedures():
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           SORT SORT-FILE\n"
        "               ON ASCENDING KEY S-KEY\n"
        "               INPUT PROCEDURE IS 1000-FILL\n"
        "               OUTPUT PROCEDURE IS 2000-EMIT\n"
        "           STOP RUN.\n"
    ))
    st = prog.paragraphs[0].statements[0]
    assert isinstance(st, SortStmt)
    assert st.verb == "SORT" and st.file == "SORT-FILE"
    assert st.input_proc == "1000-FILL"
    assert st.output_proc == "2000-EMIT"
    assert st.using == [] and st.giving == []


def test_sort_using_giving_files():
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           SORT WORK-FILE\n"
        "               USING IN-FILE\n"
        "               GIVING OUT-FILE\n"
        "           STOP RUN.\n"
    ))
    st = prog.paragraphs[0].statements[0]
    assert isinstance(st, SortStmt)
    assert st.input_proc is None and st.output_proc is None
    assert st.using == ["IN-FILE"] and st.giving == ["OUT-FILE"]


def test_perform_until_captures_target_and_control():
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           PERFORM 2000-P UNTIL WS-EOF = 'Y'\n"
        "           STOP RUN.\n"
    ))
    main = prog.paragraphs[0]
    perform = main.statements[0]
    assert isinstance(perform, PerformStmt)
    assert perform.kind == "until"
    assert perform.target == "2000-P"
    assert "UNTIL" in perform.control_text.upper()
    assert isinstance(main.statements[1], TerminateStmt)


def test_if_then_else_with_goto():
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           IF WS-X = 1\n"
        "               GO TO 9000-Z\n"
        "           ELSE\n"
        "               MOVE 1 TO WS-Y\n"
        "           END-IF.\n"
    ))
    stmt = prog.paragraphs[0].statements[0]
    assert isinstance(stmt, IfStmt)
    assert "WS-X" in stmt.cond_text
    assert isinstance(stmt.then_body[0], GoToStmt)
    assert stmt.then_body[0].targets == ["9000-Z"]
    assert stmt.else_body[0].__class__.__name__ == "Action"


def test_evaluate_dispatch_with_when_other():
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EVALUATE WS-T\n"
        "               WHEN 'D' PERFORM 100-D\n"
        "               WHEN 'W' PERFORM 200-W\n"
        "               WHEN OTHER PERFORM 900-E\n"
        "           END-EVALUATE.\n"
    ))
    ev = prog.paragraphs[0].statements[0]
    assert isinstance(ev, EvaluateStmt)
    assert len(ev.whens) == 2
    assert ev.other_body is not None
    assert isinstance(ev.whens[0][1][0], PerformStmt)


def test_read_at_end_handler():
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           READ CUST-FILE\n"
        "               AT END MOVE 'Y' TO WS-EOF\n"
        "           END-READ.\n"
    ))
    io = prog.paragraphs[0].statements[0]
    assert isinstance(io, IoStmt)
    assert io.verb == "READ"
    assert io.file == "CUST-FILE"
    assert "AT_END" in io.handlers


def test_alter_and_dynamic_call_recovered():
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           CALL WS-PGM USING WS-A\n"
        "           ALTER 100-X TO PROCEED TO 200-Y.\n"
    ))
    stmts = prog.paragraphs[0].statements
    assert isinstance(stmts[0], CallStmt)
    assert stmts[0].dynamic is True
    assert isinstance(stmts[1], AlterStmt)
    assert stmts[1].pairs == [("100-X", "200-Y")]


def test_alter_stops_at_following_goto_without_period():
    # ALTER and a following GO TO share a sentence (no period between them).
    prog = parse_program(_wrap(
        "       1000-SWITCH.\n"
        "           GO TO 1100-FIRST.\n"
        "       1100-FIRST.\n"
        "           ALTER 1000-SWITCH TO PROCEED TO 1200-NORMAL\n"
        "           GO TO 1900-DONE.\n"
    ))
    first = prog.paragraphs[1]
    alter, goto = first.statements[0], first.statements[1]
    assert isinstance(alter, AlterStmt)
    assert alter.pairs == [("1000-SWITCH", "1200-NORMAL")]
    assert isinstance(goto, GoToStmt)
    assert goto.targets == ["1900-DONE"]


def test_static_call_is_not_dynamic():
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           CALL 'SUBPGM' USING WS-A.\n"
    ))
    call = prog.paragraphs[0].statements[0]
    assert isinstance(call, CallStmt)
    assert call.dynamic is False
    assert call.target == "SUBPGM"


def test_no_procedure_division():
    prog = parse_program(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. T.\n"
    )
    assert prog.has_procedure_division is False
    assert prog.paragraphs == []
