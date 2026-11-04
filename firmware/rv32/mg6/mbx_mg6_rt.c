/* mbx_mg6_rt.c - E4c at the real 2 kHz frame rate (replay PERIOD = 50000 cycles at 100 MHz):
 * six real GRABMyo segments, four runs each (72 results, about 7.2 s); within a run the
 * results are 50 ms apart (checked from the detection times, 5 M +- 1000 cycles).
 * TOHOST: 1 = PASS. */
#define E3C_PERIOD   50000u
#define E3C_TIMEOUT  30000000u     /* first decision of a run after 400 periods (20 M cycles) */
#define MG6_PASSES   4u
#include "mg6_common.h"

int main(void)
{
    return mg6_run();
}
