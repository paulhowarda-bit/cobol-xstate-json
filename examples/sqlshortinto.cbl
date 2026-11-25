       IDENTIFICATION DIVISION.
       PROGRAM-ID. SQLSHORT.
      *----------------------------------------------------------------
      * A SELECT INTO or FETCH with fewer host variables than columns
      * (ledger item 163): Db2 assigns the first M columns in order and
      * sets SQLWARN3, so when every INTO name is declared the first M
      * are mapped. An INTO naming an undeclared item stays refused.
      * And a CASE item names the columns its THEN/ELSE read (162).
      *----------------------------------------------------------------
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  PR-PRC-N               PIC X(9).
       01  PR-CCY-C               PIC X(3).
       01  WS-FULL-NAME            PIC X(30).
       01  WS-CUS-ID               PIC X(9).
           EXEC SQL INCLUDE SQLCA END-EXEC.
           EXEC SQL DECLARE ZZPR_CURSOR CURSOR FOR
               SELECT PRC_N, CCY_C, EFF_D FROM ZZPR_PRICING
               ORDER BY PRC_N
           END-EXEC.
       PROCEDURE DIVISION.
       0000-MAIN.
           EXEC SQL OPEN ZZPR_CURSOR END-EXEC
           EXEC SQL FETCH ZZPR_CURSOR INTO :PR-PRC-N, :PR-CCY-C
           END-EXEC
           EXEC SQL SELECT PRC_N, CCY_C, EFF_D
               INTO :PR-PRC-N, :PR-CCY-C
               FROM ZZPR_PRICING WHERE PRC_N = :WS-CUS-ID
           END-EXEC
           EXEC SQL SELECT PRC_N, CCY_C, EFF_D
               INTO :PR-PRC-N, :ZZ-MISSING-GROUP
               FROM ZZPR_PRICING WHERE PRC_N = :WS-CUS-ID
           END-EXEC
           EXEC SQL SELECT CASE WHEN C.ORG_NM = ' ' THEN C.GVN_NM
                  ELSE C.ORG_NM END
               INTO :WS-FULL-NAME FROM ZZCU_CUSTOMER C
               WHERE C.CLT_ID = :WS-CUS-ID
           END-EXEC
           EXEC SQL CLOSE ZZPR_CURSOR END-EXEC
           DISPLAY PR-PRC-N PR-CCY-C WS-FULL-NAME
           GOBACK.
