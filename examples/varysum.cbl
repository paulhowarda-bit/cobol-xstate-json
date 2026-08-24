       IDENTIFICATION DIVISION.
       PROGRAM-ID. VARYSUM.
      *AUTHORS.    cobol-xstate example.
      * PERFORM ... VARYING: the control variable is initialized, the body
      * runs, then the variable is stepped (var := var + by) and re-tested.
      * Sums 1..5 into WS-SUM (= 15); WS-I ends at 6.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01 WS-I    PIC 99   VALUE 0.
       01 WS-SUM  PIC 999  VALUE 0.
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM 1000-STEP VARYING WS-I FROM 1 BY 1 UNTIL WS-I > 5
           STOP RUN.
       1000-STEP.
           ADD WS-I TO WS-SUM.
