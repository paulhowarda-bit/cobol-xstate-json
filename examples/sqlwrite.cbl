       IDENTIFICATION DIVISION.
       PROGRAM-ID. SQLWRITE.
      *================================================================*
      * Five write shapes whose host variables used to reach the       *
      * interface with no column and no reason:                        *
      *   SELECT ... FROM FINAL TABLE (INSERT / UPDATE ...) - read as  *
      *     the inner statement would be on its own, cursor or not     *
      *   MERGE ... USING (VALUES (:a, :b)) AS S (X, Y) - VALUES slot  *
      *     -> correlation column -> the column a WHEN writes          *
      *   FOR :n ROWS - a row count, not a field                       *
      *   VALUES (UPPER(:a)) - a derived entry, not a column from      *
      *     nowhere                                                    *
      *   WHERE c IN (:grp) - the group's items are row selectors      *
      *================================================================*
       DATA DIVISION.
       WORKING-STORAGE SECTION.
           EXEC SQL
               DECLARE T_XLOG_ENTRY TABLE
                 (LOG_ID   CHAR(10)      NOT NULL,
                  LOG_AMT  DECIMAL(9, 2) NOT NULL)
           END-EXEC.
       01  WS-ORD-ID         PIC X(10).
       01  WS-ORD-AMT        PIC S9(7)V99 COMP-3.
       01  WS-ORD-TS         PIC X(26).
       01  WS-ROW-TS         PIC X(26).
       01  WS-SEQ-JOB        PIC X(8).
       01  WS-SEQ-CO         PIC X(3).
       01  WS-SEQ-N          PIC S9(9) COMP.
       01  WS-ROWS           PIC S9(4) COMP.
       01  WS-NAME           PIC X(30).
       01  WS-TYP-SET.
           05 WS-TYP-SET-1    PIC X(2).
           05 WS-TYP-SET-2    PIC X(2).
       01  WS-CNT            PIC S9(9) COMP.
           EXEC SQL
               DECLARE C1 CURSOR FOR
                 SELECT SEQ_N FROM FINAL TABLE
                   (UPDATE T_XSEQ_CURSOR SET JOB_NAME = :WS-SEQ-JOB
                     WHERE CO_ID = :WS-SEQ-CO)
           END-EXEC.
       PROCEDURE DIVISION.
       0000-MAIN.
           EXEC SQL
               SELECT ROW_TS INTO :WS-ROW-TS
                 FROM FINAL TABLE
                   (INSERT INTO T_XORD_INFO (ORD_ID, ORD_AMT, ORD_TS)
                    VALUES (:WS-ORD-ID, :WS-ORD-AMT, :WS-ORD-TS))
           END-EXEC
           EXEC SQL
               SELECT SEQ_N INTO :WS-SEQ-N
                 FROM FINAL TABLE
                   (UPDATE T_XSEQ_SEQUENCE
                       SET SEQ_N = SEQ_N + 1, JOB_NAME = :WS-SEQ-JOB
                     WHERE CO_ID = :WS-SEQ-CO)
           END-EXEC
           EXEC SQL
               SELECT LOG_ID INTO :WS-ORD-ID
                 FROM FINAL TABLE
                   (INSERT INTO T_XLOG_ENTRY
                    VALUES (:WS-ORD-ID, :WS-ORD-AMT))
           END-EXEC
           EXEC SQL
               MERGE INTO T_XMRG_FUND T
               USING (VALUES (:WS-ORD-ID, :WS-ORD-AMT)
                       FOR :WS-ROWS ROWS)
                  AS S (ORD_ID, AMT)
                  ON T.ORD_ID = S.ORD_ID
                WHEN MATCHED THEN UPDATE SET T.ORD_AMT = S.AMT
                WHEN NOT MATCHED THEN INSERT (ORD_ID, ORD_AMT)
                     VALUES (S.ORD_ID, S.AMT)
           END-EXEC
           EXEC SQL
               MERGE INTO T_XMRG_NONAME T
               USING (VALUES (:WS-ORD-ID, :WS-ORD-AMT)) S
                  ON T.ORD_ID = :WS-SEQ-CO
                WHEN MATCHED THEN UPDATE SET T.ORD_AMT = 0
           END-EXEC
           EXEC SQL
               INSERT INTO T_XQUE_BATCH (ORD_ID, ORD_AMT)
                 VALUES (:WS-ORD-ID, :WS-ORD-AMT)
                 FOR :WS-ROWS ROWS ATOMIC
           END-EXEC
           EXEC SQL
               INSERT INTO T_XENR_REC (NAME_T, ORD_ID)
                 VALUES (UPPER(:WS-NAME), :WS-ORD-ID)
           END-EXEC
           EXEC SQL
               UPDATE T_XTYP_CODE SET CNT = :WS-CNT
                WHERE TYP_CD IN (:WS-TYP-SET)
           END-EXEC
           EXEC SQL OPEN C1 END-EXEC
           EXEC SQL FETCH C1 INTO :WS-SEQ-N END-EXEC
           EXEC SQL CLOSE C1 END-EXEC
           GOBACK.
