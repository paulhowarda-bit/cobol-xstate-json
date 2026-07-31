      *================================================================*
      * SQLSEL - a minimal Db2 singleton-SELECT inquiry. The vertical  *
      * slice for the REACTIVE target: one inbound GET (the SELECT row) *
      * and one response-event branch (SQLCODE). Kept FLAT (no PERFORM) *
      * so both perimeter states live in the root machine and receive   *
      * their inbound events directly - the boundary rewrite in         *
      * isolation, before scaling to nested PERFORM actors.             *
      *================================================================*
       IDENTIFICATION DIVISION.
       PROGRAM-ID. SQLSEL.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-CUST-ID          PIC 9(5) VALUE 0.
       01  WS-NAME             PIC X(20).
       01  WS-BALANCE          PIC S9(7)V99 COMP-3 VALUE 0.
       01  WS-STATUS           PIC X(10) VALUE SPACES.
       01  SQLCODE             PIC S9(9) COMP VALUE 0.
       PROCEDURE DIVISION.
       0000-MAIN.
           MOVE 12345 TO WS-CUST-ID
           EXEC SQL
               SELECT NAME, BALANCE INTO :WS-NAME, :WS-BALANCE
               FROM CUSTOMER WHERE ID = :WS-CUST-ID
           END-EXEC
           IF SQLCODE = 0
               MOVE 'FOUND' TO WS-STATUS
           ELSE
               MOVE 'MISSING' TO WS-STATUS
           END-IF
           STOP RUN.
