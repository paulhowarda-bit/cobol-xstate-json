       IDENTIFICATION DIVISION.
       PROGRAM-ID. SQLGAPS.
      *================================================================*
      * The correlation shapes SQLCOLS does not reach: the WRITE half, *
      * and the ones that must FLAG rather than fall silent.           *
      *   1000  INSERT ... VALUES - the write half of column identity  *
      *   2000  INSERT with a literal slot (CURRENT TIMESTAMP): that   *
      *         column is written from no field, so it is flagged      *
      *   3000  INSERT with no column list - the order is the table's, *
      *         which is not in the source                             *
      *   4000  rowset FETCH: positioning keywords bury the cursor     *
      *         name, and reading ROWSET as it loses the ENDPOINT too  *
      *   5000  COUNT(*) is NOT `SELECT *` - one derived slot, and the *
      *         sibling column must still correlate                    *
      *   6000  FETCH whose cursor has no visible DECLARE              *
      *================================================================*
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-ID           PIC 9(6).
       01  WS-NAME         PIC X(20).
       01  WS-BAL          PIC S9(7)V99 COMP-3.
       01  WS-N            PIC S9(9) COMP.
       01  SQLCODE         PIC S9(9) COMP VALUE 0.
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM 1000-INSERT
           PERFORM 2000-LITERAL
           PERFORM 3000-NOCOLS
           PERFORM 4000-ROWSET
           PERFORM 5000-COUNT
           PERFORM 6000-NODECL
           STOP RUN.
       1000-INSERT.
           EXEC SQL
               INSERT INTO ACCOUNT (ID, NAME, BAL)
               VALUES (:WS-ID, :WS-NAME, :WS-BAL)
           END-EXEC.
       2000-LITERAL.
           EXEC SQL
               INSERT INTO AUDITLOG (ID, STAMP)
               VALUES (:WS-ID, CURRENT TIMESTAMP)
           END-EXEC.
       3000-NOCOLS.
           EXEC SQL
               INSERT INTO ACCOUNT
               VALUES (:WS-ID, :WS-NAME, :WS-BAL)
           END-EXEC.
       4000-ROWSET.
           EXEC SQL
               DECLARE C2 CURSOR WITH ROWSET POSITIONING FOR
                   SELECT ID, NAME FROM ACCOUNT
           END-EXEC
           EXEC SQL
               FETCH NEXT ROWSET FROM C2
               FOR 10 ROWS INTO :WS-ID, :WS-NAME
           END-EXEC.
       5000-COUNT.
           EXEC SQL
               SELECT ID, COUNT(*)
               INTO :WS-ID, :WS-N
               FROM ACCOUNT GROUP BY ID
           END-EXEC.
       6000-NODECL.
           EXEC SQL
               FETCH C9 INTO :WS-ID, :WS-NAME
           END-EXEC.
