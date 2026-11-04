/* mbx_mg6_rt1.c - as mbx_mg6_rt with one run per segment (18 results, about 1.8 s): the
 * real-time variant short enough for the SoC simulation. TOHOST: 1 = PASS. */
#define E3C_PERIOD   50000u
#define E3C_TIMEOUT  30000000u
#define MG6_PASSES   1u
#include "mg6_common.h"

int main(void)
{
    return mg6_run();
}
