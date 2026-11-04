/* mbx_flow.c - E4a: one replay pass in flow mode (11 results); every result is checked
 * against E2, post-processed (K = 1, T = 0) and published to the mailbox, then the CNN
 * result FIFO is acknowledged. TOHOST: 1 = PASS. */
#include "mbx_common.h"

int main(void)
{
    return mbx_run(1, 0);
}
