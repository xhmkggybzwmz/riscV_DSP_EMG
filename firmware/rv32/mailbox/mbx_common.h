/* mbx_common.h - RV32I producer side of the E4a mailbox v1 (docs/soc/e4a_mailbox.md).
 *
 * The firmware configures the E3c pipeline (replay -> DSP -> CNN), takes every CNN
 * result from the result FIFO, checks it against the E2 golden decision (self-test
 * images), runs the post-processing (K, T; E4a: K = 1, T = 0) and publishes the
 * complete record in the shared BRAM before acknowledging the CNN FIFO.
 *
 * Publish order (protocol.py): REC_HDR = 0, words 1..15, REC_HDR = magic|version|flags,
 * PRODUCER_INDEX + 1, PUBLISHED_CNT. Every store reaches the BRAM before the next
 * access starts (one outstanding CMI access), so the PS never sees an index whose
 * record is incomplete. A full ring drops the new result (DROP_CNT), it never
 * overwrites an unconsumed record and never stalls the CNN.
 * TOHOST: 1 = PASS, otherwise (check << 1) | 1 as for the E3c programs. */
#ifndef MBX_COMMON_H
#define MBX_COMMON_H

#include "../e3c/e3c_common.h"
#include "mbx_layout.h"

#ifndef MBX_PP_K
#define MBX_PP_K 1u                 /* majority vote over the last K raw decisions (odd) */
#endif
#ifndef MBX_PP_T
#define MBX_PP_T 0                  /* minimum top-1 margin in score LSB (0 = off) */
#endif

#define MBX_REG(off)    (*(volatile uint32_t *)(MBX_BASE + (off)))
#define MBX_REC(i, w)   (*(volatile uint32_t *)(MBX_BASE + MBX_RING_OFFSET + \
                          ((i) % MBX_RING_SIZE) * MBX_RECORD_SIZE + 4u * (w)))

static struct {
    uint32_t epoch, start, prod, published, results, drops, errors, drop_pending;
} mbx;

/* ---------------------------------------------------------------- post-processing
 * reference/emg_fixed/postproc.py: top-1 (ties -> lowest index), UNCERTAIN when the
 * margin is below T, strict majority over the last K raw decisions, hold otherwise. */
static int32_t pp_hist[MBX_PP_K];
static uint32_t pp_n;
static int32_t pp_out;

static void pp_reset(void)
{
    pp_n = 0;
    pp_out = MBX_GESTURE_NO_DECISION;
}

static int32_t pp_step(const int32_t *s)
{
    uint32_t t1 = 0;
    for (uint32_t c = 1; c < 6; c++)
        if (s[c] > s[t1]) t1 = c;
    int64_t second = INT64_MIN;
    for (uint32_t c = 0; c < 6; c++)
        if (c != t1 && s[c] > second) second = s[c];
    int32_t raw = ((int64_t)s[t1] - second >= MBX_PP_T) ? (int32_t)t1 : MBX_GESTURE_UNCERTAIN;
    pp_hist[pp_n % MBX_PP_K] = raw;
    pp_n++;
    if (pp_n >= MBX_PP_K) {
        for (uint32_t i = 0; i < MBX_PP_K; i++) {
            int32_t cls = pp_hist[i];
            uint32_t cnt = 0;
            if (cls < 0) continue;
            for (uint32_t j = 0; j < MBX_PP_K; j++)
                if (pp_hist[j] == cls) cnt++;
            if (cnt >= MBX_PP_K / 2u + 1u) { pp_out = cls; break; }
        }
    }
    return pp_out;
}

/* ---------------------------------------------------------------- mailbox */
static void mbx_error(uint32_t code)
{
    mbx.errors++;
    MBX_REG(MBX_H_ERROR_CNT) = mbx.errors;
    MBX_REG(MBX_H_LAST_ERROR) = code;
}

