/* trap_jalr_misaligned.S - JALR with offset 1 is fine (bit 0 cleared);
 * JALR to base+2 traps: cause 0, tval = target, rd not written. */
#include "test_macros.h"

    .text
    .globl test_main
test_main:
    LA_ABS a3, ok_target
    jalr  x0, 1(a3)              /* (ok_target + 1) & ~1: aligned, no trap */
    jal   x0, tb_fail
ok_target:
    LA_ABS a3, target
    li    ra, 0x5EB71E11
    .globl tb_expect_pc
tb_expect_pc:
    jalr  ra, 2(a3)
    jal   x0, tb_fail
target:
    nop
    nop
    jal   x0, tb_fail

    TB_EXPECT tb_expect_cause, 0
    TB_EXPECT tb_expect_tval, target + 2
    TB_EXPECT tb_check_reg0, 1
    TB_EXPECT tb_check_val0, 0x5EB71E11
