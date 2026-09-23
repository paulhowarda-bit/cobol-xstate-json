CH0001*================================================================*
CH0001* SQLCHANGE - a data-change table reference names the TABLE      *
CH0001* inside its parentheses, never the keyword in front of them     *
CH0001* (FINAL / OLD / NEW TABLE), and the SELECT both reads and       *
CH0001* writes it. Also an EXEC SQL INCLUDE split across three lines,  *
CH0001* with a change tag in columns 1-6, which must still be found.   *
CH0001*   ACCT_ANAL / ACCT_HIST / ACCT_LOG / ACCT_QUE: get + create    *
CH0001*   FINAL: a table genuinely called FINAL, read with no TABLE (  *
CH0001*================================================================*
       IDENTIFICATION DIVISION.
       PROGRAM-ID. SQLCHANGE.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
CH0042     EXEC SQL
CH0042         INCLUDE SQLCA
CH0042     END-EXEC.
       01  WS-ID       PIC 9(5) VALUE 0.
       01  WS-AMT      PIC S9(7)V99 COMP-3 VALUE 0.
       01  WS-NEW      PIC S9(7)V99 COMP-3 VALUE 0.
           EXEC SQL
               DECLARE C1 CURSOR FOR
                 SELECT ID FROM FINAL TABLE
                   (INSERT INTO ACCT_QUE (ID) VALUES (:WS-ID))
           END-EXEC.
       PROCEDURE DIVISION.
       0000-MAIN.
           EXEC SQL
               SELECT BAL
                 INTO :WS-NEW
                 FROM FINAL TABLE
                   (UPDATE ACCT_ANAL
                       SET BAL = BAL + :WS-AMT
                     WHERE ID = :WS-ID)
           END-EXEC
           EXEC SQL
               SELECT ID INTO :WS-ID
                 FROM OLD TABLE (DELETE FROM ACCT_HIST WHERE ID = :WS-ID)
           END-EXEC
           EXEC SQL
               SELECT ID INTO :WS-ID
                 FROM NEW TABLE (INSERT INTO ACCT_LOG (ID, AMT)
                                 VALUES (:WS-ID, :WS-AMT))
           END-EXEC
           EXEC SQL
               SELECT AMT INTO :WS-AMT FROM FINAL WHERE ID = :WS-ID
           END-EXEC
           EXEC SQL OPEN C1 END-EXEC
           EXEC SQL FETCH C1 INTO :WS-ID END-EXEC
           EXEC SQL CLOSE C1 END-EXEC
           STOP RUN.
