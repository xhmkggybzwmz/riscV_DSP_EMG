/* rv32_soc.h - memory map of the RV32I SoC for bare-metal C (docs/soc/memory_map.md). */
#ifndef RV32_SOC_H
#define RV32_SOC_H

#include <stdint.h>

#define SYSCTRL_BASE      0x10000000u
#define SYSCTRL_ID        (*(volatile uint32_t *)(SYSCTRL_BASE + 0x00))
#define SYSCTRL_VERSION   (*(volatile uint32_t *)(SYSCTRL_BASE + 0x04))
#define SYSCTRL_TOHOST    (*(volatile uint32_t *)(SYSCTRL_BASE + 0x08))
#define SYSCTRL_SCRATCH   (*(volatile uint32_t *)(SYSCTRL_BASE + 0x0C))
#define SYSCTRL_LED       (*(volatile uint32_t *)(SYSCTRL_BASE + 0x10))
#define SYSCTRL_STATUS    (*(volatile uint32_t *)(SYSCTRL_BASE + 0x14))
#define SYSCTRL_CYCLE_LO  (*(volatile uint32_t *)(SYSCTRL_BASE + 0x18))
#define SYSCTRL_CYCLE_HI  (*(volatile uint32_t *)(SYSCTRL_BASE + 0x1C))

/* main() returns 0 for PASS or the number of the failing check; crt0 writes TOHOST. */
#define CHECK(cond, n)    do { if (!(cond)) return (n); } while (0)

#endif
