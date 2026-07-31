"""Record byte layout - and, more importantly, when it refuses to state one.

A field offset is the last mile of "go read PROD.PARM.CNTL": without it nobody can find
CTL-PGM-NAME in a flat dataset that has no column headers. But a WRONG offset is
indistinguishable from a right one to whoever reads the data - they find garbage and
blame the file. So roughly half of these tests are about the refusal, not the arithmetic.
"""

from cobol_xstate.parser import parse_program
from cobol_xstate.statechart import build_machine
from cobol_xstate.storage import field_position, item_size, pic_positions, record_layout


def _data(decls: str) -> dict:
    src = ("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. X.\n"
           "       DATA DIVISION.\n       WORKING-STORAGE SECTION.\n" + decls +
           "       PROCEDURE DIVISION.\n       0000-M.\n           GOBACK.\n")
    return build_machine(parse_program(src)).data


PLAIN = """\
       01  CTL-REC.
           05  CTL-KEY       PIC X(4).
           05  CTL-PGM-NAME  PIC X(8).
           05  CTL-AMT       PIC S9(7)V99 COMP-3.
           05  CTL-CNT       PIC S9(4) COMP.
           05  CTL-REST      PIC X(59).
"""


# --------------------------------------------------------------------------- #
# the arithmetic
# --------------------------------------------------------------------------- #

def test_picture_positions_ignore_the_symbols_that_occupy_no_byte():
    assert pic_positions("X(8)") == 8
    assert pic_positions("9(5)V99") == 7        # V is an IMPLIED point - no byte
    assert pic_positions("S9(4)") == 4          # sign is in the zone, not a byte
    assert pic_positions("ZZ,ZZ9.99") == 9      # edited: every symbol is a position


def test_usage_decides_the_size_not_the_digit_count():
    """The same PIC S9(7)V99 is 9 bytes zoned, 5 packed, 8 binary. Getting this wrong is
    how a layout silently drifts by a few bytes per field."""
    def sized(pic, usage=None):
        decls = f"       01  T PIC {pic}{(' ' + usage) if usage else ''}.\n"
        return item_size(_data(decls)["T"])[0]

    assert sized("S9(7)V99") == 9               # DISPLAY: one byte per digit
    assert sized("S9(7)V99", "COMP-3") == 5     # packed: 9 digits + sign nibble
    assert sized("S9(4)", "COMP") == 2          # binary halfword
    assert sized("S9(9)", "COMP") == 4          # binary fullword
    assert sized("S9(18)", "COMP") == 8         # binary doubleword
    assert sized("X(8)") == 8


def test_sign_separate_adds_a_byte_and_stays_provable():
    """SIGN IS SEPARATE is an exact, knowable +1 - so unlike SYNC it does not stop us
    stating a position."""
    data = _data("       01  T PIC S9(4) SIGN LEADING SEPARATE.\n")
    assert item_size(data["T"])[0] == 5


def test_offsets_are_one_based_and_the_record_size_is_the_sum():
    layout = record_layout(_data(PLAIN), "CTL-REC")
    assert layout["provable"] is True
    assert layout["size"] == 78                 # 4 + 8 + 5 + 2 + 59
    at = {f["name"]: (f["offset"], f["length"]) for f in layout["fields"]}
    assert at["CTL-KEY"] == (1, 4)
    assert at["CTL-PGM-NAME"] == (5, 8)         # counted the way a reader counts columns
    assert at["CTL-AMT"] == (13, 5)
    assert at["CTL-CNT"] == (18, 2)


def test_field_position_reports_the_field_within_its_own_record():
    pos = field_position(_data(PLAIN), "CTL-PGM-NAME")
    assert pos["record"] == "CTL-REC"
    assert pos["readAt"] == "bytes 5-12 of the 78-byte record"
    assert pos["recordLength"] == 78


def test_occurs_multiplies_the_group():
    data = _data("""\
       01  T-REC.
           05  T-HDR  PIC X(2).
           05  T-ROW OCCURS 3.
               10  T-A  PIC X(4).
               10  T-B  PIC 9(2).
           05  T-END  PIC X(1).
""")
    layout = record_layout(data, "T-REC")
    assert layout["provable"] is True
    assert layout["size"] == 2 + 3 * 6 + 1


# --------------------------------------------------------------------------- #
# the refusal - which is the point of the module
# --------------------------------------------------------------------------- #

def _withheld(decls, field="CTL-PGM-NAME"):
    pos = field_position(_data(decls), field)
    assert pos["provable"] is False
    assert "offset" not in pos, "an offset was stated under a blocker"
    return pos["reason"]


def test_occurs_depending_withholds_every_offset():
    """The table's length is a run-time value, so every field after it moves. Stating a
    position for CTL-PGM-NAME would be stating one of several possible answers."""
    reason = _withheld("""\
       01  CTL-REC.
           05  CTL-N         PIC 9(2).
           05  CTL-TAB OCCURS 1 TO 5 DEPENDING ON CTL-N PIC X(4).
           05  CTL-PGM-NAME  PIC X(8).
""")
    assert "OCCURS DEPENDING ON CTL-N" in reason
    assert "varies at run time" in reason


def test_synchronized_withholds_because_slack_bytes_are_not_knowable_here():
    """SYNC inserts alignment padding whose size depends on where the record itself
    starts. We capture the clause purely so we can decline."""
    reason = _withheld("""\
       01  CTL-REC.
           05  CTL-KEY       PIC X(4).
           05  CTL-BIN       PIC S9(4) COMP SYNC.
           05  CTL-PGM-NAME  PIC X(8).
""")
    assert "SYNCHRONIZED" in reason
    assert "slack bytes" in reason


def test_redefines_withholds_because_one_offset_cannot_describe_two_views():
    reason = _withheld("""\
       01  CTL-REC.
           05  CTL-KEY       PIC X(4).
           05  CTL-ALT REDEFINES CTL-KEY PIC 9(4).
           05  CTL-PGM-NAME  PIC X(8).
""")
    assert "REDEFINES CTL-KEY" in reason


def test_a_withheld_layout_still_lists_the_fields_in_order_with_pictures():
    """Refusing the arithmetic must not mean refusing the information: the ordered
    layout is still enough to count by hand, which is the whole reason to emit it."""
    layout = field_position(_data("""\
       01  CTL-REC.
           05  CTL-N         PIC 9(2).
           05  CTL-TAB OCCURS 1 TO 9 DEPENDING ON CTL-N PIC X(4).
           05  CTL-PGM-NAME  PIC X(8).
"""), "CTL-PGM-NAME")["layout"]
    assert [f["name"] for f in layout["fields"]] == ["CTL-N", "CTL-TAB", "CTL-PGM-NAME"]
    assert [f["pic"] for f in layout["fields"]] == ["9(2)", "X(4)", "X(8)"]
    assert all(f.get("length") for f in layout["fields"])   # sizes are still known
    assert "counted by hand" in layout["note"]


def test_no_offsets_are_left_behind_when_a_blocker_appears_late():
    """A blocker at the END of a record must still suppress the offsets computed before
    it - otherwise the output is a mix of trustworthy and untrustworthy numbers that a
    reader cannot tell apart."""
    layout = record_layout(_data("""\
       01  CTL-REC.
           05  CTL-KEY       PIC X(4).
           05  CTL-PGM-NAME  PIC X(8).
           05  CTL-N         PIC 9(2).
           05  CTL-TAB OCCURS 1 TO 4 DEPENDING ON CTL-N PIC X(4).
"""), "CTL-REC")
    assert layout["provable"] is False
    assert all("offset" not in f for f in layout["fields"])
