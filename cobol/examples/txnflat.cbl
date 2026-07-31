      *================================================================*
      * TXNFLAT - a FLAT transaction loop + dispatcher (inline PERFORM   *
      * UNTIL + EVALUATE, no out-of-line PERFORM). One region, so the    *
      * emitted `always` graph IS the real business flow and the         *
      * business-view distillation collapses it faithfully. It mixes:    *
      *   * boundary crossings - ACCEPT (get), DISPLAY (create)          *
      *   * business decisions - the tran-type EVALUATE, the status IF   *
      *   * technical scaffolding - the loop head (UNTIL) and the        *
      *     MOVE data step, which carry no business meaning              *
      *================================================================*
       IDENTIFICATION DIVISION.
       PROGRAM-ID. TXNFLAT.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-TRAN-TYPE        PIC X VALUE ' '.
       01  WS-STATUS           PIC XX VALUE 'OK'.
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM UNTIL WS-TRAN-TYPE = 'Q'
               ACCEPT WS-TRAN-TYPE
               MOVE 'OK' TO WS-STATUS
               EVALUATE WS-TRAN-TYPE
                   WHEN 'D'
                       DISPLAY 'DEPOSIT POSTED'
                   WHEN 'W'
                       IF WS-STATUS = 'NG'
                           DISPLAY 'WITHDRAWAL REJECTED'
                       ELSE
                           DISPLAY 'WITHDRAWAL POSTED'
                       END-IF
                   WHEN 'I'
                       DISPLAY 'INQUIRY SENT'
                   WHEN OTHER
                       CONTINUE
               END-EVALUATE
           END-PERFORM
           STOP RUN.
