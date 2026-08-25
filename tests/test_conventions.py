"""The mfdep naming-convention fallback for unmapped SQL columns.

When the statement evidence for a column<->host-variable mapping is missing (cursor
DECLARE not visible, count mismatch), the DCLGEN naming conventions indexed by mfdep
recover it - marked ``viaConventions`` and flagged as the heuristic it is, per
docs/mfdep-conventions-integration.md. mfdep is ASSUMED PRESENT in the runtime
environment: the import is deferred to the first failed correlation that needs it,
and a machine that needs it and lacks it (this one) fails LOUDLY - there is no
silent conventions-less mode. These tests drive the ``Conventions`` wrapper over a
doc-faithful fake of mfdep's API; classifying indicator variables and derived
columns is mfdep's job, so the fake's verdicts are taken verbatim, exactly as the
real one's are.
"""

import importlib.util
import sys

import json

import pytest

from cobol_xstate import conventions as conventions_module
from cobol_xstate import statechart as statechart_module
from cobol_xstate.conventions import Conventions, base_table
from cobol_xstate.lineage import build_lineage
from cobol_xstate.parser import parse_program
from cobol_xstate.statechart import build_machine

HAVE_MFDEP = importlib.util.find_spec("mfdep") is not None


class FakeMfdep:
    """Doc-faithful ``mfdep.conventions`` over a tiny in-memory DCLGEN index.

    AA is a plain DCLGEN prefix, AAR its COPY REPLACING variant (same table), and NP
    the documented collision: two entities share it, so it resolves only with table
    context or program-reference disambiguation. WS is the generic-prefix hazard
    from docs/issues/conventions-indicator-variable-bug.md: somebody's DCLGEN
    prefix AND the estate's universal working-storage prefix, so a lone WS->table
    hit is usually wrong. Classifying indicator variables and derived slots is
    mfdep's job, never the wrapper's: this fake declines what it does not know
    (IND-X, ZZ-*), exactly as the real one declines a slot it classifies as an
    indicator, and the wrapper takes that verdict verbatim.
    """

    PREFIXES = {
        "AA":  ["T_MMAA_ACC_ANAL"],
        "AAR": ["T_MMAA_ACC_ANAL"],
        "NP":  ["T_MMNP_NCMM_POSN", "T_SMNP_NWK_PART"],
        "WS":  ["T_APWS_WKFL_STEP"],
    }

    def resolve_field_variants(self, field, table=""):
        prefix, _, core = field.partition("-")
        if prefix not in self.PREFIXES or not core:
            return {"original": field, "core": field, "db2_column": "",
                    "table": "", "prefix": ""}
        cands = self.PREFIXES[prefix]
        return {"original": field, "core": core,
                "db2_column": core.replace("-", "_"),
                "table": cands[0] if len(cands) == 1 else "",
                "prefix": prefix,
                "all_prefixed": [field],
                "search_terms": [core, core.replace("-", "_"), field]}

    def infer_table_from_prefix(self, prefix):
        return list(self.PREFIXES.get(prefix, []))


class RaisingMfdep(FakeMfdep):
    def resolve_field_variants(self, field, table=""):
        raise RuntimeError("mfdep.db is locked")


def _conv():
    return Conventions(FakeMfdep())


def _machine(body, data="", conv="fake"):
    src = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. CONVTEST.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01  AA-FUND-A       PIC X(6).\n"
        "       01  AA-ACCT-NBR     PIC 9(9).\n"
        "       01  AAR-FUND-A      PIC X(6).\n"
        "       01  NP-ID-POSN-A    PIC 9(9).\n"
        "       01  IND-X           PIC S9(4) COMP.\n"
        "       01  WS-CNT          PIC S9(9) COMP.\n"
        + data +
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        + body +
        "           STOP RUN.\n")
    conventions = _conv() if conv == "fake" else conv
    return build_machine(parse_program(src), source_name="convtest.cbl",
                         conventions=conventions)


