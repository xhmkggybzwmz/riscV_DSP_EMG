/* dma_common.h - E5a: the frames come from PS memory through the AMD AXI DMA and esi_dma_src_axil
 * (docs/system/e5a_ps_dma.md). Neither the source nor the data are visible to the RV32I: the PS
 * loads the data set, starts the transfers and owns the source page. The firmware configures DSP
 * and CNN, then takes every CNN result for as long as the system runs, post-processes it (K = 1,
 * T = 0) and publishes it to the E4a mailbox before acknowledging the CNN FIFO.
 *
 * The data are not known here, so there is no golden check: the firmware checks what it can
 * (result numbering, ARGMAX against the scores of the same snapshot, latency, CNN drops) and reports a
 * failure in the mailbox header (ERROR_CNT, LAST_ERROR) without stopping. Records do not carry
 * F_GOLDEN_OK; the comparison with the E2 reference happens on the PS / PC.
 * Every ESI segment starts with SOS, so a new CNN segment number = new data: F_NEW_SEGMENT.
 *
 * DMA_STOP_RESULTS = 0: runs for ever, STATE stays RUNNING, no TOHOST (board image).
 * DMA_STOP_RESULTS = N: stops after N results with STATE = DONE, TOHOST 1 = no failure (simulation). */
#ifndef DMA_COMMON_H
#define DMA_COMMON_H

/* mbx_common.h / e3c_common.h also define the replay helpers these programs do not use */
#pragma GCC diagnostic ignored "-Wunused-function"
#include "../mailbox/mbx_common.h"

#ifndef DMA_STOP_RESULTS
#define DMA_STOP_RESULTS 0u
#endif

/* ps_e6a: gen_spi_live.py adds build/spi/fw_inc, which holds the live mask and coefficients (50 Hz notch).
 * Every other build keeps the E2 chain of e3c_expected.h. A quoted include of e3c_expected.h always finds the
 * tracked file next to e3c_common.h, so a generated copy of it cannot override these. */
#if __has_include("spi_live_dsp.h")
#include "spi_live_dsp.h"
#else
#define DMA_DSP_SEC_EN 0xDu
#define DMA_DSP_COEF e3c_dsp_coef
#endif

/* e3c_ident() without the replay page, which this SoC has no route to */
static int dma_ident(void)
{
    if (SYSCTRL_ID != 0x53595331u) return 1;
    if (DSP_ID != ID_DSP1 || DSP_VERSION != E3C_VERSION) return 2;
    if (CNN_ID != ID_CNN1 || CNN_VERSION != E3C_VERSION) return 3;
    return 0;
}

/* DSP and CNN only; frames waiting at the source are held by DSP TREADY until ENABLE */
static int dma_setup(void)
{
    DSP_CTRL = DSP_CTRL_RESET;
    CNN_CTRL = CNN_CTRL_RESET;
    if (!(DSP_STATUS & DSP_ST_IDLE)) return 10;
    if (CNN_STATUS != 0) return 11;
    if (CNN_RESULT_CNT != 0 || CNN_DROP_CNT != 0) return 12;
    DSP_SEC_EN = DMA_DSP_SEC_EN;
    DSP_COEF_ADDR = 0;
    for (int i = 0; i < 20; i++) {
        DSP_COEF_DATA = (uint32_t)DMA_DSP_COEF[i];
        while (DSP_STATUS & DSP_ST_COEF_PEND) { }
    }
    if (DSP_COEF_ADDR != 20 || DSP_COEF_REJ != 0) return 14;
    CNN_CTRL = CNN_CTRL_ENABLE;
    DSP_CTRL = DSP_CTRL_ENABLE;
    return 0;
}

/* wait for a result, refreshing HEARTBEAT: no data for a long time is normal here */
static void dma_wait(void)
{
    uint32_t n = 0;
    while (!(CNN_STATUS & CNN_ST_PENDING)) {
        if ((++n & 63u) == 0) MBX_REG(MBX_H_HEARTBEAT) = cycles();
    }
}

static int dma_run(void)
{
    int rc = dma_ident();
    if (rc) return rc;
    mbx_init(0);
    pp_reset();
    rc = dma_setup();
    if (rc) {
        mbx_error(MBX_ERR_CNN_CHECK);
        MBX_REG(MBX_H_STATE) = MBX_STATE_ERROR;
        return rc;
    }
    uint32_t n = 0, seg_prev = 0xFFFFFFFFu, drops = 0, failed = 0;
    for (;;) {
        cnn_result_t r;
        dma_wait();
        uint32_t detect = cycles();
        cnn_read(&r);
        n++;
        uint32_t flags = MBX_F_POSTPROC | MBX_F_ARGMAX_OK;
        uint32_t best = 0;
        for (uint32_t c = 1; c < 6; c++)
            if (r.score[c] > r.score[best]) best = c;
        if (r.argmax != best) {
            flags &= ~MBX_F_ARGMAX_OK;
            mbx_error(MBX_ERR_ARGMAX);
            failed = 1;
        }
        if (r.index != n || r.lat < 100 || r.lat > 20000) {
            mbx_error(MBX_ERR_CNN_CHECK);
            failed = 1;
        }
        if (CNN_DROP_CNT != drops) {
            drops = CNN_DROP_CNT;
            mbx_error(MBX_ERR_CNN_CHECK);
            failed = 1;
        }
        if (r.seg != seg_prev) {
            seg_prev = r.seg;
            pp_reset();
            flags |= MBX_F_NEW_SEGMENT;
        }
        int32_t g = pp_step(r.score);
        mbx_publish(&r, flags, g, detect);
        CNN_RESULT_ACK = 0;
#if DMA_STOP_RESULTS != 0
        if (n == DMA_STOP_RESULTS) break;
#endif
    }
    if (mbx.published + mbx.drops + mbx.errors < mbx.results) return 72;
    if (MBX_REG(MBX_H_PRODUCER_INDEX) != mbx.start + mbx.published) return 73;
    MBX_REG(MBX_H_STATE) = MBX_STATE_DONE;
    return failed ? 74 : 0;
}

#endif
