      *================================================================*
      * DYNSQL - dynamic SQL in both spellings: PREPARE/EXECUTE with a *
      * parameter marker, and EXECUTE IMMEDIATE. The statement text is  *
      * a run-time value, so no table or column is statically knowable  *
      * - the point of this example is the honest classification: the   *
      * endpoint is <dynamic-sql> with endpointType "dynamic_sql" (not  *
      * "db2"), marked dynamic, flagged, and excluded from the          *
      * unload/load pattern proofs.                                     *
      *================================================================*
       IDENTIFICATION DIVISION.
       PROGRAM-ID. DYNSQL.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-STMT-TXT         PIC X(200) VALUE SPACES.
       01  WS-DEL-TXT          PIC X(100) VALUE SPACES.
       01  WS-CUST-ID          PIC 9(5) VALUE 0.
       01  SQLCODE             PIC S9(9) COMP VALUE 0.
       PROCEDURE DIVISION.
       0000-MAIN.
           MOVE 'UPDATE T SET C = ? WHERE ID = ?' TO WS-STMT-TXT
           EXEC SQL
               PREPARE S1 FROM :WS-STMT-TXT
           END-EXEC
           EXEC SQL
               EXECUTE S1 USING :WS-CUST-ID
           END-EXEC
           MOVE 'DELETE FROM T WHERE C = 0' TO WS-DEL-TXT
           EXEC SQL
               EXECUTE IMMEDIATE :WS-DEL-TXT
           END-EXEC
           IF SQLCODE = 0
               CONTINUE
           END-IF
           STOP RUN.
