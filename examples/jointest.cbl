       IDENTIFICATION DIVISION.
       PROGRAM-ID. JOINTEST.
      *================================================================*
      * One SQL statement, several tables: a comma list, a JOIN, and   *
      * a subquery in a SELECT, a DELETE, an INSERT and an UPDATE, and *
      * a cursor over a join declared in WORKING-STORAGE. Only the     *
      * first table after FROM (or the verb's target) used to be       *
      * published, so seven of these fourteen tables were in no view.  *
      *================================================================*
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-A                PIC X(10).
       01  WS-B                PIC X(10).
       01  WS-C                PIC X(10).
           EXEC SQL INCLUDE SQLCA END-EXEC.
           EXEC SQL DECLARE CJOIN CURSOR FOR
               SELECT A.COL1, B.COL2
                 FROM T_CUR_FIRST A
                 INNER JOIN T_CUR_JOINED B ON A.K = B.K
           END-EXEC.
       PROCEDURE DIVISION.
       0000-MAIN.
           EXEC SQL
               SELECT A.COL1, B.COL2 INTO :WS-A, :WS-B
                 FROM T_COMMA_FIRST A, T_COMMA_SECOND B
                WHERE A.K = B.K
           END-EXEC
           EXEC SQL
               SELECT A.COL1 INTO :WS-A
                 FROM T_JOIN_FIRST A
                 LEFT OUTER JOIN T_JOIN_SECOND B ON A.K = B.K
           END-EXEC
           EXEC SQL
               SELECT A.COL1 INTO :WS-C
                 FROM T_SUB_OUTER A
                WHERE EXISTS (SELECT 1 FROM T_SUB_INNER S
                              WHERE S.K = A.K)
           END-EXEC
           EXEC SQL
               DELETE FROM T_DEL_TARGET
                WHERE K IN (SELECT K FROM T_DEL_SUB)
           END-EXEC
           EXEC SQL
               INSERT INTO T_INS_TARGET (K)
               SELECT K FROM T_INS_SOURCE
           END-EXEC
           EXEC SQL
               UPDATE T_UPD_TARGET SET COL1 = :WS-A
                WHERE K IN (SELECT K FROM T_UPD_SUB)
           END-EXEC
           EXEC SQL OPEN CJOIN END-EXEC
           EXEC SQL FETCH CJOIN INTO :WS-A, :WS-B END-EXEC
           GOBACK.
