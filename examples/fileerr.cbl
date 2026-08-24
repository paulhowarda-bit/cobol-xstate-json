      *================================================================*
      * FILEERR - DECLARATIVES USE AFTER ERROR. The USE procedure is an *
      * orthogonal handler: it is NOT in the main flow; it runs only when*
      * an I/O error occurs on CUST-FILE. Modeled as a parallel HANDLERS *
      * region that watches IO.ERROR.CUST-FILE and performs the handler. *
      *================================================================*
       IDENTIFICATION DIVISION.
       PROGRAM-ID. FILEERR.
       ENVIRONMENT DIVISION.
       INPUT-OUTPUT SECTION.
       FILE-CONTROL.
           SELECT CUST-FILE ASSIGN TO CUSTIN
               ORGANIZATION IS SEQUENTIAL.
       DATA DIVISION.
       FILE SECTION.
       FD  CUST-FILE.
       01  CUST-REC          PIC X(80).
       WORKING-STORAGE SECTION.
       01  WS-ERR-COUNT      PIC 9(3) VALUE 0.
       PROCEDURE DIVISION.
       DECLARATIVES.
       IO-ERR SECTION.
           USE AFTER STANDARD ERROR PROCEDURE ON CUST-FILE.
       IO-ERR-HANDLER.
           ADD 1 TO WS-ERR-COUNT.
       END DECLARATIVES.
       0000-MAIN.
           OPEN INPUT CUST-FILE
           CLOSE CUST-FILE
           STOP RUN.
