       IDENTIFICATION DIVISION.
       PROGRAM-ID. DUPNAMES.
      *----------------------------------------------------------------
      * One data name declared in several records (ledger item 154).
      * ORD-N and CUST-REF-N are declared under IN-REC, ORD-ENTRY and
      * (CUST-REF-N) ORD-INFO. The bundle's `data` keys a name once -
      * its last declaration - so every declaration is listed under
      * `declarations`, and each record keeps its own members: the
      * READ fills IN-REC's, the group MOVE is placed byte by byte, and
      * ORD-INFO lays out in its own order.
      *----------------------------------------------------------------
       ENVIRONMENT DIVISION.
       INPUT-OUTPUT SECTION.
       FILE-CONTROL.
           SELECT IN-FILE ASSIGN TO INFILE.
       DATA DIVISION.
       FILE SECTION.
       FD  IN-FILE.
       01  IN-REC.
           05  ORD-N               PIC X(10).
           05  CUST-REF-N          PIC X(10).
       WORKING-STORAGE SECTION.
       01  ORD-ENTRY.
           05  ORD-N               PIC X(10).
           05  CUST-REF-N          PIC X(10).
       01  ORD-INFO.
           05  INFO-ORD            PIC X(10).
           05  CUST-REF-N          PIC X(10).
       01  WS-OUT                  PIC X(10).
       PROCEDURE DIVISION.
       0000-MAIN.
           OPEN INPUT IN-FILE
           READ IN-FILE
           MOVE IN-REC TO ORD-ENTRY
           MOVE CUST-REF-N OF ORD-ENTRY TO WS-OUT
           DISPLAY WS-OUT
           CLOSE IN-FILE
           GOBACK.
