/* e3c_rt.c - E3c with the replay source in real-time mode: one frame every
 * E3C_PERIOD cycles (12000 cycles = 120 us at 100 MHz, accelerated from the 2 kHz
 * / 50000-cycle rate to keep the simulation short; the CNN's worst frame is
 * 11151 cycles). Checks every result like e3c_flow, no lost period (LATE_CNT = 0)
 * and that the run took at least NFRAMES * PERIOD cycles. TOHOST: 1 = PASS. */
#define E3C_PERIOD 12000u
#define E3C_TIMEOUT 8000000u   /* the first decision comes after 400 frames = 4.8 M cycles */
#include "e3c_common.h"

int main(void)
{
    int rc = e3c_ident();
    if (rc) return rc;
    uint32_t t0 = cycles();
    rc = e3c_start(0);
    if (rc) return rc;
    for (int k = 0; k < E3C_NRES; k++) {
        cnn_result_t r;
        if (cnn_wait(E3C_TIMEOUT)) return 40;
        cnn_read(&r);
        rc = e3c_check(&r, k, 0, (uint32_t)k + 1);
        if (rc) return rc;
        CNN_RESULT_ACK = 0;
    }
    uint32_t t1 = cycles();
    while ((RPL_STATUS & (RPL_ST_DONE | RPL_ST_VALID)) != RPL_ST_DONE) { }
    if (RPL_FRAME_CNT != E3C_NFRAMES) return 41;
    if (RPL_LATE_CNT != 0) return 42;
    /* the last decision ends at frame 1399: at least 1399 periods after the start */
    if (t1 - t0 < (E3C_NFRAMES - 1) * E3C_PERIOD) return 43;
    if (CNN_DROP_CNT != 0) return 44;
    e3c_reset();
    return 0;
}
