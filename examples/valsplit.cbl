       IDENTIFICATION DIVISION.
       PROGRAM-ID. VALSPLIT.
      *----------------------------------------------------------------
      * Dynamic CALL targets whose VALUE clause does not share a line
      * with the data name. A data description entry runs to its
      * terminating period, and carrying a clause onto the next line
      * needs no continuation indicator - so every layout below is the
      * same declaration as the one-line form, and each CALL resolves.
      *
      * WS-PGM is declared in two groups with two different literals:
      * which one reaches the CALL is not something constant
      * propagation can pin, so it must stay flagged with both
      * candidates - never resolve to whichever copy came last.
      * WS-SAME is declared twice with ONE literal, and still resolves.
      *----------------------------------------------------------------
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-MODULES.
           05  WS-ONELINE     PIC X(08) VALUE 'PROGA'.
           05  WS-VALNEXT     PIC X(08)
                              VALUE 'PROGB'.
           05  WS-PICNEXT
                              PIC X(08) VALUE 'PROGC'.
           05  WS-LITNEXT     PIC X(08) VALUE
                              'PROGD'.
           05  WS-ISNEXT      PIC X(08) VALUE IS
                              'PROGE'.
       01  GRP-A.
           05  WS-PGM         PIC X(08) VALUE 'PROGX'.
           05  WS-SAME        PIC X(08) VALUE 'PROGS'.
       01  GRP-B.
           05  WS-PGM         PIC X(08) VALUE 'PROGY'.
           05  WS-SAME        PIC X(08)
                              VALUE 'PROGS'.
       01  WS-LINK-REC        PIC X(10).
       PROCEDURE DIVISION.
       0000-MAIN.
           CALL WS-ONELINE USING WS-LINK-REC
           CALL WS-VALNEXT USING WS-LINK-REC
           CALL WS-PICNEXT USING WS-LINK-REC
           CALL WS-LITNEXT USING WS-LINK-REC
           CALL WS-ISNEXT USING WS-LINK-REC
           CALL WS-PGM OF GRP-A USING WS-LINK-REC
           CALL WS-SAME OF GRP-B USING WS-LINK-REC
           GOBACK.
