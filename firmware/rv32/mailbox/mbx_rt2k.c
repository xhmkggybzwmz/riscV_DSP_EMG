/* mbx_rt2k.c - E4a at the real 2 kHz frame rate (replay PERIOD = 50000 cycles at 100 MHz):
 * 11 results 5 M cycles (50 ms, 20 Hz) apart, each published to the mailbox. The
 * firmware checks the result spacing from its own detection times (5 M +- 1000).
 * TOHOST: 1 = PASS. */
#define E3C_PERIOD   50000u
#define E3C_SEQ_BASE 0u
#define E3C_TIMEOUT  30000000u     /* first decision after 400 periods (20 M cycles) */
#include "mbx_common.h"

int main(void)
{
    return mbx_run(1, 0);
}
