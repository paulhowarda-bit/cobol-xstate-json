       IDENTIFICATION DIVISION.
       PROGRAM-ID. SQLPREPCAND.
      *================================================================*
      * Ledger item 153: cursors DECLAREd FOR PREPAREd statements,     *
      * whose text the program builds only from literals. The select   *
      * list is then in the source, once per way the text is built,    *
      * and each FETCH publishes it as numbered `dynamic` CANDIDATES.  *
      *                                                                *
      * CSR_ONE  - STRING of a group of FILLER VALUE literals into the *
      *            text half of a VARCHAR host variable: 1 candidate.  *
      * CSR_TWO  - a literal MOVEd on each arm of an IF, the second a  *
      *            join with qualified columns: 2 candidates.          *
      * CSR_LINK - text MOVEd from a LINKAGE item: none, as before.    *
      *================================================================*
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-SQL-ONE.
           49  WS-SQL-ONE-LEN  PIC S9(4) COMP.
           49  WS-SQL-ONE-TXT  PIC X(200).
       01  WS-SEL-ONE.
           05  FILLER          PIC X(7)  VALUE 'SELECT '.
           05  FILLER          PIC X(10) VALUE 'ZZ_BRCH, '.
           05  FILLER          PIC X(8)  VALUE 'ZZ_BASE '.
           05  FILLER          PIC X(13) VALUE 'FROM ZZ_PRTC '.
       01  WS-SQL-TWO          PIC X(200).
       01  WS-SQL-LINK         PIC X(200).
       01  WS-MODE             PIC X.
       01  WS-A                PIC X(4).
       01  WS-B                PIC X(4).
       01  SQLCODE             PIC S9(9) COMP VALUE 0.
           EXEC SQL DECLARE CSR_ONE CURSOR FOR STMT_ONE END-EXEC.
           EXEC SQL DECLARE CSR_TWO CURSOR FOR STMT_TWO END-EXEC.
           EXEC SQL DECLARE CSR_LINK CURSOR FOR STMT_LINK END-EXEC.
       LINKAGE SECTION.
       01  LK-STMT             PIC X(200).
       PROCEDURE DIVISION USING LK-STMT.
       0000-MAIN.
           MOVE SPACES TO WS-SQL-ONE-TXT
           STRING WS-SEL-ONE DELIMITED BY SIZE INTO WS-SQL-ONE-TXT
           MOVE LENGTH OF WS-SEL-ONE TO WS-SQL-ONE-LEN
           EXEC SQL PREPARE STMT_ONE FROM :WS-SQL-ONE END-EXEC
           EXEC SQL OPEN CSR_ONE END-EXEC
           EXEC SQL FETCH CSR_ONE INTO :WS-A, :WS-B END-EXEC
           IF WS-MODE = 'A'
               MOVE 'SELECT COL_A, COL_B FROM ZZ_TAB_A' TO WS-SQL-TWO
           ELSE
               MOVE 'SELECT X.COL_C, Y.COL_D FROM ZZ_TAB_B X JOIN ZZ_T
      -             'AB_C Y ON X.K = Y.K' TO WS-SQL-TWO
           END-IF
           EXEC SQL PREPARE STMT_TWO FROM :WS-SQL-TWO END-EXEC
           EXEC SQL OPEN CSR_TWO END-EXEC
           EXEC SQL FETCH CSR_TWO INTO :WS-A, :WS-B END-EXEC
           MOVE LK-STMT TO WS-SQL-LINK
           EXEC SQL PREPARE STMT_LINK FROM :WS-SQL-LINK END-EXEC
           EXEC SQL OPEN CSR_LINK END-EXEC
           EXEC SQL FETCH CSR_LINK INTO :WS-A, :WS-B END-EXEC
           GOBACK.
