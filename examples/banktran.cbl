      *================================================================*
      * BANKTRAN - transaction dispatch with EVALUATE, a GO TO, a      *
      * dynamic CALL, and an ALTER, to exercise guarded transitions    *
      * and the FLAGGING of constructs a static pass cannot resolve.   *
      *================================================================*
       IDENTIFICATION DIVISION.
       PROGRAM-ID. BANKTRAN.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-TRAN-TYPE        PIC X.
       01  WS-STATUS           PIC XX.
       01  WS-SUBPGM           PIC X(8) VALUE 'POSTLOG '.
       01  WS-EOF              PIC X VALUE 'N'.
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM 1000-OPEN
           PERFORM 2000-DISPATCH UNTIL WS-EOF = 'Y'
           PERFORM 9000-CLOSE
           GOBACK.
       1000-OPEN.
           OPEN INPUT TRAN-FILE
           READ TRAN-FILE
               AT END MOVE 'Y' TO WS-EOF
           END-READ.
       2000-DISPATCH.
           EVALUATE WS-TRAN-TYPE
               WHEN 'D'  PERFORM 2100-DEPOSIT
               WHEN 'W'  PERFORM 2200-WITHDRAW
               WHEN 'I'  PERFORM 2300-INQUIRY
               WHEN OTHER PERFORM 2900-ERROR
           END-EVALUATE
           READ TRAN-FILE
               AT END MOVE 'Y' TO WS-EOF
           END-READ.
       2100-DEPOSIT.
           ADD 1 TO WS-STATUS
           CALL WS-SUBPGM USING WS-TRAN-TYPE.
       2200-WITHDRAW.
           IF WS-STATUS = 'NG'
               GO TO 2900-ERROR
           END-IF
           SUBTRACT 1 FROM WS-STATUS.
       2300-INQUIRY.
           DISPLAY 'INQUIRY'.
       2900-ERROR.
           DISPLAY 'BAD TRAN'
           ALTER 2300-INQUIRY TO PROCEED TO 2900-ERROR.
       9000-CLOSE.
           CLOSE TRAN-FILE.
