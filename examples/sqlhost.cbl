       IDENTIFICATION DIVISION.
       PROGRAM-ID. SQLHOST.
      *================================================================*
      * HOST STRUCTURES: a GROUP-level host variable stands for every  *
      * elementary item under it. The Db2 precompiler expands it       *
      * before the statement reaches the database, so a recovery that  *
      * keeps the source spelling sees ONE host variable where the     *
      * cursor has four columns - the count gate then refuses, and     *
      * four fields map to nothing. And a null indicator (:D:I) is     *
      * part of the value it qualifies, not a slot of its own.         *
      *   1000  FETCH INTO a group: 4 columns, 4 elementary items      *
      *   2000  INSERT with a column list: a group slot fills FOUR     *
      *   3000  INSERT with NO column list, zipped to its DECLARE      *
      *   4000  a group, a scalar, and a null indicator in one INTO    *
      *   5000  a group the data division does not hold (its copybook  *
      *         never arrived): NOT expanded, and still refused        *
      *   6000  a NESTED group expands on its own too                  *
      * FILLER, and a REDEFINES with its subordinates, are excluded    *
      * from every expansion - Db2 excludes them too.                  *
      *================================================================*
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  DSTI-TRNF-INIT.
           05  TRNF-NBR            PIC X(14).
           05  SBRX-GRP.
               10  SBRX-OFFC-C     PIC X(3).
               10  SBRX-BASE-C     PIC X(6).
           05  FILLER              PIC X(2).
           05  BRCH-CORR-I         PIC X(1).
           05  BRCH-CORR-N  REDEFINES BRCH-CORR-I PIC 9(1).
       01  WS-MULTI-CO-N           PIC 9(3).
       01  WS-NULL-IND-01          PIC S9(4) COMP.
       01  SQLCODE                 PIC S9(9) COMP VALUE 0.
           EXEC SQL
               DECLARE T_DSTI_TRNF_INIT TABLE
               (TRNF_NBR      CHAR(14),
                SBRX_OFFC_C   CHAR(3),
                SBRX_BASE_C   CHAR(6),
                BRCH_CORR_I   CHAR(1),
                MULTI_CO_N    DECIMAL(3))
           END-EXEC.
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM 0500-OPEN
           PERFORM 1000-FETCH-GROUP
           PERFORM 2000-INSERT-COLS
           PERFORM 3000-INSERT-NO-COLS
           PERFORM 4000-MIXED
           PERFORM 5000-ABSENT
           PERFORM 6000-NESTED
           STOP RUN.
       0500-OPEN.
           EXEC SQL
               DECLARE POSN-UPDT-CURSOR CURSOR FOR
                   SELECT TRNF_NBR, SBRX_OFFC_C, SBRX_BASE_C,
                          BRCH_CORR_I
                   FROM T_DSTI_TRNF_INIT
           END-EXEC
           EXEC SQL OPEN POSN-UPDT-CURSOR END-EXEC.
       1000-FETCH-GROUP.
           EXEC SQL
               FETCH POSN-UPDT-CURSOR
               INTO :DSTI-TRNF-INIT
           END-EXEC.
       2000-INSERT-COLS.
           EXEC SQL
               INSERT INTO T_DSTI_TRNF_INIT
                      (TRNF_NBR, SBRX_OFFC_C, SBRX_BASE_C, BRCH_CORR_I,
                       MULTI_CO_N)
               VALUES (:DSTI-TRNF-INIT, :WS-MULTI-CO-N)
           END-EXEC.
       3000-INSERT-NO-COLS.
           EXEC SQL
               INSERT INTO T_DSTI_TRNF_INIT
               VALUES (:DSTI-TRNF-INIT, :WS-MULTI-CO-N)
           END-EXEC.
       4000-MIXED.
           EXEC SQL
               SELECT TRNF_NBR, SBRX_OFFC_C, SBRX_BASE_C, BRCH_CORR_I,
                      MULTI_CO_N
               INTO :DSTI-TRNF-INIT, :WS-MULTI-CO-N:WS-NULL-IND-01
               FROM T_DSTI_TRNF_INIT
           END-EXEC.
       5000-ABSENT.
           EXEC SQL
               FETCH POSN-UPDT-CURSOR
               INTO :COPY-MISSING-REC
           END-EXEC.
       6000-NESTED.
           EXEC SQL
               SELECT SBRX_OFFC_C, SBRX_BASE_C
               INTO :SBRX-GRP
               FROM T_DSTI_TRNF_INIT
           END-EXEC.
