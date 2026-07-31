"""A deterministic stand-in for the estate's artifact service.

The real default client is ``network_drive.mf_fetch:fetch_artifact`` - an external,
out-of-tree library that talks to a mainframe share. Nothing in this repository can
reach it, so without a stand-in the two retrieval reports (``.prefetch.json`` /
``.fetch.json``) are untestable and un-hashable, and the byte-stability ratchet can
only cover the views.

This client answers from a fixed table, so a run is reproducible on any machine with no
network at all. It deliberately covers every OUTCOME the reporting code distinguishes,
because those distinctions are the thing most likely to be broken silently by a
refactor:

    CUSTREC   found - but also on the local search path, so a run should resolve it
              LOCALLY and never ask us at all (proves the local-beats-network rule)
    SQLCA     found, plain
    CMQV      found, and the estate offers ALTERNATIVES from other libraries
    USEMQ     found when asked for as `cobol`
    SUBFEE    NOT found as `cobol`, found as `asm` - the probe chain, and the
              `languageBasis` string that is derived from which probe missed first
    PAYAUX    found, but the estate says it is really something else than we asked
              for (a `detected_type` disagreement)
    ABENDL    RAISES - the request failed. This is NOT the same as absence and must
              never be reported as if it were: a client that returns None on a
              connection error makes an entire estate read as empty.
    anything  not found - the service was asked and had nothing
    else

``copied_to`` is deliberately NOT returned. Letting ``artifact_service.collect`` do the
saving keeps the ``copiedTo`` path in the reports derived from *this* run's ``--outdir``
rather than from wherever a gather box happened to put it.
"""

from __future__ import annotations

# Every answer is a plain string of member text. Kept small but syntactically real, so a
# parse that consumes one does not fall over for reasons unrelated to what is under test.

_SQLCA = """\
       01  SQLCA.
           05  SQLCAID      PIC X(8).
           05  SQLCABC      PIC S9(9) COMP.
           05  SQLCODE      PIC S9(9) COMP.
           05  SQLERRM.
               49  SQLERRML PIC S9(4) COMP.
               49  SQLERRMC PIC X(70).
           05  SQLSTATE     PIC X(5).
"""

_CMQV = """\
       01  MQ-CONSTANTS.
           05  MQOO-INPUT-SHARED   PIC S9(9) BINARY VALUE 2.
           05  MQOO-OUTPUT         PIC S9(9) BINARY VALUE 16.
           05  MQCC-OK             PIC S9(9) BINARY VALUE 0.
           05  MQRC-NONE           PIC S9(9) BINARY VALUE 0.
"""

_CUSTREC = """\
       01  CUSTOMER-RECORD.
           05  CUST-ID      PIC 9(6).
           05  CUST-NAME    PIC X(30).
           05  CUST-BALANCE PIC S9(7)V99 COMP-3.
"""

_USEMQ = """\
       IDENTIFICATION DIVISION.
       PROGRAM-ID. USEMQ.
       PROCEDURE DIVISION.
       0000-MAIN.
           DISPLAY 'USEMQ'.
           GOBACK.
"""

_SUBFEE_ASM = """\
SUBFEE   CSECT
         STM   R14,R12,12(R13)
         LR    R12,R15
         USING SUBFEE,R12
         LM    R14,R12,12(R13)
         BR    R14
         END
"""

_PAYAUX = """\
       IDENTIFICATION DIVISION.
       PROGRAM-ID. PAYAUX.
       PROCEDURE DIVISION.
       0000-MAIN.
           GOBACK.
"""


class EstateRequestFailed(RuntimeError):
    """The request itself failed - credentials, connectivity, a service fault.

    Distinct from "not found" on purpose. ``artifact_service.call_service`` turns any
    exception from a client into ``ServiceUnavailable``, which is reported as an
    ``error`` row, not as an absent member.
    """


def fetch_artifact(name, type=None, copy=None):        # noqa: A002 - `type` is the wire name
    """The mf-fetch calling convention: ``f(name, type=..., copy=...)``.

    Both keywords are optional in the contract; this client accepts and honours both so
    the normal path (which passes them) is what gets exercised.
    """
    key = str(name).strip().strip("'\"").upper()
    want = (type or "").strip().lower() or None

    if key == "ABENDL":
        raise EstateRequestFailed("estate share unreachable (simulated)")

    if key == "SUBFEE":
        # Not in the COBOL libraries; it is an assembler module. The probe chain asks
        # `cobol` first and must MISS before asking `asm`.
        if want == "cobol":
            return {"found": False}
        if want in (None, "asm"):
            return _answer(key, _SUBFEE_ASM, detected="asm")
        return {"found": False}

    if key == "PAYAUX":
        # We asked for one thing; the estate knows better and says so.
        return _answer(key, _PAYAUX, detected="copybook")

    table = {
        "SQLCA": (_SQLCA, "copybook", None),
        "CUSTREC": (_CUSTREC, "copybook", None),
        "CMQV": (_CMQV, "copybook",
                 ["SYS2.MQM.COPYLIB(CMQV)", "TEST.COPYLIB(CMQV)"]),
        "USEMQ": (_USEMQ, "cobol", None),
    }
    hit = table.get(key)
    if hit is None:
        return {"found": False}
    text, detected, alternatives = hit
    return _answer(key, text, detected=detected, alternatives=alternatives)


def _answer(key, text, detected=None, alternatives=None):
    out = {
        "artifact_name": key,
        "found": True,
        "text": text,
        "source_location": f"PROD.SRCLIB({key})",
    }
    if detected:
        out["detected_type"] = detected
    if alternatives:
        out["alternatives"] = list(alternatives)
    return out
