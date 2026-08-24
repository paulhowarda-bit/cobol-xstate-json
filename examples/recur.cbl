       IDENTIFICATION DIVISION.
       PROGRAM-ID. RECUR.
      *================================================================*
      * Mutually recursive PERFORM. The reactive target flattens calls *
      * into ONE machine with a return-address field per paragraph, so *
      * a re-entrant call would overwrite the address and return to the*
      * wrong place. It must refuse, not emit a wrong machine.         *
      * (--target js can express this: its actors are separate copies.)*
      *================================================================*
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-D            PIC 9 VALUE 0.
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM 1000-A
           STOP RUN.
       1000-A.
           ADD 1 TO WS-D
           IF WS-D < 3
               PERFORM 2000-B
           END-IF.
       2000-B.
           PERFORM 1000-A.
