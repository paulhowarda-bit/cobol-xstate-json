       IDENTIFICATION DIVISION.
       PROGRAM-ID. SQLVARCH.
      *================================================================*
      * VARCHAR HOST STRUCTURES: a Db2 VARCHAR column is held as a     *
      * group of two elementary items - a level-49 length and the     *
      * text. That group is ONE host variable filling ONE column, so   *
      * the recovered mapping anchors on the PARENT (BE-CMT-X), never  *
      * on either child: an edge from a length counter to a text      *
      * column would assert something untrue, and it would double the *
      * edge count for every VARCHAR column in the estate.            *
      *   1000  SELECT INTO the VARCHAR group itself, then MOVEs OUT   *
      *         of its children - the text child must still carry the *
      *         SELECT's origin, not read as "internally set"          *
      *   2000  FETCH INTO a record that CONTAINS a VARCHAR member:    *
      *         the record expands to its scalar and the VARCHAR       *
      *         PARENT - not the outer record, not the pair            *
      *   3000  a column-list-less INSERT of such a record, zipped to  *
      *         the DECLARE: the VARCHAR parent fills the CMT column   *
      *================================================================*
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  BE-ACC-N                PIC X(9).
       01  BE-CMT-X.
           49  BE-CMT-X-LEN        PIC S9(4) COMP.
           49  BE-CMT-X-TEXT       PIC X(200).
       01  BE-REC.
           10  BE-R-ACC            PIC X(9).
           10  BE-R-CMT.
               49  BE-R-CMT-LEN    PIC S9(4) COMP.
               49  BE-R-CMT-TEXT   PIC X(200).
       01  BE-OUT.
           10  BE-O-ACC            PIC X(9).
           10  BE-O-CMT.
               49  BE-O-CMT-LEN    PIC S9(4) COMP.
               49  BE-O-CMT-TEXT   PIC X(200).
       01  WS-NOTE                 PIC X(200).
       01  WS-LEN                  PIC S9(4) COMP.
       01  SQLCODE                 PIC S9(9) COMP VALUE 0.
           EXEC SQL
               DECLARE T_DEMO_COMMENT TABLE
               (ACC_N         CHAR(9) NOT NULL,
                CMT           VARCHAR(200) NOT NULL)
           END-EXEC.
           EXEC SQL
               DECLARE CMT_CSR CURSOR FOR
                   SELECT ACC_N, CMT
                   FROM T_DEMO_COMMENT
           END-EXEC.
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM 1000-SELECT-ONE
           PERFORM 2000-FETCH-RECORD
           PERFORM 3000-INSERT-RECORD
           STOP RUN.
       1000-SELECT-ONE.
           EXEC SQL
               SELECT CMT
               INTO :BE-CMT-X
               FROM T_DEMO_COMMENT
               WHERE ACC_N = :BE-ACC-N
           END-EXEC
           MOVE BE-CMT-X-TEXT TO WS-NOTE
           MOVE BE-CMT-X-LEN TO WS-LEN.
       2000-FETCH-RECORD.
           EXEC SQL OPEN CMT_CSR END-EXEC
           EXEC SQL
               FETCH CMT_CSR
               INTO :BE-REC
           END-EXEC
           EXEC SQL CLOSE CMT_CSR END-EXEC.
       3000-INSERT-RECORD.
           MOVE BE-ACC-N TO BE-O-ACC
           MOVE WS-NOTE TO BE-O-CMT-TEXT
           MOVE WS-LEN TO BE-O-CMT-LEN
           EXEC SQL
               INSERT INTO T_DEMO_COMMENT
               VALUES (:BE-OUT)
           END-EXEC.
