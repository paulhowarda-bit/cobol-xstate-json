"""A program that READs one file at many sites, for the field-list interning tests in
test_interface, test_business and test_reactive.

Each READ (no INTO) carries every leaf of every record the FD declares, so the list
was written again at every site - in the interface overlay, and in the business view's
boundary actions and state meta - until each distinct list became one `fieldSets`
entry."""

from cobol_xstate.parser import parse_program
from cobol_xstate.statechart import build_machine

#: What every READ of IN-FILE carries: both records and their leaves, in order.
READ_FIELDS = ["IN-A", "IN-A-KEY", "IN-A-AMT", "IN-B", "IN-B-KEY", "IN-B-NAME"]

#: Each of those fields' picture (a group has none: its category stands in).
READ_PICS = {"IN-A": "group", "IN-A-KEY": "X(4)", "IN-A-AMT": "9(5)",
             "IN-B": "group", "IN-B-KEY": "X(4)", "IN-B-NAME": "X(20)"}


def many_reads(sites: int):
    """The machine of a program whose ``sites`` paragraphs each READ IN-FILE (two
    records) and WRITE OUT-LINE."""
    paras = "".join(f"       P{i}.\n           READ IN-FILE\n           WRITE OUT-LINE.\n"
                    for i in range(sites))
    return build_machine(parse_program(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. MANYREAD.\n"
        "       ENVIRONMENT DIVISION.\n"
        "       INPUT-OUTPUT SECTION.\n"
        "       FILE-CONTROL.\n"
        "           SELECT IN-FILE ASSIGN TO INDD.\n"
        "           SELECT OUT-FILE ASSIGN TO OUTDD.\n"
        "       DATA DIVISION.\n"
        "       FILE SECTION.\n"
        "       FD IN-FILE.\n"
        "       01 IN-A.\n"
        "          05 IN-A-KEY PIC X(4).\n"
        "          05 IN-A-AMT PIC 9(5).\n"
        "       01 IN-B.\n"
        "          05 IN-B-KEY PIC X(4).\n"
        "          05 IN-B-NAME PIC X(20).\n"
        "       FD OUT-FILE.\n"
        "       01 OUT-LINE.\n"
        "          05 OUT-KEY PIC X(4).\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        "           OPEN INPUT IN-FILE OUTPUT OUT-FILE\n"
        + "".join(f"           PERFORM P{i}\n" for i in range(sites)) +
        "           STOP RUN.\n" + paras), source_name="manyread.cbl")
