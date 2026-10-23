       IDENTIFICATION DIVISION.
       PROGRAM-ID. TSQGRP.
      *================================================================*
      * A TS queue named through a GROUP: QNAME() passes 16 characters *
      * of it and CALL only 8, so one group holds two names.           *
      *   WS-LOG-TSQ-NAME - 'XAP' + 'QRSLOG' + SPACES: the queue is    *
      *                     XAPQRSLOG; CALL through it is XAPQRSLO     *
      *   WS-TSQ-PAIR     - two program names side by side: the queue  *
      *                     is XAB0310PXAB7700P, never XAB0310P        *
      *   WS-TSQ-KEYED    - bytes 9-16 a terminal id the program moves *
      *                     in: the queue stays flagged                *
      *   WS-TDQ          - a TD QUEUE() is read as before             *
      *================================================================*
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-LOG-TSQ-NAME.
           05  FILLER                PIC X(03) VALUE 'XAP'.
           05  FILLER                PIC X(08) VALUE 'QRSLOG  '.
           05  FILLER                PIC X(05) VALUE SPACES.
       01  WS-TSQ-PAIR.
           05  WS-TSQ-INVOKING       PIC X(08) VALUE 'XAB0310P'.
           05  WS-TSQ-CALLED         PIC X(08) VALUE 'XAB7700P'.
       01  WS-TSQ-KEYED.
           05  WS-TSQ-KEYED-PFX      PIC X(08) VALUE 'XKEYQ001'.
           05  WS-TSQ-KEYED-TERM     PIC X(08) VALUE SPACES.
       01  WS-TDQ                    PIC X(04) VALUE 'XTDQ'.
       01  WS-REC                    PIC X(80).
       PROCEDURE DIVISION.
       0000-MAIN.
           MOVE EIBTRMID TO WS-TSQ-KEYED-TERM
           EXEC CICS WRITEQ TS QNAME(WS-LOG-TSQ-NAME) FROM(WS-REC)
           END-EXEC
           EXEC CICS READQ TS QNAME(WS-TSQ-PAIR) INTO(WS-REC)
           END-EXEC
           EXEC CICS WRITEQ TS QNAME(WS-TSQ-KEYED) FROM(WS-REC)
           END-EXEC
           EXEC CICS WRITEQ TD QUEUE(WS-TDQ) FROM(WS-REC) END-EXEC
           CALL WS-LOG-TSQ-NAME USING WS-REC
           EXEC CICS RETURN END-EXEC.
