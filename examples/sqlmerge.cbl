       IDENTIFICATION DIVISION.
       PROGRAM-ID. SQLMERGE.
      *================================================================*
      * MERGE writes its target (matched -> UPDATE, not matched ->     *
      * INSERT) and reads its USING source; a statement that begins    *
      * WITH is the SELECT behind its common table expressions, which  *
      * are not tables. Neither published any table before: both fell  *
      * through to "not a data crossing".                              *
      *================================================================*
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-ID                PIC X(10).
       01  WS-AMT               PIC S9(7)V99 COMP-3.
       01  WS-CNT               PIC S9(9) COMP.
       01  WS-LAST              PIC X(26).
       PROCEDURE DIVISION.
       0000-MAIN.
           EXEC SQL
               MERGE INTO T_MRG_VALUES T
               USING (VALUES (:WS-ID, :WS-AMT)) AS S (ACCT_ID, AMT)
                  ON T.ACCT_ID = S.ACCT_ID
                WHEN MATCHED THEN UPDATE SET T.AMT = S.AMT
                WHEN NOT MATCHED THEN INSERT (ACCT_ID, AMT)
                     VALUES (S.ACCT_ID, S.AMT)
           END-EXEC
           EXEC SQL
               MERGE INTO T_MRG_TARGET T
               USING (SELECT ACCT_ID, AMT FROM T_MRG_SOURCE) AS S
                  ON T.ACCT_ID = S.ACCT_ID
                WHEN MATCHED THEN UPDATE SET T.AMT = S.AMT
           END-EXEC
           EXEC SQL
               WITH RECENT (ACCT_ID, LAST_TS) AS
                    (SELECT ACCT_ID, MAX(TXN_TS) FROM T_WITH_TXN
                      GROUP BY ACCT_ID)
               SELECT COUNT(*), MAX(LAST_TS) INTO :WS-CNT, :WS-LAST
                 FROM RECENT
                WHERE ACCT_ID = :WS-ID
           END-EXEC
           GOBACK.
