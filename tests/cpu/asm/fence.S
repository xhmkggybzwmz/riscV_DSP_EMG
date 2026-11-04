/* fence.S - FENCE variants execute as NOPs without breaking memory ordering.
 * The testbench additionally checks that no CMI transaction is outstanding
 * when any instruction (including each FENCE) retires. */
#include "test_macros.h"

    .bss
    .align 4
buf:
    .space 32

    .text
    .globl test_main
test_main:
    LA_ABS s1, buf

    li    a0, 0x13579BDF
    sw    a0, 0(s1)
    fence
    lw    a1, 0(s1)
    CHECK a1, 0x13579BDF, 1

    li    a0, 0x2468ACE0
    sw    a0, 4(s1)
    fence rw, rw
    lw    a1, 4(s1)
    CHECK a1, 0x2468ACE0, 2

    li    a0, 0x5A
    sb    a0, 9(s1)
    fence w, r
    lbu   a1, 9(s1)
    CHECK a1, 0x5A, 3

    /* store burst, one fence, then read everything back */
    li    a0, 0x11111111
    sw    a0, 12(s1)
    li    a0, 0x22222222
    sw    a0, 16(s1)
    li    a0, 0x3333
    sh    a0, 20(s1)
    li    a0, 0x44
    sb    a0, 22(s1)
    fence iorw, iorw
    lw    a1, 12(s1)
    CHECK a1, 0x11111111, 4
    lw    a1, 16(s1)
    CHECK a1, 0x22222222, 5
    lw    a1, 20(s1)
    CHECK a1, 0x00443333, 6

    /* FENCE.TSO, PAUSE and a FENCE with non-zero rd/rs1 fields (ignored, no write) */
    li    a0, 0x600DF00D
    .word 0x8330000F             /* fence.tso */
    .word 0x0100000F             /* pause */
    .word 0x0FF5850F             /* fence iorw,iorw with rd=x10, rs1=x11 */
    CHECK a0, 0x600DF00D, 7

    /* fence between dependent ALU ops changes nothing */
    li    a2, 5
    fence
    addi  a2, a2, 1
    fence
    CHECK a2, 6, 8

    PASS
