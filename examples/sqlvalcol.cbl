       IDENTIFICATION DIVISION.
       PROGRAM-ID. SQLVALCOL.
      * Ledger item 161: a column an INSERT, UPDATE or MERGE writes from
      * a clock register or a literal, rather than from a host variable.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-ID                PIC S9(9) COMP.
       01  WS-NAME              PIC X(20).
       PROCEDURE DIVISION.
       0000-MAIN.
           EXEC SQL
               INSERT INTO ZZTAB (ZZ_ID, ZZ_STAT, ZZ_UPD_TS)
               VALUES (:WS-ID, 'N', CURRENT TIMESTAMP)
           END-EXEC
           EXEC SQL
               UPDATE ZZTAB SET ZZ_STAT = 'Y',
                      ZZ_UPD_TS = CURRENT TIMESTAMP
               WHERE ZZ_ID = :WS-ID
           END-EXEC
           EXEC SQL
               UPDATE ZZTAB SET ZZ_RUN_DT = CURRENT DATE,
                      ZZ_RETRY = ZZ_RETRY + 1,
                      NM = UPPER(:WS-NAME)
               WHERE ZZ_ID = :WS-ID
           END-EXEC
           EXEC SQL
               MERGE INTO ZZTAB T
               USING (VALUES (:WS-ID, 'P')) AS S (ZZ_ID, ZZ_STAT)
               ON T.ZZ_ID = S.ZZ_ID
               WHEN MATCHED THEN UPDATE SET ZZ_STAT = S.ZZ_STAT,
                    ZZ_UPD_TS = CURRENT TIMESTAMP
               WHEN NOT MATCHED THEN
                    INSERT (ZZ_ID, ZZ_STAT, ZZ_UPD_TS)
                    VALUES (S.ZZ_ID, S.ZZ_STAT, CURRENT TIMESTAMP)
           END-EXEC
           GOBACK.
