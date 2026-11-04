/* trap_ecall.S - ECALL halts with cause 11, tval 0. */
#include "test_macros.h"

    .text
    .globl test_main
test_main:
    li    a0, 1
    .globl tb_expect_pc
tb_expect_pc:
    ecall
    jal   x0, tb_fail

    TB_EXPECT tb_expect_cause, 11
    TB_EXPECT tb_expect_tval, 0
