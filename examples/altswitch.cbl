      *================================================================*
      * ALTSWITCH - the classic ALTER first-time-switch idiom, plus a  *
      * genuinely runtime-determined dynamic CALL.                     *
      *   * 1000-SWITCH is a one-line GO TO whose exit is ALTERed, so   *
      *     it is drawn as a context-driven guard set (modeled, but     *
      *     flagged as runtime-switched - the active target is data).   *
      *   * CALL WS-PGM is set from another variable, so it stays       *
      *     flagged: the target cannot be proven constant.             *
      *================================================================*
       IDENTIFICATION DIVISION.
       PROGRAM-ID. ALTSWITCH.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-PGM          PIC X(8).
       01  WS-ROUTE        PIC X(8).
       01  WS-CNT          PIC 9 VALUE 0.
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM 2000-CYCLE 3 TIMES
           STOP RUN.
       1000-SWITCH.
           GO TO 1100-FIRST.
       1100-FIRST.
           DISPLAY 'FIRST TIME SETUP'
           ALTER 1000-SWITCH TO PROCEED TO 1200-NORMAL
           GO TO 1900-DONE.
       1200-NORMAL.
           DISPLAY 'STEADY STATE'
           GO TO 1900-DONE.
       1900-DONE.
           EXIT.
       2000-CYCLE.
           PERFORM 1000-SWITCH THRU 1900-DONE
           MOVE WS-ROUTE TO WS-PGM
           CALL WS-PGM USING WS-CNT.
