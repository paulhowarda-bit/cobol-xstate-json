      *================================================================*
      * TBLSUM - OCCURS table: write five elements by literal subscript,*
      * then sum them with a variable subscript in a PERFORM loop.      *
      * WS-SUM -> 10+20+30+40+50 = 150. Runs end-to-end under XState.    *
      *================================================================*
       IDENTIFICATION DIVISION.
       PROGRAM-ID. TBLSUM.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-I      PIC 9(2) VALUE 0.
       01  WS-SUM    PIC 9(4) VALUE 0.
       01  WS-TBL.
           05  TBL-AMT  PIC 9(3) OCCURS 5.
       PROCEDURE DIVISION.
       0000-MAIN.
           MOVE 10 TO TBL-AMT(1)
           MOVE 20 TO TBL-AMT(2)
           MOVE 30 TO TBL-AMT(3)
           MOVE 40 TO TBL-AMT(4)
           MOVE 50 TO TBL-AMT(5)
           PERFORM 1000-SUM UNTIL WS-I = 5
           STOP RUN.
       1000-SUM.
           ADD 1 TO WS-I
           ADD TBL-AMT(WS-I) TO WS-SUM.
