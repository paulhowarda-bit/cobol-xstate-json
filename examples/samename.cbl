       IDENTIFICATION DIVISION.
       PROGRAM-ID. SAMENAME.
      *----------------------------------------------------------------
      * A data name declared inside a group of the same name (ledger
      * item 169). Children are found by name, so the inner item used
      * to be handed the outer group's children again, forever: the
      * layout walkers raised RecursionError and the program got no
      * bundle at all. Each shape below recursed:
      *   ZZADDR-POSTAL-CODE  - a 20 under a 15 REDEFINES of its name
      *   WS-AREA             - a 05 under the 01 of the same name
      *   FILL                - a 10 under a REDEFINES inside 01 FILL
      *   CY-B / CY-C         - a cycle through two records
      *----------------------------------------------------------------
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  ZZ-COMM-REC.
           15  ZZADDR-DOM-ZIP-AREA.
               20  ZZADDR-DOM-ZIP-ERR          PIC X.
               20  ZZADDR-DOM-ZIP              PIC X(5).
               20  ZZADDR-DOM-ZIP-PLUS-FOUR    PIC X(4).
               20  ZZADDR-DOM-ZIP-PLUS-FOUR-ERR PIC X.
           15  ZZADDR-POSTAL-CODE REDEFINES ZZADDR-DOM-ZIP-AREA.
               20  ZZADDR-POSTAL-CODE-ERR      PIC X.
               20  ZZADDR-POSTAL-CODE          PIC X(9).
               20  FILLER                      PIC X.
       01  WS-AREA.
           05  WS-AREA                         PIC X(600).
       01  WS-AREA-R REDEFINES WS-AREA.
           05  WS-AREA-HEAD                    PIC X(10).
           05  FILLER                          PIC X(590).
       01  FILL.
           05  EDIT-AMT                        PIC ZZ9.99.
           05  EDIT-AMT-X REDEFINES EDIT-AMT.
               10  FILL                        PIC X.
       01  CY-REC-1.
           05  CY-B.
               10  CY-C                        PIC X(2).
       01  CY-REC-2.
           05  CY-C.
               10  CY-B                        PIC X(2).
       01  IN-ERR                              PIC X.
       01  OUT-ERR                             PIC X.
       01  OUT-HEAD                            PIC X(10).
       PROCEDURE DIVISION.
       0000-MAIN.
           ACCEPT IN-ERR
           MOVE IN-ERR TO ZZADDR-DOM-ZIP-ERR
           MOVE ZZADDR-POSTAL-CODE-ERR TO OUT-ERR
           ACCEPT WS-AREA
           MOVE WS-AREA-HEAD TO OUT-HEAD
           DISPLAY OUT-ERR OUT-HEAD
           MOVE FILL TO OUT-HEAD
           MOVE CY-REC-1 TO CY-REC-2
           DISPLAY OUT-HEAD CY-REC-2
           GOBACK.
