/* crt0.S - start-up code for bare-metal C programs on the RV32I SoC.
 *
 * Reset vector 0 (docs/soc/memory_map.md). The whole image (code, rodata,
 * data) is in the local RAM, so .data needs no copy; .bss is zeroed here.
 *   1. initialise x1..x31 (the register file has no reset value)
 *   2. sp = top of RAM, gp = __global_pointer$ (linker relaxation is disabled,
 *      gp is set anyway for code built with relaxation in the future)
 *   3. zero .bss, call main()
 *   4. report through SYSCTRL TOHOST: main() == 0 -> 1 (PASS),
 *      main() == n -> (n << 1) | 1 (FAIL with code n); then spin
 */
    .section .text.init, "ax"
    .globl _start
_start:
    li   x1, 0
    li   x2, 0
    li   x3, 0
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

    .option push
    .option norelax
    la   sp, __stack_top
    la   gp, __global_pointer$
    .option pop

    la   t0, __bss_start
    la   t1, __bss_end
1:  bgeu t0, t1, 2f
    sw   zero, 0(t0)
    addi t0, t0, 4
    j    1b
2:
    call main

    beqz a0, 3f
    slli a0, a0, 1
    ori  a0, a0, 1
    j    4f
3:  li   a0, 1
4:  li   t0, 0x10000008          /* SYSCTRL TOHOST */
    sw   a0, 0(t0)
5:  j    5b
