/* mbx_loop2.c - E4a: two replay passes in LOOP mode (22 results, more than the 16 ring
 * slots): without a consumer the first 16 are published and 6 dropped (ring full, no
 * overwrite, the CNN is never stalled). TOHOST: 1 = PASS. */
#include "mbx_common.h"

int main(void)
{
    return mbx_run(2, 0);
}
