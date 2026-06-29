      *================================================================*
      * THRURANGE - PERFORM p THRU q runs paragraphs p..q (in source    *
      * order) then returns. Here PERFORM 1000-A THRU 3000-C runs A,B,C *
      * once: WS-N -> 100 + 20 + 3 = 123.                                *
      *================================================================*
       IDENTIFICATION DIVISION.
       PROGRAM-ID. THRURANGE.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-N      PIC 9(4) VALUE 0.
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM 1000-A THRU 3000-C
           STOP RUN.
       1000-A.
           ADD 100 TO WS-N.
       2000-B.
           ADD 20 TO WS-N.
       3000-C.
           ADD 3 TO WS-N.
