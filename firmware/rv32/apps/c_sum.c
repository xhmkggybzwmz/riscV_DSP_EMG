/* c_sum.c - array sum, .data/.bss initialisation and a function call. */
#include <stdint.h>
#include "rv32_soc.h"

static int32_t data_init[8] = {1, -2, 3, -4, 5, -6, 7, 100000};   /* .data */
static int32_t bss_zero[16];                                         /* .bss  */
static int32_t sum_out;

static int32_t __attribute__((noinline)) sum(const int32_t *a, int n)
{
    int32_t s = 0;
    for (int i = 0; i < n; i++)
        s += a[i];
    return s;
}

int main(void)
{
    CHECK(sum(data_init, 8) == 100004, 1);
    for (int i = 0; i < 16; i++)
        CHECK(bss_zero[i] == 0, 2);
    int32_t big[64];
    for (int i = 0; i < 64; i++)
        big[i] = i * 3 - 50;                 /* stack array */
    sum_out = sum(big, 64);
    CHECK(sum_out == (3 * 63 * 64 / 2) - 50 * 64, 3);   /* 2848 */
    return 0;
}
