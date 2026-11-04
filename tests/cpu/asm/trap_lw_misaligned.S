/* trap_lw_misaligned.S - LW at offset 2: cause 4, tval = address, rd not written. Aligned/byte accesses before it work. */
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
    lh    a1, 2(s1)              /* aligned half: ok */
    lb    a1, 3(s1)              /* byte: never misaligned */
    li    a0, 0x5EB71E11
    .globl tb_expect_pc
tb_expect_pc:
    lw    a0, 2(s1)
    jal   x0, tb_fail

    TB_EXPECT tb_expect_cause, 4
    TB_EXPECT tb_expect_tval, buf + 2
    TB_EXPECT tb_check_reg0, 10
    TB_EXPECT tb_check_val0, 0x5EB71E11
