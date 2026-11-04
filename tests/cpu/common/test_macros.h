/* test_macros.h - helpers for self-checking RV32I assembly tests.
 *
 * Conventions
 *   - Result reporting (simulation TOHOST at 0x1000_0008, docs/soc/memory_map.md):
 *       PASS            writes 1
 *       FAIL            writes (TESTNUM << 1) | 1
 *   - TESTNUM (x3 / gp) holds the number of the check in progress.
 *   - t6 (x31) is scratch for the CHECK macros; tests must not keep data in it.
 *   - crt0 sets every register x1..x31 to a known value before test_main;
 *     tests must still initialise each register before relying on it.
 *   - Only RV32I instructions (build uses -march=rv32i -mabi=ilp32).
 *
 * Testbench expectation symbols (read from the ELF with nm by
 * scripts/sw/build_asm_tests.py and passed as plusargs):
 *   tb_expect_cause / tb_expect_pc / tb_expect_tval   test must end in a trap
 *   tb_check_reg0 / tb_check_val0 (and ..1)           register value after the end
 *   tb_check_mem_addr / tb_check_mem_val              memory word after the end
 *   tb_probe_addr / tb_expect_probe_reads             count data reads of one word
 *   tb_i_err_lo / tb_i_err_hi, tb_d_err_lo / tb_d_err_hi   error-injection windows
 */
#ifndef TEST_MACROS_H
#define TEST_MACROS_H

#define TOHOST_ADDR 0x10000008
#define TESTNUM     x3

/* Load the absolute address of a symbol without auipc (so AUIPC itself can be tested). */
.macro LA_ABS reg, sym
    lui  \reg, %hi(\sym)
    addi \reg, \reg, %lo(\sym)
.endm

/* Fail with check number \num unless \reg == \val (immediate). */
.macro CHECK reg, val, num
    li   TESTNUM, \num
    li   t6, \val
    beq  \reg, t6, .Lck_ok\@
    jal  x0, tb_fail
.Lck_ok\@:
.endm

/* Fail with check number \num unless \ra == \rb. */
.macro CHECK_REGS ra, rb, num
    li   TESTNUM, \num
    beq  \ra, \rb, .Lckr_ok\@
    jal  x0, tb_fail
.Lckr_ok\@:
.endm

.macro PASS
    jal  x0, tb_pass
.endm

/* Declare a testbench expectation constant: TB_EXPECT name, value */
.macro TB_EXPECT name, value
    .globl \name
    .set   \name, \value
.endm

#endif
