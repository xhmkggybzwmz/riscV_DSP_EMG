/* alu.S - OP-IMM, OP, LUI, AUIPC, x0 behaviour and back-to-back dependencies. */
#include "test_macros.h"

    .text
    .globl test_main
test_main:
    /* ---------------- OP-IMM ---------------- */
    li    a0, 0x12345678
    addi  a1, a0, -1
    CHECK a1, 0x12345677, 1
    li    a0, 0x7fffffff
    addi  a1, a0, 1                 /* wraps */
    CHECK a1, 0x80000000, 2
    li    a0, -1
    slti  a1, a0, 0                 /* -1 < 0 signed */
    CHECK a1, 1, 3
    sltiu a1, a0, 0                 /* 0xffffffff < 0 unsigned: false */
    CHECK a1, 0, 4
    li    a0, 0
    sltiu a1, a0, 1                 /* seqz */
    CHECK a1, 1, 5
    li    a0, 5
    sltiu a1, a0, -1                /* imm sign-extends to 0xffffffff, compared unsigned */
    CHECK a1, 1, 6
    li    a0, 0x0f0f0f0f
    xori  a1, a0, -1
    CHECK a1, 0xf0f0f0f0, 7
    ori   a1, a0, 0x0f0
    CHECK a1, 0x0f0f0fff, 8
    andi  a1, a0, -16
    CHECK a1, 0x0f0f0f00, 9
    li    a0, 1
    slli  a1, a0, 31
    CHECK a1, 0x80000000, 10
    li    a0, 0x80000000
    srli  a1, a0, 31
    CHECK a1, 1, 11
    srai  a1, a0, 31
    CHECK a1, 0xffffffff, 12
    srai  a1, a0, 0
    CHECK a1, 0x80000000, 13
    srai  a1, a0, 4
    CHECK a1, 0xf8000000, 14

    /* ---------------- OP ---------------- */
    li    a0, 0x7fffffff
    li    a2, 1
    add   a1, a0, a2
    CHECK a1, 0x80000000, 15
    li    a0, 0
    sub   a1, a0, a2
    CHECK a1, 0xffffffff, 16
    li    a0, 1
    li    a2, 35                    /* only rs2[4:0] = 3 is used */
    sll   a1, a0, a2
    CHECK a1, 8, 17
    li    a0, 0x80000000
    li    a2, 33
    srl   a1, a0, a2
    CHECK a1, 0x40000000, 18
    sra   a1, a0, a2
    CHECK a1, 0xc0000000, 19
    li    a0, -2
    li    a2, 1
    slt   a1, a0, a2
    CHECK a1, 1, 20
    sltu  a1, a0, a2
    CHECK a1, 0, 21
    li    a0, 0x80000000
    li    a2, 0x7fffffff
    slt   a1, a0, a2
    CHECK a1, 1, 22
    sltu  a1, a0, a2
    CHECK a1, 0, 23
    slt   a1, a2, a0
    CHECK a1, 0, 24
    sltu  a1, a2, a0
    CHECK a1, 1, 25
    li    a0, 5
    slt   a1, a0, a0
    CHECK a1, 0, 26
    li    a0, 0xff00ff00
    li    a2, 0x0f0f0f0f
    xor   a1, a0, a2
    CHECK a1, 0xf00ff00f, 27
    or    a1, a0, a2
    CHECK a1, 0xff0fff0f, 28
    and   a1, a0, a2
    CHECK a1, 0x0f000f00, 29

    /* ---------------- LUI / AUIPC ---------------- */
    lui   a1, 0xfffff
    CHECK a1, 0xfffff000, 30
    lui   a1, 0x80000
    CHECK a1, 0x80000000, 31
    lui   a1, 0
    CHECK a1, 0, 32
auipc_0:
    auipc a1, 0
    LA_ABS a2, auipc_0
    CHECK_REGS a1, a2, 33
auipc_1:
    auipc a1, 0x1
    LA_ABS a2, auipc_1 + 0x1000
    CHECK_REGS a1, a2, 34
auipc_2:
    auipc a1, 0xfffff               /* pc - 0x1000, wraps below address 0 */
    LA_ABS a2, auipc_2 - 0x1000
    CHECK_REGS a1, a2, 35

    /* ---------------- x0 is hard-wired to zero ---------------- */
    addi  x0, x0, 5
    CHECK x0, 0, 36
    lui   x0, 0x12345
    CHECK x0, 0, 37
    li    a0, 7
    add   x0, a0, a0
    CHECK x0, 0, 38
    add   a1, x0, x0
    CHECK a1, 0, 39

    /* ---------------- dependency chains ---------------- */
    li    a0, 1
    add   a0, a0, a0
    add   a0, a0, a0
    add   a0, a0, a0
    add   a0, a0, a0
    add   a0, a0, a0
    add   a0, a0, a0
    add   a0, a0, a0
    add   a0, a0, a0
    add   a0, a0, a0
    add   a0, a0, a0
    CHECK a0, 1024, 40
    li    a0, 3
    addi  a1, a0, 4                 /* 7  */
    add   a2, a1, a0                /* 10 */
    sub   a3, a2, a1                /* 3  */
    xor   a4, a3, a2                /* 9  */
    sll   a5, a4, a3                /* 72 */
    sub   a6, x0, a5                /* -72 */
    srai  a7, a6, 3                 /* -9 */
    add   a7, a7, a4                /* 0 */
    CHECK a5, 72, 41
    CHECK a7, 0, 42

    PASS