/* new epoch: header rewritten, all slots invalidated, PRODUCER_INDEX = start */
static void mbx_init(uint32_t start)
{
    uint32_t old = 0;
    if (MBX_REG(MBX_H_MAGIC) == MBX_MAGIC && (MBX_REG(MBX_H_VERSION) >> 16) == (MBX_VERSION >> 16))
        old = MBX_REG(MBX_H_RESET_EPOCH);
    mbx.epoch = old + 1u;
    if (mbx.epoch == 0) mbx.epoch = 1;
    mbx.start = mbx.prod = start;
    mbx.published = mbx.results = mbx.drops = mbx.errors = mbx.drop_pending = 0;

    MBX_REG(MBX_H_STATE) = MBX_STATE_INIT;
    for (uint32_t s = 0; s < MBX_RING_SIZE; s++)
        MBX_REC(s, MBX_W_REC_HDR) = 0;
    MBX_REG(MBX_H_VERSION) = MBX_VERSION;
    MBX_REG(MBX_H_RING_SIZE) = MBX_RING_SIZE;
    MBX_REG(MBX_H_RECORD_SIZE) = MBX_RECORD_SIZE;
    MBX_REG(MBX_H_RING_OFFSET) = MBX_RING_OFFSET;
    MBX_REG(MBX_H_RESET_EPOCH) = mbx.epoch;
    MBX_REG(MBX_H_PRODUCER_INDEX) = start;
    MBX_REG(MBX_H_EPOCH_START_INDEX) = start;
    MBX_REG(MBX_H_PUBLISHED_CNT) = 0;
    MBX_REG(MBX_H_RESULT_CNT) = 0;
    MBX_REG(MBX_H_DROP_CNT) = 0;
    MBX_REG(MBX_H_ERROR_CNT) = 0;
    MBX_REG(MBX_H_LAST_ERROR) = MBX_ERR_NONE;
    MBX_REG(MBX_H_HEARTBEAT) = cycles();
    MBX_REG(MBX_H_MAGIC) = MBX_MAGIC;
    MBX_REG(MBX_H_STATE) = MBX_STATE_RUNNING;
}

/* consumer position: CONSUMER_INDEX if the consumer is attached to this epoch,
 * EPOCH_START_INDEX otherwise. Epoch and index are read twice; a pair that changed
 * between the reads (PS writing at the same time) is read again. If it never
 * settles, the ring is reported full (the result is dropped, nothing overwritten). */
static uint32_t mbx_consumer(void)
{
    for (int i = 0; i < 4; i++) {
        uint32_t e1 = MBX_REG(MBX_H_CONSUMER_EPOCH);
        uint32_t c1 = MBX_REG(MBX_H_CONSUMER_INDEX);
        uint32_t c2 = MBX_REG(MBX_H_CONSUMER_INDEX);
        uint32_t e2 = MBX_REG(MBX_H_CONSUMER_EPOCH);
        if (e1 == e2 && c1 == c2)
            return (e1 == mbx.epoch) ? c1 : mbx.start;
    }
    return mbx.prod - MBX_RING_SIZE;
}

/* publish one result; 0 = published, 1 = dropped (ring full), 2 = dropped (bad consumer index) */
static int mbx_publish(const cnn_result_t *r, uint32_t flags, int32_t gesture, uint32_t detect)
{
    mbx.results++;
    MBX_REG(MBX_H_RESULT_CNT) = mbx.results;
    uint32_t used = mbx.prod - mbx_consumer();
    if (used > MBX_RING_SIZE) {
        mbx_error(MBX_ERR_BAD_CONSUMER);
        mbx.drop_pending = 1;
        return 2;
    }
    if (used == MBX_RING_SIZE) {
        mbx.drops++;
        MBX_REG(MBX_H_DROP_CNT) = mbx.drops;
        mbx.drop_pending = 1;
        return 1;
    }
    uint32_t i = mbx.prod;
    if (mbx.drop_pending) flags |= MBX_F_DROP_BEFORE;
    if (mbx.published == 0) flags |= MBX_F_NEW_SEGMENT;
    MBX_REC(i, MBX_W_REC_HDR) = 0;
    MBX_REC(i, MBX_W_PUBLISH_INDEX) = i;
    MBX_REC(i, MBX_W_RESET_EPOCH) = mbx.epoch;
    MBX_REC(i, MBX_W_RESULT_INDEX) = r->index;
    MBX_REC(i, MBX_W_WINDOW_END_SEQ) = r->seq;
    MBX_REC(i, MBX_W_ESI_TIMESTAMP) = r->ts;
    MBX_REC(i, MBX_W_CLASS) = ((uint32_t)gesture & 0xFFu) | ((r->argmax & 0xFFu) << 8) | ((r->seg & 0xFFFFu) << 16);
    for (uint32_t c = 0; c < 6; c++)
        MBX_REC(i, MBX_W_SCORE0 + c) = (uint32_t)r->score[c];
    MBX_REC(i, MBX_W_CNN_LAT_CYCLES) = r->lat;
    MBX_REC(i, MBX_W_DETECT_CYCLE) = detect;
    MBX_REC(i, MBX_W_PUBLISH_CYCLE) = cycles();
    MBX_REC(i, MBX_W_REC_HDR) = (MBX_REC_MAGIC << 16) | (MBX_REC_VERSION << 8) | (flags & 0xFFu);
    mbx.prod = i + 1u;
    MBX_REG(MBX_H_PRODUCER_INDEX) = mbx.prod;
    mbx.published++;
    MBX_REG(MBX_H_PUBLISHED_CNT) = mbx.published;
    mbx.drop_pending = 0;
    return 0;
}

