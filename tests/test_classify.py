"""Classifying every called program by code analysis: a program CONTAINED in this source
(nested PROGRAM-ID) is internal, a standard IBM subsystem entry point is runtime, and
anything not positively identified stays honestly `unresolved` - never guessed. The fetch
stage may then resolve an `unresolved` target to cobol / assembler source.

Covers classify.py in isolation and end-to-end (parser -> machine -> artifacts -> fetch)."""

from cobol_xstate.artifacts import build_artifacts
from cobol_xstate.classify import (
    CATEGORY_COBOL, CATEGORY_IBM, CATEGORY_INTERNAL, CATEGORY_UNRESOLVED,
    classify_call_target)
from cobol_xstate.fetch import fetch_dependencies
from cobol_xstate.parser import parse_program
from cobol_xstate.statechart import build_machine


# --------------------------------------------------------------------------- #
# the classifier in isolation
# --------------------------------------------------------------------------- #

def test_contained_program_is_internal():
    r = classify_call_target("USEMQ", internal_programs=["USEMQ"])
    assert r["category"] == CATEGORY_INTERNAL


def test_mqi_verb_is_ibm_mq():
    r = classify_call_target("MQPUT")
    assert r["category"] == CATEGORY_IBM and r["subsystem"] == "ibm-mq"


def test_mq_copybook_corroboration_is_noted():
    r = classify_call_target("MQGET", copybooks=[{"member": "CMQODV"}])
    assert r["subsystem"] == "ibm-mq" and "copybook" in r["reason"].lower()


def test_db2_module_needs_sql_context():
    assert classify_call_target("DSNTIAC", uses_sql=True)["subsystem"] == "ibm-db2"
    # Without EXEC SQL context the same name is NOT positively Db2 - honest, not guessed.
    assert classify_call_target("DSNTIAC", uses_sql=False)["category"] == CATEGORY_UNRESOLVED


def test_le_service_is_ibm_le():
    assert classify_call_target("CEESITST")["subsystem"] == "ibm-le"


def test_unknown_name_stays_unresolved():
    # A site abend-handler utility with no source and no recognised API: not force-fit into
    # an invented label - it stays `unresolved` until the fetch stage (or a human) resolves it.
    assert classify_call_target("ABENDL")["category"] == CATEGORY_UNRESOLVED


def test_internal_wins_over_a_colliding_api_name():
    # A contained program that happens to share an MQI verb's name is still internal.
    assert classify_call_target(
        "MQPUT", internal_programs=["MQPUT"])["category"] == CATEGORY_INTERNAL


# --------------------------------------------------------------------------- #
# end to end: nested detection, no pollution, classification in the manifest
# --------------------------------------------------------------------------- #

NESTED = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. OUTER.\n"
    "       DATA DIVISION.\n"
    "       WORKING-STORAGE SECTION.\n"
    "       01  WS-CODE PIC 9(4) VALUE 0.\n"
    "       PROCEDURE DIVISION.\n"
    "       0000-MAIN.\n"
    "           CALL 'INNER' USING WS-CODE\n"
    "           CALL 'ABENDL' USING WS-CODE\n"
    "           GOBACK.\n"
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. INNER.\n"
    "       DATA DIVISION.\n"
    "       LINKAGE SECTION.\n"
    "       01  LK-CODE PIC 9(4).\n"
    "       PROCEDURE DIVISION USING LK-CODE.\n"
    "       0000-INNER.\n"
    "           MOVE 1 TO LK-CODE\n"
    "           GOBACK.\n"
    "       END PROGRAM INNER.\n"
    "       END PROGRAM OUTER.\n"
)


def _machine(src):
    return build_machine(parse_program(src))


def test_parser_records_contained_programs():
    prog = parse_program(NESTED)
    assert prog.program_id == "OUTER"
    assert prog.nested_programs == ["INNER"]


def test_nested_body_does_not_pollute_the_outer_program():
    prog = parse_program(NESTED)
    para_names = {p.name for p in prog.paragraphs}
    # Neither INNER's paragraph nor a phantom "PROGRAM-ID" paragraph may leak into OUTER.
    assert para_names == {"0000-MAIN"}
    # OUTER's data is its own - INNER's LINKAGE item must not have folded in.
    assert "LK-CODE" not in {k.upper() for k in prog.data_by_name}


def test_manifest_classifies_contained_vs_unresolved():
    man = build_artifacts(_machine(NESTED))
    progs = {r["artifact"]: r for r in man["artifacts"] if r["kind"] == "program"}
    assert progs["INNER"]["classification"] == CATEGORY_INTERNAL
    assert progs["INNER"]["identity"] == "internal"
    assert "needs" not in progs["INNER"]           # nothing to chase - it is contained here
    assert progs["ABENDL"]["classification"] == CATEGORY_UNRESOLVED


