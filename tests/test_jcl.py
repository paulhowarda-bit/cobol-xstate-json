"""JCL / PROC parsing, dataflow + control-card field lineage, and the artifact manifest."""

from pathlib import Path

from cobol_xstate.jcl import parse_jcl
from cobol_xstate.jcl_views import build_jcl_artifacts, build_jcl_lineage

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "jcl"


def _job(name: str, resolver=None):
    return parse_jcl((EXAMPLES / name).read_text(), resolver=resolver, source_name=name)


def _art_by_name(job) -> dict:
    return {a["artifact"]: a for a in build_jcl_artifacts(job)["artifacts"]}


# --------------------------------------------------------------------------- #
# parsing: statements, symbolics, DISP, GDG, concatenation
# --------------------------------------------------------------------------- #

def test_symbolic_and_gdg_resolution():
    job = _job("acctunld.jcl")
    assert job.name == "ACCTUNLD"
    assert job.symbols["HLQ"] == "PROD"
    out = next(dd for s in job.steps for dd in s.dds if dd.ddname == "OUTDD")
    seg = out.segments[0]
    assert seg.dsn == "PROD.ACCT.UNLOAD"      # &HLQ..ACCT.UNLOAD resolved
    assert seg.gdg == "+1"                     # (+1) stripped to the base + generation
    assert seg.disp == ["NEW", "CATLG", "DELETE"]


def test_continuation_lines_are_merged():
    # OUTDD spans three physical lines (DSN,/DISP,/SPACE); all operands must be present.
    job = _job("acctunld.jcl")
    out = next(dd for s in job.steps for dd in s.dds if dd.ddname == "OUTDD")
    assert out.segments[0].disp == ["NEW", "CATLG", "DELETE"]


def test_unresolved_symbolic_is_flagged_not_guessed():
    job = parse_jcl("//J JOB\n//S EXEC PGM=P\n//IN DD DSN=&NOPE..DATA,DISP=SHR\n")
    seg = job.steps[0].dds[0].segments[0]
    assert "&NOPE" in seg.dsn                   # left visible, not blanked
    assert any("NOPE" in f for f in job.flags)


def test_concatenated_dd_is_one_dd_many_segments():
    job = parse_jcl(
        "//J JOB\n//S EXEC PGM=P\n"
        "//IN DD DSN=PROD.A,DISP=SHR\n"
        "//   DD DSN=PROD.B,DISP=SHR\n")
    dd = job.steps[0].dds[0]
    assert dd.ddname == "IN"
    assert [s.dsn for s in dd.segments] == ["PROD.A", "PROD.B"]


# --------------------------------------------------------------------------- #
# PROC expansion, overrides, INCLUDE (with a caller-provided resolver)
# --------------------------------------------------------------------------- #

def test_inline_proc_expands_with_override_symbolic_and_override_dd():
    job = _job("dailypost.jcl")
    step = next(s for s in job.steps if s.from_proc == "POSTPRC")
    assert step.pgm == "DAILYPOST"
    assert step.proc_step == "RUNPOST"
    tranin = next(dd for dd in step.dds if dd.ddname == "TRANIN")
    assert tranin.segments[0].dsn == "PROD.FIN.TRANS"     # &ENV -> PROD (EXEC override)
    audit = next(dd for dd in step.dds if dd.ddname == "AUDIT")
    assert audit.override is True                         # //RUNPOST.AUDIT DD ... applied
    assert audit.segments[0].dsn == "PROD.FIN.AUDIT"


def test_cataloged_proc_resolved_via_provided_function():
    lib = {"MYPROC": "//MYPROC PROC\n//RUN EXEC PGM=EDIT\n"
                     "//IN DD DSN=PROD.IN,DISP=SHR\n//   PEND\n"}
    job = parse_jcl("//J JOB\n//S1 EXEC MYPROC\n", resolver=lambda n: lib.get(n.upper()))
    step = next(s for s in job.steps if s.from_proc == "MYPROC")
    assert step.pgm == "EDIT"
    assert step.proc_resolved is True
    assert not job.flags


def test_unresolved_proc_is_flagged_not_invented():
    job = parse_jcl("//J JOB\n//S1 EXEC NOSUCH\n")     # no resolver
    step = job.steps[0]
    assert step.proc == "NOSUCH" and step.proc_resolved is False
    assert any("NOSUCH" in f for f in job.flags)


def test_include_member_resolved_and_its_dd_attaches_to_the_open_step():
    lib = {"FINSTD": "//STDLIB DD DSN=PROD.FIN.STDCTL,DISP=SHR"}
    job = _job("dailypost.jcl", resolver=lambda n: lib.get(n.upper()))
    binds = build_jcl_lineage(job)["ddBindings"]
    assert any(b["ddname"] == "STDLIB" and b["dataset"] == "PROD.FIN.STDCTL"
               for b in binds)
    assert not job.flags


# --------------------------------------------------------------------------- #
# lineage: dataflow across steps + control-card field lineage
# --------------------------------------------------------------------------- #

def test_dataflow_edge_between_producer_and_consumer_step():
    lin = build_jcl_lineage(_job("acctunld.jcl"))
    edges = {(e["from"], e["to"], e["dataset"]) for e in lin["dataflow"]}
    assert ("STEP01", "STEP02", "PROD.ACCT.UNLOAD") in edges
    # the shared dataset is marked intermediate (produced then consumed within the job)
    inter = next(d for d in lin["datasets"] if d["dsn"] == "PROD.ACCT.UNLOAD")
    assert inter["intermediate"] is True


