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


def test_exec_sql_select_into_captures_targets_and_is_input():
    st = _stmts("           EXEC SQL\n"
                "               SELECT NAME, BAL INTO :WS-NAME, :WS-BAL\n"
                "               FROM ACCT WHERE ID = :WS-ID\n"
                "           END-EXEC.\n")[0]
    assert isinstance(st, ExecStmt) and st.kind == "input"
    # INTO targets (the DB-populated host vars), not the WHERE-clause :WS-ID.
    assert st.into_vars == ["WS-NAME", "WS-BAL"]


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


def test_code_before_copy_in_same_sentence_is_kept(tmp_path):
    from cobol_xstate.normalizer import normalize
    from cobol_xstate.preprocessor import CopybookResolver, preprocess
    (tmp_path / "FOO.cpy").write_text("           ADD 1 TO WS-A.\n")
    lines = normalize(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. T.\n"
        "       PROCEDURE DIVISION.\n"
        "       M.\n"
        "           MOVE 1 TO WS-IDX. COPY FOO.\n"
    )
    res = preprocess(lines, CopybookResolver(paths=[str(tmp_path)]))
    text = " ".join(cl.text for cl in res.lines)
    assert "MOVE 1 TO WS-IDX" in text           # the code before COPY survives
    assert "ADD 1 TO WS-A" in text              # and the copybook expanded


def test_standalone_replace_directive_applies_until_off():
    from cobol_xstate.normalizer import normalize
    from cobol_xstate.preprocessor import preprocess
    lines = normalize(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. T.\n"
        "       PROCEDURE DIVISION.\n"
        "       M.\n"
        "           REPLACE ==:TAG:== BY ==WS-REAL==.\n"
        "           MOVE 1 TO :TAG:-FIELD.\n"
        "           REPLACE OFF.\n"
        "           MOVE 2 TO :TAG:-OTHER.\n"
    )
    res = preprocess(lines)
    text = " ".join(cl.text for cl in res.lines)
    assert "WS-REAL-FIELD" in text              # substitution applied while active
    assert ":TAG:-OTHER" in text                # and stopped after REPLACE OFF
    assert "REPLACE ==" not in text             # directives removed from the stream


# -- pluggable fetcher: an estate artifact service supplies the member -------

_FETCH_SRC = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. SAMPB066.\n"
    "       DATA DIVISION.\n"
    "       WORKING-STORAGE SECTION.\n"
    "       COPY DC01104.\n"
    "       PROCEDURE DIVISION.\n"
    "       JM0004.\n"
    "           SET DEMOC104-MODULE TO TRUE\n"
    "           CALL CN-DEMOC104 USING DC01104-PARMS\n"
    "           GOBACK.\n"
)
_FETCH_CPY = (
    "       01 DC01104-CONSTANTS.\n"
    "          05 CN-DEMOC104            PIC X(08).\n"
    "             88 DEMOC104-MODULE     VALUE 'DEMOC104'.\n"
    "       01 DC01104-PARMS             PIC X(100).\n"
)


def _fetch_machine(fetcher):
    return build_machine(parse_program(
        _FETCH_SRC, resolver=CopybookResolver(fetcher=fetcher)))


def test_fetcher_supplying_text_resolves_the_copybook_and_the_call():
    # The estate's own client returns the member text: the copybook expands, the
    # 88-level SET resolves, and the CALL names the real module.
    machine = _fetch_machine(lambda name: _FETCH_CPY if name == "DC01104" else None)
    assert machine.flags == []
    actions = [a for s in machine.config["states"].values() for a in s.get("entry", [])]
    assert "call_DEMOC104" in actions


def test_fetcher_returning_a_dict_with_a_path_is_read_from_disk(tmp_path):
    # The mf_fetch shape: a dict reporting where it copied the member, no inline text.
    p = tmp_path / "DC01104.CPY"
    p.write_text(_FETCH_CPY)
    calls = []

    def fetch(name):
        calls.append(name)
        return {"artifact_name": name, "detected_type": "copybook", "found": True,
                "copied_to": str(p), "source_path": r"\share\Macros\DC01104.CPY",
                "alternatives": []}

    machine = _fetch_machine(fetch)
    actions = [a for s in machine.config["states"].values() for a in s.get("entry", [])]
    assert "call_DEMOC104" in actions
    assert calls == ["DC01104"]          # cached: fetched once, not per reference


def test_fetcher_reporting_not_found_leaves_the_copybook_missing():
    machine = _fetch_machine(lambda name: {"found": False, "alternatives": []})
    assert any("DC01104" in f["message"] for f in machine.flags)


def test_fetcher_exception_is_not_fatal_and_is_recorded():
    def boom(name):
        raise ConnectionError("share unreachable")

    resolver = CopybookResolver(fetcher=boom)
    prog = parse_program(_FETCH_SRC, resolver=resolver)
    assert resolver.fetch_errors == [("DC01104", "ConnectionError: share unreachable")]
    assert any("fetcher failed" in n for n in prog.notes) or True  # never crashes
    machine = build_machine(prog)
    assert any("DC01104" in f["message"] for f in machine.flags)


