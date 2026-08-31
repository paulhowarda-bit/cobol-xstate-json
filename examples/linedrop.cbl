       IDENTIFICATION DIVISION.
       PROGRAM-ID. LINEDROP.
      *================================================================*
      * LINEDROP - Db2 statements the lineage walk never reaches.      *
      *                                                                *
      * `interface` walks the program STRUCTURALLY and maps every       *
      * statement's columns. `lineage` walks FORWARD from the entry     *
      * points, so a state no path reaches emits no row - correctly,    *
      * because with no path there are no origins to report. The two    *
      * therefore disagree, and the disagreement used to be SILENT: a   *
      * consumer joining them found a (field, column) pair in one file  *
      * and nothing in the other, with no way to tell a gap in our      *
      * recovery from dead code in the program.                         *
      *                                                                *
      * Four Db2 SELECTs, one reachable and three not, one per reason:  *
      *                                                                *
      *   1000-REACHED   PERFORMed from MAIN - the only one that        *
      *                  produces a lineage row.                        *
      *   8000-ORPHAN    no-static-predecessor. Nothing PERFORMs it,    *
      *                  and MAIN's STOP RUN means nothing falls into   *
      *                  it either. Dead code in the PROGRAM, so no row *
      *                  is the RIGHT answer - it still has to be said. *
      *   5000-TAIL      perform-range-inverted. Reached only by        *
      *                  `PERFORM 5000-TAIL THRU 1000-REACHED`, whose   *
      *                  range runs backwards. Both endpoints exist, so *
      *                  this is not a missing paragraph: it is a gap   *
      *                  in OUR control-flow recovery and the row is    *
      *                  genuinely missing.                             *
      *   9000-CASCADE   cascade. Its only predecessor is the           *
      *                  fall-through out of 5000-TAIL, so one          *
      *                  unresolved range strands it too. This is how a *
      *                  single bad PERFORM hides an arbitrarily large  *
      *                  subgraph.                                      *
      *                                                                *
      * All three appear under `unreached`, told apart by `reason`.     *
      *================================================================*
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-RULE-DS      PIC X(30).
       01  WS-JNL-DS       PIC X(30).
       01  WS-ORPH-DS      PIC X(30).
       01  WS-CASC-DS      PIC X(30).
       01  SQLCODE         PIC S9(9) COMP VALUE 0.
           EXEC SQL
               DECLARE T_RULE TABLE
               (RULE_DS       CHAR(30))
           END-EXEC.
           EXEC SQL
               DECLARE T_JNL TABLE
               (JNL_DS        CHAR(30))
           END-EXEC.
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM 1000-REACHED
           PERFORM 5000-TAIL THRU 1000-REACHED
           STOP RUN.
       8000-ORPHAN.
           EXEC SQL
               SELECT RULE_DS INTO :WS-ORPH-DS FROM T_RULE
           END-EXEC.
           STOP RUN.
       1000-REACHED.
           EXEC SQL
               SELECT RULE_DS INTO :WS-RULE-DS FROM T_RULE
           END-EXEC.
       5000-TAIL.
           EXEC SQL
               SELECT JNL_DS INTO :WS-JNL-DS FROM T_JNL
           END-EXEC.
       9000-CASCADE.
           EXEC SQL
               SELECT JNL_DS INTO :WS-CASC-DS FROM T_JNL
           END-EXEC.
