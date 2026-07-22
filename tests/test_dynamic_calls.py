"""True dynamic calls: which artifact supplies the target's name, and how it gets here.

The claim under test is narrow and worth stating plainly: this view never names the
target. It names the ARTIFACT the target's name is read from, and the route from that
artifact to the CALL. A test that asserted a resolved program name here would be
asserting a fiction - the control file's contents are run-time data.
"""

import json

from cobol_xstate.artifacts import build_artifacts
from cobol_xstate.dynamic_calls import annotate_artifacts, build_dynamic_calls
from cobol_xstate.fetch import build_fetch_plan, fetch_dependencies
from cobol_xstate.jcl import parse_jcl
from cobol_xstate.jcl_views import bind_cobol_artifacts
from cobol_xstate.parser import parse_program
from cobol_xstate.preprocessor import CopybookResolver
from cobol_xstate.statechart import build_machine

# A dispatcher: the program it calls is written in a control file it reads.
FILE_FED = """\
       IDENTIFICATION DIVISION.
       PROGRAM-ID. DISPATCH.
       ENVIRONMENT DIVISION.
       INPUT-OUTPUT SECTION.
       FILE-CONTROL.
           SELECT CTL-FILE ASSIGN TO CTLDD.
       DATA DIVISION.
       FILE SECTION.
       FD  CTL-FILE.
       01  CTL-REC.
           05  CTL-PGM-NAME  PIC X(8).
           05  CTL-REST      PIC X(72).
       WORKING-STORAGE SECTION.
       01  WS-HOLD    PIC X(8).
       01  WS-SUBPGM  PIC X(8).
       PROCEDURE DIVISION.
       0000-MAIN.
           READ CTL-FILE AT END CONTINUE END-READ
           MOVE CTL-PGM-NAME TO WS-HOLD
           MOVE WS-HOLD TO WS-SUBPGM
           CALL WS-SUBPGM
           GOBACK.
"""

JCL = """\
//DISPJOB  JOB (ACCT)
//S1       EXEC PGM=DISPATCH
//CTLDD    DD DSN=PROD.PARM.CNTL,DISP=SHR
"""


def _machine(src, resolver=None):
    return build_machine(parse_program(src, resolver=resolver), source_name="T.cbl")


def _view(src, jcl=None, resolver=None):
    m = _machine(src, resolver)
    art = build_artifacts(m)
    if jcl:
        art = bind_cobol_artifacts(art, [parse_jcl(jcl, source_name="J.jcl")])
    return m, art, build_dynamic_calls(m, art)


def _only(view):
    assert len(view["dynamicCalls"]) == 1, view["dynamicCalls"]
    return view["dynamicCalls"][0]


# --------------------------------------------------------------------------- #
# the answer: the artifact, and the route from it to the call
# --------------------------------------------------------------------------- #

def test_the_source_artifact_and_the_route_to_the_call_are_named():
    _m, _art, view = _view(FILE_FED)
    row = _only(view)
    assert row["item"] == "WS-SUBPGM"
    assert row["names"] == "program"

    src = row["sources"][0]
    assert src["artifact"] == "CTL-FILE"
    assert src["kind"] == "file"
    assert src["ddname"] == "CTLDD"
    # HOW the name leaves the artifact: the verb, and the field it lands in.
    assert src["how"]["verb"] == "READ"
    assert src["how"]["field"] == "CTL-PGM-NAME"
    # ...and how it travels from there to the CALL, in source order.
    assert [(s["from"], s["to"]) for s in src["chain"]] == [
        ("CTL-PGM-NAME", "WS-HOLD"), ("WS-HOLD", "WS-SUBPGM")]


def test_the_dataset_is_named_once_the_jcl_binds_the_ddname():
    """CTLDD is a program-local ddname; PROD.PARM.CNTL is the thing you can go and read.
    Without the JCL the row honestly stops at the ddname."""
    _m, _art, bound = _view(FILE_FED, jcl=JCL)
    src = _only(bound)["sources"][0]
    assert src["dataset"] == "PROD.PARM.CNTL"
    assert src["boundBy"][0]["job"] == "DISPJOB"

    _m, _art, unbound = _view(FILE_FED)
    assert "dataset" not in _only(unbound)["sources"][0]


