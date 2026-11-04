/* mbx_mg6.c - E4c: six real GRABMyo segments (one per class) in flow mode (simulation speed),
 * two runs per segment: 36 results checked against the E2 decisions and published.
 * TOHOST: 1 = PASS. */
#define MG6_PASSES 2u
#include "mg6_common.h"

int main(void)
{
    return mg6_run();
}
