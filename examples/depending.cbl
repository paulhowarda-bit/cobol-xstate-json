       IDENTIFICATION DIVISION.
       PROGRAM-ID. DEPENDING.
      * GO TO ... DEPENDING ON: the index variable selects the target
      * with a real guard (var = i), executable at run time.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-BRANCH       PIC 9 VALUE 2.
       01  WS-R            PIC X VALUE ' '.
       PROCEDURE DIVISION.
       0000-MAIN.
           GO TO 1000-A 2000-B 3000-C DEPENDING ON WS-BRANCH.
       0010-FALL.
           MOVE 'F' TO WS-R
           STOP RUN.
       1000-A.
           MOVE 'A' TO WS-R
           STOP RUN.
       2000-B.
           MOVE 'B' TO WS-R
           STOP RUN.
       3000-C.
           MOVE 'C' TO WS-R
           STOP RUN.
