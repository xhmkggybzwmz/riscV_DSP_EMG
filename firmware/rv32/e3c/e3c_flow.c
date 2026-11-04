/* e3c_flow.c - E3c main test: the RV32I configures DSP, CNN and the replay source
 * through CMI -> cmi2axil -> SmartConnect, the replayed E2 stream runs through the
 * FPGA DSP and INT8 CNN, and the CPU reads every class-score snapshot, checks it
 * against the E2 integer reference (e3c_expected.h) and acknowledges it. Then the
 * pipeline is reset and the whole run is repeated (same results). TOHOST: 1 = PASS. */
#include "e3c_common.h"

static int run_once(void)
{
    int rc = e3c_start(0);
    if (rc) return rc;
    for (int k = 0; k < E3C_NRES; k++) {
        cnn_result_t r;
        if (cnn_wait(E3C_TIMEOUT)) return 40;
        cnn_read(&r);
        rc = e3c_check(&r, k, 0, (uint32_t)k + 1);
        if (rc) return rc;
        CNN_RESULT_ACK = 0;
    }
    /* drain: the replay finishes, no further result, statistics consistent */
    uint32_t t0 = cycles();
    while ((RPL_STATUS & (RPL_ST_DONE | RPL_ST_VALID)) != RPL_ST_DONE)
        if (cycles() - t0 > E3C_TIMEOUT) return 41;
    t0 = cycles();
    while (!(DSP_STATUS & DSP_ST_IDLE) || (CNN_STATUS & CNN_ST_BUSY) || DSP_FRAMES_OUT != E3C_NFRAMES)
        if (cycles() - t0 > E3C_TIMEOUT) return 42;
    if (RPL_FRAME_CNT != E3C_NFRAMES) return 43;
    if (DSP_FRAMES_IN != E3C_NFRAMES || DSP_LAST_SEQ != E3C_SEQ_BASE + E3C_NFRAMES - 1) return 44;
    if (DSP_SOS_CNT != E3C_NSEG) return 45;
    if (CNN_FRAMES != E3C_NFRAMES || CNN_NOT_POST_DSP != 0) return 46;
    if (CNN_RESULT_CNT != E3C_NRES || CNN_DROP_CNT != 0 || CNN_ACK_CNT != E3C_NRES) return 47;
    if (CNN_STATUS & CNN_ST_PENDING) return 48;
    if (E3C_PERIOD == 0 && RPL_LATE_CNT != 0) return 49;
    return 0;
}

int main(void)
{
    int rc = e3c_ident();
    if (rc) return rc;
    rc = run_once();
    if (rc) return rc;
    rc = run_once();                    /* second run after the pipeline reset */
    if (rc) return 50 + rc;
    e3c_reset();
    return 0;
}
