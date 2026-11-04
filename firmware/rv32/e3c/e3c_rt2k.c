/* e3c_rt2k.c - E3c at the real 2 kHz frame rate: replay PERIOD = 50000 cycles at
 * 100 MHz, all 1400 frames (4 EMG lanes per frame), SEQ_BASE = 0. The first CNN
 * decision needs 400 frames (window end = frame 399, about 20 M cycles after the
 * start), then one decision every 100 frames (50 ms = 5 M cycles): 11 results with
 * RES_SEQ = RES_TS = 399, 499, ..., 1399. Every result is checked against the E2
 * integer reference (scores, argmax, end, index), acknowledged, and the CPU-side
 * detection time is recorded: consecutive results must be 5 M cycles apart (+-1000,
 * polling jitter). No drop, no lost period. TOHOST: 1 = PASS. */
#define E3C_PERIOD   50000u
#define E3C_SEQ_BASE 0u
#define E3C_TIMEOUT  30000000u     /* > 400 periods (20 M) + processing; later results 5 M apart */
#include "e3c_common.h"

volatile uint32_t e3c_seen[E3C_NRES];   /* CPU cycle (SYSCTRL CYCLE_LO) of each detection */

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
        e3c_seen[k] = cycles();
        cnn_read(&r);
        rc = e3c_check(&r, k, 0, (uint32_t)k + 1);
        if (rc) return rc;
        if (r.seq != 399u + 100u * (uint32_t)k) return 34;      /* SEQ_BASE = 0: window end sample */
        if (CNN_DROP_CNT != 0) return 35;
        CNN_RESULT_ACK = 0;
        if (CNN_ACK_CNT != (uint32_t)k + 1) return 36;
    }
    /* first result: at least 399 periods after the start; then 100 periods apart */
    if (e3c_seen[0] - t0 < 399u * E3C_PERIOD) return 41;
    for (int k = 1; k < E3C_NRES; k++) {
        uint32_t d = e3c_seen[k] - e3c_seen[k - 1];
        if (d < 100u * E3C_PERIOD - 1000u || d > 100u * E3C_PERIOD + 1000u) return 42;
    }
    uint32_t t1 = cycles();
    while ((RPL_STATUS & (RPL_ST_DONE | RPL_ST_VALID)) != RPL_ST_DONE)
        if (cycles() - t1 > 2u * E3C_PERIOD) return 43;
    if (RPL_FRAME_CNT != E3C_NFRAMES || RPL_LATE_CNT != 0) return 44;
    if (DSP_FRAMES_OUT != E3C_NFRAMES || DSP_GAP_CNT != 0 || DSP_SOS_CNT != 1) return 45;
    if (CNN_RESULT_CNT != E3C_NRES || CNN_DROP_CNT != 0) return 46;
    e3c_reset();
    return 0;
}
