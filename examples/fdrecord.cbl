       IDENTIFICATION DIVISION.
       PROGRAM-ID. FDRECORD.
      *----------------------------------------------------------------
      * A WRITE names a RECORD; the interface publishes the FILE. What
      * links the two, and what the row says when nothing does:
      *
      *   A-FILE  its 01 is in the source           -> endpoint A-FILE
      *   B-FILE  its 01 is in a COPY that does not  -> endpoint B-FILE,
      *           resolve, but the FD's DATA RECORD     through the clause
      *           clause names the record
      *   C-FILE  its 01 is in a COPY that does not  -> endpoint C-REC,
      *           resolve, and nothing names it         marked a record
      *
      * B-FILE's clauses run over three lines. They used to be read as
      * part of A-REST, the last item above them - VALUE OF included.
      *----------------------------------------------------------------
       ENVIRONMENT DIVISION.
       INPUT-OUTPUT SECTION.
       FILE-CONTROL.
           SELECT A-FILE ASSIGN TO DDAOUT.
           SELECT B-FILE ASSIGN TO DDBOUT.
           SELECT C-FILE ASSIGN TO DDCOUT.
       DATA DIVISION.
       FILE SECTION.
       FD  A-FILE.
       01  A-REC.
           05  A-KEY          PIC X(08).
           05  A-REST         PIC X(72).
       FD  B-FILE
           RECORDING MODE IS F
           VALUE OF FILE-ID IS 'BFILE.DAT'
           DATA RECORD IS B-REC.
           COPY FDBRECX.
       FD  C-FILE.
           COPY FDCRECX.
       WORKING-STORAGE SECTION.
       01  WS-LINE            PIC X(80).
       PROCEDURE DIVISION.
       0000-MAIN.
           OPEN OUTPUT A-FILE B-FILE C-FILE
           MOVE WS-LINE TO A-REC
           WRITE A-REC
           WRITE B-REC FROM WS-LINE
           WRITE C-REC FROM WS-LINE
           CLOSE A-FILE B-FILE C-FILE
           GOBACK.
