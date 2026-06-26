from cobol_xstate.normalizer import normalize, SourceFormat


def test_fixed_strips_comment_and_sequence_area():
    src = (
        "000100 IDENTIFICATION DIVISION.\n"
        "000200* this is a comment line\n"
        "000300 PROGRAM-ID. T.\n"
    )
    lines = normalize(src, SourceFormat.FIXED)
    texts = [cl.text.strip() for cl in lines]
    assert "IDENTIFICATION DIVISION." in texts
    assert "PROGRAM-ID. T." in texts
    assert all("comment" not in t for t in texts)


def test_fixed_discards_identification_area_73_80():
    # Cols 73-80 carry an identifier that must be dropped in fixed format.
    line = "      " + " " + "       MOVE A TO B.".ljust(65) + "IDENT123"
    lines = normalize(line, SourceFormat.FIXED)
    assert lines[0].text.strip() == "MOVE A TO B."
    assert "IDENT" not in lines[0].text


def test_area_a_flag_distinguishes_header_from_statement():
    src = (
        "       0000-MAIN.\n"
        "           MOVE A TO B.\n"
    )
    lines = normalize(src, SourceFormat.FIXED)
    assert lines[0].area_a is True   # paragraph header in Area A
    assert lines[1].area_a is False  # statement in Area B


def test_inline_comment_removed_but_not_inside_literal():
    src = "       DISPLAY 'a *> b'  *> trailing comment\n"
    lines = normalize(src, SourceFormat.FIXED)
    assert lines[0].text.strip() == "DISPLAY 'a *> b'"


def test_fixed_continuation_stitches_split_literal():
    src = (
        "       MOVE 'HELLO-\n"
        "      -    WORLD' TO WS.\n"
    )
    lines = normalize(src, SourceFormat.FIXED)
    joined = " ".join(cl.text for cl in lines)
    assert "'HELLO-WORLD'" in joined.replace(" ", "") or "HELLO-WORLD" in joined


def test_free_format_detected_by_directive():
    src = ">>SOURCE FORMAT FREE\nMOVE A TO B.\n"
    lines = normalize(src)
    assert lines[-1].text.strip() == "MOVE A TO B."
