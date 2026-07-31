       IDENTIFICATION DIVISION.
       PROGRAM-ID. LINEAGE.
      *================================================================*
      * Lineage fixture. The caller passes LK-CUST/LK-QTY; the program *
      * ACCEPTs a rate, CALLs a subprogram BY REFERENCE (which may     *
      * rewrite its argument), STRINGs two fields together, and writes *
      * a file. Every row of --target lineage is hand-checkable:       *
      *   OUT-NAME  <- LK-CUST        (caller, via WS-NAME)            *
      *   OUT-FEE   <- LK-QTY, WS-RATE (caller + console)              *
      *   OUT-MEMO  <- STRING of WS-NAME + WS-REF (dependency only)    *
      *   WS-REF    <- maybe rewritten by SUBFEE (BY REFERENCE)        *
      *================================================================*
       ENVIRONMENT DIVISION.
       INPUT-OUTPUT SECTION.
       FILE-CONTROL.
           SELECT OUT-FILE ASSIGN TO OUTDD.
       DATA DIVISION.
       FILE SECTION.
       FD  OUT-FILE.
       01  OUT-REC.
           05  OUT-NAME    PIC X(20).
           05  OUT-FEE     PIC 9(5)V99.
           05  OUT-MEMO    PIC X(40).
       WORKING-STORAGE SECTION.
       01  WS-NAME         PIC X(20).
       01  WS-RATE         PIC 9(3)V99.
       01  WS-REF          PIC X(10).
       01  WS-MEMO         PIC X(40).
       LINKAGE SECTION.
       01  LK-PARM.
           05  LK-CUST     PIC X(20).
           05  LK-QTY      PIC 9(3).
       PROCEDURE DIVISION USING LK-PARM.
       0000-MAIN.
           ACCEPT WS-RATE
           MOVE LK-CUST TO WS-NAME
           PERFORM 1000-BUILD
           WRITE OUT-REC
           STOP RUN.
       1000-BUILD.
           MOVE WS-NAME TO OUT-NAME
           COMPUTE OUT-FEE = LK-QTY * WS-RATE
           CALL 'SUBFEE' USING WS-REF
           STRING WS-NAME WS-REF DELIMITED BY SIZE INTO WS-MEMO
           END-STRING
           MOVE WS-MEMO TO OUT-MEMO.
