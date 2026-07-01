from cobol_xstate.normalizer import (
    normalize,
    SourceFormat,
    detect_source_format,
)


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


def test_spaced_source_format_directive_wins_over_column_heuristic():
    # `>>SOURCE FORMAT FREE` (the standard IBM spaced form) must be honored even when
    # the body is indented enough that the column heuristic would vote FIXED. If the
    # directive were missed, fixed-format normalization would chop cols 1-7 off every
    # line and corrupt the whole program downstream.
    src = (
        ">>SOURCE FORMAT FREE\n"
        "        MOVE WS-A TO WS-B.\n"
        "        ADD 1 TO WS-A.\n"
    )
    lines = normalize(src)
    texts = [cl.text.strip() for cl in lines]
    assert "MOVE WS-A TO WS-B." in texts
    assert "ADD 1 TO WS-A." in texts


def test_fixed_format_directive_spaced_form():
    src = ">>SOURCE FORMAT IS FIXED\n000100 PROGRAM-ID. T.\n"
    lines = normalize(src)
    assert any(cl.text.strip() == "PROGRAM-ID. T." for cl in lines)


def test_free_format_indented_header_is_area_a_candidate():
    # Free format has no Area A column rule; an indented paragraph header must still
    # be flagged as a header candidate so the parser can find it.
    src = ">>SOURCE FORMAT FREE\n    1000-MAIN.\n        MOVE A TO B.\n"
    lines = normalize(src)
    header = next(cl for cl in lines if cl.text.strip() == "1000-MAIN.")
    assert header.area_a is True


# --- source-format detection: layered, with confidence ---------------------- #

def test_detect_directive_is_authoritative_and_certain():
    det = detect_source_format(">>SOURCE FORMAT FREE\nMOVE A TO B.\n")
    assert det.format is SourceFormat.FREE
    assert det.confidence == 1.0


def test_detect_numbered_fixed_source_is_confident():
    src = (
        "000100 IDENTIFICATION DIVISION.\n"
        "000200 PROGRAM-ID. T.\n"
        "000300 PROCEDURE DIVISION.\n"
    )
    det = detect_source_format(src)
    assert det.format is SourceFormat.FIXED
    assert det.is_confident


def test_detect_left_margin_code_is_free_and_confident():
    src = (
        "IDENTIFICATION DIVISION.\n"
        "PROGRAM-ID. T.\n"
        "PROCEDURE DIVISION.\n"
        "MOVE A TO B.\n"
    )
    det = detect_source_format(src)
    assert det.format is SourceFormat.FREE
    assert det.is_confident


def test_detect_content_past_column_80_is_free():
    src = "        MOVE SOURCE-FIELD TO A-TARGET-FIELD-WHOSE-NAME-RUNS-WELL-PAST-COLUMN-EIGHTY.\n"
    assert len(src.rstrip()) > 80
    det = detect_source_format(src)
    assert det.format is SourceFormat.FREE
    assert det.is_confident


def test_detect_ambiguous_layout_is_low_confidence_but_lossless():
    # Code indented to col 8, no directive/sequence numbers/long lines: byte-identical
    # to blank-sequence fixed. We cannot know - so flag it LOW confidence rather than
    # pretend. Crucially, defaulting to fixed is still lossless for this layout.
    src = (
        "        IDENTIFICATION DIVISION.\n"
        "        PROGRAM-ID. T.\n"
        "        PROCEDURE DIVISION.\n"
        "        MOVE A TO B.\n"
    )
    det = detect_source_format(src)
    assert not det.is_confident            # caller should warn / recommend --format
    texts = [cl.text.strip() for cl in normalize(src)]
    assert "IDENTIFICATION DIVISION." in texts
    assert "PROCEDURE DIVISION." in texts  # anchors survive whichever way we defaulted


def test_detect_shape_check_recovers_free_when_fixed_would_mangle():
    # A free program whose code sits in the indicator/sequence area is unreadable as
    # fixed (cols 1-7 get sliced off). Detection must recover FREE.
    src = (
        "IDENTIFICATION DIVISION.\n"
        "PROGRAM-ID. FOO.\n"
        "PROCEDURE DIVISION.\n"
        "    DISPLAY 'HI'.\n"
        "    STOP RUN.\n"
    )
    det = detect_source_format(src)
    assert det.format is SourceFormat.FREE
    # And normalization keeps the program intact.
    texts = [cl.text.strip() for cl in normalize(src)]
    assert "PROGRAM-ID. FOO." in texts
