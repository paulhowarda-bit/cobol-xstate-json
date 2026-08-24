      *================================================================*
      * SQLDML - exercises all four Db2 DML verbs against one table, so *
      * the interface overlay shows a Db2 endpoint with BOTH directions *
      * (get + create) and an SQLCODE response branch. Flat / single    *
      * region (EVALUATE dispatch, no out-of-line PERFORM).             *
      *   SELECT (get)  UPDATE / INSERT / DELETE (create)  on ACCOUNT   *
      *================================================================*
       IDENTIFICATION DIVISION.
       PROGRAM-ID. SQLDML.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-ID       PIC 9(5) VALUE 0.
       01  WS-NAME     PIC X(20).
       01  WS-BAL      PIC S9(7)V99 COMP-3 VALUE 0.
       01  SQLCODE     PIC S9(9) COMP VALUE 0.
       PROCEDURE DIVISION.
       0000-MAIN.
           MOVE 12345 TO WS-ID
           EXEC SQL
               SELECT NAME, BALANCE INTO :WS-NAME, :WS-BAL
               FROM ACCOUNT WHERE ID = :WS-ID
           END-EXEC
           EVALUATE SQLCODE
               WHEN 0
                   EXEC SQL
                       UPDATE ACCOUNT SET BALANCE = :WS-BAL
                       WHERE ID = :WS-ID
                   END-EXEC
               WHEN 100
                   EXEC SQL
                       INSERT INTO ACCOUNT (ID, NAME, BALANCE)
                       VALUES (:WS-ID, :WS-NAME, :WS-BAL)
                   END-EXEC
               WHEN OTHER
                   EXEC SQL
                       DELETE FROM ACCOUNT WHERE ID = :WS-ID
                   END-EXEC
           END-EVALUATE
           STOP RUN.
