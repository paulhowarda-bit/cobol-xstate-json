       IDENTIFICATION DIVISION.
       PROGRAM-ID. SQLRETCS.
      *================================================================*
      * A Db2 stored procedure's result set: a CURSOR WITH RETURN is   *
      * OPENed and handed to the CALLER, which does the fetching, so   *
      * this program never FETCHes it. Db2 runs the query at OPEN. The *
      * OPEN used to publish nothing, so of the four tables below only *
      * the SELECT INTO's was in any view. OPN_CSR is a plain cursor   *
      * opened and not fetched; NEVER_CSR is declared, never opened.   *
      *================================================================*
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-ID                PIC X(10).
       01  WS-NAME              PIC X(30).
           EXEC SQL DECLARE RET_CSR CURSOR WITH RETURN FOR
               SELECT A.ACCT_ID, B.ACCT_NM
                 FROM T_RET_FIRST A
                 JOIN T_RET_SECOND B ON B.ACCT_ID = A.ACCT_ID
                WHERE A.ACCT_ID = :WS-ID
           END-EXEC.
           EXEC SQL DECLARE OPN_CSR CURSOR FOR
               SELECT ACCT_NM FROM T_OPEN_ONLY
                WHERE ACCT_ID = :WS-ID
           END-EXEC.
           EXEC SQL DECLARE NEVER_CSR CURSOR FOR
               SELECT ACCT_NM FROM T_NEVER_OPENED
           END-EXEC.
       LINKAGE SECTION.
       01  LK-ID                PIC X(10).
       PROCEDURE DIVISION USING LK-ID.
       MAIN-PARA.
           MOVE LK-ID TO WS-ID.
           EXEC SQL SELECT ACCT_NM INTO :WS-NAME
                FROM T_SELECT_INTO WHERE ACCT_ID = :WS-ID
           END-EXEC.
           EXEC SQL OPEN RET_CSR END-EXEC.
           EXEC SQL OPEN OPN_CSR END-EXEC.
           GOBACK.
