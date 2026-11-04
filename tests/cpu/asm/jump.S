/* jump.S - JAL / JALR: link value pc+4, rd = x0, backward jumps, JALR bit-0
 * clearing, negative offsets and rd == rs1. */
#include "test_macros.h"

    .text
    .globl test_main
test_main:
    /* JAL writes pc+4 */
jal_site:
    jal   ra, 1f
    jal   x0, tb_fail
1:  LA_ABS a1, jal_site + 4
    CHECK_REGS ra, a1, 1

    /* JAL with rd = x0 */
    jal   x0, 2f
    jal   x0, tb_fail
2:  CHECK x0, 0, 2

    /* backward JAL */
    li    a2, 0
    jal   x0, 4f
3:  li    a2, 0x33
    jal   x0, 5f
jal_back_site:
4:  jal   t0, 3b
5:  CHECK a2, 0x33, 3
    LA_ABS a1, jal_back_site + 4
    CHECK_REGS t0, a1, 4

    /* JALR basic */
    LA_ABS a3, jt1
jalr_site:
    jalr  ra, 0(a3)
    jal   x0, tb_fail
jt1:
    LA_ABS a1, jalr_site + 4
    CHECK_REGS ra, a1, 5

    /* JALR target bit 0 is cleared: base + 1 lands on jt2 (no trap) */
    LA_ABS a3, jt2
jalr_odd_site:
    jalr  ra, 1(a3)
    jal   x0, tb_fail
jt2:
    LA_ABS a1, jalr_odd_site + 4
    CHECK_REGS ra, a1, 6

    /* odd base, even offset: (jt3 - 3) + 3 */
    LA_ABS a3, jt3 - 3
    jalr  ra, 3(a3)
    jal   x0, tb_fail
jt3:
    /* negative offset */
    LA_ABS a3, jt4 + 8
    jalr  x0, -8(a3)
    jal   x0, tb_fail
jt4:
    /* rd == rs1: target uses the old value, then rd gets pc+4 */
    LA_ABS ra, jt5
jalr_same_site:
    jalr  ra, 0(ra)
    jal   x0, tb_fail
jt5:
    LA_ABS a1, jalr_same_site + 4
    CHECK_REGS ra, a1, 7

    /* JALR with rd = x0 does not write x0 */
    LA_ABS a3, jt6
    jalr  x0, 0(a3)
    jal   x0, tb_fail
jt6:
    CHECK x0, 0, 8

    /* call / return through a register */
    li    a0, 41
    jal   ra, inc_fn
    CHECK a0, 42, 9

    /* the branch after a JAL target must still see correct state */
    li    a4, 3
    jal   ra, 9f
    jal   x0, tb_fail
9:  addi  a4, a4, -1
    bnez  a4, 9b                 /* backward branch to a JAL target, 3 times */
    CHECK a4, 0, 10

    PASS

inc_fn:
    addi  a0, a0, 1
    jalr  x0, 0(ra)