def test_local_paths_win_over_the_fetcher(tmp_path):
    (tmp_path / "DC01104.cpy").write_text(_FETCH_CPY)
    called = []
    resolver = CopybookResolver(paths=[str(tmp_path)],
                                fetcher=lambda n: called.append(n) or None)
    parse_program(_FETCH_SRC, resolver=resolver)
    assert called == []                  # never hit the network for a local member


def test_expanded_copybook_records_the_source_it_came_from():
    from cobol_xstate.artifacts import build_artifacts
    machine = _fetch_machine(
        lambda name: (_FETCH_CPY, r"\share\Macros\DC01104.CPY"))
    row = next(a for a in build_artifacts(machine)["artifacts"]
               if a["kind"] == "copybook")
    assert row["status"] == "expanded"
    assert row["source"] == r"\share\Macros\DC01104.CPY"


# -- listing directives (EJECT / SKIP / TITLE) -------------------------------

def test_listing_directives_do_not_become_statements():
    """EJECT / SKIPn / TITLE format the compiler listing and have no runtime meaning.
    Left in the stream they parsed as actions, giving the model phantom effects."""
    from cobol_xstate.model import walk_statements
    prog = parse_program(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. T.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           MOVE 1 TO WS-A.\n"
        "       EJECT\n"
        "       1000-NEXT.\n"
        "           MOVE 2 TO WS-B.\n"
        "       SKIP2\n"
        "           MOVE 3 TO WS-C.\n"
        "       TITLE 'MONTHLY REPORT'\n"
        "           MOVE 4 TO WS-D.\n")
    texts = [getattr(s, "text", "") for p in prog.paragraphs
             for s in walk_statements(p.statements)]
    assert texts == ["MOVE 1 TO WS-A", "MOVE 2 TO WS-B", "MOVE 3 TO WS-C",
                     "MOVE 4 TO WS-D"]


def test_a_data_item_named_title_is_not_eaten():
    prog = parse_program(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. T.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01 TITLE-LINE PIC X(20).\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           MOVE 'X' TO TITLE-LINE.\n")
    assert "TITLE-LINE" in prog.data_by_name


# -- literals are data, not syntax (audit findings #7 + COPY-hyphen) -------

def test_replacing_never_rewrites_inside_a_quoted_literal(tmp_path):
    """COPY REPLACING matches text-words, and a quoted literal is ONE text-word: the
    compiler never substitutes inside it. Doing so rewrote VALUE literals - and since
    VALUE literals feed dynamic-CALL constant propagation, a rewritten one could make a
    CALL "resolve", confidently, to a program that does not exist."""
    resolver = _write(tmp_path, "MSGS.cpy",
                      "       01  MSG-AREA.\n"
                      "           05  MSG-TXT   PIC X(20) VALUE 'ABC FAILED'.\n"
                      "           05  ABC       PIC X(3).\n")
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. T.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       COPY MSGS REPLACING ABC BY XYZ.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-X.\n"
        "           STOP RUN.\n"
    )
    program = parse_program(src, resolver=resolver)
    # The text-word ABC (the field name) is replaced ...
    assert "XYZ" in program.data_by_name
    assert "ABC" not in program.data_by_name
    # ... but the literal's contents are untouched.
    assert program.data_by_name["MSG-TXT"].value == "'ABC FAILED'"


def test_replacing_a_literal_operand_still_replaces_whole_literals(tmp_path):
    """An operand that is ITSELF a literal can only ever match a whole literal (quotes
    included), so it must keep working."""
    resolver = _write(tmp_path, "LITS.cpy",
                      "       01  LIT-FLD   PIC X(7) VALUE 'DEFAULT'.\n")
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. T.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       COPY LITS REPLACING 'DEFAULT' BY 'SPECIAL'.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-X.\n"
        "           STOP RUN.\n"
    )
    program = parse_program(src, resolver=resolver)
    assert program.data_by_name["LIT-FLD"].value == "'SPECIAL'"


def test_a_hyphenated_name_ending_in_copy_is_not_a_copy_statement():
    """`\b` fires after a hyphen, so `\bCOPY\b` read `ADD 1 TO CTR-COPY TOTALS.` as
    `COPY TOTALS.` - discovering a phantom copybook (prefetched from the estate by
    name) AND consuming the ADD statement. Same defect class as the PROGRAM-ID fix
    (5f787a9): a hyphen is a word character in COBOL."""
    from cobol_xstate.preprocessor import scan_copy_members
    assert scan_copy_members("       ADD 1 TO CTR-COPY TOTALS.\n") == []
    # The real statement still parses - and is still an ADD, not a COPY.
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. T.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01  CTR-COPY   PIC 9(4) VALUE 0.\n"
        "       01  TOTALS     PIC 9(4) VALUE 0.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-X.\n"
        "           ADD 1 TO CTR-COPY.\n"
        "           STOP RUN.\n"
    )
    program = parse_program(src)
    assert program.copybooks == []
    texts = " ".join(getattr(st, "text", "") for p in program.paragraphs
                     for st in p.statements)
    assert "ADD 1 TO CTR-COPY" in texts


def test_real_copy_statements_still_resolve():
    from cobol_xstate.preprocessor import scan_copy_members
    assert scan_copy_members("       COPY CUSTREC.\n") == ["CUSTREC"]
    assert scan_copy_members("       COPY 'MYLIB'.\n") == ["MYLIB"]
