       IDENTIFICATION DIVISION.
       PROGRAM-ID. DIVREM.
      * DIVIDE ... GIVING ... REMAINDER: both receivers are modeled;
      * the remainder reads the stored (truncated) quotient.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-Q            PIC 9(4) VALUE 0.
       01  WS-R            PIC 9(4) VALUE 0.
       PROCEDURE DIVISION.
       0000-MAIN.
           DIVIDE 7 BY 2 GIVING WS-Q REMAINDER WS-R
           STOP RUN.
