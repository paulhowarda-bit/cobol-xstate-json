       IDENTIFICATION DIVISION.
       PROGRAM-ID. TIMESEXIT.
      * PERFORM n TIMES with a modeled counter; EXIT PERFORM as a loop
      * break; EXIT PARAGRAPH skipping the rest of a paragraph; stacked
      * WHENs falling into the shared body.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-T           PIC 9(4) VALUE 0.
       01  WS-I           PIC 9(4) VALUE 0.
       01  WS-SKIP        PIC X    VALUE 'Y'.
       01  WS-X           PIC 9    VALUE 1.
       01  WS-R           PIC X    VALUE ' '.
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM 1000-BUMP 3 TIMES
           PERFORM 2000-GUARDED
           PERFORM UNTIL WS-I > 10
               ADD 1 TO WS-I
               IF WS-I = 4
                   EXIT PERFORM
               END-IF
           END-PERFORM
           EVALUATE WS-X
               WHEN 1
               WHEN 2
                   MOVE 'A' TO WS-R
               WHEN OTHER
                   MOVE 'Z' TO WS-R
           END-EVALUATE
           STOP RUN.
       1000-BUMP.
           ADD 2 TO WS-T.
       2000-GUARDED.
           IF WS-SKIP = 'Y'
               EXIT PARAGRAPH
           END-IF
           ADD 100 TO WS-T.
