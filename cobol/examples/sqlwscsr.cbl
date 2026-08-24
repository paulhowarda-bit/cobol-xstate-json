       IDENTIFICATION DIVISION.
       PROGRAM-ID. SQLWSCSR.
      *================================================================*
      * The cursor DECLARE lives in WORKING-STORAGE - where production *
      * code actually keeps it (beside the DCLGEN, often in a          *
      * copybook). The statement compiler never walks the DATA         *
      * DIVISION, so before the whole-stream scan this FETCH lost both *
      * its column mapping AND its real table endpoint: 77% of one     *
      * measured estate's unmapped lineage fields were exactly this.   *
      *================================================================*
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-FUND         PIC X(4).
       01  WS-ACCT         PIC X(10).
       01  WS-BAL          PIC S9(9)V99 COMP-3.
       01  SQLCODE         PIC S9(9) COMP VALUE 0.
           EXEC SQL
               DECLARE ACCT_CSR CURSOR FOR
                   SELECT FUND_A, ACCOUNT_N, BALANCE_A
                   FROM T_MMAA_ACC_ANAL
                   WHERE FUND_A > ' '
           END-EXEC.
       PROCEDURE DIVISION.
       0000-MAIN.
           EXEC SQL OPEN ACCT_CSR END-EXEC
           EXEC SQL
               FETCH ACCT_CSR
               INTO :WS-FUND, :WS-ACCT, :WS-BAL
           END-EXEC
           EXEC SQL CLOSE ACCT_CSR END-EXEC
           STOP RUN.
