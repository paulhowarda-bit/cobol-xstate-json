       IDENTIFICATION DIVISION.
       PROGRAM-ID. CALLCHAIN.
      *----------------------------------------------------------------
      * Dynamic CALL targets set from ANOTHER data item rather than
      * from a literal. The literal is still the only value the program
      * ever puts there, so the chain is followed to it:
      *
      *   WS-HOP1   <- WS-SRC ('CHAINA')                  resolves
      *   WS-HOP2   <- WS-HOP1 <- WS-SRC                  resolves
      *   WS-MIXED  <- WS-SRC, and also <- WS-UNSET       stays flagged
      *   WS-RING   <- WS-LOOP <- WS-RING                 terminates
      *
      * WS-UNSET is declared and nothing in this program assigns it, so
      * what it holds at run time is not a value this analysis can
      * name: the literal reaching WS-MIXED is a candidate, not an
      * answer. The ring is the cycle case - walking it must stop, and
      * with no literal anywhere in it the target stays runtime-
      * determined.
      *----------------------------------------------------------------
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-SRC             PIC X(08) VALUE 'CHAINA'.
       01  WS-HOP1            PIC X(08).
       01  WS-HOP2            PIC X(08).
       01  WS-MIXED           PIC X(08).
       01  WS-UNSET           PIC X(08).
       01  WS-RING            PIC X(08).
       01  WS-LOOP            PIC X(08).
       01  WS-LINK-REC        PIC X(10).
       PROCEDURE DIVISION.
       0000-MAIN.
           MOVE WS-SRC TO WS-HOP1
           MOVE WS-HOP1 TO WS-HOP2
           MOVE WS-SRC TO WS-MIXED
           MOVE WS-UNSET TO WS-MIXED
           MOVE WS-RING TO WS-LOOP
           MOVE WS-LOOP TO WS-RING
           CALL WS-HOP1 USING WS-LINK-REC
           CALL WS-HOP2 USING WS-LINK-REC
           CALL WS-MIXED USING WS-LINK-REC
           CALL WS-RING USING WS-LINK-REC
           GOBACK.
