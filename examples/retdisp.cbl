       IDENTIFICATION DIVISION.
       PROGRAM-ID. RETDISP.
      *================================================================*
      * Return-address dispatch. 9000-BUMP is PERFORMed from TWO sites,*
      * so a flattened machine must remember which one to come back to *
      * (every other fixture is single-site, where a wrong return is    *
      * indistinguishable from a right one). 2000-B is performed both  *
      * alone AND inside the 1000-A THRU 3000-C range, so its two       *
      * inlined copies must stay disjoint.                             *
      *   WS-N: 0 +1(BUMP) =1 *10 =10 +1(BUMP) =11 +100(A) =111        *
      *         +1(BUMP via B in range) =112 +2(C) =114                *
      *================================================================*
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-N            PIC S9(5) VALUE 0.
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM 9000-BUMP
           MULTIPLY 10 BY WS-N
           PERFORM 9000-BUMP
           PERFORM 1000-A THRU 3000-C
           STOP RUN.
       1000-A.
           ADD 100 TO WS-N.
       2000-B.
           PERFORM 9000-BUMP.
       3000-C.
           ADD 2 TO WS-N.
       9000-BUMP.
           ADD 1 TO WS-N.
