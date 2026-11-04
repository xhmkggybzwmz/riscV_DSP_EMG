/* e3c_loop.c - replay LOOP mode, two passes: frame 0 carries SOS, so DSP and CNN
 * restart at the second pass and must give the same scores (segment + 1,
 * sequence numbers continuing). TOHOST: 1 = PASS. */
#include "e3c_common.h"

int main(void)
{
    int rc = e3c_ident();
    if (rc) return rc;
    rc = e3c_start(1);
    if (rc) return rc;
    for (int p = 0; p < 2; p++) {
        for (int k = 0; k < E3C_NRES; k++) {
            cnn_result_t r;
            if (cnn_wait(E3C_TIMEOUT)) return 40;
            cnn_read(&r);
            rc = e3c_check(&r, k, p, (uint32_t)(p * E3C_NRES + k) + 1);
            if (rc) return rc + (p ? 30 : 0);
            CNN_RESULT_ACK = 0;
        }
    }
    RPL_CTRL = RPL_CTRL_LOOP;                    /* RUN = 0: the replay stops */
    if (CNN_DROP_CNT != 0) return 70;
    if (DSP_SOS_CNT < 2 || CNN_RESETS < 2) return 71;
    e3c_reset();
    return 0;
}
