       IDENTIFICATION DIVISION.
       PROGRAM-ID. CICSNOFL.
      *----------------------------------------------------------------
      * CICS commands that share a verb with the file commands, and the
      * endpoint each one is published under:
      *
      *   WRITE OPERATOR         the console (DISPLAY's endpoint type)
      *   DELETE CONTAINER/      no crossing - GET/PUT CONTAINER are
      *     COUNTER               none either
      *   WRITE JOURNALNAME      <file>, marked no-operand: a journal is
      *                          not a file, and has no type of its own
      *   REWRITE FILE(x(1:8))   <file>, marked no-operand: an operand
      *                          this tool cannot read
      *   READQ/WRITEQ TS QNAME  the queue QNAME names - literal, or
      *                          resolved through the data item
      *----------------------------------------------------------------
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-MSG             PIC X(40).
       01  WS-ANSWER          PIC X(01).
       01  WS-REC             PIC X(80).
       01  WS-KEY             PIC X(08).
       01  WS-QNAME           PIC X(16) VALUE 'APPLAUDITQUEUE01'.
       PROCEDURE DIVISION.
       0000-MAIN.
           EXEC CICS WRITE OPERATOR TEXT(WS-MSG) REPLY(WS-ANSWER)
                MAXLENGTH(1)
           END-EXEC
           EXEC CICS DELETE CONTAINER('WORKCONT') CHANNEL('WORKCHAN')
           END-EXEC
           EXEC CICS DELETE COUNTER('ORDERSEQ') END-EXEC
           EXEC CICS WRITE JOURNALNAME('AUDITJNL') JTYPEID('AU')
                FROM(WS-REC)
           END-EXEC
           EXEC CICS REWRITE FILE(WS-REC(1:8)) FROM(WS-REC) END-EXEC
           EXEC CICS READQ TS QNAME('APPLSTATEQUEUE01') INTO(WS-REC)
           END-EXEC
           EXEC CICS WRITEQ TS QNAME(WS-QNAME) FROM(WS-REC) END-EXEC
           EXEC CICS RETURN END-EXEC.
