       IDENTIFICATION DIVISION.
       PROGRAM-ID. SQLDERIV.
      *================================================================*
      * DERIVED select-list slots: what the value was made of.         *
      * `SUM(SPOKE_DOL_A)` aggregates over MANY ROWS, so the host      *
      * variable is NOT that column and must never be mapped to it -   *
      * the slot stays `derived`. But the column is right there in the *
      * statement, and throwing it away left a field that came from    *
      * nowhere. It is kept as PROVENANCE beside the refusal:          *
      *   1000  SUM(COL)          - one extractable source column      *
      *   2000  VALUE(SUM(COL),0) - nested; the innermost name wins    *
      *   3000  SELECT 'Y'        - a literal: truly no source column  *
      *   4000  COUNT(*)          - aggregates all rows: no source     *
      *   5000  QTY * PRICE       - an expression over two columns     *
      *   6000  CASE ... END      - which branch supplied it is a      *
      *         RUN-TIME fact, so the sources are NOT proven: refused  *
      *   7000  a cursor whose DECLARE holds the aggregate, and the    *
      *         FETCH that fills the slot without ever seeing it       *
      *================================================================*
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  W-TOTAL-SPOKE       PIC S9(11)V99 COMP-3.
       01  W-TOTAL-HST         PIC S9(11)V99 COMP-3.
       01  W-EXISTS-SW         PIC X.
       01  W-ROW-COUNT         PIC S9(9) COMP.
       01  W-EXTENDED          PIC S9(9)V99 COMP-3.
       01  W-BANDING           PIC X(4).
       01  W-BRCH-C            PIC X(4).
       01  W-BASE-C            PIC X(4).
       01  W-SUM-ADJ           PIC S9(9)V99 COMP-3.
       01  SQLCODE             PIC S9(9) COMP VALUE 0.
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM 1000-SUM
           PERFORM 2000-NESTED
           PERFORM 3000-LITERAL
           PERFORM 4000-COUNT
           PERFORM 5000-EXPRESSION
           PERFORM 6000-CASE
           PERFORM 7000-CURSOR
           STOP RUN.
       1000-SUM.
           EXEC SQL
               SELECT SUM(SPOKE_DOL_A)
               INTO :W-TOTAL-SPOKE
               FROM T_MMJT_JRNL_TXN
           END-EXEC.
       2000-NESTED.
           EXEC SQL
               SELECT VALUE(SUM(TRX_RUN_COLL_BAL_A), 0)
               INTO :W-TOTAL-HST
               FROM T_MMAR_ACC_ANAL_HST
           END-EXEC.
       3000-LITERAL.
           EXEC SQL
               SELECT 'Y'
               INTO :W-EXISTS-SW
               FROM T_MMTC_TRX_CTL
           END-EXEC.
       4000-COUNT.
           EXEC SQL
               SELECT COUNT(*)
               INTO :W-ROW-COUNT
               FROM T_MMTC_TRX_CTL
           END-EXEC.
       5000-EXPRESSION.
           EXEC SQL
               SELECT QTY * PRICE
               INTO :W-EXTENDED
               FROM T_MMOR_ORDER
           END-EXEC.
       6000-CASE.
           EXEC SQL
               SELECT CASE WHEN BAL_A > 0 THEN HIGH_C ELSE LOW_C END
               INTO :W-BANDING
               FROM T_MMAR_ACC_ANAL
           END-EXEC.
       7000-CURSOR.
           EXEC SQL
               DECLARE BOLA_CURSOR CURSOR FOR
                   SELECT FBSI_BRCH_C, FBSI_BASE_C, SUM(ADJ_A)
                   FROM RTOA_ADJUSTMENT
           END-EXEC
           EXEC SQL
               FETCH BOLA_CURSOR
               INTO :W-BRCH-C, :W-BASE-C, :W-SUM-ADJ
           END-EXEC.
