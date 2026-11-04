/* call.S - function calls with a stack: leaf and non-leaf functions,
 * argument passing, callee-saved registers, recursion (sum, fibonacci,
 * factorial using a shift-and-add multiply). */
#include "test_macros.h"

    .text
    .globl test_main
test_main:
    li    s0, 0x5A5A0000         /* callee-saved values must survive calls */
    li    s1, 0x0000A5A5

    li    a0, 5
    jal   ra, sum_to_n           /* 5+4+3+2+1 */
    CHECK a0, 15, 1

    li    a0, 10
    jal   ra, fib                /* fib(10) */
    CHECK a0, 55, 2

    li    a0, 1234
    li    a1, 5678
    jal   ra, mul32
    CHECK a0, 7006652, 3

    li    a0, 10
    jal   ra, fact               /* 10! */
    CHECK a0, 3628800, 4

    li    a0, 1
    li    a1, 2
    li    a2, 3
    li    a3, 4
    li    a4, 5
    li    a5, 6
    li    a6, 7
    li    a7, 8
    jal   ra, weighted_sum       /* sum(k * a_k) = 1+4+9+...+64 = 204 */
    CHECK a0, 204, 5

    CHECK s0, 0x5A5A0000, 6
    CHECK s1, 0x0000A5A5, 7
    LA_ABS t0, __stack_top
    CHECK_REGS sp, t0, 8         /* stack balanced */

    PASS

/* a0 = n -> a0 = n + (n-1) + ... + 1 (recursive) */
sum_to_n:
    addi  sp, sp, -8
    sw    ra, 4(sp)
    sw    s0, 0(sp)
    mv    s0, a0
    beqz  a0, 1f
    addi  a0, a0, -1
    jal   ra, sum_to_n
    add   a0, a0, s0
1:  lw    s0, 0(sp)
    lw    ra, 4(sp)
    addi  sp, sp, 8
    ret

/* a0 = n -> a0 = fib(n) (doubly recursive) */
fib:
    addi  sp, sp, -12
    sw    ra, 8(sp)
    sw    s0, 4(sp)
    sw    s1, 0(sp)
    li    t0, 2
    blt   a0, t0, 2f
    mv    s0, a0
    addi  a0, s0, -1
    jal   ra, fib
    mv    s1, a0
    addi  a0, s0, -2
    jal   ra, fib
    add   a0, a0, s1
2:  lw    s1, 0(sp)
    lw    s0, 4(sp)
    lw    ra, 8(sp)
    addi  sp, sp, 12
    ret

/* a0 = a0 * a1 (low 32 bits), shift-and-add, leaf function */
mul32:
    li    t0, 0
3:  beqz  a1, 5f
    andi  t1, a1, 1
    beqz  t1, 4f
    add   t0, t0, a0
4:  slli  a0, a0, 1
    srli  a1, a1, 1
    jal   x0, 3b
5:  mv    a0, t0
    ret

/* a0 = n -> n! (recursive, calls mul32) */
fact:
    addi  sp, sp, -8
    sw    ra, 4(sp)
    sw    s0, 0(sp)
    li    t0, 2
    blt   a0, t0, 6f
    mv    s0, a0
    addi  a0, a0, -1
    jal   ra, fact
    mv    a1, s0
    jal   ra, mul32
    jal   x0, 7f
6:  li    a0, 1
7:  lw    s0, 0(sp)
    lw    ra, 4(sp)
    addi  sp, sp, 8
    ret

/* a0..a7 -> sum(k * a_{k-1}), k = 1..8 */
weighted_sum:
    addi  sp, sp, -4
    sw    ra, 0(sp)
    mv    t2, a0                 /* k=1 */
    slli  t0, a1, 1              /* k=2 */
    add   t2, t2, t0
    slli  t0, a2, 1
    add   t0, t0, a2             /* k=3 */
    add   t2, t2, t0
    slli  t0, a3, 2              /* k=4 */
    add   t2, t2, t0
    slli  t0, a4, 2
    add   t0, t0, a4             /* k=5 */
    add   t2, t2, t0
    slli  t0, a5, 2
    slli  t1, a5, 1
    add   t0, t0, t1             /* k=6 */
    add   t2, t2, t0
    slli  t0, a6, 3
    sub   t0, t0, a6             /* k=7 */
    add   t2, t2, t0
    slli  t0, a7, 3              /* k=8 */
    add   a0, t2, t0
    lw    ra, 0(sp)
    addi  sp, sp, 4
    ret
