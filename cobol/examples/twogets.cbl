       IDENTIFICATION DIVISION.
       PROGRAM-ID. TWOGETS.
      *================================================================*
      * Two reads folded into ONE state. A state can only wait for one *
      * event at a time, so the reactive lowering must split the run   *
      * and wait twice - otherwise the second ACCEPT is silently lost. *
      *================================================================*
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-A            PIC X(4).
       01  WS-B            PIC X(4).
       01  WS-BOTH         PIC X(8).
       PROCEDURE DIVISION.
       0000-MAIN.
           ACCEPT WS-A
           ACCEPT WS-B
           MOVE WS-A TO WS-BOTH
           STOP RUN.
