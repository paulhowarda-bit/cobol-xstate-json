       IDENTIFICATION DIVISION.
       PROGRAM-ID. SQLINCNP.
      *================================================================*
      * EXEC SQL INCLUDE with no period after END-EXEC. The statement  *
      * ends at END-EXEC, so every member is still found and expanded: *
      *   CUSTREC  - split over three lines, a data item right after   *
      *   SQLCA    - on one line                                       *
      *   XSQLERRP - inside a paragraph, before an IF and the period   *
      *================================================================*
       DATA DIVISION.
       WORKING-STORAGE SECTION.
           EXEC SQL
               INCLUDE CUSTREC
           END-EXEC
       01  WS-PROGRAM-ID             PIC X(08) VALUE 'SQLINCNP'.
           EXEC SQL INCLUDE SQLCA END-EXEC
       01  WS-FLAG                   PIC X(01) VALUE 'N'.
       PROCEDURE DIVISION.
       0000-MAIN.
           EXEC SQL
               INCLUDE XSQLERRP
           END-EXEC
           IF CUST-ACTIVE
               MOVE 'Y' TO WS-FLAG
           END-IF
           GOBACK.