def test_a_db2_source_names_the_column_not_the_host_variable():
    """WS-PGM is this program's private name for the value; ROUTING.HANDLER is the
    database's, and is what someone goes and selects to enumerate the targets."""
    _m, _art, view = _view("""\
       IDENTIFICATION DIVISION.
       PROGRAM-ID. DB2P.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-PGM  PIC X(8).
       PROCEDURE DIVISION.
       0000-MAIN.
           EXEC SQL SELECT HANDLER INTO :WS-PGM FROM ROUTING END-EXEC
           CALL WS-PGM
           GOBACK.
""")
    src = _only(view)["sources"][0]
    assert src["artifact"] == "ROUTING"
    assert src["kind"] == "db2-table"
    assert src["how"]["column"] == "HANDLER"
    assert src["how"]["readAt"] == "ROUTING.HANDLER"


def test_a_caller_supplied_target_says_the_answer_is_in_the_callers():
    """A LINKAGE item is filled by whoever calls this program. Reporting "no source"
    would be wrong twice: there IS one, and it is not on this program's boundary."""
    _m, _art, view = _view("""\
       IDENTIFICATION DIVISION.
       PROGRAM-ID. LINKP.
       DATA DIVISION.
       LINKAGE SECTION.
       01  LK-PGM  PIC X(8).
       PROCEDURE DIVISION USING LK-PGM.
       0000-MAIN.
           CALL LK-PGM
           GOBACK.
""")
    src = _only(view)["sources"][0]
    assert src["kind"] == "caller"
    assert "enumerate this program's CALLERs" in src["how"]
    assert view["counts"]["callerSupplied"] == 1


def test_literals_with_no_external_source_are_the_complete_target_set():
    """Two literals and nothing external: the target is genuinely one of two, which is a
    much better answer than 'unresolvable' and must not be reported as the same thing."""
    _m, _art, view = _view("""\
       IDENTIFICATION DIVISION.
       PROGRAM-ID. LITSP.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-PGM  PIC X(8).
       01  WS-FLAG PIC X.
       PROCEDURE DIVISION.
       0000-MAIN.
           IF WS-FLAG = 'A'
              MOVE 'PGMONE' TO WS-PGM
           ELSE
              MOVE 'PGMTWO' TO WS-PGM
           END-IF
           CALL WS-PGM
           GOBACK.
""")
    row = _only(view)
    assert row["candidates"] == ["PGMONE", "PGMTWO"]
    assert row["sources"] == []
    assert "FULL set of possible targets" in row["sourcesNote"]


def test_a_chain_that_bottoms_out_in_an_unassigned_item_names_it_as_a_likely_defect():
    """WS-PGM comes only from WS-ROUTE, and nothing ever assigns WS-ROUTE. That is not a
    run-time indirection - the target is whatever the item was initialised to, which is
    a defect. Calling it 'unresolvable' would file a bug as a modelling limitation."""
    _m, _art, view = _view("""\
       IDENTIFICATION DIVISION.
       PROGRAM-ID. DEADEND.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-PGM   PIC X(8).
       01  WS-ROUTE PIC X(8).
       PROCEDURE DIVISION.
       0000-MAIN.
           MOVE WS-ROUTE TO WS-PGM
           CALL WS-PGM
           GOBACK.
""")
    row = _only(view)
    assert row["sources"] == []
    assert row["deadEnds"] == ["WS-ROUTE"]
    assert "usually a defect" in row["sourcesNote"]
    # ...and it must NOT blame a copybook: WS-PGM is plainly declared.
    assert "copybook" not in row["sourcesNote"]


def test_an_undeclared_item_points_at_the_missing_copybook_not_at_runtime_config():
    """The item is not declared at all, which almost always means a copybook did not
    resolve - a fixable local problem, not a genuine run-time indirection. Calling it
    'run-time determined' would send the reader hunting for a control file that does
    not exist."""
    _m, _art, view = _view("""\
       IDENTIFICATION DIVISION.
       PROGRAM-ID. NODECL.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       COPY MISSINGCPY.
       PROCEDURE DIVISION.
       0000-MAIN.
           CALL WS-SUBPGM
           GOBACK.
""", resolver=CopybookResolver(paths=["/nonexistent"]))
    row = _only(view)
    assert row["sources"] == []
    # It stays in the view - but marked provisional, because supplying the member may
    # resolve the target and delete this row entirely. It rests on an incomplete model,
    # not on a property of the program, and must not read as an equal finding.
    assert row["provisional"] is True
    assert "may not be a dynamic call at all" in row["provisionalNote"]
    assert "MISSINGCPY" in row["provisionalNote"]
    assert row["missingMembers"] == ["MISSINGCPY"]
    assert "prefetch report" in row["provisionalNote"]


