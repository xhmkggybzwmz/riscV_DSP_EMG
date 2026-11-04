/* mbx_mmio.c - E4a: raw accesses of the RV32I to the mailbox window (no protocol; the
 * test bench runs it with +mon_off=1): word / halfword / byte stores and loads, both
 * ends of the 4 KB window (0x2002_0000 and 0x2002_0FFC, no aliasing), read after
 * write, a pattern over all 1024 words. Leaves the window zeroed. TOHOST: 1 = PASS. */
#include <stdint.h>
#include "rv32_soc.h"
#include "mbx_layout.h"

#define W(o) (*(volatile uint32_t *)(MBX_BASE + (o)))
#define H(o) (*(volatile uint16_t *)(MBX_BASE + (o)))
#define B(o) (*(volatile uint8_t *)(MBX_BASE + (o)))

int main(void)
{
    W(0x000) = 0x01234567u;
    W(0xFFC) = 0x89ABCDEFu;
    if (W(0x000) != 0x01234567u) return 1;
    if (W(0xFFC) != 0x89ABCDEFu) return 2;
    W(0x800) = 0;
    B(0x801) = 0x5Au;
    if (W(0x800) != 0x00005A00u) return 3;
    H(0x802) = 0xBEEFu;
    if (W(0x800) != 0xBEEF5A00u) return 4;
    if (B(0x803) != 0xBEu || H(0x800) != 0x5A00u) return 5;
    if (*(volatile int8_t *)(MBX_BASE + 0x803) != (int8_t)0xBE) return 6;
    for (uint32_t i = 0; i < MBX_SIZE / 4u; i++)
        W(4u * i) = i * 0x9E3779B9u;
    for (uint32_t i = 0; i < MBX_SIZE / 4u; i++)
        if (W(4u * i) != i * 0x9E3779B9u) return 7;
    for (uint32_t i = 0; i < MBX_SIZE / 4u; i++)
        W(4u * i) = 0;
    for (uint32_t i = 0; i < MBX_SIZE / 4u; i++)
        if (W(4u * i) != 0) return 8;
    return 0;
}
