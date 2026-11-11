       IDENTIFICATION DIVISION.
       PROGRAM-ID. CALLARGS.
      *================================================================*
      * CALL ... USING by position. Each CALL event carries            *
      * `arguments`: one entry per argument, the data name passed or   *
      * null where it passes no data item:                             *
      *   'P1'  [null, WS-A]           a literal first                  *
      *   'P2'  [WS-A, null, WS-B]     OMITTED in the middle            *
      *   'P3'  [WS-B, null, WS-C]     ADDRESS OF / LENGTH OF / X OF G  *
      *   'P4'  [WS-T, WS-A]           a subscript is no argument       *
      *================================================================*
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-A            PIC X(8).
       01  WS-B            PIC X(8).
       01  WS-G.
           05  WS-C        PIC X(4).
       01  WS-I            PIC 9(2) VALUE 1.
       01  WS-TBL.
           05  WS-T        PIC X(4) OCCURS 5.
       PROCEDURE DIVISION.
       0000-MAIN.
           CALL 'P1' USING 'ABC' WS-A
           CALL 'P2' USING WS-A OMITTED WS-B
           CALL 'P3' USING ADDRESS OF WS-B
                BY CONTENT LENGTH OF WS-B
                BY REFERENCE WS-C OF WS-G
           CALL 'P4' USING WS-T (WS-I) WS-A
           GOBACK.
