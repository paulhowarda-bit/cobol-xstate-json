       IDENTIFICATION DIVISION.
       PROGRAM-ID. CALLGRP.
      *================================================================*
      * CALL through a GROUP: a group has no VALUE of its own, but its *
      * bytes are its subordinates', and CALL passes the first eight.  *
      *   WS-XPGMA400   - one X(08) VALUE spells the name: resolved    *
      *   XQ2K-XQSD2FSW - three FILLERs spell it together: resolved    *
      *   WS-ERR-MSG    - a message area whose first field receives a  *
      *                   table name: a store, so it stays flagged     *
      *================================================================*
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-XPGMA400.
           05  WS-XPGMA400-CALL      PIC X(08) VALUE 'XPGMA400'.
           05  WS-XPGMA400-FLAG      PIC X(01) VALUE 'N'.
       01  XQ2K-DATE-PARMS.
           05  XQ2K-XQSD2FSW.
               10  FILLER            PIC X(05) VALUE 'XQSD2'.
               10  FILLER            PIC X     VALUE 'F'.
               10  FILLER            PIC XX    VALUE 'SW'.
           05  XQ2K-DATE             PIC X(10) VALUE SPACES.
       01  WS-ERR-MSG.
           05  WS-ERR-MSG-TEXT       PIC X(20) VALUE ' '.
           05  WS-ERR-MSG-CODE       PIC S9(4) COMP VALUE 0.
       01  WS-PARMS                  PIC X(20).
       PROCEDURE DIVISION.
       0000-MAIN.
           MOVE 'Y' TO WS-XPGMA400-FLAG
           CALL WS-XPGMA400 USING WS-PARMS
           CALL XQ2K-XQSD2FSW USING WS-PARMS
           MOVE 'ACCT_TABLE' TO WS-ERR-MSG-TEXT
           CALL WS-ERR-MSG USING WS-PARMS
           GOBACK.
