      *================================================================*
      * SQLUNLD - the canonical Db2 -> file UNLOAD: declare a cursor,    *
      * OPEN it, FETCH each row in a loop until SQLCODE 100, WRITE each  *
      * to a sequential output file, CLOSE. The interface overlay should*
      * show a Db2 GET (the FETCH) + a file CREATE (the WRITE) in the    *
      * same flow = an unload. Flat / single region (inline PERFORM).    *
      *================================================================*
       IDENTIFICATION DIVISION.
       PROGRAM-ID. SQLUNLD.
       ENVIRONMENT DIVISION.
       INPUT-OUTPUT SECTION.
       FILE-CONTROL.
           SELECT OUT-FILE ASSIGN TO OUTDD
               ORGANIZATION IS SEQUENTIAL.
       DATA DIVISION.
       FILE SECTION.
       FD  OUT-FILE.
       01  OUT-REC.
           05  OUT-ID      PIC 9(5).
           05  OUT-NAME    PIC X(20).
           05  OUT-BAL     PIC S9(7)V99.
       WORKING-STORAGE SECTION.
       01  WS-ID           PIC 9(5).
       01  WS-NAME         PIC X(20).
       01  WS-BAL          PIC S9(7)V99 COMP-3.
       01  WS-DONE         PIC X VALUE 'N'.
       01  SQLCODE         PIC S9(9) COMP VALUE 0.
       PROCEDURE DIVISION.
       0000-MAIN.
           EXEC SQL
               DECLARE C1 CURSOR FOR
                   SELECT ID, NAME, BALANCE FROM ACCOUNT
                   WHERE STATUS = 'A'
           END-EXEC
           OPEN OUTPUT OUT-FILE
           EXEC SQL OPEN C1 END-EXEC
           PERFORM UNTIL WS-DONE = 'Y'
               EXEC SQL
                   FETCH C1 INTO :WS-ID, :WS-NAME, :WS-BAL
               END-EXEC
               IF SQLCODE = 100
                   MOVE 'Y' TO WS-DONE
               ELSE
                   MOVE WS-ID   TO OUT-ID
                   MOVE WS-NAME TO OUT-NAME
                   MOVE WS-BAL  TO OUT-BAL
                   WRITE OUT-REC
               END-IF
           END-PERFORM
           EXEC SQL CLOSE C1 END-EXEC
           CLOSE OUT-FILE
           STOP RUN.
