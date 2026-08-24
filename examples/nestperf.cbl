      *================================================================*
      * NESTPERF - nested PERFORM: 1000-OUTER performs 2000-INNER, so  *
      * WORKING-STORAGE must thread through two levels of call-return.  *
      * WS-SUM -> 10 (outer) + 1 (inner) = 11.                          *
      *================================================================*
       IDENTIFICATION DIVISION.
       PROGRAM-ID. NESTPERF.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-SUM    PIC 9(6) VALUE ZERO.
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM 1000-OUTER
           STOP RUN.
       1000-OUTER.
           ADD 10 TO WS-SUM
           PERFORM 2000-INNER.
       2000-INNER.
           ADD 1 TO WS-SUM.