def _spec(machine, verb):
    return next(s for s in machine.semantics["actions"].values()
                if isinstance(s, dict) and s.get("verb") == verb)


def _messages(machine):
    return [f["message"] for f in machine.flags]


FETCH_NODECL = (
    "           EXEC SQL\n"
    "               FETCH C9 INTO :AA-FUND-A, :AA-ACCT-NBR\n"
    "           END-EXEC\n")


# --------------------------------------------------------------------- the wrapper

@pytest.mark.skipif(not HAVE_MFDEP, reason="mfdep not installed here")
def test_load_returns_working_conventions_with_mfdep():
    # The with-mfdep half of the loud-failure contract (the other team's Fix 3(B)):
    # on a machine that has mfdep - the work machine - load() must hand back a
    # healthy wrapper, not merely import. Skips on this mfdep-less dev checkout.
    conv = conventions_module.load()
    assert isinstance(conv, Conventions)
    assert conv.disabled_reason is None


@pytest.mark.skipif(HAVE_MFDEP, reason="mfdep installed: the requirement is met")
def test_load_fails_loudly_without_mfdep():
    # mfdep is assumed present in the runtime environment. A machine that needs the
    # conventions and lacks the package must DIE on the import, never degrade to
    # silently unmapped columns (the v50 stale-build failure, as a policy).
    with pytest.raises(ImportError):
        conventions_module.load()


def test_base_table_drops_schema_qualifier():
    assert base_table("MMD1DBO.T_MMAA_ACC_ANAL") == "T_MMAA_ACC_ANAL"
    assert base_table("T_MMAA_ACC_ANAL") == "T_MMAA_ACC_ANAL"


def test_resolve_field_unique_prefix():
    assert _conv().resolve_field("AA-FUND-A") == {
        "column": "FUND_A", "table": "T_MMAA_ACC_ANAL"}


def test_resolve_field_replacing_variant_reaches_same_table():
    assert _conv().resolve_field("AAR-FUND-A") == {
        "column": "FUND_A", "table": "T_MMAA_ACC_ANAL"}


def test_resolve_field_table_context_validated_first():
    got = _conv().resolve_field("NP-ID-POSN-A", table="T_SMNP_NWK_PART")
    assert got == {"column": "ID_POSN_A", "table": "T_SMNP_NWK_PART"}


def test_resolve_field_ambiguous_narrowed_by_program_tables():
    got = _conv().resolve_field("NP-ID-POSN-A",
                                program_tables=frozenset({"T_SMNP_NWK_PART"}))
    assert got == {"column": "ID_POSN_A", "table": "T_SMNP_NWK_PART"}


def test_resolve_field_ambiguous_stays_unresolved():
    # Two candidate tables, no context that narrows them: a guessed table is wrong
    # lineage, which is worse than none.
    assert _conv().resolve_field("NP-ID-POSN-A") is None
    assert _conv().resolve_field("NP-ID-POSN-A",
                                 program_tables=frozenset({"T_MMNP_NCMM_POSN",
                                                           "T_SMNP_NWK_PART"})) is None


def test_resolve_field_unknown_prefix_unresolved():
    assert _conv().resolve_field("ZZ-FOO") is None


def test_resolve_field_endpoint_contradiction_rejected():
    # The statement's own table is the ground truth: a prefix whose candidate
    # tables contradict it (WS -> T_APWS_WKFL_STEP vs FROM CUSTOMER) is the
    # generic-prefix hazard, not evidence - it must resolve nothing.
    assert _conv().resolve_field("WS-NAME", table="CUSTOMER") is None


def test_resolve_field_program_reference_veto():
    # Endpoint unknown: the program's own table references veto a resolution
    # naming a table the program never touches...
    assert _conv().resolve_field(
        "WS-NAME", program_tables=frozenset({"CUSTOMER"})) is None
    # ...while an EMPTY reference set vetoes nothing (a pure reader whose only
    # table mention was the missing DECLARE).
    assert _conv().resolve_field("WS-NAME") == {
        "column": "NAME", "table": "T_APWS_WKFL_STEP"}


