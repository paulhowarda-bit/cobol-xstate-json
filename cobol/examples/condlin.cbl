       IDENTIFICATION DIVISION.
       PROGRAM-ID. CONDLIN.
      *================================================================*
      * Guard conditions in the lineage table. "Where did this value   *
      * come from" is only half a business rule; the other half is     *
      * "under what condition". Every shape that must be got right:    *
      *   1000  a plain guarded write - conditions = [ACTIVE]          *
      *   2000  IF/ELSE that RECONVERGES - the write after it is       *
      *         unconditional; A and NOT A must cancel, not stack up   *
      *   3000  WHEN OTHER - carries no guard of its own, so its real  *
      *         condition is the NEGATION of every WHEN before it      *
      *   4000  the same paragraph performed from two guarded sites -  *
      *         reached under a DISJUNCTION, which a conjunction       *
      *         cannot state: MUST is empty and it must say so         *
      *         rather than read as "unconditional"                    *
      *================================================================*
       ENVIRONMENT DIVISION.
       INPUT-OUTPUT SECTION.
       FILE-CONTROL.
           SELECT OUT-FILE ASSIGN TO OUTF.
       DATA DIVISION.
       FILE SECTION.
       FD  OUT-FILE.
       01  OUT-REC.
           05  OUT-NAME    PIC X(20).
           05  OUT-CODE    PIC X(4).
       WORKING-STORAGE SECTION.
       01  WS-STATUS       PIC X.
           88  CUST-ACTIVE VALUE 'A'.
       01  WS-KIND         PIC X.
       01  WS-FLAG-A       PIC X.
       01  WS-FLAG-B       PIC X.
       01  WS-NAME         PIC X(20).
       PROCEDURE DIVISION.
       0000-MAIN.
           OPEN OUTPUT OUT-FILE
           ACCEPT WS-STATUS
           ACCEPT WS-KIND
           ACCEPT WS-FLAG-A
           ACCEPT WS-FLAG-B
           ACCEPT WS-NAME
           PERFORM 1000-GUARDED
           PERFORM 2000-REJOIN
           PERFORM 3000-OTHER
           PERFORM 4000-TWOSITE
           CLOSE OUT-FILE
           STOP RUN.
      *---- a guarded write: WRITE happens only when CUST-ACTIVE ------*
       1000-GUARDED.
           IF CUST-ACTIVE
               MOVE WS-NAME TO OUT-NAME
               MOVE 'ACTV' TO OUT-CODE
               WRITE OUT-REC
           END-IF.
      *---- IF/ELSE rejoins: the WRITE after it always happens --------*
       2000-REJOIN.
           IF WS-KIND = 'X'
               MOVE 'KNDX' TO OUT-CODE
           ELSE
               MOVE 'KNDY' TO OUT-CODE
           END-IF
           WRITE OUT-REC.
      *---- WHEN OTHER: condition is NOT 'P' AND NOT 'Q' --------------*
       3000-OTHER.
           EVALUATE WS-KIND
               WHEN 'P'   MOVE 'ISP ' TO OUT-CODE
               WHEN 'Q'   MOVE 'ISQ ' TO OUT-CODE
               WHEN OTHER MOVE 'NONE' TO OUT-CODE
                          WRITE OUT-REC
           END-EVALUATE.
      *---- one paragraph, two guarded call sites: a disjunction ------*
       4000-TWOSITE.
           IF WS-FLAG-A = 'Y'
               PERFORM 4900-EMIT
           END-IF
           IF WS-FLAG-B = 'Y'
               PERFORM 4900-EMIT
           END-IF.
       4900-EMIT.
           MOVE 'BOTH' TO OUT-CODE
           WRITE OUT-REC.
