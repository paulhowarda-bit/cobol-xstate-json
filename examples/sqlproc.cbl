       IDENTIFICATION DIVISION.
       PROGRAM-ID. SQLPROC.
      *================================================================*
      * EXEC SQL CALL invokes a Db2 STORED PROCEDURE. Its operands are *
      * the procedure's PARAMETERS (linkage), not table columns -      *
      * classified as a table, downstream tooling goes hunting for     *
      * Column nodes that cannot exist. The endpoint kind db2_proc is  *
      * the discriminator; which parameters are IN and which OUT is    *
      * the procedure's signature, which is not in this source.        *
      *================================================================*
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  IN-MESSAGE      PIC X(100).
       01  OUT-RETURN-CODE PIC S9(4) COMP.
       01  SQLCODE         PIC S9(9) COMP VALUE 0.
       PROCEDURE DIVISION.
       0000-MAIN.
           EXEC SQL
               CALL PCBEN171 (:IN-MESSAGE, :OUT-RETURN-CODE)
           END-EXEC
           IF SQLCODE NOT = 0
               DISPLAY 'CALL FAILED'
           END-IF
           STOP RUN.
