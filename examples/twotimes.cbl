      * Two textually identical PERFORM n TIMES loops. Each has its own synthetic
      * counter, so each loop-exit guard must test a DIFFERENT counter - sharing one
      * (the registry dedups on control text) made the second loop run zero times.
      * Ends WS-A = 5, WS-B = 5.
       IDENTIFICATION DIVISION.
       PROGRAM-ID. TWOTIMES.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01 WS-A PIC 9(4) VALUE 0.
       01 WS-B PIC 9(4) VALUE 0.
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM 5 TIMES
               ADD 1 TO WS-A
           END-PERFORM
           PERFORM 5 TIMES
               ADD 1 TO WS-B
           END-PERFORM
           STOP RUN.
