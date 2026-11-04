/* trap_load_x0_fault.S - a load to x0 is still performed: from a data-error
 * window it faults with cause 5. */
#include "test_macros.h"

    .data
    .align 4
errbuf:
    .word 0x11111111, 0x22222222

    .text
    .globl test_main
test_main:
    LA_ABS s1, errbuf
    .globl tb_expect_pc
tb_expect_pc:
    lw    x0, 0(s1)
    jal   x0, tb_fail

    TB_EXPECT tb_d_err_lo, errbuf
    TB_EXPECT tb_d_err_hi, errbuf + 7
    TB_EXPECT tb_expect_cause, 5
    TB_EXPECT tb_expect_tval, errbuf
