      *================================================================*
      * NESTEDPGM - an outer program CONTAINING a nested program USEMQ. *
      * CALL 'USEMQ' is an INTERNAL call (USEMQ is contained here), so   *
      * it classifies internal-nested, not a missing external module.   *
      * CALL 'ABENDL' is a site utility with no source and no recognized *
      * IBM API, so it stays honestly `unresolved` - never guessed.     *
      * The nested body must NOT fold into the outer program's logic.   *
      *================================================================*
       IDENTIFICATION DIVISION.
       PROGRAM-ID. NESTEDPGM.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-CODE    PIC 9(4) VALUE 0.
       PROCEDURE DIVISION.
       0000-MAIN.
           CALL 'USEMQ' USING WS-CODE
           CALL 'ABENDL' USING WS-CODE
           GOBACK.
       IDENTIFICATION DIVISION.
       PROGRAM-ID. USEMQ.
       DATA DIVISION.
       LINKAGE SECTION.
       01  LK-CODE    PIC 9(4).
       PROCEDURE DIVISION USING LK-CODE.
       0000-INNER.
           MOVE 1 TO LK-CODE
           GOBACK.
       END PROGRAM USEMQ.
       END PROGRAM NESTEDPGM.
