       IDENTIFICATION DIVISION.
       PROGRAM-ID. SQLQUAL.
      *================================================================*
      * QUALIFIED host variables: `:GFAC . AC-ACC-N` names the field   *
      * AC-ACC-N inside group GFAC. Reading only the word after the    *
      * ':' names the GROUP, so every slot of a VALUES list written    *
      * that way comes back as one repeated name that matches no       *
      * column - and that the data dictionary has no field for.        *
      *   1000  INSERT with an explicit column list, qualified VALUES  *
      *   2000  UPDATE ... SET from a qualified value, qualified WHERE *
      *   3000  SELECT INTO a qualified target                         *
      *   4000  a qualified value with a null INDICATOR: still refused *
      *         (the slot is not 1:1, whatever its name resolves to)   *
      *================================================================*
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  GFAC.
           05  AC-MULTI-CO-N   PIC 9(3).
           05  AC-ACC-N        PIC 9(9).
           05  AC-CSDN-C       PIC X(4).
           05  AC-BAL-A        PIC S9(7)V99 COMP-3.
       01  WS-IND-BAL          PIC S9(4) COMP.
       01  SQLCODE             PIC S9(9) COMP VALUE 0.
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM 1000-INSERT
           PERFORM 2000-UPDATE
           PERFORM 3000-SELECT
           PERFORM 4000-INDICATOR
           STOP RUN.
       1000-INSERT.
           EXEC SQL
               INSERT INTO GFAC_ACC (MULTI_CO_N, ACC_N, CSDN_C)
               VALUES (:GFAC . AC-MULTI-CO-N,
                       :GFAC . AC-ACC-N,
                       :GFAC . AC-CSDN-C)
           END-EXEC.
       2000-UPDATE.
           EXEC SQL
               UPDATE GFAC_ACC SET BAL_A = :GFAC . AC-BAL-A
               WHERE ACC_N = :GFAC . AC-ACC-N
           END-EXEC.
       3000-SELECT.
           EXEC SQL
               SELECT BAL_A
               INTO :GFAC . AC-BAL-A
               FROM GFAC_ACC
               WHERE ACC_N = :GFAC . AC-ACC-N
           END-EXEC.
       4000-INDICATOR.
           EXEC SQL
               UPDATE GFAC_ACC SET BAL_A = :GFAC . AC-BAL-A :WS-IND-BAL
               WHERE ACC_N = :GFAC . AC-ACC-N
           END-EXEC.
