/* trap_store_fault.S - store to an unmapped address: the request is accepted,
 * the response is an error: cause 7, tval = address. Earlier stores complete. */
#include "test_macros.h"

    .data
    .align 4
okbuf:
    .word 0

    .text
    .globl test_main
test_main:
    LA_ABS s1, okbuf
    li    a0, 0x0BADC0DE
    sw    a0, 0(s1)              /* succeeds */
    li    s2, 0x20000000
    .globl tb_expect_pc
tb_expect_pc:
    sw    a0, 0(s2)
    jal   x0, tb_fail

    TB_EXPECT tb_expect_cause, 7
    TB_EXPECT tb_expect_tval, 0x20000000
    TB_EXPECT tb_check_mem_addr, okbuf
    TB_EXPECT tb_check_mem_val, 0x0BADC0DE
