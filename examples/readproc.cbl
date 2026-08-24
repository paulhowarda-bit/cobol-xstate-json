      * A read-process-write paragraph: fetch a row, derive fields from it, then act.
      * Under the reactive (push) target the processing MUST run when the row event
      * arrives, not on entry - otherwise it derives from empty host variables every
      * cycle. Driven under push: send GET.DB2.CUST with WS-NAME='ACME', WS-AMT=00021,
      * and OUT-NAME must become 'ACME', OUT-DBL must become 000042.
      * (SQL SELECT INTO, not file READ, only because a SELECT's INTO host variables are
      * captured field-by-field, so the arriving event can be driven end-to-end; the push
      * rewrite path exercised is identical to a file read's.)
       IDENTIFICATION DIVISION.
       PROGRAM-ID. READPROC.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01 WS-ID    PIC 9(5) VALUE 42.
       01 WS-NAME  PIC X(20).
       01 WS-AMT   PIC 9(5).
       01 OUT-NAME PIC X(20).
       01 OUT-DBL  PIC 9(6).
       PROCEDURE DIVISION.
       0000-MAIN.
           EXEC SQL
               SELECT CNAME, CAMT INTO :WS-NAME, :WS-AMT
               FROM CUST WHERE CID = :WS-ID
           END-EXEC
           MOVE WS-NAME TO OUT-NAME
           COMPUTE OUT-DBL = WS-AMT * 2
           STOP RUN.
