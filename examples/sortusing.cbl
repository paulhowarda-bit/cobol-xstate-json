      *================================================================*
      * SORTUSE - ledger item 170. The file crossings a SORT / MERGE    *
      * makes through USING and GIVING: SRT-AB sorts A-FILE into        *
      * B-FILE; SRT-AP sorts A-FILE into an OUTPUT PROCEDURE that       *
      * RETURNs into WS-X (A-FILE's records, not the sort file's); the  *
      * MERGE of A-FILE and D-FILE gives C-FILE both inputs.            *
      *================================================================*
       IDENTIFICATION DIVISION.
       PROGRAM-ID. SORTUSE.
       ENVIRONMENT DIVISION.
       INPUT-OUTPUT SECTION.
       FILE-CONTROL.
           SELECT A-FILE  ASSIGN TO AFILE.
           SELECT B-FILE  ASSIGN TO BFILE.
           SELECT C-FILE  ASSIGN TO CFILE.
           SELECT D-FILE  ASSIGN TO DFILE.
           SELECT SRT-AB  ASSIGN TO SORTWK1.
           SELECT SRT-AP  ASSIGN TO SORTWK2.
           SELECT MRG-W   ASSIGN TO SORTWK3.
       DATA DIVISION.
       FILE SECTION.
       FD  A-FILE.
       01  A-REC.
           05  A-KEY      PIC X(4).
           05  A-VAL      PIC 9(5).
       FD  B-FILE.
       01  B-REC.
           05  B-KEY      PIC X(4).
           05  B-VAL      PIC 9(5).
       FD  C-FILE.
       01  C-REC.
           05  C-KEY      PIC X(4).
           05  C-VAL      PIC 9(5).
       FD  D-FILE.
       01  D-REC.
           05  D-KEY      PIC X(4).
           05  D-VAL      PIC 9(5).
       SD  SRT-AB.
       01  AB-REC.
           05  AB-KEY     PIC X(4).
           05  AB-VAL     PIC 9(5).
       SD  SRT-AP.
       01  AP-REC.
           05  AP-KEY     PIC X(4).
           05  AP-VAL     PIC 9(5).
       SD  MRG-W.
       01  MW-REC.
           05  MW-KEY     PIC X(4).
           05  MW-VAL     PIC 9(5).
       WORKING-STORAGE SECTION.
       01  WS-X.
           05  WS-X-KEY   PIC X(4).
           05  WS-X-VAL   PIC 9(5).
       01  WS-EOF         PIC X VALUE 'N'.
       PROCEDURE DIVISION.
       0000-MAIN.
           SORT SRT-AB ON ASCENDING KEY AB-KEY
               USING A-FILE GIVING B-FILE
           SORT SRT-AP ON ASCENDING KEY AP-KEY
               USING A-FILE
               OUTPUT PROCEDURE 2000-OUT
           MERGE MRG-W ON ASCENDING KEY MW-KEY
               USING A-FILE D-FILE GIVING C-FILE
           STOP RUN.
       2000-OUT.
           RETURN SRT-AP INTO WS-X AT END MOVE 'Y' TO WS-EOF
           END-RETURN
           DISPLAY WS-X-KEY ' ' WS-X-VAL.
