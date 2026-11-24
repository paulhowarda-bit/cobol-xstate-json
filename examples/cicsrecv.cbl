       IDENTIFICATION DIVISION.
       PROGRAM-ID. CICSRECV.
      *----------------------------------------------------------------
      * EXEC CICS options that return a value (ledger item 158): RESP
      * and RESP2 on any command, every ASSIGN option, GET CONTAINER
      * INTO, CONVERSE INTO, WEB RECEIVE INTO, and a locate-mode READ
      * SET(ADDRESS OF X), whose record is X. CICS stores each of them
      * into the named field, so none is "internally set".
      *----------------------------------------------------------------
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-APPLID               PIC X(8).
       01  WS-RESP                 PIC S9(8) COMP.
       01  WS-RESP2                PIC S9(8) COMP.
       01  WS-ABCODE               PIC X(4).
       01  WS-REC                  PIC X(80).
       01  WS-CONT                 PIC X(80).
       01  WS-IN                   PIC X(80).
       01  WS-WEB                  PIC X(80).
       01  WS-KEY                  PIC X(8).
       01  WS-OUT                  PIC X(400).
       01  WS-CA                   PIC X(20).
       LINKAGE SECTION.
       01  LK-REC.
           05  LK-A                PIC X(10).
       PROCEDURE DIVISION.
       0000-MAIN.
           EXEC CICS ASSIGN APPLID(WS-APPLID) END-EXEC
           EXEC CICS ASSIGN ABCODE(WS-ABCODE) END-EXEC
           EXEC CICS READ FILE('CUSTF') INTO(WS-REC) RIDFLD(WS-KEY)
                RESP(WS-RESP) RESP2(WS-RESP2) END-EXEC
           EXEC CICS GET CONTAINER('C1') CHANNEL('CH') INTO(WS-CONT)
           END-EXEC
           EXEC CICS CONVERSE FROM(WS-REC) INTO(WS-IN) END-EXEC
           EXEC CICS WEB RECEIVE INTO(WS-WEB) END-EXEC
           EXEC CICS READ FILE('F2') SET(ADDRESS OF LK-REC)
                RIDFLD(WS-KEY) END-EXEC
           EXEC CICS LINK PROGRAM('SUBP') COMMAREA(WS-CA)
                RESP(WS-RESP) END-EXEC
           EXEC CICS HANDLE CONDITION NOTFND(9000-NF) END-EXEC
           STRING WS-APPLID WS-RESP WS-RESP2 WS-ABCODE WS-REC WS-CONT
                WS-IN WS-WEB LK-A DELIMITED BY SIZE INTO WS-OUT
           EXEC CICS WRITEQ TS QUEUE('Q2') FROM(WS-OUT) END-EXEC
           EXEC CICS RETURN END-EXEC.
       9000-NF.
           EXEC CICS RETURN END-EXEC.
