       IDENTIFICATION DIVISION.
       PROGRAM-ID. REDEFALIAS.
      *----------------------------------------------------------------
      * A store carried across a REDEFINES (ledger item 159). ACCEPT
      * C-TIME fills the bytes C-TIME-HOUR renames through a FILLER
      * REDEFINES; a MOVE into F1 reaches G1 of the REC-B alternative
      * (byte for byte: not G2); and a REDEFINES of bytes nothing
      * writes gives its items no origin. A named item under a FILLER
      * group says which FILLER (`parentLine`), and the FILLER lists
      * its named `members`.
      *----------------------------------------------------------------
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-CLOCK.
           05  C-TIME              PIC 9(08).
           05  FILLER REDEFINES C-TIME.
               10  C-TIME-HOUR     PIC 9(02).
               10  C-TIME-MIN      PIC 9(02).
               10  FILLER          PIC 9(04).
           05  C-DATE              PIC 9(08).
           05  FILLER REDEFINES C-DATE.
               10  C-DATE-CC       PIC 9(02).
               10  C-DATE-YY       PIC 9(06).
       01  REC-A.
           05  F1                  PIC X(02).
           05  F2                  PIC X(02).
       01  REC-B REDEFINES REC-A.
           05  G1                  PIC X(02).
           05  G2                  PIC X(02).
       01  NOBODY.
           05  NB-1                PIC X(04).
       01  NOBODY-R REDEFINES NOBODY.
           05  NB-R1               PIC X(02).
           05  NB-R2               PIC X(02).
       01  IN-A                    PIC X(02).
       01  WS-OUT                  PIC X(10).
       01  OUT-G1                  PIC X(02).
       01  OUT-G2                  PIC X(02).
       01  OUT-NB                  PIC X(02).
       PROCEDURE DIVISION.
       0000-MAIN.
           ACCEPT C-TIME FROM TIME
           MOVE C-TIME-HOUR TO WS-OUT
           DISPLAY WS-OUT
           ACCEPT IN-A
           MOVE IN-A TO F1
           MOVE G1 TO OUT-G1
           MOVE G2 TO OUT-G2
           MOVE NB-R1 TO OUT-NB
           DISPLAY OUT-G1 OUT-G2 OUT-NB
           GOBACK.
