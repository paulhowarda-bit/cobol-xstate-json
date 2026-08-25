"""The mfdep naming-convention fallback for unmapped SQL columns.

When the statement evidence for a column<->host-variable mapping is missing (cursor
DECLARE not visible, count mismatch), the DCLGEN naming conventions indexed by mfdep
can still recover it - marked ``viaConventions`` and flagged as the heuristic it is,
per docs/mfdep-conventions-integration.md. mfdep itself is estate-side and not
installed here, so these tests drive the ``Conventions`` wrapper over a doc-faithful
fake of its API; the always-on auto-load path is proven inert without mfdep.
"""

import json

from cobol_xstate.conventions import Conventions, base_table, load
from cobol_xstate.lineage import build_lineage
from cobol_xstate.parser import parse_program
from cobol_xstate.statechart import build_machine


class FakeMfdep:
    """Doc-faithful ``mfdep.conventions`` over a tiny in-memory DCLGEN index.

    AA is a plain DCLGEN prefix, AAR its COPY REPLACING variant (same table), and NP
    the documented collision: two entities share it, so it resolves only with table
    context or program-reference disambiguation.
    """

    PREFIXES = {
        "AA":  ["T_MMAA_ACC_ANAL"],
        "AAR": ["T_MMAA_ACC_ANAL"],
        "NP":  ["T_MMNP_NCMM_POSN", "T_SMNP_NWK_PART"],
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

def test_load_returns_none_without_mfdep():
    # mfdep is estate-side and deliberately not a dependency; on this machine the
    # always-on auto-load must come back empty, which is what keeps every output
    # byte-identical (the gate proves the bytes; this pins the mechanism).
    assert load() is None


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
    assert _conv().resolve_field("WS-CNT") is None


def test_resolve_columns_marks_each_slot():
    cols, n = _conv().resolve_columns(["AA-FUND-A", "IND-X"], "T_MMAA_ACC_ANAL")
    assert n == 1
    assert cols == [
        {"column": "FUND_A", "hostVar": "AA-FUND-A", "table": "T_MMAA_ACC_ANAL",
         "viaConventions": True},
        {"hostVar": "IND-X", "unresolved": True},
    ]


def test_resolve_columns_nothing_resolved_is_no_list():
    assert _conv().resolve_columns(["WS-CNT", "IND-X"], "") == (None, 0)


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


def test_auto_load_defaults_to_off_without_mfdep():
    # The always-on default (no conventions argument at all) must equal an explicit
    # conventions=None build on a machine without mfdep - same bytes, same flags.
    src_spec = _machine(FETCH_NODECL, conv=None)
    auto = build_machine(parse_program(
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
        "       PROCEDURE DIVISION.\n"
        "       0000-MAIN.\n"
        + FETCH_NODECL +
        "           STOP RUN.\n"), source_name="convtest.cbl")
    assert auto.to_json() == src_spec.to_json()


# ------------------------------------- FETCH count mismatch, DECLARE visible (§1)

def test_fetch_count_mismatch_partial_resolution_with_cursor_table():
    m = _machine(
        "           EXEC SQL\n"
        "               DECLARE C1 CURSOR FOR\n"
        "                   SELECT FUND_A FROM T_MMAA_ACC_ANAL\n"
        "           END-EXEC\n"
        "           EXEC SQL\n"
        "               FETCH C1 INTO :AA-FUND-A :IND-X\n"
        "           END-EXEC\n")
    spec = _spec(m, "FETCH")
    assert spec["columns"] == [
        {"column": "FUND_A", "hostVar": "AA-FUND-A", "table": "T_MMAA_ACC_ANAL",
         "viaConventions": True},
        {"hostVar": "IND-X", "unresolved": True},
    ]
    assert any("(1 of 2 host variable(s) resolved" in x for x in _messages(m))


# --------------------------------------------------- SELECT count mismatch (§2)

SELECT_MISMATCH = (
    "           EXEC SQL\n"
    "               SELECT FUND_A\n"
    "               INTO :AA-FUND-A :IND-X\n"
    "               FROM T_MMAA_ACC_ANAL\n"
    "           END-EXEC\n")


def test_select_count_mismatch_recovered_by_convention():
    m = _machine(SELECT_MISMATCH)
    spec = _spec(m, "SELECT")
    assert spec["columns"][0] == {"column": "FUND_A", "hostVar": "AA-FUND-A",
                                  "table": "T_MMAA_ACC_ANAL",
                                  "viaConventions": True}
    assert spec["columnsFrom"] == "mfdep naming conventions"
    msgs = _messages(m)
    assert any("EXEC SQL SELECT: column<->host-variable mapping recovered by mfdep "
               "NAMING CONVENTION" in x for x in msgs)
    assert not any("mapping not recovered" in x for x in msgs)


def test_select_without_conventions_keeps_original_flag():
    m = _machine(SELECT_MISMATCH, conv=None)
    assert "columns" not in _spec(m, "SELECT")
    assert any("EXEC SQL SELECT: column<->host-variable mapping not recovered" in x
               for x in _messages(m))


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
