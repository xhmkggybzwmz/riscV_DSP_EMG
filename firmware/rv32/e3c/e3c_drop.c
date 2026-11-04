/* e3c_drop.c - result FIFO policy: the CPU does not acknowledge while the stream
 * runs. The snapshot must stay result #1 (all fields unchanged while further
 * results arrive), the FIFO keeps results 1..4, the other 7 are dropped
 * (DROP_CNT); after that the kept results are read and acknowledged in order.
 * TOHOST: 1 = PASS. */
#include "e3c_common.h"

int main(void)
{
    int rc = e3c_ident();
    if (rc) return rc;
    rc = e3c_start(0);
    if (rc) return rc;
    if (cnn_wait(E3C_TIMEOUT)) return 40;
    cnn_result_t first, again;
    cnn_read(&first);
    rc = e3c_check(&first, 0, 0, 1);
    if (rc) return rc;
    /* re-read the snapshot every time a new result has been produced */
    uint32_t seen = CNN_RESULT_CNT, t0 = cycles();
    while (seen < E3C_NRES) {
        uint32_t n = CNN_RESULT_CNT;
        if (n != seen) {
            seen = n;
            cnn_read(&again);
            if (again.index != first.index || again.seq != first.seq || again.end != first.end ||
                again.ts != first.ts || again.argmax != first.argmax || again.lat != first.lat)
                return 50;
            for (int c = 0; c < 6; c++)
                if (again.score[c] != first.score[c]) return 51;
            t0 = cycles();
        }
        if (cycles() - t0 > E3C_TIMEOUT) return 52;
    }
    if (CNN_DROP_CNT != E3C_NRES - 4) return 53;
    if (!(CNN_STATUS & CNN_ST_FULL) || ((CNN_STATUS >> 4) & 7u) != 4) return 54;
    for (int k = 0; k < 4; k++) {
        cnn_result_t r;
        if (!(CNN_STATUS & CNN_ST_PENDING)) return 55;
        cnn_read(&r);
        rc = e3c_check(&r, k, 0, (uint32_t)k + 1);
        if (rc) return 60 + rc;
        CNN_RESULT_ACK = 0;
    }
    if (CNN_STATUS & CNN_ST_PENDING) return 56;
    if (CNN_ACK_CNT != 4 || CNN_RESULT_CNT != E3C_NRES) return 57;
    CNN_RESULT_ACK = 0;                          /* ACK on an empty FIFO: no effect */
    if (CNN_ACK_CNT != 4) return 58;
    e3c_reset();
    return 0;
}
