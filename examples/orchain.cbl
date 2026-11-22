      *================================================================*
      * ORCHAIN - a run of ORs, and a run of ANDs, is one condition    *
      * node holding every term; a parenthesized group stays its own.  *
      * No file I/O, so it runs end to end under stock XState:         *
      * WS-HIT -> 2, WS-BOTH -> 1, WS-MISS -> 0.                       *
      *================================================================*
       IDENTIFICATION DIVISION.
       PROGRAM-ID. ORCHAIN.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-A      PIC 9 VALUE 3.
       01  WS-B      PIC 9 VALUE 2.
       01  WS-HIT    PIC 9 VALUE ZERO.
       01  WS-BOTH   PIC 9 VALUE ZERO.
       01  WS-MISS   PIC 9 VALUE ZERO.
       PROCEDURE DIVISION.
       0000-MAIN.
           IF WS-A = 1 OR 2 OR 3
               MOVE 1 TO WS-HIT
           END-IF
           IF WS-A > 0 AND WS-A < 5 AND WS-B = 2
               MOVE 1 TO WS-BOTH
           END-IF
           IF WS-A = 7 OR WS-B = 7 OR WS-A = 8
               MOVE 1 TO WS-MISS
           END-IF
           IF (WS-A = 3 OR WS-B = 9) AND WS-B = 2 AND WS-A NOT = 4
               ADD 1 TO WS-HIT
           END-IF
           STOP RUN.
