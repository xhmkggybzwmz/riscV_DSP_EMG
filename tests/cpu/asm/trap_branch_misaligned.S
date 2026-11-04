/* trap_branch_misaligned.S - not-taken branches to a misaligned target must NOT
 * trap; the taken one does: cause 0, tval = target. */
#include "test_macros.h"

    .text
    .globl test_main
test_main:
    li    a0, 1
    li    a1, 2
    li    a2, 0
    bne   x0, x0, target + 2     /* not taken */
    beq   a0, a1, target + 2     /* not taken */
    bge   x0, a0, target + 2     /* not taken */
    bltu  a1, a0, target + 2     /* not taken */
    li    a2, 0x77               /* proves execution continued */
    .globl tb_expect_pc
tb_expect_pc:
    blt   a0, a1, target + 2     /* taken -> trap */
    jal   x0, tb_fail
target:
    nop
    nop
    jal   x0, tb_fail

    TB_EXPECT tb_expect_cause, 0
    TB_EXPECT tb_expect_tval, target + 2
    TB_EXPECT tb_check_reg0, 12
    TB_EXPECT tb_check_val0, 0x77