def test_resolve_columns_marks_each_slot():
    # The indicator variable is MFDEP'S call: it declines IND-X, and the wrapper
    # records exactly that (an explicit unresolved entry) - no re-classification.
    cols, n = _conv().resolve_columns(["AA-FUND-A", "IND-X"], "T_MMAA_ACC_ANAL")
    assert n == 1
    assert cols == [
        {"column": "FUND_A", "hostVar": "AA-FUND-A", "table": "T_MMAA_ACC_ANAL",
         "viaConventions": True},
        {"hostVar": "IND-X", "unresolved": True},
    ]


def test_resolve_columns_nothing_resolved_is_no_list():
    assert _conv().resolve_columns(["ZZ-A", "IND-X"], "") == (None, 0)


def test_mfdep_failure_disables_not_crashes():
    conv = Conventions(RaisingMfdep())
    assert conv.resolve_columns(["AA-FUND-A"], "") == (None, 0)
    assert "RuntimeError" in conv.disabled_reason
    # and it STAYS disabled - no half-resolved output later in the run
    assert conv.resolve_field("AA-FUND-A") is None


# ------------------------------------------------- FETCH, DECLARE not visible (§1)

def test_fetch_without_declare_recovered_by_convention():
    m = _machine(FETCH_NODECL)
    spec = _spec(m, "FETCH")
    assert spec["columns"] == [
        {"column": "FUND_A", "hostVar": "AA-FUND-A", "table": "T_MMAA_ACC_ANAL",
         "viaConventions": True},
        {"column": "ACCT_NBR", "hostVar": "AA-ACCT-NBR", "table": "T_MMAA_ACC_ANAL",
         "viaConventions": True},
    ]
    assert spec["columnsFrom"] == "mfdep naming conventions"
    assert "NAMING CONVENTION" in spec["columnNote"]
    assert "no DECLARE for cursor C9" in spec["columnNote"]  # the why survives
    msgs = _messages(m)
    assert any("EXEC SQL FETCH: column<->host-variable mapping recovered by mfdep "
               "NAMING CONVENTION (2 of 2 host variable(s) resolved" in x
               for x in msgs)
    assert not any("mapping not recovered" in x for x in msgs)


def test_fetch_without_conventions_unchanged():
    m = _machine(FETCH_NODECL, conv=None)
    spec = _spec(m, "FETCH")
    assert "columns" not in spec
    assert "no DECLARE for cursor C9" in spec["columnNote"]
    assert any("EXEC SQL FETCH: column<->host-variable mapping not recovered" in x
               for x in _messages(m))


def test_suite_pin_equals_explicit_none():
    # conftest pins the suite's default builds conventions-less (a determinism pin:
    # test output can no more depend on the day's mfdep.db than a golden can). The
    # pinned default must be byte-identical to an explicit conventions=None build.
    pinned = _machine(FETCH_NODECL, conv=statechart_module._AUTO_CONVENTIONS)
    explicit = _machine(FETCH_NODECL, conv=None)
    assert pinned.to_json() == explicit.to_json()


def test_mfdep_is_imported_only_on_first_need(monkeypatch):
    # The always-on default defers the mfdep import to the first FAILED correlation:
    # a program with nothing to recover must never touch mfdep at all (which is also
    # what keeps SQL-clean runs alive in environments like the separation-proof
    # venvs). Un-pin the suite fixture so the real load path is live, and prove a
    # cleanly-correlating program never triggers it.
    monkeypatch.setattr(statechart_module, "_load_conventions",
                        conventions_module.load)
    monkeypatch.delitem(sys.modules, "mfdep", raising=False)
    m = _machine(
        "           EXEC SQL\n"
        "               SELECT FUND_A INTO :AA-FUND-A\n"
        "               FROM T_MMAA_ACC_ANAL\n"
        "           END-EXEC\n", conv=statechart_module._AUTO_CONVENTIONS)
    assert _spec(m, "SELECT")["columns"] == [
        {"column": "FUND_A", "hostVar": "AA-FUND-A"}]
    assert "mfdep" not in sys.modules


