      *================================================================*
      * SORTGIVE - ledger item 170. A SORT whose INPUT PROCEDURE        *
      * RELEASEs the records it reads from IN-FILE and which GIVES      *
      * WORK-OUT, then reads WORK-OUT back and writes RPT-FILE. Every   *
      * WO-* field the sort gives, every field read back from WORK-OUT, *
      * and every RPT-* field written from them comes from IN-FILE.     *
      *================================================================*
       IDENTIFICATION DIVISION.
       PROGRAM-ID. SORTGIVE.
       ENVIRONMENT DIVISION.
       INPUT-OUTPUT SECTION.
       FILE-CONTROL.
           SELECT IN-FILE   ASSIGN TO INFILE.
           SELECT WORK-OUT  ASSIGN TO WORKFILE.
           SELECT SORT-WORK ASSIGN TO SORTWK.
           SELECT RPT-FILE  ASSIGN TO RPTFILE.
       DATA DIVISION.
       FILE SECTION.
       FD  IN-FILE.
       01  IN-REC.
           05  IN-KEY     PIC X(5).
           05  IN-AMT     PIC 9(7).
       FD  WORK-OUT.
       01  WO-REC.
           05  WO-KEY     PIC X(5).
           05  WO-AMT     PIC 9(7).
       SD  SORT-WORK.
       01  SD-REC.
           05  SD-KEY     PIC X(5).
           05  SD-AMT     PIC 9(7).
       FD  RPT-FILE.
       01  RPT-REC.
           05  RPT-KEY    PIC X(5).
           05  RPT-AMT    PIC 9(7).
       WORKING-STORAGE SECTION.
       01  WS-EOF         PIC X VALUE 'N'.
       01  WS-IN-EOF      PIC X VALUE 'N'.
       PROCEDURE DIVISION.
       0000-MAIN.
           SORT SORT-WORK ON ASCENDING KEY SD-KEY
               INPUT PROCEDURE 1000-IN
               GIVING WORK-OUT
           OPEN INPUT WORK-OUT
           OPEN OUTPUT RPT-FILE
           READ WORK-OUT AT END MOVE 'Y' TO WS-EOF
           END-READ
           PERFORM UNTIL WS-EOF = 'Y'
               MOVE WO-REC TO RPT-REC
               WRITE RPT-REC
               READ WORK-OUT AT END MOVE 'Y' TO WS-EOF
               END-READ
           END-PERFORM
           CLOSE WORK-OUT RPT-FILE
           STOP RUN.
       1000-IN.
           OPEN INPUT IN-FILE
           READ IN-FILE AT END MOVE 'Y' TO WS-IN-EOF
           END-READ
           PERFORM UNTIL WS-IN-EOF = 'Y'
               MOVE IN-REC TO SD-REC
               RELEASE SD-REC
               READ IN-FILE AT END MOVE 'Y' TO WS-IN-EOF
               END-READ
           END-PERFORM
           CLOSE IN-FILE.
