/* mbx_rt2k_long.c - E4a: 8 replay passes at the real 2 kHz rate in LOOP mode (88 results
 * at 20 Hz, about 5.6 s) for continuous PS consumption tests. TOHOST: 1 = PASS. */
#define E3C_PERIOD   50000u
#define E3C_SEQ_BASE 0u
#define E3C_TIMEOUT  30000000u
#include "mbx_common.h"

int main(void)
{
    return mbx_run(8, 0);
}
