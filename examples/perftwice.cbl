      * One paragraph performed two ways: plainly, then n TIMES. The name registry
      * appends _2 to the second PERFORM's action, and readers that sliced off the
      * `perform_` prefix took "1000-INIT_2" as the target - a paragraph that does not
      * exist, so that PERFORM silently became a no-op. Both must invoke 1000-INIT.
      * 1000-INIT runs 1 + 3 = 4 times, so WS-A ends 4.
       IDENTIFICATION DIVISION.
       PROGRAM-ID. PERFTWICE.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01 WS-A PIC 9(4) VALUE 0.
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM 1000-INIT
           PERFORM 1000-INIT 3 TIMES
           STOP RUN.
       1000-INIT.
           ADD 1 TO WS-A.
