       IDENTIFICATION DIVISION.
       PROGRAM-ID. LITNAME.
      *----------------------------------------------------------------
      * A quoted literal whose text spells a data name (ledger item
      * 156). 'TBL-GRP' is the name of a group here too, but moving the
      * literal moves a constant: no flow source, no origin, and a
      * group given the literal is a constant store, not a group copy.
      *----------------------------------------------------------------
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  TBL-GRP.
           05  TBL-KEY             PIC X(10).
       01  WS-TABLE-NM             PIC X(18).
       01  WS-MSG                  PIC X(40).
       01  WS-X                    PIC X(40).
       01  WS-LEN                  PIC 9(4).
       PROCEDURE DIVISION.
       0000-MAIN.
           ACCEPT TBL-GRP
           MOVE 'TBL-GRP' TO WS-TABLE-NM
           MOVE 'TBL-KEY NOT FOUND' TO WS-MSG
           COMPUTE WS-LEN = FUNCTION LENGTH('TBL-GRP')
           IF WS-TABLE-NM = 'TBL-GRP' MOVE 'WS-MSG' TO WS-X END-IF
           MOVE 'IT''S' TO WS-X
           MOVE TBL-KEY TO WS-MSG
           MOVE 'TBL-GRP' TO TBL-GRP
           DISPLAY 'TBL-GRP' WS-TABLE-NM WS-MSG WS-X WS-LEN
           GOBACK.