@pytest.mark.skipif(HAVE_MFDEP, reason="mfdep installed: the requirement is met")
def test_needed_but_missing_mfdep_fails_the_build(monkeypatch):
    # ...and a program that DOES need recovering, on a machine without mfdep, dies
    # on the ImportError - loud beats silently unmapped.
    monkeypatch.setattr(statechart_module, "_load_conventions",
                        conventions_module.load)
    with pytest.raises(ImportError):
        _machine(FETCH_NODECL, conv=statechart_module._AUTO_CONVENTIONS)


# ------------------- count mismatches are NEVER convention-resolved (bug doc)

def test_fetch_count_mismatch_is_never_convention_resolved():
    # A visible DECLARE that still failed is a COUNT MISMATCH - the indicator
    # variable breaks the 1:1 column<->variable assumption, so per-field prefix
    # resolution would inject wrong lineage where the parser correctly refused.
    # The refusal (and its flag) must stand, identical to a pinned build.
    body = (
        "           EXEC SQL\n"
        "               DECLARE C1 CURSOR FOR\n"
        "                   SELECT FUND_A FROM T_MMAA_ACC_ANAL\n"
        "           END-EXEC\n"
        "           EXEC SQL\n"
        "               FETCH C1 INTO :AA-FUND-A :IND-X\n"
        "           END-EXEC\n")
    with_conv = _machine(body)
    pinned = _machine(body, conv=None)
    assert _spec(with_conv, "FETCH") == _spec(pinned, "FETCH")
    assert _messages(with_conv) == _messages(pinned)
    assert "not correlatable" in _spec(with_conv, "FETCH")["columnNote"]


SELECT_MISMATCH = (
    "           EXEC SQL\n"
    "               SELECT FUND_A\n"
    "               INTO :AA-FUND-A :IND-X\n"
    "               FROM T_MMAA_ACC_ANAL\n"
    "           END-EXEC\n")


def test_select_count_mismatch_is_never_convention_resolved():
    with_conv = _machine(SELECT_MISMATCH)
    pinned = _machine(SELECT_MISMATCH, conv=None)
    assert _spec(with_conv, "SELECT") == _spec(pinned, "SELECT")
    assert _messages(with_conv) == _messages(pinned)


def test_bug_doc_reproduction_indicator_refusal_stands():
    # docs/issues/conventions-indicator-variable-bug.md verbatim: 2 columns,
    # 2 real host variables + 1 null indicator. The parser refuses (2 vs 3);
    # the conventions must not overrule it - especially not to WS's table.
    m = _machine(
        "           EXEC SQL\n"
        "               SELECT NAME, BAL\n"
        "               INTO :WS-NAME, :WS-BAL:IND-BAL\n"
        "               FROM CUSTOMER\n"
        "           END-EXEC\n")
    spec = _spec(m, "SELECT")
    assert "columns" not in spec
    assert "not correlatable" in spec["columnNote"]
    assert any("mapping not recovered" in x for x in _messages(m))


# ------------------------------------------- SELECT * IS recoverable (§2)

def test_select_star_recovered_by_convention():
    # SELECT * fails for a different reason - the column list is simply not in
    # the source - so the 1:1 assumption holds and the conventions may recover
    # it, validated against the statement's own FROM table.
    m = _machine(
        "           EXEC SQL\n"
        "               SELECT *\n"
        "               INTO :AA-FUND-A\n"
        "               FROM T_MMAA_ACC_ANAL\n"
        "           END-EXEC\n")
    spec = _spec(m, "SELECT")
    assert spec["columns"] == [
        {"column": "FUND_A", "hostVar": "AA-FUND-A", "table": "T_MMAA_ACC_ANAL",
         "viaConventions": True}]
    assert spec["columnsFrom"] == "mfdep naming conventions"
    assert any("EXEC SQL SELECT: column<->host-variable mapping recovered by mfdep "
               "NAMING CONVENTION" in x for x in _messages(m))