DB2 = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. D.\n"
    "       DATA DIVISION.\n"
    "       WORKING-STORAGE SECTION.\n"
    "       01  W-TEXT PIC X(120).\n"
    "       PROCEDURE DIVISION.\n"
    "       0000-MAIN.\n"
    "           EXEC SQL SELECT C1 INTO :W-TEXT FROM T1 END-EXEC\n"
    "           CALL 'DSNTIAC' USING SQLCA W-TEXT\n"
    "           GOBACK.\n"
)


def test_manifest_classifies_db2_module_with_sql_context():
    man = build_artifacts(_machine(DB2))
    row = next(r for r in man["artifacts"] if r["artifact"] == "DSNTIAC")
    assert row["classification"] == CATEGORY_IBM and row["subsystem"] == "ibm-db2"


# --------------------------------------------------------------------------- #
# fetch: skip the recognized categories, keep unresolved, refine to real source
# --------------------------------------------------------------------------- #

def _driver(body):
    return build_artifacts(_machine(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. DRIVER.\n"
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n" + body +
        "           GOBACK.\n"))


def test_fetch_skips_ibm_runtime_and_refines_unresolved_to_cobol():
    man = _driver("           CALL 'MQPUT'\n           CALL 'REALSUB'\n")
    real = ("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. REALSUB.\n"
            "       PROCEDURE DIVISION.\n       0000-MAIN.\n"
            "           DISPLAY 'HI'\n           GOBACK.\n")

    def fetch(name, type=None):
        if name.upper() == "REALSUB":
            return {"artifact_name": name, "found": True, "text": real,
                    "detected_type": "cobol", "source_path": "x"}
        return {"artifact_name": name, "found": False}

    rows = {r["artifact"]: r for r in fetch_dependencies(man, fetch)["artifacts"]}
    # A recognised IBM runtime API has no application source: it is never even requested.
    assert rows["MQPUT"]["status"] == "skipped"
    assert rows["MQPUT"]["classification"] == CATEGORY_IBM
    # An unresolved target the estate DOES hold is refined by the language that retrieved it.
    assert rows["REALSUB"]["status"] == "fetched"
    assert rows["REALSUB"]["classification"] == CATEGORY_COBOL


def test_fetch_keeps_unresolved_when_estate_has_nothing():
    man = _driver("           CALL 'ABENDL'\n")

    def fetch(name, type=None):
        return {"artifact_name": name, "found": False}

    row = next(r for r in fetch_dependencies(man, fetch)["artifacts"]
               if r["artifact"] == "ABENDL")
    assert row["status"] == "not-found"
    assert row["classification"] == CATEGORY_UNRESOLVED   # honest, not guessed


# --------------------------------------------------------------------------- #
# regression: multiple PROGRAM-IDs that are NOT well-formed nested programs
# (concatenated separate units, or a unit missing its END PROGRAM) must not have
# a whole unit's body - and its CALLs - silently dropped from the manifest.
# --------------------------------------------------------------------------- #

# Two separate compilation units in one member, with NO END PROGRAM. These are not
# nested programs; each CALLs a different external subprogram.
TWO_UNITS = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. PROGA.\n"
    "       DATA DIVISION.\n"
    "       WORKING-STORAGE SECTION.\n"
    "       01  WS PIC 9(4) VALUE 0.\n"
    "       PROCEDURE DIVISION.\n"
    "       0000-A.\n"
    "           CALL 'EXTX' USING WS\n"
    "           GOBACK.\n"
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. PROGB.\n"
    "       DATA DIVISION.\n"
    "       WORKING-STORAGE SECTION.\n"
    "       01  WS PIC 9(4) VALUE 0.\n"
    "       PROCEDURE DIVISION.\n"
    "       0000-B.\n"
    "           CALL 'EXTY' USING WS\n"
    "           GOBACK.\n"
)


def test_unbalanced_program_units_do_not_drop_a_units_calls():
    """Splitting on PROGRAM-ID count treated the 2nd unit as 'contained' and dropped its
    body when END PROGRAM was absent, deleting its CALL from the dependency manifest. The
    unit-splitter must fall back to one program when nesting is not well-formed."""
    prog = parse_program(TWO_UNITS)
    assert prog.nested_programs == []          # not mistaken for nesting; nothing stripped
    called = {r["artifact"] for r in build_artifacts(_machine(TWO_UNITS))["artifacts"]
              if r["kind"] == "program"}
    assert {"EXTX", "EXTY"} <= called, f"a unit's CALL was dropped: {called}"
