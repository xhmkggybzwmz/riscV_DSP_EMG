/* crt0.S - start-up code for RV32I core tests (linked first, at the reset vector).
 *
 * 1. Initialise every integer register (the register file has no reset value).
 * 2. Set sp to the top of RAM, zero .bss.
 * 3. Jump to test_main.
 * tb_pass / tb_fail report the result through TOHOST and then spin.
 */
#include "test_macros.h"

    .section .text.init, "ax"
    .globl _start
_start:
    li   x1, 0
    li   x2, 0
    li   x3, 0x3FF              /* TESTNUM: non-zero so a stray tb_fail never reads as PASS */
    li   x4, 0
    li   x5, 0
    li   x6, 0
    li   x7, 0
    li   x8, 0
    li   x9, 0
    li   x10, 0
    li   x11, 0
    li   x12, 0
    li   x13, 0
    li   x14, 0
    li   x15, 0
    li   x16, 0
    li   x17, 0
    li   x18, 0
    li   x19, 0
    li   x20, 0
    li   x21, 0
    li   x22, 0
    li   x23, 0
    li   x24, 0
    li   x25, 0
    li   x26, 0
    li   x27, 0
    li   x28, 0
    li   x29, 0
    li   x30, 0
    li   x31, 0

    LA_ABS sp, __stack_top

    LA_ABS t0, __bss_start
    LA_ABS t1, __bss_end
1:  bgeu t0, t1, 2f
    sw   x0, 0(t0)
    addi t0, t0, 4
    jal  x0, 1b
2:  li   t0, 0
    li   t1, 0
    jal  x0, test_main

    .globl tb_pass
tb_pass:
    li   t0, 1
    li   t1, TOHOST_ADDR
    sw   t0, 0(t1)
3:  jal  x0, 3b

    .globl tb_fail
tb_fail:
    slli t0, TESTNUM, 1
    ori  t0, t0, 1
    li   t1, TOHOST_ADDR
    sw   t0, 0(t1)
4:  jal  x0, 4b
