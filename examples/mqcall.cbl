      *================================================================*
      * MQCALL - drives IBM MQ through the MQI. COPY CMQV brings in the *
      * MQ definitions, and CALL 'MQOPEN' / 'MQPUT' are MQI verbs, so   *
      * both classify as ibm-runtime (ibm-mq): a runtime library, with  *
      * no application source to fetch.                                 *
      *================================================================*
       IDENTIFICATION DIVISION.
       PROGRAM-ID. MQCALL.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       COPY CMQV.
       01  W-HCONN     PIC S9(9) BINARY VALUE 0.
       01  W-HOBJ      PIC S9(9) BINARY VALUE 0.
       01  W-COMPCODE  PIC S9(9) BINARY.
       01  W-REASON    PIC S9(9) BINARY.
       01  W-BUFFER    PIC X(80).
       PROCEDURE DIVISION.
       0000-MAIN.
           CALL 'MQOPEN' USING W-HCONN W-HOBJ W-COMPCODE W-REASON
           CALL 'MQPUT'  USING W-HCONN W-HOBJ W-BUFFER W-COMPCODE
           GOBACK.
