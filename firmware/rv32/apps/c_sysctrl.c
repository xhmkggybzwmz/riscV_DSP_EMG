/* c_sysctrl.c - SYSCTRL registers through the real AXI4-Lite path (SoC only:
 * the core test bench has no SYSCTRL, so this program is not ISS-compared).
 * ID/VERSION, SCRATCH with byte stores, LED, STATUS before TOHOST, the cycle
 * counter, read-only and reserved offsets. */
#include <stdint.h>
#include "rv32_soc.h"

int main(void)
{
    CHECK(SYSCTRL_ID == 0x53595331u, 1);
    CHECK(SYSCTRL_VERSION == 0x00010000u, 2);
    CHECK(SYSCTRL_STATUS == 0, 3);                         /* TOHOST not written yet */

    SYSCTRL_SCRATCH = 0x11223344u;
    CHECK(SYSCTRL_SCRATCH == 0x11223344u, 4);
    *(volatile uint8_t *)(SYSCTRL_BASE + 0x0D) = 0xAB;     /* byte store: WSTRB = 0010 */
    CHECK(SYSCTRL_SCRATCH == 0x1122AB44u, 5);
    *(volatile uint16_t *)(SYSCTRL_BASE + 0x0E) = 0xCDEF;  /* half store: WSTRB = 1100 */
    CHECK(SYSCTRL_SCRATCH == 0xCDEFAB44u, 6);

    SYSCTRL_LED = 0x5A;
    CHECK(SYSCTRL_LED == 0x5A, 7);

    SYSCTRL_ID = 0xFFFFFFFFu;                              /* read-only: ignored */
    CHECK(SYSCTRL_ID == 0x53595331u, 8);
    *(volatile uint32_t *)(SYSCTRL_BASE + 0x100) = 0x12345678u;   /* reserved: ignored */
    CHECK(*(volatile uint32_t *)(SYSCTRL_BASE + 0x100) == 0, 9);

    uint32_t c0 = SYSCTRL_CYCLE_LO;
    for (volatile int i = 0; i < 20; i++) { }
    uint32_t c1 = SYSCTRL_CYCLE_LO;
    CHECK(c1 - c0 > 100, 10);                              /* the counter runs */
    CHECK(SYSCTRL_CYCLE_HI == 0, 11);
    return 0;
}
