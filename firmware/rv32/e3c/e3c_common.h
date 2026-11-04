/* e3c_common.h - shared steps of the E3c firmware tests. */
#ifndef E3C_COMMON_H
#define E3C_COMMON_H

#include "rv32_e3c.h"
#include "e3c_expected.h"

#ifndef E3C_PERIOD
#define E3C_PERIOD 0u            /* replay PERIOD: 0 = flow mode (simulation speed) */
#endif
#ifndef E3C_SEQ_BASE
#define E3C_SEQ_BASE  0x00A00000u
#endif
#ifndef E3C_TIMEOUT
#define E3C_TIMEOUT   2000000u   /* cycles per result (flow mode) */
#endif

/* identification of all pages; returns 0 or a check number */
static int e3c_ident(void)
{
    if (SYSCTRL_ID != 0x53595331u) return 1;
    if (DSP_ID != ID_DSP1 || DSP_VERSION != E3C_VERSION) return 2;
    if (CNN_ID != ID_CNN1 || CNN_VERSION != E3C_VERSION) return 3;
    if (RPL_ID != ID_RPL1 || RPL_VERSION != E3C_VERSION) return 4;
    if (RPL_DEPTH < E3C_NFRAMES) return 5;
    return 0;
}

/* reset, configure DSP (SEC_EN = DC + BP0 + BP1, coefficients rewritten through the
 * port: same values as the BRAM init), CNN and replay, start. */
static int e3c_start(uint32_t loop)
{
    e3c_reset();
    if (!(DSP_STATUS & DSP_ST_IDLE)) return 10;
    if (CNN_STATUS != 0) return 11;                     /* nothing pending after reset */
    if (CNN_RESULT_CNT != 0 || CNN_DROP_CNT != 0) return 12;
    if (RPL_FRAME_CNT != 0 || (RPL_STATUS & (RPL_ST_ACTIVE | RPL_ST_DONE))) return 13;
    DSP_SEC_EN = 0xDu;
    DSP_COEF_ADDR = 0;
    for (int i = 0; i < 20; i++) {
        DSP_COEF_DATA = (uint32_t)e3c_dsp_coef[i];
        while (DSP_STATUS & DSP_ST_COEF_PEND) { }      /* applied while the core is idle */
    }
    if (DSP_COEF_ADDR != 20 || DSP_COEF_REJ != 0) return 14;
    RPL_NFRAMES = E3C_NFRAMES;
    RPL_PERIOD = E3C_PERIOD;
    RPL_SEQ_BASE = E3C_SEQ_BASE;
    DSP_CTRL = DSP_CTRL_ENABLE;
    CNN_CTRL = CNN_CTRL_ENABLE;
    RPL_CTRL = RPL_CTRL_RUN | (loop ? RPL_CTRL_LOOP : 0);
    return 0;
}

/* compare a result with expected decision k of pass p (core result number idx) */
static int e3c_check(const cnn_result_t *r, int k, int p, uint32_t idx)
{
    const e3c_result_t *e = &e3c_expected[k];
    uint32_t fidx = e->fidx + (uint32_t)p * E3C_NFRAMES;
    if (r->index != idx) return 20;
    if (r->end != e->end) return 21;
    if (r->seg != ((e->seg + (uint32_t)p * E3C_NSEG) & 0xFFu)) return 22;
    if (r->ts != fidx) return 23;
    if (r->seq != E3C_SEQ_BASE + fidx) return 24;
    if (r->argmax != e->argmax) return 25;
    for (int c = 0; c < 6; c++)
        if (r->score[c] != e->score[c]) return 26 + c;
    /* ARGMAX must belong to the scores of the same snapshot (ties -> lowest index) */
    uint32_t best = 0;
    for (uint32_t c = 1; c < 6; c++)
        if (r->score[c] > r->score[best]) best = c;
    if (r->argmax != best) return 33;
    if (r->lat < 100 || r->lat > 20000) return 32;
    return 0;
}

#endif
