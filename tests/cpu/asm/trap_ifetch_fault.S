/* trap_ifetch_fault.S - jump into an instruction-error window: the JALR retires
 * (link written), then the fetch at the target fails: cause 1, pc = tval = target. */
#include "test_macros.h"

    .text
    .globl test_main
test_main:
    li    a3, 0x8000
    li    ra, 0x5EB71E11
jump_site:
    jalr  ra, 0(a3)
    jal   x0, tb_fail

    TB_EXPECT tb_i_err_lo, 0x8000
    TB_EXPECT tb_i_err_hi, 0x8fff
    TB_EXPECT tb_expect_cause, 1
    TB_EXPECT tb_expect_pc, 0x8000
    TB_EXPECT tb_expect_tval, 0x8000
    TB_EXPECT tb_check_reg0, 1
    TB_EXPECT tb_check_val0, jump_site + 4
