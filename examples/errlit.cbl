       IDENTIFICATION DIVISION.
       PROGRAM-ID. ERRLIT.
      *----------------------------------------------------------------
      * Error-message literals that CONTAIN COBOL keywords. The words
      * inside a quoted literal are DATA, not syntax: tearing
      *     MOVE 'CALL TO FRCEMAIL FAILED' TO WS-ERR-MSG
      * at the TO inside the message manufactures a phantom assignment
      * FRCEMAIL := 'CALL' - and the dynamic CALL below then "resolves",
      * confidently, to a program named CALL. FRCEMAIL's real target
      * arrives only via the MOVE 'PGMEMAIL'; the messages must not
      * contribute candidates, targets, or truncated expressions.
      *----------------------------------------------------------------
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  FRCEMAIL          PIC X(08).
       01  WS-PAYPGM         PIC X(08)  VALUE 'PAYCALC'.
       01  WS-ERR-MSG        PIC X(40).
       01  WS-LOG-MSG        PIC X(40).
       PROCEDURE DIVISION.
       0000-MAIN.
           MOVE 'CALL TO FRCEMAIL FAILED' TO WS-ERR-MSG
           MOVE 'UNABLE TO REACH PAYCALC' TO WS-LOG-MSG
           MOVE 'PGMEMAIL' TO FRCEMAIL
           CALL FRCEMAIL
           CALL WS-PAYPGM
           DISPLAY WS-ERR-MSG
           STOP RUN.
