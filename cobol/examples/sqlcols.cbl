       IDENTIFICATION DIVISION.
       PROGRAM-ID. SQLCOLS.
      *================================================================*
      * SQL column <-> host-variable correlation. A host-variable NAME *
      * is program-local; the COLUMN is the database's, so it is the   *
      * only thing that proves two programs read the same state.       *
      * Every shape that must work, and every one that must refuse:    *
      *   1000  SELECT with a qualified name and an AS alias           *
      *   2000  SELECT with a derived SUM(A,B) - must not break the    *
      *         comma split, and names no column                       *
      *   3000  UPDATE ... SET - explicit pairs, the best fidelity     *
      *   4000  cursor DECLARE (columns) + FETCH (host vars)           *
      *   5000  INDICATOR variable - 2 columns, 3 host vars: MUST NOT  *
      *         correlate (a naive zip maps BAL -> IND-BAL)            *
      *   6000  SELECT * - the column list is not in the source        *
      *================================================================*
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-ID           PIC 9(6).
       01  WS-NAME         PIC X(20).
       01  WS-BAL          PIC S9(7)V99 COMP-3.
       01  WS-TOT          PIC S9(9)V99 COMP-3.
       01  WS-ST           PIC X.
       01  IND-BAL         PIC S9(4) COMP.
       01  WS-REC          PIC X(60).
       01  SQLCODE         PIC S9(9) COMP VALUE 0.
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM 1000-ALIAS
           PERFORM 2000-DERIVED
           PERFORM 3000-UPDATE
           PERFORM 4000-CURSOR
           PERFORM 5000-INDICATOR
           PERFORM 6000-STAR
           STOP RUN.
       1000-ALIAS.
           EXEC SQL
               SELECT C.NAME, C.BAL AS B
               INTO :WS-NAME, :WS-BAL
               FROM ADMIN.CUSTOMER C
               WHERE C.ID = :WS-ID
           END-EXEC.
       2000-DERIVED.
           EXEC SQL
               SELECT ID, SUM(DEBIT, CREDIT)
               INTO :WS-ID, :WS-TOT
               FROM LEDGER
           END-EXEC.
       3000-UPDATE.
           EXEC SQL
               UPDATE CUSTOMER SET BAL = :WS-BAL, STATUS = :WS-ST
               WHERE ID = :WS-ID
           END-EXEC.
       4000-CURSOR.
           EXEC SQL
               DECLARE C1 CURSOR FOR
                   SELECT ID, BAL FROM CUSTOMER
           END-EXEC
           EXEC SQL
               FETCH C1 INTO :WS-ID, :WS-BAL
           END-EXEC.
       5000-INDICATOR.
           EXEC SQL
               SELECT NAME, BAL
               INTO :WS-NAME, :WS-BAL:IND-BAL
               FROM CUSTOMER
           END-EXEC.
       6000-STAR.
           EXEC SQL
               SELECT * INTO :WS-REC FROM CUSTOMER
           END-EXEC.