/* wait for a pending CNN result, refreshing HEARTBEAT; 0 = ok, 1 = timeout */
static int mbx_wait(uint32_t max_cycles)
{
    uint32_t t0 = cycles(), n = 0;
    while (!(CNN_STATUS & CNN_ST_PENDING)) {
        uint32_t t = cycles();
        if ((++n & 63u) == 0) MBX_REG(MBX_H_HEARTBEAT) = t;
        if (t - t0 > max_cycles) return 1;
    }
    return 0;
}

/* E3C_PERIOD != 0: real-time replay, consecutive results of one pass 100 periods apart */
#define MBX_MAX_PASSES 16u
static uint32_t mbx_seen[MBX_MAX_PASSES * E3C_NRES];

/* `passes` replay passes (LOOP mode for passes > 1), every result checked and published.
 * Returns 0 or a check number. */
static int mbx_run(uint32_t passes, uint32_t start_index)
{
    int rc = e3c_ident();
    if (rc) return rc;
    if (passes == 0 || passes > MBX_MAX_PASSES) return 9;
    mbx_init(start_index);
    pp_reset();
    rc = e3c_start(passes > 1u);
    if (rc) { mbx_error(MBX_ERR_CNN_CHECK); return rc; }
    uint32_t seg_prev = 0xFFFFFFFFu, n = 0;
    for (uint32_t p = 0; p < passes; p++) {
        for (int k = 0; k < E3C_NRES; k++, n++) {
            cnn_result_t r;
            if (mbx_wait(E3C_TIMEOUT)) { mbx_error(MBX_ERR_TIMEOUT); return 40; }
            uint32_t detect = cycles();
            mbx_seen[n] = detect;
            cnn_read(&r);
            rc = e3c_check(&r, k, (int)p, n + 1u);
            if (rc) { mbx_error(rc == 33 ? MBX_ERR_ARGMAX : MBX_ERR_CNN_CHECK); return rc + (p ? 30 : 0); }
            uint32_t flags = MBX_F_POSTPROC | MBX_F_ARGMAX_OK | MBX_F_GOLDEN_OK;
            if (r.seg != seg_prev) {                /* new ESI segment (SOS): vote history restarts */
                pp_reset();
                flags |= MBX_F_NEW_SEGMENT;
                seg_prev = r.seg;
            }
            if (p + 1u == passes && k + 1 == E3C_NRES) flags |= MBX_F_LAST;
            int32_t g = pp_step(r.score);
            mbx_publish(&r, flags, g, detect);
            CNN_RESULT_ACK = 0;
        }
    }
    if (passes > 1u) RPL_CTRL = RPL_CTRL_LOOP;    /* RUN = 0: the replay stops */
#if E3C_PERIOD != 0
    for (uint32_t q = 0; q < passes; q++)
        for (uint32_t k = 1; k < E3C_NRES; k++) {
            uint32_t d = mbx_seen[q * E3C_NRES + k] - mbx_seen[q * E3C_NRES + k - 1u];
            if (d < 100u * E3C_PERIOD - 1000u || d > 100u * E3C_PERIOD + 1000u) return 42;
        }
    if (RPL_LATE_CNT != 0) return 44;
#endif
    if (CNN_DROP_CNT != 0) return 70;
    if (mbx.results != passes * E3C_NRES) return 71;
    if (mbx.published + mbx.drops + mbx.errors != mbx.results) return 72;
    if (MBX_REG(MBX_H_PRODUCER_INDEX) != mbx.start + mbx.published) return 73;
    MBX_REG(MBX_H_STATE) = MBX_STATE_DONE;
    e3c_reset();
    return 0;
}

#endif
