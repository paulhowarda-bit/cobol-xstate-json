       IDENTIFICATION DIVISION.
       PROGRAM-ID. CALLFILT.
      *----------------------------------------------------------------
      * Literals that reach a dynamic target but cannot be the name it
      * invokes. Each is published as a rejected candidate, never as a
      * candidate, and never promotes the names that remain:
      *
      *   WS-TO-PGM      a 25-byte message moved into an 8-byte item
      *                  (length) beside two real names -> both stay
      *   WS-LNK-TARGET  9 bytes, but CICS passes 8 of a PROGRAM()
      *                  operand -> resolves to its first 8 characters
      *   WS-TMPL-PGM    a wildcard template VALUE; its 88-level names
      *                  the real programs -> declared-88 candidates
      *   WS-PH-PGM      a placeholder completed at run time ->
      *                  nothing admissible, so no candidate at all
      *   WS-DSP-PGM     a filler initialiser, then one of two real
      *                  names -> the filler goes, both names stay
      *----------------------------------------------------------------
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-TO-PGM          PIC X(08) VALUE SPACES.
       01  WS-DONE-MSG        PIC X(25)
                              VALUE 'RUN COMPLETED NORMALLY OK'.
       01  WS-LNK-TARGET      PIC X(09) VALUE 'ABC40001C'.
       01  WS-TMPL-PGM        PIC X(08) VALUE 'ABCDE***'.
           88  WS-TMPL-GOOD             VALUE 'ABCDE200' 'ABCDE300'.
       01  WS-PH-PGM          PIC X(08).
       01  WS-PH-SUFFIX       PIC X(02).
       01  WS-DSP-PGM         PIC X(08) VALUE 'ZZZZZZZZ'.
       01  WS-DSP-PGM-ONE     PIC X(08) VALUE 'PGMDSP0A'.
       01  WS-DSP-PGM-TWO     PIC X(08) VALUE 'PGMDSP0B'.
       01  WS-FLAG            PIC X.
       01  WS-COMM            PIC X(20).
       PROCEDURE DIVISION.
       0000-MAIN.
           IF WS-FLAG = 'A'
               MOVE 'PGMTO001' TO WS-TO-PGM
           ELSE
               MOVE 'PGMTO002' TO WS-TO-PGM
           END-IF
           MOVE WS-DONE-MSG TO WS-TO-PGM
           EXEC CICS LINK PROGRAM(WS-LNK-TARGET) COMMAREA(WS-COMM)
           END-EXEC
           CALL WS-TMPL-PGM USING WS-COMM
           MOVE 'ABCD??X' TO WS-PH-PGM
           MOVE WS-PH-SUFFIX TO WS-PH-PGM(5:2)
           CALL WS-PH-PGM USING WS-COMM
           IF WS-FLAG = 'C'
               MOVE WS-DSP-PGM-ONE TO WS-DSP-PGM
           ELSE
               MOVE WS-DSP-PGM-TWO TO WS-DSP-PGM
           END-IF
           CALL WS-DSP-PGM USING WS-COMM
           EXEC CICS XCTL PROGRAM(WS-TO-PGM) COMMAREA(WS-COMM)
           END-EXEC
           GOBACK.
