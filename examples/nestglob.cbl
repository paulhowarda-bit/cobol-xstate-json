      *================================================================*
      * NESTGLOB - GLOBAL data is seen by the programs contained here.  *
      * GLOBLEAF calls through WS-LOGPGM, under NESTGLOB's GLOBAL       *
      * record, and through WS-OWNPGM: GLOBMID declares its own, but    *
      * not GLOBAL, so the one GLOBLEAF sees is NESTGLOB's. GLOBMID     *
      * calls its own WS-OWNPGM, which hides the outer one. NESTGLOB    *
      * calls WS-AUDPGM, which it never sets: GLOBMID's SET AUDIT-V2    *
      * stores into the item they share.                                *
      *================================================================*
       IDENTIFICATION DIVISION.
       PROGRAM-ID. NESTGLOB.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-SHARED  GLOBAL.
           05  WS-LOGPGM  PIC X(8) VALUE 'GLOBLOG'.
           05  WS-AUDPGM  PIC X(8).
               88  AUDIT-V2  VALUE 'AUDITV2'.
       01  WS-OWNPGM  PIC X(8) VALUE 'OUTRMOD' GLOBAL.
       PROCEDURE DIVISION.
       0000-MAIN.
           CALL 'GLOBMID'
           CALL WS-AUDPGM
           GOBACK.
       IDENTIFICATION DIVISION.
       PROGRAM-ID. GLOBMID.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-OWNPGM  PIC X(8) VALUE 'MIDMOD'.
       PROCEDURE DIVISION.
       1000-MID.
           SET AUDIT-V2 TO TRUE
           CALL WS-OWNPGM
           CALL 'GLOBLEAF'
           GOBACK.
       IDENTIFICATION DIVISION.
       PROGRAM-ID. GLOBLEAF.
       PROCEDURE DIVISION.
       2000-LEAF.
           CALL WS-LOGPGM
           CALL WS-OWNPGM
           GOBACK.
       END PROGRAM GLOBLEAF.
       END PROGRAM GLOBMID.
       END PROGRAM NESTGLOB.
