/* trap_illegal_zero.S - the all-zero word is illegal: cause 2, tval = instruction. */
#include "test_macros.h"

    .text
    .globl test_main
test_main:
    li    a0, 0x5EB71E11
    .globl tb_expect_pc
tb_expect_pc:
    .word 0x00000000
    jal   x0, tb_fail

    TB_EXPECT tb_expect_cause, 2
    TB_EXPECT tb_expect_tval, 0x00000000
