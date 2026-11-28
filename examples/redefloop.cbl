       IDENTIFICATION DIVISION.
       PROGRAM-ID. REDEFLOOP.
      *----------------------------------------------------------------
      * A group store into one alternative of a REDEFINES whose
      * alternatives cannot be placed by byte offset, inside a loop
      * (ledger item 176). REC-W holds a REDEFINES of its own, so
      * every item of REC-V is an alias of every item of REC-W. The
      * MOVE into REC-V is placed field by field from IN-REC, so its
      * three fields carry three different origins, and each REC-W
      * item gains the union of all three; the loop revisits the
      * store with every alias already holding them.
      *----------------------------------------------------------------
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-CNT                  PIC 9(02).
       01  WS-I                    PIC 9(02).
       01  IN-REC.
           05  IN-A                PIC X(02).
           05  IN-B                PIC 9(06).
           05  IN-C                PIC 9(08).
       01  REC-V.
           05  V-A                 PIC X(02).
           05  V-B                 PIC X(06).
           05  V-C                 PIC X(08).
       01  REC-W REDEFINES REC-V.
           05  W-A                 PIC X(03).
           05  W-B                 PIC X(03).
           05  W-B-N REDEFINES W-B PIC 9(03).
           05  W-C                 PIC X(10).
       01  OUT-A                   PIC X(03).
       01  OUT-B                   PIC X(03).
       PROCEDURE DIVISION.
       0000-MAIN.
           ACCEPT WS-CNT
           PERFORM VARYING WS-I FROM 1 BY 1 UNTIL WS-I > WS-CNT
               ACCEPT IN-A
               ACCEPT IN-B FROM DATE
               ACCEPT IN-C FROM TIME
               MOVE IN-REC TO REC-V
               MOVE W-B TO OUT-B
               DISPLAY OUT-B
           END-PERFORM
           MOVE W-A TO OUT-A
           DISPLAY OUT-A
           GOBACK.
