      *================================================================*
      * NESTDEPS - what a CONTAINED program touches is a dependency of  *
      * this member (ledger item 55). NESTSQL calls EXTMOD, calls       *
      * INNERMOD through its OWN WS-TARGET (the outer one says          *
      * OUTERMOD), and reads ACCOUNT_MASTER. NESTLOG, nested one level  *
      * deeper, writes LOGDD and calls what its caller passes - only    *
      * this source calls it: NESTSQL passes 'LOGWRITE'. A CALL to a    *
      * program contained here stays internal, never fetched.           *
      *================================================================*
       IDENTIFICATION DIVISION.
       PROGRAM-ID. NESTDEPS.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-TARGET  PIC X(8) VALUE 'OUTERMOD'.
       PROCEDURE DIVISION.
       0000-MAIN.
           CALL 'NESTSQL'
           CALL WS-TARGET
           GOBACK.
       IDENTIFICATION DIVISION.
       PROGRAM-ID. NESTSQL.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-TARGET  PIC X(8) VALUE 'INNERMOD'.
       01  WS-ACCT    PIC X(10) VALUE 'A000000001'.
       01  WS-BAL     PIC S9(9)V99 COMP-3.
       01  WS-LOGPGM  PIC X(8) VALUE 'LOGWRITE'.
       PROCEDURE DIVISION.
       1000-INNER.
           CALL 'EXTMOD' USING WS-ACCT
           CALL WS-TARGET USING WS-ACCT
           EXEC SQL
               SELECT BALANCE
                 INTO :WS-BAL
                 FROM ACCOUNT_MASTER
                WHERE ACCT_ID = :WS-ACCT
           END-EXEC
           CALL 'NESTLOG' USING WS-LOGPGM
           GOBACK.
       IDENTIFICATION DIVISION.
       PROGRAM-ID. NESTLOG.
       ENVIRONMENT DIVISION.
       INPUT-OUTPUT SECTION.
       FILE-CONTROL.
           SELECT LOG-FILE ASSIGN TO LOGDD.
       DATA DIVISION.
       FILE SECTION.
       FD  LOG-FILE.
       01  LOG-REC    PIC X(80).
       LINKAGE SECTION.
       01  LK-PGM     PIC X(8).
       PROCEDURE DIVISION USING LK-PGM.
       2000-LOG.
           OPEN OUTPUT LOG-FILE
           MOVE 'LOGGED' TO LOG-REC
           WRITE LOG-REC
           CLOSE LOG-FILE
           CALL LK-PGM
           GOBACK.
       END PROGRAM NESTLOG.
       END PROGRAM NESTSQL.
       END PROGRAM NESTDEPS.
