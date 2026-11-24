       IDENTIFICATION DIVISION.
       PROGRAM-ID. FILESTAT.
      *----------------------------------------------------------------
      * A FILE STATUS item (ledger item 160): every OPEN, READ, WRITE
      * and CLOSE on the file stores the file system's two-character
      * response into it, so a status moved into a message traces back
      * to the file, not to nothing. NOSTAT-FILE has no FILE STATUS.
      *----------------------------------------------------------------
       ENVIRONMENT DIVISION.
       INPUT-OUTPUT SECTION.
       FILE-CONTROL.
           SELECT IN-FILE ASSIGN TO INFILE
               FILE STATUS IS WS-FS.
           SELECT OUT-FILE ASSIGN TO OUTFILE
               FILE STATUS IS WS-OFS.
           SELECT NOSTAT-FILE ASSIGN TO NOSTAT.
       DATA DIVISION.
       FILE SECTION.
       FD  IN-FILE.
       01  IN-REC                  PIC X(20).
       FD  OUT-FILE.
       01  OUT-REC                 PIC X(20).
       FD  NOSTAT-FILE.
       01  NOSTAT-REC              PIC X(20).
       WORKING-STORAGE SECTION.
       01  WS-FS                   PIC XX.
       01  WS-OFS                  PIC XX.
       01  WS-REC                  PIC X(20).
       01  WS-MSG.
           05  FILLER              PIC X(10) VALUE 'STATUS = '.
           05  WS-MSG-FS           PIC XX.
       PROCEDURE DIVISION.
       0000-MAIN.
           OPEN INPUT IN-FILE NOSTAT-FILE
           OPEN OUTPUT OUT-FILE
           READ IN-FILE
           MOVE WS-FS TO WS-MSG-FS
           DISPLAY WS-MSG
           READ NOSTAT-FILE INTO WS-REC
           WRITE OUT-REC FROM WS-REC
           CLOSE IN-FILE OUT-FILE NOSTAT-FILE
           DISPLAY WS-OFS
           GOBACK.
