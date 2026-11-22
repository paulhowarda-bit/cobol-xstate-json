       IDENTIFICATION DIVISION.
       PROGRAM-ID. RESTART.
      * Restart-from-step: the run resumes at WS-STEP and performs every
      * step from there through the common exit, so the PERFORM ... THRU
      * ranges overlap - each one holds every later step. STEP-3 THRU
      * STEP-EXIT is performed from two places, and a shared step
      * performs a paragraph of its own.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01 WS-STEP    PIC 9    VALUE 2.
       01 WS-CNT     PIC 9(4) VALUE 0.
       01 WS-AUDIT   PIC 9(4) VALUE 0.
       PROCEDURE DIVISION.
       0000-MAIN.
           EVALUATE WS-STEP
             WHEN 1
               PERFORM STEP-1 THRU STEP-EXIT
             WHEN 2
               PERFORM STEP-2 THRU STEP-EXIT
             WHEN 3
               PERFORM STEP-3 THRU STEP-EXIT
           END-EVALUATE
           PERFORM STEP-3 THRU STEP-EXIT
           STOP RUN.
       STEP-1.
           ADD 1 TO WS-CNT.
       STEP-2.
           ADD 10 TO WS-CNT
           PERFORM 9000-AUDIT.
       STEP-3.
           ADD 100 TO WS-CNT.
       STEP-EXIT.
           EXIT.
       9000-AUDIT.
           ADD 1 TO WS-AUDIT.
