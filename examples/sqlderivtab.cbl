       IDENTIFICATION DIVISION.
       PROGRAM-ID. SQLDTAB.
      *================================================================*
      * Ledger item 171: which TABLE each column a derived slot reads   *
      * belongs to. The entry's `table` is the statement's first FROM   *
      * table; `derivedFromColumns` says where each column really is:   *
      *   1000  COALESCE over a join - ADDR_1 is T_ADDR's, not T_ACCT's *
      *   2000  SUM over a nested table expression - BB binds no table  *
      *   3000  STRIP(.., TRAILING, ..) - TRAILING is no column         *
      *   4000  a cursor's derived slot, carried to the FETCH           *
      *================================================================*
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-KEY              PIC X(10).
       01  WS-ADDR             PIC X(40).
       01  WS-DOL              PIC S9(11)V99 COMP-3.
       01  WS-NM               PIC X(30).
       01  WS-PLAN             PIC X(8).
       01  WS-BNK              PIC X(40).
       01  SQLCODE             PIC S9(9) COMP VALUE 0.
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM 1000-JOIN
           PERFORM 2000-NESTED
           PERFORM 3000-STRIP
           PERFORM 4000-CURSOR
           STOP RUN.
       1000-JOIN.
           EXEC SQL
               SELECT COALESCE(ADR.ADDR_1, ' ')
               INTO :WS-ADDR
               FROM T_ACCT ACCT, T_ADDR ADR
               WHERE ACCT.KEY_ID = :WS-KEY
                 AND ADR.KEY_ID = ACCT.KEY_ID
           END-EXEC.
       2000-NESTED.
           EXEC SQL
               SELECT ABS(SUM(BB.ZOL_A))
               INTO :WS-DOL
               FROM (SELECT BM.ZOL_A FROM T_QHPR_PART_REF PR,
                            T_QHBM_BKPG_MAIN BM
                      WHERE PR.KEY_ID = BM.KEY_ID) BB
           END-EXEC.
       3000-STRIP.
           EXEC SQL
               SELECT STRIP(QMSP.ZSS_1_DS, TRAILING, ' ')
               INTO :WS-NM
               FROM T_QFOI_REPL QFOIR, T_QMSP_SPEED QMSP
               WHERE QFOIR.KEY_ID = :WS-KEY
           END-EXEC.
       4000-CURSOR.
           EXEC SQL
               DECLARE BNK_CSR CURSOR FOR
                   SELECT QAPL.QLN_ID,
                          COALESCE(QXRI.ZNK_NM_AD_1, QXES.ZNK_NM_AD_1)
                   FROM T_QAPL_PLAN QAPL
                   JOIN T_QXRI_BANK QXRI ON QXRI.QLN_ID = QAPL.QLN_ID
                   JOIN T_QXES_BANK QXES ON QXES.QLN_ID = QAPL.QLN_ID
           END-EXEC
           EXEC SQL
               FETCH BNK_CSR INTO :WS-PLAN, :WS-BNK
           END-EXEC.
