      *****************************************************************
      * CALLTWICE - one program called more than once, spelled once.
      *
      * MQINQ is called twice with different operand lists, so the name
      * registry keys them as two statements (call_MQINQ, call_MQINQ_2).
      * Both name the SAME program, so the perimeter must show one
      * endpoint - a "MQINQ_2" would be a load module that exists
      * nowhere, and would be classified, manifested and fetched as if
      * it did.
      *
      * The dynamic CALL is here for the same reason from the other
      * direction: its endpoint comes from the resolved literal, and it
      * is also called twice.
      *****************************************************************
       IDENTIFICATION DIVISION.
       PROGRAM-ID. CALLTWICE.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-HCONN     PIC S9(9) COMP VALUE 0.
       01  WS-HOBJ      PIC S9(9) COMP VALUE 0.
       01  WS-SELECTOR  PIC S9(9) COMP VALUE 0.
       01  WS-DEPTH     PIC S9(9) COMP VALUE 0.
       01  WS-BACKOUT   PIC S9(9) COMP VALUE 0.
       01  WS-REASON    PIC S9(9) COMP VALUE 0.
       01  WS-LOGPGM    PIC X(8)       VALUE 'POSTLOG '.
       01  WS-TRAN      PIC X(4)       VALUE 'DEP '.
       01  WS-AMT       PIC 9(7)V99    VALUE 0.
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM 1000-INQUIRE
           PERFORM 2000-LOG
           STOP RUN.

      * Two MQI inquiries, different operands - two statements, one
      * program.
       1000-INQUIRE.
           CALL 'MQINQ' USING WS-HCONN WS-HOBJ WS-SELECTOR WS-DEPTH
           CALL 'MQINQ' USING WS-HCONN WS-HOBJ WS-SELECTOR WS-BACKOUT
           CALL 'MQCMIT' USING WS-HCONN WS-REASON.

      * A dynamic target proved constant, also called twice.
       2000-LOG.
           CALL WS-LOGPGM USING WS-TRAN
           CALL WS-LOGPGM USING WS-AMT.
