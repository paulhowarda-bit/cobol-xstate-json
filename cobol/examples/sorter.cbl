      *================================================================*
      * SORTER - SORT with INPUT/OUTPUT PROCEDURE. The compiler runs    *
      * 1000-FILL (releases records), the sort, then 2000-EMIT (returns *
      * them). Modeled as perform INPUT -> sort effect -> perform OUTPUT,*
      * so the two procedures are real call-returns. WS-IN 5, WS-OUT 7.  *
      *================================================================*
       IDENTIFICATION DIVISION.
       PROGRAM-ID. SORTER.
       ENVIRONMENT DIVISION.
       INPUT-OUTPUT SECTION.
       FILE-CONTROL.
           SELECT SORT-FILE ASSIGN TO SORTWK.
       DATA DIVISION.
       FILE SECTION.
       SD  SORT-FILE.
       01  SORT-REC.
           05  S-KEY  PIC 9(3).
       WORKING-STORAGE SECTION.
       01  WS-IN     PIC 9(2) VALUE 0.
       01  WS-OUT    PIC 9(2) VALUE 0.
       PROCEDURE DIVISION.
       0000-MAIN.
           SORT SORT-FILE
               ON ASCENDING KEY S-KEY
               INPUT PROCEDURE IS 1000-FILL
               OUTPUT PROCEDURE IS 2000-EMIT
           STOP RUN.
       1000-FILL.
           ADD 5 TO WS-IN.
       2000-EMIT.
           ADD 7 TO WS-OUT.
