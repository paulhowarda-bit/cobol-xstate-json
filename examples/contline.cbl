       IDENTIFICATION DIVISION.
       PROGRAM-ID. CONTLINE.
      *================================================================*
      * HYPHEN CONTINUATION (column 7), both directions. Column 8 is   *
      * the only thing that tells them apart, and getting it wrong is  *
      * the one continuation defect that CORRUPTS rather than omits:   *
      * a fabricated name is silent, because nothing downstream can    *
      * know the name it indexed was never in the source.              *
      *   1000  a WORD resumed in column 8: joins with NO separator.   *
      *         CUST_STA + TE is the column CUST_STATE. Joining with   *
      *         a space made it two tokens - a phantom column and a    *
      *         junk word - which also broke the 5-vs-6 arity and so   *
      *         cost the whole statement its column mapping.           *
      *   2000  a STATEMENT continued after blanks: joins with ONE     *
      *         space. MOVE WS-A / TO WS-B must not become WS-ATO.     *
      *   3000  a split TABLE name: PRODDB.CUS + TOMER. Joining with   *
      *         a space records a DELETE against PRODDB.CUS, which     *
      *         does not exist, and loses the one that does.           *
      *   4000  a continued LITERAL, which is a different rule again   *
      *         (no separator, and the resume quote is not data) -     *
      *         here so a fix to the word case cannot break it.        *
      *================================================================*
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-ID               PIC 9(9).
       01  WS-NAME             PIC X(20).
       01  WS-ADDR             PIC X(30).
       01  WS-CITY             PIC X(20).
       01  WS-STATE            PIC X(2).
       01  WS-A                PIC X(10).
       01  WS-B                PIC X(10).
       01  WS-LONG             PIC X(15).
       01  SQLCODE             PIC S9(9) COMP VALUE 0.
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM 1000-SELECT
           PERFORM 2000-MOVE
           PERFORM 3000-DELETE
           PERFORM 4000-LITERAL
           STOP RUN.
       1000-SELECT.
           EXEC SQL
               SELECT CUST_ID, CUST_NAME, CUST_ADDR, CUST_CITY, CUST_STA
      -TE
               INTO :WS-ID, :WS-NAME, :WS-ADDR, :WS-CITY,
      -             :WS-STATE
               FROM PRODDB.CUSTOMER
               WHERE CUST_ID = :WS-ID
           END-EXEC.
       2000-MOVE.
           MOVE WS-A
      -        TO WS-B.
       3000-DELETE.
           EXEC SQL DELETE FROM PRODDB.CUS
      -TOMER WHERE CUST_ID = :WS-ID
           END-EXEC.
       4000-LITERAL.
           MOVE 'ABCDEFGHIJ
      -    'KLMNO' TO WS-LONG.