# --------------------------------------------------------------------------- #
# what must NOT appear
# --------------------------------------------------------------------------- #

def test_a_resolvable_call_is_not_a_dynamic_call():
    """The single literal reaching WS-SUBPGM proves the target, so it is an ordinary
    dependency. Listing it here would drown the real ones."""
    _m, _art, view = _view("""\
       IDENTIFICATION DIVISION.
       PROGRAM-ID. RESOLVED.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-SUBPGM PIC X(8) VALUE 'POSTLOG'.
       PROCEDURE DIVISION.
       0000-MAIN.
           CALL WS-SUBPGM
           GOBACK.
""")
    assert view["dynamicCalls"] == []
    assert view["counts"]["dynamicTargets"] == 0


def test_the_target_is_never_guessed():
    """The control file's CONTENTS are run-time data. Naming the artifact is a fact;
    naming a program would be an invention, and no field here may carry one."""
    _m, _art, view = _view(FILE_FED, jcl=JCL)
    blob = json.dumps(view)
    assert "POSTLOG" not in blob
    row = _only(view)
    assert "resolved" not in row
    assert "target" not in row


# --------------------------------------------------------------------------- #
# the cross-references: the answer reaches wherever the reader starts
# --------------------------------------------------------------------------- #

def test_the_artifact_manifest_row_carries_the_pointer():
    m, art, view = _view(FILE_FED, jcl=JCL)
    annotated = annotate_artifacts(art, view)
    row = next(r for r in annotated["artifacts"] if r.get("dynamic"))
    assert row["namedBy"][0]["dataset"] == "PROD.PARM.CNTL"
    assert row["namedBy"][0]["read"] == "CTL-PGM-NAME"
    # the stale "a reaching-definition trace is needed" text is REPLACED, not appended
    # to - that trace has now been done.
    assert "is needed to name" not in row["needs"]
    assert "PROD.PARM.CNTL" in row["needs"]


def test_the_fetch_plan_says_what_to_fetch_instead():
    """The dynamic row still cannot be fetched. But the artifact that names its target
    can, and a skip reason that says so is an instruction rather than a dead end."""
    m, art, view = _view(FILE_FED, jcl=JCL)
    plan = build_fetch_plan(annotate_artifacts(art, view))
    row = next(p for p in plan if p["artifact"] == "WS-SUBPGM")
    assert row["status"] == "skipped"
    assert "PROD.PARM.CNTL" in row["reason"]
    assert "fetch that instead" in row["reason"]


# --------------------------------------------------------------------------- #
# candidates: fetched, graded, and kept out of the manifest
# --------------------------------------------------------------------------- #

TWO_LITERALS = """\
       IDENTIFICATION DIVISION.
       PROGRAM-ID. LITSP.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-PGM  PIC X(8).
       01  WS-FLAG PIC X.
       PROCEDURE DIVISION.
       0000-MAIN.
           IF WS-FLAG = 'A'
              MOVE 'PGMONE' TO WS-PGM
           ELSE
              MOVE 'PGMTWO' TO WS-PGM
           END-IF
           CALL WS-PGM
           GOBACK.
"""

EIGHTY_EIGHT = """\
       IDENTIFICATION DIVISION.
       PROGRAM-ID. C88P.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-PGM  PIC X(8).
           88  WS-POST  VALUE 'POSTLOG'.
           88  WS-AUDIT VALUE 'AUDITLOG'.
       PROCEDURE DIVISION.
       0000-MAIN.
           CALL WS-PGM
           GOBACK.
"""


def test_candidates_are_fetched_but_never_become_manifest_dependencies():
    """A candidate is a real member name worth having locally, but it is NOT a proven
    dependency - and the manifest's whole value is that everything in it is real."""
    m, art, view = _view(TWO_LITERALS)
    annotated = annotate_artifacts(art, view)
    programs = {r["artifact"] for r in annotated["artifacts"] if r["kind"] == "program"}
    assert "PGMONE" not in programs and "PGMTWO" not in programs

    got = []

    def mf(name, type=None, copy=None):
        got.append(name)
        return {"artifact_name": name, "found": True, "text": "X\n",
                "source_location": f"PROD.SRCLIB({name})"}

    rep = fetch_dependencies(annotated, mf, dynamic=view)
    assert {"PGMONE", "PGMTWO"} <= set(got)
    rows = {r["artifact"]: r for r in rep["artifacts"]}
    assert rows["PGMONE"]["status"] == "fetched"
    assert rows["PGMONE"]["forDynamicCall"] == "WS-PGM"
    assert rows["PGMONE"]["evidence"] == "assigned"


