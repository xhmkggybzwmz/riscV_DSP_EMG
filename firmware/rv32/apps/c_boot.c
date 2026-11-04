/* c_boot.c - minimal start-up check: crt0 (register init, sp/gp, .bss clear,
 * call main) followed by TOHOST PASS through cmi2axil -> SmartConnect ->
 * SYSCTRL. First program run on the KV260 board (stage 6A). */
#include "rv32_soc.h"

int main(void)
{
    return 0;
}