def test_correlated_select_with_derived_slot_is_not_deferred():
    # A SELECT that correlated fine but carries a residual note (COUNT(*) fills a
    # derived slot) has nothing for the conventions to recover: its columns and its
    # flag must come through exactly as in a pinned build - deferral would silently
    # drop the flag (this bit: the suite caught it on sqlgaps.cbl).
    body = (
        "           EXEC SQL\n"
        "               SELECT FUND_A, COUNT(*)\n"
        "               INTO :AA-FUND-A, :WS-CNT\n"
        "               FROM T_MMAA_ACC_ANAL GROUP BY FUND_A\n"
        "           END-EXEC\n")
    with_conv = _machine(body)
    pinned = _machine(body, conv=None)
    assert _spec(with_conv, "SELECT") == _spec(pinned, "SELECT")
    assert _messages(with_conv) == _messages(pinned)
    assert _spec(with_conv, "SELECT")["columns"][1] == {"hostVar": "WS-CNT",
                                                        "derived": True}


def test_select_without_conventions_keeps_original_flag():
    m = _machine(SELECT_MISMATCH, conv=None)
    assert "columns" not in _spec(m, "SELECT")
    assert any("EXEC SQL SELECT: column<->host-variable mapping not recovered" in x
               for x in _messages(m))


# ---------------------------------------- null indicators never reach the lookup

def test_indicator_vars_from_raw_sql():
    # The second colon-variable inside one comma group of the INTO clause is a
    # Db2 null indicator. The four shapes the feedback doc tested, verbatim.
    iv = statechart_module._indicator_vars
    assert iv("INTO : WS-NAME , : WS-BAL : IND-BAL FROM CUSTOMER") == {"IND-BAL"}
    assert iv("INTO : AA-FUND-A : IND-X , : AA-ACCT-NBR END-EXEC") == {"IND-X"}
    assert iv("INTO : WS-ID , : WS-N FROM ACCOUNT") == frozenset()
    assert iv("INTO : WS-ID , : WS-BAL END-EXEC") == frozenset()
    assert iv("") == frozenset()


def test_fetch_indicator_stripped_before_conventions():
    # A no-DECLARE FETCH is recoverable - but its null indicator must never reach
    # the column lookup (a real mfdep index would resolve IND-BAL to column BAL).
    # The indicator simply does not appear in columns[] at all, and the flag
    # counts data variables only.
    m = _machine(
        "           EXEC SQL\n"
        "               FETCH C9 INTO :AA-FUND-A:IND-X\n"
        "           END-EXEC\n")
    spec = _spec(m, "FETCH")
    assert spec["columns"] == [
        {"column": "FUND_A", "hostVar": "AA-FUND-A", "table": "T_MMAA_ACC_ANAL",
         "viaConventions": True}]
    assert not any("IND-X" in str(c) for c in spec["columns"])
    assert any("(1 of 1 host variable(s) resolved" in x for x in _messages(m))


def test_select_star_indicator_stripped_before_conventions():
    m = _machine(
        "           EXEC SQL\n"
        "               SELECT *\n"
        "               INTO :AA-FUND-A:IND-X\n"
        "               FROM T_MMAA_ACC_ANAL\n"
        "           END-EXEC\n")
    spec = _spec(m, "SELECT")
    assert spec["columns"] == [
        {"column": "FUND_A", "hostVar": "AA-FUND-A", "table": "T_MMAA_ACC_ANAL",
         "viaConventions": True}]
    assert any("(1 of 1 host variable(s) resolved" in x for x in _messages(m))


