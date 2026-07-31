      *================================================================*
      * SQLLOAD - the canonical file -> Db2 LOAD: OPEN an input file,    *
      * READ each record in a loop until end-of-file, INSERT it into a   *
      * table, check SQLCODE, CLOSE. The interface overlay should show a *
      * file GET (the READ) + a Db2 CREATE (the INSERT) in the same flow *
      * = a load. Flat / single region (inline PERFORM).                *
      *================================================================*
       IDENTIFICATION DIVISION.
       PROGRAM-ID. SQLLOAD.
       ENVIRONMENT DIVISION.
       INPUT-OUTPUT SECTION.
       FILE-CONTROL.
           SELECT IN-FILE ASSIGN TO INDD
               ORGANIZATION IS SEQUENTIAL.
       DATA DIVISION.
       FILE SECTION.
       FD  IN-FILE.
       01  IN-REC.
           05  IN-ID       PIC 9(5).
           05  IN-NAME     PIC X(20).
           05  IN-BAL      PIC S9(7)V99.
       WORKING-STORAGE SECTION.
       01  WS-EOF          PIC X VALUE 'N'.
       01  WS-COUNT        PIC 9(7) VALUE 0.
       01  SQLCODE         PIC S9(9) COMP VALUE 0.
       PROCEDURE DIVISION.
       0000-MAIN.
           OPEN INPUT IN-FILE
           PERFORM UNTIL WS-EOF = 'Y'
               READ IN-FILE
                   AT END
                       MOVE 'Y' TO WS-EOF
                   NOT AT END
                       EXEC SQL
                           INSERT INTO ACCOUNT
                               (ID, NAME, BALANCE)
                           VALUES (:IN-ID, :IN-NAME, :IN-BAL)
                       END-EXEC
                       IF SQLCODE = 0
                           ADD 1 TO WS-COUNT
                       ELSE
                           DISPLAY 'INSERT FAILED'
                       END-IF
               END-READ
           END-PERFORM
           CLOSE IN-FILE
           STOP RUN.
