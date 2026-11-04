/* mbx_mg6_p4.c - E4c: the run sequence of mbx_mg6_rt (six segments, four runs each, 72 results)
 * in flow mode, for the full-length SoC simulation; only the 2 kHz pacing and its timing checks
 * (E3C_PERIOD != 0) differ from mbx_mg6_rt. TOHOST: 1 = PASS. */
#define MG6_PASSES 4u
#include "mg6_common.h"

int main(void)
{
    return mg6_run();
}
