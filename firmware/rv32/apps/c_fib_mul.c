/* c_fib_mul.c - iterative/recursive Fibonacci and software multiply/divide
 * (RV32I has no M extension: '*', '/' and '%' become libgcc calls). */
#include <stdint.h>
#include "rv32_soc.h"

static uint32_t fib_iter(int n)
{
    uint32_t a = 0, b = 1;
    for (int i = 0; i < n; i++) {
        uint32_t t = a + b;
        a = b;
        b = t;
    }
    return a;
}

static uint32_t __attribute__((noinline)) fib_rec(int n)
{
    return n < 2 ? (uint32_t)n : fib_rec(n - 1) + fib_rec(n - 2);
}

static volatile int32_t va = -1234, vb = 5678;          /* volatile: keep the calls */
static volatile uint32_t vu = 0xDEADBEEFu, vd = 12345u;

int main(void)
{
    CHECK(fib_iter(30) == 832040u, 1);
    CHECK(fib_iter(47) == 2971215073u, 2);
    CHECK(fib_rec(15) == 610u, 3);
    CHECK(va * vb == -7006652, 4);                        /* __mulsi3 */
    CHECK(vu * 3u == 0x9C093CCDu, 5);
    CHECK(vu / vd == 302626u, 6);                         /* __udivsi3 */
    CHECK(vu % vd == 10589u, 7);                          /* __umodsi3 */
    CHECK(va / 7 == -176, 8);                             /* __divsi3, truncates toward 0 */
    CHECK(va % 7 == -2, 9);
    return 0;
}
