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


def test_declaratives_split_out_with_use_trigger():
    prog = parse_program(_wrap(
        "       DECLARATIVES.\n"
        "       IO-ERR SECTION.\n"
        "           USE AFTER STANDARD ERROR PROCEDURE ON CUST-FILE.\n"
        "       IO-ERR-HANDLER.\n"
        "           ADD 1 TO WS-ERR.\n"
        "       END DECLARATIVES.\n"
        "       0000-MAIN.\n"
        "           STOP RUN.\n"
    ))
    # the USE section is NOT in the main flow
    assert [p.name for p in prog.paragraphs] == ["0000-MAIN"]
    decl_names = [p.name for p in prog.declaratives]
    assert "IO-ERR" in decl_names and "IO-ERR-HANDLER" in decl_names
    io_err = next(p for p in prog.declaratives if p.name == "IO-ERR")
    assert io_err.use_trigger == "ERROR"
    assert io_err.use_files == ["CUST-FILE"]
    # the USE statement itself is not executable and is dropped
    assert io_err.statements == []


def test_cics_handle_condition_pairs_captured():
    prog = parse_program(_wrap(
        "       0000-MAIN.\n"
        "           EXEC CICS HANDLE CONDITION NOTFND(9000-NF) END-EXEC\n"
        "           STOP RUN.\n"
        "       9000-NF.\n"
        "           DISPLAY 'NF'.\n"
    ))
    assert prog.cics_handlers == [("NOTFND", "9000-NF")]


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


def _para(prog, name):
    return next(p for p in prog.paragraphs if p.name == name)


def test_string_without_end_string_does_not_swallow_following_statements():
    # A STRING with no END-STRING terminator must end at the next statement verb,
    # not consume the rest of the paragraph (one-period-per-paragraph style).
    from cobol_xstate.model import walk_statements, Action, IfStmt
    prog = parse_program(_wrap(
        "       5000-PROCESS.\n"
        "           STRING WS-A DELIMITED BY SIZE\n"
        "                  WS-B DELIMITED BY SIZE\n"
        "               INTO WS-OUT\n"
        "           MOVE 1 TO WS-FLAG\n"
        "           IF WS-FLAG = 1\n"
        "               PERFORM 6000-NEXT\n"
        "           END-IF\n"
        "           IF WS-A > 0\n"
        "               MOVE 2 TO WS-FLAG\n"
        "           END-IF.\n"
    ))
    stmts = _para(prog, "5000-PROCESS").statements
    ifs = [s for s in walk_statements(stmts) if isinstance(s, IfStmt)]
    assert len(ifs) == 2, "the two IFs after the STRING must survive as real control flow"
    # The STRING itself is still captured as an opaque action, but only its own text.
    string_actions = [s for s in walk_statements(stmts)
                      if isinstance(s, Action) and s.verb == "STRING"]
    assert len(string_actions) == 1
    assert "PERFORM" not in string_actions[0].text  # did not swallow the paragraph


def test_string_with_end_string_and_overflow_keeps_its_imperative():
    # With an explicit END-STRING, the ON OVERFLOW imperative (which contains verbs)
    # belongs to the STRING and must not prematurely terminate the opaque scope.
    from cobol_xstate.model import walk_statements, Action, IfStmt
    prog = parse_program(_wrap(
        "       5000-PROCESS.\n"
        "           STRING WS-A DELIMITED BY SIZE INTO WS-OUT\n"
        "               ON OVERFLOW\n"
        "                   MOVE 1 TO WS-ERR\n"
        "                   PERFORM 9000-ERR\n"
        "           END-STRING\n"
        "           MOVE 5 TO WS-DONE\n"
        "           IF WS-DONE = 5\n"
        "               PERFORM 6000-NEXT\n"
        "           END-IF.\n"
    ))
    stmts = _para(prog, "5000-PROCESS").statements
    string_actions = [s for s in walk_statements(stmts)
                      if isinstance(s, Action) and s.verb == "STRING"]
    assert len(string_actions) == 1
    # The overflow imperative stayed inside the STRING opaque text.
    assert "OVERFLOW" in string_actions[0].text
    assert "END-STRING" in string_actions[0].text
    # And the statement AFTER END-STRING is still its own control flow.
    ifs = [s for s in walk_statements(stmts) if isinstance(s, IfStmt)]
    assert len(ifs) == 1


def test_string_without_end_string_inside_if_does_not_eat_end_if():
    from cobol_xstate.model import walk_statements, IfStmt, Action
    prog = parse_program(_wrap(
        "       5000-PROCESS.\n"
        "           IF WS-A > 0\n"
        "               STRING WS-A DELIMITED BY SIZE INTO WS-OUT\n"
        "               MOVE 1 TO WS-FLAG\n"
        "           END-IF\n"
        "           PERFORM 6000-NEXT.\n"
    ))
    stmts = _para(prog, "5000-PROCESS").statements
    ifs = [s for s in walk_statements(stmts) if isinstance(s, IfStmt)]
    assert len(ifs) == 1
    # The MOVE lives inside the IF then-body, and the STRING did not eat END-IF.
    string_actions = [s for s in walk_statements(stmts)
                      if isinstance(s, Action) and s.verb == "STRING"]
    assert len(string_actions) == 1
    assert "MOVE" not in string_actions[0].text
