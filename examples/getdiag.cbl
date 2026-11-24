       IDENTIFICATION DIVISION.
       PROGRAM-ID. GETDIAG.
      *----------------------------------------------------------------
      * EXEC SQL GET DIAGNOSTICS (ledger item 157): Db2 stores its own
      * diagnostics area into each target host variable - a system
      * read, like SET :hv = CURRENT TIMESTAMP. A host variable after
      * CONDITION picks the condition; it is a param, not a target.
      *----------------------------------------------------------------
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-RC                   PIC S9(9) COMP.
       01  WS-ROWNUM               PIC S9(9) COMP.
       01  WS-ROWCNT               PIC S9(9) COMP.
       01  WS-COND                 PIC S9(9) COMP.
       01  WS-MSGTXT               PIC X(70).
       01  WS-ALL                  PIC X(200).
       01  WS-OUT                  PIC X(400).
       PROCEDURE DIVISION.
       0000-MAIN.
           EXEC SQL
               GET DIAGNOSTICS :WS-ROWCNT = ROW_COUNT
           END-EXEC
           EXEC SQL
               GET DIAGNOSTICS CONDITION 1
                   :WS-RC = DB2_RETURNED_SQLCODE,
                   :WS-ROWNUM = DB2_ROW_NUMBER,
                   :WS-MSGTXT = MESSAGE_TEXT
           END-EXEC
           EXEC SQL
               GET DIAGNOSTICS CONDITION :WS-COND :WS-RC =
                   DB2_RETURNED_SQLCODE
           END-EXEC
           EXEC SQL
               GET DIAGNOSTICS :WS-ALL = ALL STATEMENT
           END-EXEC
           STRING WS-RC WS-ROWNUM WS-MSGTXT WS-ROWCNT WS-ALL
               DELIMITED BY SIZE INTO WS-OUT
           DISPLAY WS-OUT
           GOBACK.
