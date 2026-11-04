/* trap_sh_misaligned.S - SH at offset 3: cause 6, no bus write, memory unchanged. */
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
    li    a0, 0x5555
    .globl tb_expect_pc
tb_expect_pc:
    sh    a0, 3(s1)
    jal   x0, tb_fail

    TB_EXPECT tb_expect_cause, 6
    TB_EXPECT tb_expect_tval, buf + 3
    TB_EXPECT tb_check_mem_addr, buf
    TB_EXPECT tb_check_mem_val, 0xA1B2C3D4
