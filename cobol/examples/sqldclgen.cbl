       IDENTIFICATION DIVISION.
       PROGRAM-ID. SQLDCLGN.
      *================================================================*
      * DECLARE TABLE is the DCLGEN's own statement of the table's     *
      * declared column ORDER - the fact a column-list-less INSERT     *
      * needs, stated in the source rather than fetched from the       *
      * catalog.                                                       *
      *   1000  INSERT INTO T VALUES (:H) with the table's DECLARE     *
      *         TABLE present: the zip is proven, positionally         *
      *   2000  the same pattern under a SYNONYM: the DECLARE is for   *
      *         the BASE table, so without a synonym map (external     *
      *         catalog knowledge) it must FLAG, not guess             *
      *================================================================*
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  MFER-ERROR      PIC X(80).
       01  WS-ACCT-ID      PIC S9(9) COMP.
       01  WS-ACCT-NAME    PIC X(20).
       01  SQLCODE         PIC S9(9) COMP VALUE 0.
           EXEC SQL
               DECLARE T_MFER_ERROR TABLE
                   ( MFER_ERROR       CHAR(80) NOT NULL )
           END-EXEC.
           EXEC SQL
               DECLARE T_RTAC_ACCOUNT TABLE
                   ( ACCT_ID          INTEGER NOT NULL,
                     ACCT_NAME        CHAR(20) NOT NULL )
           END-EXEC.
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM 1000-DECLARED
           PERFORM 2000-SYNONYM
           STOP RUN.
       1000-DECLARED.
           EXEC SQL
               INSERT INTO T_MFER_ERROR VALUES (:MFER-ERROR)
           END-EXEC.
       2000-SYNONYM.
           EXEC SQL
               INSERT INTO RTAC_ACCOUNT
               VALUES (:WS-ACCT-ID, :WS-ACCT-NAME)
           END-EXEC.
