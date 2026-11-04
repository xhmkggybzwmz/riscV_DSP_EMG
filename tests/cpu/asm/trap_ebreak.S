/* trap_ebreak.S - EBREAK halts with cause 3, tval = pc. */
#include "test_macros.h"

    .text
    .globl test_main
test_main:
    li    a0, 1
    .globl tb_expect_pc
tb_expect_pc:
    ebreak
    jal   x0, tb_fail

    TB_EXPECT tb_expect_cause, 3
    TB_EXPECT tb_expect_tval, tb_expect_pc
