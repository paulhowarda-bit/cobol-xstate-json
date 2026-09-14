       IDENTIFICATION DIVISION.
       PROGRAM-ID. DYNCANDS.
      *----------------------------------------------------------------
      * What a dynamic target publishes when constant propagation finds
      * literals but cannot pin one. Each item is a different grade of
      * answer, and the interface must say which:
      *
      *   WS-ONE     one literal reaches it       -> resolved, no list
      *   WS-MANY    two literal MOVEs            -> candidates, assigned
      *   WS-MIXED   VALUE plus a variable MOVE   -> the one candidate is
      *              NOT the whole set (hasVariableAssignment)
      *   WS-COND    88-level VALUEs, never SET   -> declared-88, not proof
      *   WS-VARONLY variables only               -> dynamic, no list
      *
      * A CALL and a CICS XCTL/LINK over the same item ask the same
      * question, so they must publish the same answer.
      *----------------------------------------------------------------
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-ONE             PIC X(08) VALUE 'MODONE01'.
       01  WS-MANY            PIC X(08).
       01  WS-MIXED           PIC X(08) VALUE 'MODMIX01'.
       01  WS-OTHER           PIC X(08).
       01  WS-COND            PIC X(08).
           88  COND-A                   VALUE 'MOD88A'.
           88  COND-B                   VALUE 'MOD88B'.
       01  WS-VARONLY         PIC X(08).
       01  WS-LINK-REC        PIC X(10).
       PROCEDURE DIVISION.
       0000-MAIN.
           MOVE 'MODTWO01' TO WS-MANY
           MOVE 'MODTWO02' TO WS-MANY
           MOVE WS-OTHER   TO WS-MIXED
           MOVE WS-OTHER   TO WS-VARONLY
           CALL WS-ONE     USING WS-LINK-REC
           CALL WS-MANY    USING WS-LINK-REC
           CALL WS-MIXED   USING WS-LINK-REC
           CALL WS-COND    USING WS-LINK-REC
           CALL WS-VARONLY USING WS-LINK-REC
           EXEC CICS LINK PROGRAM(WS-COND) END-EXEC
           EXEC CICS XCTL PROGRAM(WS-MANY) END-EXEC
           GOBACK.
