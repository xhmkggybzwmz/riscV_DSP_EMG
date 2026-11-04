/* trap_load_fault.S - load from a data-error window: cause 5, tval = address,
 * rd not written. */
#include "test_macros.h"

    .data
    .align 4
errbuf:
    .word 0x11111111, 0x22222222, 0x33333333, 0x44444444

    .text
    .globl test_main
test_main:
    LA_ABS s1, errbuf
    li    a0, 0x5EB71E11
    .globl tb_expect_pc
tb_expect_pc:
    lw    a0, 4(s1)
    jal   x0, tb_fail

    TB_EXPECT tb_d_err_lo, errbuf
    TB_EXPECT tb_d_err_hi, errbuf + 15
    TB_EXPECT tb_expect_cause, 5
    TB_EXPECT tb_expect_tval, errbuf + 4
    TB_EXPECT tb_check_reg0, 10
    TB_EXPECT tb_check_val0, 0x5EB71E11
