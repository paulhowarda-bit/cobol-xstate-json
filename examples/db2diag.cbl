      *================================================================*
      * DB2DIAG - a Db2 program that formats an SQLCA error via DSNTIAC.*
      * It uses EXEC SQL (so uses_sql holds) and CALLs 'DSNTIAC', the   *
      * Db2 message-formatting module, so DSNTIAC classifies ibm-db2:   *
      * precompiler runtime, no application source to fetch.            *
      *================================================================*
       IDENTIFICATION DIVISION.
       PROGRAM-ID. DB2DIAG.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  W-ERRMSG.
           05  W-LEN     PIC S9(4) COMP VALUE 120.
           05  W-TEXT    PIC X(120).
       EXEC SQL INCLUDE SQLCA END-EXEC.
       PROCEDURE DIVISION.
       0000-MAIN.
           EXEC SQL
               SELECT COL1 INTO :W-TEXT FROM TAB1
           END-EXEC
           CALL 'DSNTIAC' USING SQLCA W-ERRMSG
           GOBACK.
