       IDENTIFICATION DIVISION.
       PROGRAM-ID. SQLDYNCSR.
      *================================================================*
      * A cursor DECLAREd FOR a PREPAREd statement, not FOR a select    *
      * list. Its columns do not exist until run time, so there is      *
      * nothing to recover - and that is an INHERENT unknown, not a     *
      * failed recovery.                                                *
      *                                                                 *
      * Before the DECLARE's FOR form was recorded, an empty select     *
      * list here was indistinguishable from an unreadable one, so this *
      * FETCH was reported as "no DECLARE ... visible" - contradicting  *
      * the parse, which holds the DECLARE - and its endpoint was       *
      * table-typed `<cursor DYN_CSR>` for something that has no table. *
      * The PREPARE on the same statement was flagged honestly at the   *
      * same time, so one statement carried two contradictory answers.  *
      *                                                                 *
      * ACCT_CSR is here as the contrast: same program, same FETCH      *
      * shape, a real select list, and it still resolves to its table.  *
      *================================================================*
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-STMT         PIC X(200).
       01  WS-ID           PIC S9(9) COMP.
       01  WS-NAME         PIC X(20).
       01  WS-FUND         PIC X(4).
       01  WS-BAL          PIC S9(9)V99 COMP-3.
       01  SQLCODE         PIC S9(9) COMP VALUE 0.
           EXEC SQL
               DECLARE DYN_CSR CURSOR FOR DYNSTMT
           END-EXEC.
           EXEC SQL
               DECLARE ACCT_CSR CURSOR FOR
                   SELECT FUND_A, BALANCE_A
                   FROM T_DMAA_ACC_ANAL
           END-EXEC.
       PROCEDURE DIVISION.
       0000-MAIN.
           MOVE 'SELECT ID, NAME FROM T_ANY' TO WS-STMT
           EXEC SQL
               PREPARE DYNSTMT FROM :WS-STMT
           END-EXEC
           EXEC SQL OPEN DYN_CSR END-EXEC
           EXEC SQL
               FETCH DYN_CSR
               INTO :WS-ID, :WS-NAME
           END-EXEC
           EXEC SQL CLOSE DYN_CSR END-EXEC
           EXEC SQL OPEN ACCT_CSR END-EXEC
           EXEC SQL
               FETCH ACCT_CSR
               INTO :WS-FUND, :WS-BAL
           END-EXEC
           EXEC SQL CLOSE ACCT_CSR END-EXEC
           STOP RUN.
