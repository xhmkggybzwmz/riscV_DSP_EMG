/* trap_ifetch_unmapped.S - fetching from the peripheral region (not executable,
 * not RAM) fails: cause 1, pc = tval = 0x10000000. */
#include "test_macros.h"

    .text
    .globl test_main
test_main:
    li    a3, 0x10000000
    jalr  x0, 0(a3)
    jal   x0, tb_fail

    TB_EXPECT tb_expect_cause, 1
    TB_EXPECT tb_expect_pc, 0x10000000
    TB_EXPECT tb_expect_tval, 0x10000000