# ------------------------------------------------ collision disambiguation (§4)

AMBIG_FETCH = (
    "           EXEC SQL\n"
    "               FETCH C9 INTO :NP-ID-POSN-A\n"
    "           END-EXEC\n")


def test_ambiguous_prefix_narrowed_by_program_reference():
    # The program provably touches T_SMNP_NWK_PART (the COUNT(*) SELECT's FROM), so
    # the two-entity NP prefix narrows to it.
    m = _machine(
        "           EXEC SQL\n"
        "               SELECT COUNT(*) INTO :WS-CNT\n"
        "               FROM T_SMNP_NWK_PART\n"
        "           END-EXEC\n"
        + AMBIG_FETCH)
    spec = _spec(m, "FETCH")
    assert spec["columns"] == [
        {"column": "ID_POSN_A", "hostVar": "NP-ID-POSN-A",
         "table": "T_SMNP_NWK_PART", "viaConventions": True}]


def test_ambiguous_prefix_without_context_degrades_to_todays_output():
    with_conv = _machine(AMBIG_FETCH)
    without = _machine(AMBIG_FETCH, conv=None)
    assert _spec(with_conv, "FETCH") == _spec(without, "FETCH")
    assert _messages(with_conv) == _messages(without)


def test_generic_prefix_vetoed_by_program_references():
    # WS- is somebody's DCLGEN prefix AND the universal working-storage prefix.
    # A no-DECLARE FETCH of a WS- field must not resolve to that somebody's
    # table when this program's own references contradict it (the bug doc's
    # secondary issue) - the refusal stands instead.
    m = _machine(
        "           EXEC SQL\n"
        "               UPDATE T_MMAA_ACC_ANAL SET FUND_A = :AA-FUND-A\n"
        "           END-EXEC\n"
        "           EXEC SQL\n"
        "               FETCH C9 INTO :WS-CNT\n"
        "           END-EXEC\n")
    spec = _spec(m, "FETCH")
    assert "columns" not in spec
    assert any("EXEC SQL FETCH: column<->host-variable mapping not recovered" in x
               for x in _messages(m))


# ------------------------------------------------------------- failure surface

def test_mfdep_error_degrades_with_a_flag():
    m = _machine(FETCH_NODECL, conv=Conventions(RaisingMfdep()))
    spec = _spec(m, "FETCH")
    assert "columns" not in spec           # today's output...
    msgs = _messages(m)
    assert any("EXEC SQL FETCH: column<->host-variable mapping not recovered" in x
               for x in msgs)
    assert any("mfdep conventions lookup failed mid-run" in x
               and "RuntimeError" in x for x in msgs)   # ...plus the WHY


# ---------------------------------------------------- downstream views carry it

def test_interface_event_carries_convention_columns():
    m = _machine(FETCH_NODECL)
    iface = m.bundle()["interface"]
    ev = next(e for e in iface["events"] if e["verb"] == "FETCH")
    assert ev["columns"] == [
        {"table": "T_MMAA_ACC_ANAL", "column": "FUND_A", "hostVar": "AA-FUND-A",
         "viaConventions": True},
        {"table": "T_MMAA_ACC_ANAL", "column": "ACCT_NBR", "hostVar": "AA-ACCT-NBR",
         "viaConventions": True},
    ]
    assert "NAMING CONVENTION" in ev["columnNote"]


def test_every_view_builds_over_convention_columns():
    # The graph loader's consumer is the interface events' columns[] (asserted above);
    # here: no other view's walker chokes on the extra viaConventions/unresolved keys.
    from cobol_xstate.business import build_business_view
    from cobol_xstate.reactive import build_reactive_view

    m = _machine(FETCH_NODECL)
    assert json.dumps(build_lineage(m))
    assert json.dumps(build_business_view(m))
    assert json.dumps(build_reactive_view(m))
    assert m.to_json()
