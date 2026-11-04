/* trap_jal_misaligned.S - JAL to a target with bit 1 set: cause 0, tval = target,
 * the link register must NOT be written. */
#include "test_macros.h"

    .text
    .globl test_main
test_main:
    li    ra, 0x5EB71E11
    .globl tb_expect_pc
tb_expect_pc:
    jal   ra, target + 2
    jal   x0, tb_fail
target:
    nop
    nop
    jal   x0, tb_fail

    TB_EXPECT tb_expect_cause, 0
    TB_EXPECT tb_expect_tval, target + 2
    TB_EXPECT tb_check_reg0, 1
    TB_EXPECT tb_check_val0, 0x5EB71E11