def test_sort_control_card_gives_byte_field_lineage():
    lin = build_jcl_lineage(_job("acctunld.jcl"))
    fl = next(r for r in lin["fieldLineage"] if r["utility"] == "SORT/DFSORT")
    assert fl["input"] == "PROD.ACCT.UNLOAD" and fl["output"] == "PROD.ACCT.SORTED"
    assert fl["filter"]["kind"] == "INCLUDE"
    # BUILD=(1,5,6,20,28,8) -> three fields; the third copies input 28-35 to output 26-33
    f3 = fl["fields"][2]
    assert f3["from"] == "input" and f3["inBytes"] == "28-35" and f3["outBytes"] == "26-33"


def test_idcams_repro_is_a_copy_edge():
    lin = build_jcl_lineage(_job("copyrepr.jcl"))
    fl = next(r for r in lin["fieldLineage"] if r["utility"] == "IDCAMS")
    assert fl["operations"][0]["op"] == "REPRO"


def test_dd_bindings_resolve_a_cobol_programs_ddname_to_a_dataset():
    """The whole point: STEP01 runs SQLUNLD, whose OUT-FILE is ASSIGNed to ddname OUTDD.
    The COBOL side could only say 'OUTDD, DSN in the JCL'; this supplies the DSN."""
    lin = build_jcl_lineage(_job("acctunld.jcl"))
    b = next(x for x in lin["ddBindings"]
             if x["program"] == "SQLUNLD" and x["ddname"] == "OUTDD")
    assert b["dataset"] == "PROD.ACCT.UNLOAD" and b["io"] == "output"


# --------------------------------------------------------------------------- #
# artifacts manifest (same shape as the COBOL one)
# --------------------------------------------------------------------------- #

def test_artifacts_list_datasets_programs_and_dependency_tags():
    art = _art_by_name(_job("acctunld.jcl"))
    ds = art["PROD.ACCT.UNLOAD"]
    assert ds["kind"] == "dataset" and ds["dependency"] == "runtime"
    assert ds["io"] == "read-write"            # written by STEP01, read by STEP02
    assert ds["identity"] == "global" and ds["resolvedBy"] is None   # DSN is the identity
    assert ds["gdg"] is True
    assert art["SQLUNLD"]["kind"] == "program"
    assert art["SORT"]["kind"] == "program"


def test_proc_and_include_are_compile_time_artifacts():
    art = _art_by_name(_job("dailypost.jcl"))
    assert art["POSTPRC"]["kind"] == "proc"
    assert art["POSTPRC"]["dependency"] == "compile-time"
    assert art["FINSTD"]["kind"] == "include-member"
    assert art["FINSTD"]["dependency"] == "compile-time"


def test_temp_dataset_is_job_scoped_not_global():
    job = parse_jcl(
        "//J JOB\n//S1 EXEC PGM=A\n//OUT DD DSN=&&WORK,DISP=(NEW,PASS)\n"
        "//S2 EXEC PGM=B\n//IN DD DSN=&&WORK,DISP=(OLD,DELETE)\n")
    art = {a["artifact"]: a for a in build_jcl_artifacts(job)["artifacts"]}
    work = art["&&WORK"]
    assert work["identity"] == "job-scoped" and work["temporary"] is True


def test_sysout_and_dummy_are_excluded_with_reason():
    job = parse_jcl(
        "//J JOB\n//S EXEC PGM=P\n//RPT DD SYSOUT=*\n//SCR DD DUMMY\n")
    art = build_jcl_artifacts(job)
    ex = {e["name"]: e for e in art["excluded"]}
    assert ex["RPT"]["kind"] == "spool"
    assert ex["SCR"]["kind"] == "dummy"


# --------------------------------------------------------------------------- #
# CLI integration: auto-detection and companion output
# --------------------------------------------------------------------------- #

def test_cli_autodetects_jcl_and_writes_both_views(tmp_path):
    import json
    from cobol_xstate.cli import run
    assert run([str(EXAMPLES / "acctunld.jcl"), "--outdir", str(tmp_path)]) == 0
    names = {f.name for f in tmp_path.iterdir()}
    assert names == {"acctunld.jcl.artifacts.json", "acctunld.jcl.lineage.json"}
    art = json.loads((tmp_path / "acctunld.jcl.artifacts.json").read_text())
    assert art["format"] == "cobol-xstate-jcl-artifacts"


def test_bare_proc_member_is_analysed_with_its_defaults():
    """A .prc that only DEFINES a PROC (never EXECs it) is analysed directly, expanded with
    its own default symbolics, so the member is not empty."""
    job = _job("edvalid.prc")
    assert job.is_proc is True
    step = job.steps[0]
    assert step.from_proc == "EDVALID" and step.pgm == "EDCHECK"
    cardin = next(dd for dd in step.dds if dd.ddname == "CARDIN")
    assert cardin.segments[0].dsn == "TEST.EDIT.CARDS"    # &ENV -> TEST (the default)


def test_cli_jcl_detection_does_not_misfire_on_cobol():
    from cobol_xstate.cli import _looks_like_jcl
    cobol = "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. T.\n"
    assert _looks_like_jcl("t.cbl", cobol) is False
    assert _looks_like_jcl("t.jcl", "//J JOB\n//S EXEC PGM=P\n") is True
