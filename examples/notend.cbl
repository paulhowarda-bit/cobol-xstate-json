       IDENTIFICATION DIVISION.
       PROGRAM-ID. NOTEND.
      * READ ... AT END / NOT AT END: the NOT AT END body is the normal
      * per-record path and must run for every record actually read.
       ENVIRONMENT DIVISION.
       INPUT-OUTPUT SECTION.
       FILE-CONTROL.
           SELECT IN-FILE ASSIGN TO INDD.
       DATA DIVISION.
       FILE SECTION.
       FD  IN-FILE.
       01  IN-REC.
           05  IN-AMT      PIC 9(3)V99.
       WORKING-STORAGE SECTION.
       01  WS-EOF          PIC X     VALUE 'N'.
       01  WS-CNT          PIC 9(3)  VALUE 0.
       01  WS-SUM          PIC 9(5)V99 VALUE 0.
       PROCEDURE DIVISION.
       0000-MAIN.
           OPEN INPUT IN-FILE
           PERFORM UNTIL WS-EOF = 'Y'
               READ IN-FILE
                   AT END MOVE 'Y' TO WS-EOF
                   NOT AT END
                       ADD 1 TO WS-CNT
                       ADD IN-AMT TO WS-SUM
               END-READ
           END-PERFORM
           CLOSE IN-FILE
           STOP RUN.
