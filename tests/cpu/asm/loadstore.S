/* loadstore.S - LB/LH/LW/LBU/LHU at every byte offset with sign bits,
 * SB/SH/SW byte-lane placement, negative offsets, store->load dependency
 * and loads to x0 (the access must still happen: counted by the testbench
 * through tb_probe_addr / tb_expect_probe_reads). */
#include "test_macros.h"

    .data
    .align 4
ld_data:
    .word 0x80FF7F01
    .word 0x8001FFFE
    .word 0x12345678
    .word 0xFFFFFFFF
st_area:
    .space 16
st_guard:
    .word 0xC0FFEE00             /* must never be modified */
    .globl tb_probe_addr
tb_probe_addr:
    .word 0x0BADF00D

    TB_EXPECT tb_expect_probe_reads, 3

    .text
    .globl test_main
test_main:
    LA_ABS s1, ld_data

    /* ---- LB / LBU ---- */
    lb    a0, 0(s1)
    CHECK a0, 0x00000001, 1
    lb    a0, 1(s1)
    CHECK a0, 0x0000007f, 2
    lb    a0, 2(s1)
    CHECK a0, 0xffffffff, 3
    lb    a0, 3(s1)
    CHECK a0, 0xffffff80, 4
    lbu   a0, 2(s1)
    CHECK a0, 0x000000ff, 5
    lbu   a0, 3(s1)
    CHECK a0, 0x00000080, 6
    lbu   a0, 1(s1)
    CHECK a0, 0x0000007f, 7

    /* ---- LH / LHU ---- */
    lh    a0, 0(s1)
    CHECK a0, 0x00007f01, 8
    lh    a0, 2(s1)
    CHECK a0, 0xffff80ff, 9
    lh    a0, 4(s1)
    CHECK a0, 0xfffffffe, 10
    lh    a0, 6(s1)
    CHECK a0, 0xffff8001, 11
    lhu   a0, 4(s1)
    CHECK a0, 0x0000fffe, 12
    lhu   a0, 6(s1)
    CHECK a0, 0x00008001, 13
    lhu   a0, 2(s1)
    CHECK a0, 0x000080ff, 14

    /* ---- LW ---- */
    lw    a0, 8(s1)
    CHECK a0, 0x12345678, 15
    lw    a0, 12(s1)
    CHECK a0, 0xffffffff, 16
    lw    a0, 0(s1)
    CHECK a0, 0x80FF7F01, 17

    /* ---- negative offsets ---- */
    addi  s2, s1, 16
    lw    a0, -8(s2)
    CHECK a0, 0x12345678, 18
    lb    a0, -1(s2)
    CHECK a0, 0xffffffff, 19
    lhu   a0, -10(s2)
    CHECK a0, 0x00008001, 20

    /* ---- stores: lane placement, read back with LW ---- */
    LA_ABS s3, st_area
    li    t0, 0x11223344
    sw    t0, 0(s3)
    lw    a0, 0(s3)                    /* store -> load, same address */
    CHECK a0, 0x11223344, 21
    li    t0, 0xFFFFFFAA               /* only the low byte is stored */
    sb    t0, 1(s3)
    lw    a0, 0(s3)
    CHECK a0, 0x1122AA44, 22
    li    t0, 0x000000BB
    sb    t0, 3(s3)
    lw    a0, 0(s3)
    CHECK a0, 0xBB22AA44, 23
    li    t0, 0x7777CCDD               /* only the low half is stored */
    sh    t0, 0(s3)
    lw    a0, 0(s3)
    CHECK a0, 0xBB22CCDD, 24
    li    t0, 0xEEFF
    sh    t0, 2(s3)
    lw    a0, 0(s3)
    CHECK a0, 0xEEFFCCDD, 25

    li    t0, 0x01
    sb    t0, 4(s3)
    li    t0, 0x02
    sb    t0, 5(s3)
    li    t0, 0x03
    sb    t0, 6(s3)
    li    t0, 0x04
    sb    t0, 7(s3)
    lw    a0, 4(s3)
    CHECK a0, 0x04030201, 26
    lbu   a0, 7(s3)
    CHECK a0, 0x04, 27
    lh    a0, 6(s3)
    CHECK a0, 0x0403, 28

    addi  s4, s3, 12
    li    t0, 0xDEADBEEF
    sw    t0, -4(s4)                   /* st_area + 8 */
    lw    a0, 8(s3)
    CHECK a0, 0xDEADBEEF, 29
    lw    a0, 12(s3)                   /* untouched neighbour */
    CHECK a0, 0, 30
    LA_ABS t1, st_guard
    lw    a0, 0(t1)
    CHECK a0, 0xC0FFEE00, 31

    /* ---- loads to x0: x0 stays 0, but each access is performed ---- */
    LA_ABS s5, tb_probe_addr
    lw    x0, 0(s5)
    lb    x0, 1(s5)
    lhu   x0, 2(s5)
    CHECK x0, 0, 32

    PASS
