       IDENTIFICATION DIVISION.
       PROGRAM-ID. SQLCALLV.
      *================================================================*
      * EXEC SQL CALL :host-variable: the procedure name is the item's *
      * run-time value, the same situation as a dynamic COBOL CALL,    *
      * resolved by the same constant propagation:                     *
      *   * WS-PROC-A has one VALUE literal       -> PROCA             *
      *   * WS-PROC-B is MOVEd a qualified name   -> SCH.PROCB         *
      *   * WS-PROC-C can hold two literals       -> flagged, both     *
      *     published as candidates                                    *
      *   * WS-PROC-D is set from another item    -> runtime-determined*
      *================================================================*
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-PROC-A       PIC X(8)  VALUE 'PROCA'.
       01  WS-PROC-B       PIC X(18).
       01  WS-PROC-C       PIC X(8)  VALUE 'PROCC1'.
       01  WS-PROC-D       PIC X(8).
       01  WS-PROC-NAME    PIC X(8).
       01  IN-KEY          PIC X(10).
       01  OUT-RETURN-CODE PIC S9(4) COMP.
       01  SQLCODE         PIC S9(9) COMP VALUE 0.
       PROCEDURE DIVISION.
       0000-MAIN.
           ACCEPT IN-KEY
           EXEC SQL
               CALL :WS-PROC-A (:IN-KEY, :OUT-RETURN-CODE)
           END-EXEC
           MOVE 'SCH.PROCB' TO WS-PROC-B
           EXEC SQL
               CALL :WS-PROC-B (:IN-KEY, :OUT-RETURN-CODE)
           END-EXEC
           IF IN-KEY = SPACES
               MOVE 'PROCC2' TO WS-PROC-C
           END-IF
           EXEC SQL
               CALL :WS-PROC-C (:IN-KEY, :OUT-RETURN-CODE)
           END-EXEC
           ACCEPT WS-PROC-NAME
           MOVE WS-PROC-NAME TO WS-PROC-D
           EXEC SQL
               CALL :WS-PROC-D (:IN-KEY, :OUT-RETURN-CODE)
           END-EXEC
           IF SQLCODE NOT = 0
               DISPLAY 'CALL FAILED'
           END-IF
           STOP RUN.
