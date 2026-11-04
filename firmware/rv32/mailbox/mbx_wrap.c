/* mbx_wrap.c - E4a: as mbx_loop2 with the epoch starting at PRODUCER_INDEX = 0xFFFFFFF8, so
 * the free-running 32-bit indices wrap to 0 after 8 records. TOHOST: 1 = PASS. */
#include "mbx_common.h"

int main(void)
{
    return mbx_run(2, 0xFFFFFFF8u);
}