def test_an_88_level_value_is_a_candidate_but_a_weaker_one():
    """`88 WS-POST VALUE 'POSTLOG'` says the program was WRITTEN to allow POSTLOG. With
    no SET ... TO TRUE anywhere, nothing proves it is ever stored - so it must not be
    reported with the confidence of a MOVE, nor counted as the target set."""
    _m, _art, view = _view(EIGHTY_EIGHT)
    row = _only(view)
    assert row["declaredCandidates"] == ["AUDITLOG", "POSTLOG"]
    assert "candidates" not in row, "unproven values must not sit in the proven field"
    assert "NOTHING in the visible source" in row["declaredCandidatesNote"]
    # ...and the completeness claim that belongs to proven literals must NOT appear
    assert "FULL set of possible targets" not in row.get("sourcesNote", "")


def test_88_candidates_are_still_fetched_carrying_their_weaker_grade():
    _m, _art, view = _view(EIGHTY_EIGHT)
    got = []

    def mf(name, type=None, copy=None):
        got.append(name)
        return {"artifact_name": name, "found": True, "text": "X\n"}

    rep = fetch_dependencies({"program": "C88P", "artifacts": []}, mf, dynamic=view)
    assert {"POSTLOG", "AUDITLOG"} <= set(got)
    row = next(r for r in rep["artifacts"] if r["artifact"] == "POSTLOG")
    assert row["evidence"] == "declared-88"


def test_a_candidate_the_estate_lacks_counts_as_a_gap_like_any_other():
    """We had a concrete name and the estate could not produce it. How we came by the
    name does not change that - one tally, no special case."""
    _m, _art, view = _view(TWO_LITERALS)

    def absent(name, type=None, copy=None):
        return {"artifact_name": name, "found": False}

    rep = fetch_dependencies({"program": "LITSP", "artifacts": []}, absent, dynamic=view)
    assert rep["counts"]["not-found"] == 2
    assert all(r["status"] == "not-found" for r in rep["artifacts"])


# --------------------------------------------------------------------------- #
# the extraction recipe: the last mile
# --------------------------------------------------------------------------- #

def test_a_db2_source_emits_the_query_that_lists_the_real_targets():
    _m, _art, view = _view("""\
       IDENTIFICATION DIVISION.
       PROGRAM-ID. DB2P.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-PGM  PIC X(8).
       PROCEDURE DIVISION.
       0000-MAIN.
           EXEC SQL SELECT HANDLER INTO :WS-PGM FROM ROUTING END-EXEC
           CALL WS-PGM
           GOBACK.
""")
    extract = _only(view)["sources"][0]["extract"]
    assert extract["kind"] == "sql"
    assert extract["run"] == "SELECT DISTINCT HANDLER FROM ROUTING"


def test_a_file_source_emits_the_byte_position_to_read():
    _m, _art, view = _view(FILE_FED)
    extract = _only(view)["sources"][0]["extract"]
    assert extract["kind"] == "file-field"
    assert extract["record"] == "CTL-REC"
    # CTL-PGM-NAME is the first field of CTL-REC, so bytes 1-8 of the 80-byte record.
    assert extract["readAt"] == "bytes 1-8 of the 80-byte record"


def test_a_file_source_withholds_the_position_it_cannot_prove():
    """Same shape, but the record now has an OCCURS DEPENDING table in front of the
    field. The recipe still names the field and the layout - it just refuses the byte."""
    _m, _art, view = _view(FILE_FED.replace(
        "           05  CTL-REST      PIC X(72).\n",
        "           05  CTL-N         PIC 9(2).\n"
        "           05  CTL-TAB OCCURS 1 TO 5 DEPENDING ON CTL-N PIC X(4).\n"))
    extract = _only(view)["sources"][0]["extract"]
    assert "readAt" not in extract
    assert "OCCURS DEPENDING ON CTL-N" in extract["positionWithheld"]
    assert extract["layout"]["fields"], "the layout must survive the refusal"


def test_the_report_is_self_describing_and_serializable():
    _m, _art, view = _view(FILE_FED)
    assert view["format"] == "cobol-xstate-dynamic-calls"
    assert view["program"] == "DISPATCH"
    assert view["counts"]["withAnArtifactSource"] == 1
    assert "would be a fiction" in view["note"]
    json.dumps(view)
