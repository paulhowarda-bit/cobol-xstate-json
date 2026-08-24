      * READ f INTO ws-rec under the reactive (push) target: the arriving record's
      * elementary fields must reach context so the per-record processing derives from
      * THIS record. The file analogue of readproc.cbl's SQL SELECT INTO - drivable only
      * now that a file READ event carries the INTO record's LEAVES (WS-KEY / WS-AMT), the
      * way SQL host variables always have. The FD record is one opaque X(13) field; the
      * INTO target reinterprets those bytes as key + amount, so the event must carry the
      * INTO record's leaves, NOT the FD record's.
       IDENTIFICATION DIVISION.
       PROGRAM-ID. READINTO.
       ENVIRONMENT DIVISION.
       INPUT-OUTPUT SECTION.
       FILE-CONTROL.
           SELECT IN-FILE ASSIGN TO INDD.
       DATA DIVISION.
       FILE SECTION.
       FD  IN-FILE.
       01  IN-REC          PIC X(13).
       WORKING-STORAGE SECTION.
       01  WS-REC.
           05  WS-KEY      PIC X(8).
           05  WS-AMT      PIC 9(5).
       01  WS-EOF          PIC X       VALUE 'N'.
       01  WS-CNT          PIC 9(3)    VALUE 0.
       01  WS-SUM          PIC 9(7)    VALUE 0.
       01  OUT-KEY         PIC X(8).
       PROCEDURE DIVISION.
       0000-MAIN.
           OPEN INPUT IN-FILE
           PERFORM UNTIL WS-EOF = 'Y'
               READ IN-FILE INTO WS-REC
                   AT END MOVE 'Y' TO WS-EOF
                   NOT AT END
                       ADD 1 TO WS-CNT
                       ADD WS-AMT TO WS-SUM
                       MOVE WS-KEY TO OUT-KEY
               END-READ
           END-PERFORM
           CLOSE IN-FILE
           STOP RUN.
