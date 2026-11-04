/* trap_lh_misaligned.S - LHU at offset 1: cause 4, tval = address, rd not written. */
#include "test_macros.h"

    .data
    .align 4
buf:
    .word 0xA1B2C3D4
    .word 0x0F1E2D3C

    .text
    .globl test_main
test_main:
    LA_ABS s1, buf
    li    a0, 0x5EB71E11
    .globl tb_expect_pc
tb_expect_pc:
    lhu   a0, 1(s1)
    jal   x0, tb_fail

    TB_EXPECT tb_expect_cause, 4
    TB_EXPECT tb_expect_tval, buf + 1
    TB_EXPECT tb_check_reg0, 10
    TB_EXPECT tb_check_val0, 0x5EB71E11
