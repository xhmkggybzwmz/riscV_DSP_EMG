/* trap_illegal_csr.S - CSRRW (Zicsr not implemented) is illegal; rd must not be written. */
#include "test_macros.h"

    .text
    .globl test_main
test_main:
    li    a0, 0x5EB71E11
    li    a1, 0x12345678
    .globl tb_expect_pc
tb_expect_pc:
    .word 0x30059573             /* csrrw a0, mstatus, a1 */
    jal   x0, tb_fail

    TB_EXPECT tb_expect_cause, 2
    TB_EXPECT tb_expect_tval, 0x30059573
    TB_EXPECT tb_check_reg0, 10
    TB_EXPECT tb_check_val0, 0x5EB71E11
