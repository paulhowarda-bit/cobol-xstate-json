       IDENTIFICATION DIVISION.
       PROGRAM-ID. SECTPERF.
      * PERFORM of a SECTION runs the whole section extent (all member
      * paragraphs), then returns. PERFORM a THRU a-section spans too.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-A            PIC 9(4) VALUE 0.
       01  WS-B            PIC 9(4) VALUE 0.
       PROCEDURE DIVISION.
       0000-MAIN SECTION.
       0010-DRIVE.
           PERFORM 1000-CALC
           PERFORM 2000-POST
           STOP RUN.
       1000-CALC SECTION.
       1010-STEP1.
           ADD 5 TO WS-A.
       1020-STEP2.
           ADD 7 TO WS-A.
       2000-POST SECTION.
       2010-ONLY.
           MOVE WS-A TO WS-B.
