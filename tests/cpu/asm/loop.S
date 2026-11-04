/* loop.S - loops and dependent instruction sequences. The program only reads
 * .rodata and writes .bss (zeroed by crt0), so it can be restarted after a
 * reset: the testbench reset test uses it. */
#include "test_macros.h"

    .section .rodata
    .align 4
src_bytes:
    .byte 0x01, 0x23, 0x45, 0x67, 0x89, 0xab, 0xcd, 0xef
    .byte 0xfe, 0xdc, 0xba, 0x98, 0x76, 0x54, 0x32, 0x10
    .byte 0x00, 0xff, 0x80, 0x7f, 0x11, 0x22, 0x33, 0x44
    .byte 0x55, 0x66, 0x77, 0x88, 0x99, 0xaa, 0xbb, 0xcc

    .bss
    .align 4
dst_bytes:
    .space 32

    .text
    .globl test_main
test_main:
    /* sum 1..100 */
    li    a0, 0
    li    a1, 1
    li    a2, 101
1:  add   a0, a0, a1
    addi  a1, a1, 1
    bne   a1, a2, 1b
    CHECK a0, 5050, 1

    /* nested 10 x 10 */
    li    a0, 0
    li    a1, 10
2:  li    a2, 10
3:  addi  a0, a0, 1
    addi  a2, a2, -1
    bnez  a2, 3b
    addi  a1, a1, -1
    bnez  a1, 2b
    CHECK a0, 100, 2

    /* iterative fibonacci: fib(30) */
    li    a0, 0
    li    a1, 1
    li    a2, 30
4:  add   a3, a0, a1
    mv    a0, a1
    mv    a1, a3
    addi  a2, a2, -1
    bnez  a2, 4b
    CHECK a0, 832040, 3

    /* shift-and-add multiply in a loop: 0x1234 * 0x5678 */
    li    a0, 0x1234
    li    a1, 0x5678
    li    a2, 0
5:  andi  t0, a1, 1
    beqz  t0, 6f
    add   a2, a2, a0
6:  slli  a0, a0, 1
    srli  a1, a1, 1
    bnez  a1, 5b
    CHECK a2, 0x06260060, 4

    /* byte copy .rodata -> .bss, then compare words */
    LA_ABS s1, src_bytes
    LA_ABS s2, dst_bytes
    li    a1, 32
    mv    t1, s1
    mv    t2, s2
7:  lbu   t0, 0(t1)
    sb    t0, 0(t2)
    addi  t1, t1, 1
    addi  t2, t2, 1
    addi  a1, a1, -1
    bnez  a1, 7b
    li    a1, 8
    mv    t1, s1
    mv    t2, s2
    li    a4, 0                    /* checksum */
8:  lw    t0, 0(t1)
    lw    a3, 0(t2)
    li    TESTNUM, 5
    bne   t0, a3, tb_fail_far
    add   a4, a4, t0
    addi  t1, t1, 4
    addi  t2, t2, 4
    addi  a1, a1, -1
    bnez  a1, 8b
    CHECK a4, 0x18E731FD, 6

    /* count set bits of 0xF0F0F00F with a shift loop */
    li    a0, 0xF0F0F00F
    li    a1, 0
9:  andi  t0, a0, 1
    add   a1, a1, t0
    srli  a0, a0, 1
    bnez  a0, 9b
    CHECK a1, 16, 7

    PASS

tb_fail_far:
    jal   x0, tb_fail
