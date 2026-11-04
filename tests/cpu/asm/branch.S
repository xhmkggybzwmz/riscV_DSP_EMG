/* branch.S - every conditional branch taken and not taken, signed/unsigned
 * edge cases, backward loops and near-maximum branch offsets. */
#include "test_macros.h"

/* Branch must be taken. */
.macro BR_T op, ra, rb, num
    li   TESTNUM, \num
    \op  \ra, \rb, .Lt\@
    jal  x0, tb_fail
.Lt\@:
.endm

/* Branch must not be taken. */
.macro BR_N op, ra, rb, num
    li   TESTNUM, \num
    \op  \ra, \rb, .Lnf\@
    jal  x0, .Ln\@
.Lnf\@:
    jal  x0, tb_fail
.Ln\@:
.endm

    .text
    .globl test_main
test_main:
    li   a0, -1                  /* 0xffffffff */
    li   a1, 1
    li   a2, 0x80000000
    li   a3, 0x7fffffff
    li   a4, -1

    BR_T beq,  a0, a4, 1
    BR_N beq,  a0, a1, 2
    BR_T bne,  a0, a1, 3
    BR_N bne,  a0, a4, 4
    BR_T blt,  a0, a1, 5         /* -1 < 1 */
    BR_N blt,  a1, a0, 6
    BR_T blt,  a2, a3, 7         /* INT_MIN < INT_MAX */
    BR_N blt,  a0, a0, 8         /* equal */
    BR_T bge,  a1, a0, 9
    BR_T bge,  a0, a0, 10        /* equal */
    BR_N bge,  a0, a1, 11
    BR_N bge,  a2, a3, 12
    BR_T bltu, a1, a0, 13        /* 1 < 0xffffffff */
    BR_N bltu, a0, a1, 14
    BR_T bltu, a3, a2, 15        /* 0x7fffffff < 0x80000000 */
    BR_N bltu, a1, a1, 16
    BR_T bgeu, a0, a1, 17
    BR_T bgeu, a1, a1, 18
    BR_N bgeu, a1, a0, 19
    BR_N bgeu, a3, a2, 20
    BR_T beq,  x0, x0, 21
    BR_N bne,  x0, x0, 22
    BR_T blt,  x0, a1, 23
    BR_N blt,  x0, a0, 24        /* 0 < -1 is false */
    BR_T bltu, x0, a0, 25
    BR_T bge,  x0, a0, 26        /* 0 >= -1 */

    /* backward loop: 10 iterations */
    li   a5, 10
    li   a6, 0
1:  addi a6, a6, 1
    addi a5, a5, -1
    bne  a5, x0, 1b
    CHECK a6, 10, 30

    /* bltu-controlled loop counting to 0x20 */
    li   a5, 0
    li   a6, 0x20
2:  addi a5, a5, 4
    bltu a5, a6, 2b
    CHECK a5, 0x20, 31

    /* near-maximum offsets: forward ~+4 KB, then backward ~-4 KB */
    jal  x0, 3f
back_target:
    li   TESTNUM, 41
    jal  x0, far_done
3:  li   TESTNUM, 40
    beq  x0, x0, far_fwd
    .fill 1000, 4, 0x00000000    /* illegal words: reached only if a branch is wrong */
far_fwd:
    bne  a0, a1, back_target     /* backward, taken */
    jal  x0, tb_fail
far_done:
    mv    a7, TESTNUM             /* CHECK overwrites TESTNUM, so copy it first */
    CHECK a7, 41, 42

    PASS
