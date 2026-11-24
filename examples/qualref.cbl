       IDENTIFICATION DIVISION.
       PROGRAM-ID. QUALREF.
      *----------------------------------------------------------------
      * Qualified references X OF G (ledger item 155). The words after
      * OF/IN say WHICH X; they are not further operands. Read as
      * operands they made `MOVE A TO B OF H C` assign B, OF, H and C,
      * the qualifying record a source, and INITIALIZE X OF R clear R.
      *----------------------------------------------------------------
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  ACC-MSTR.
           05  ACC-OPEN-D          PIC X(10).
       01  OTHER-REC.
           05  ACC-OPEN-D          PIC X(10).
       01  OPEN-DATE               PIC X(10).
       01  R.
           05  CNT                 PIC 9(4).
           05  X                   PIC X(10).
       01  H.
           05  B                   PIC X(10).
       01  C                       PIC X(10).
       01  A                       PIC X(10).
       01  N                       PIC 9(4).
       01  WS-LEN                  PIC 9(4).
       PROCEDURE DIVISION.
       0000-MAIN.
           ACCEPT A
           ACCEPT N
           ACCEPT ACC-MSTR
           MOVE ACC-OPEN-D OF ACC-MSTR TO OPEN-DATE
           DISPLAY ACC-OPEN-D OF OTHER-REC
           MOVE A TO B OF H C
           ADD N TO CNT OF R
           INITIALIZE X OF R
           MOVE LENGTH OF R TO WS-LEN
           CALL 'SUBP' USING X OF R
           DISPLAY OPEN-DATE B C CNT WS-LEN
           GOBACK.
