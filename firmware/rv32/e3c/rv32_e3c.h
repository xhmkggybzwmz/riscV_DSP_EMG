/* rv32_e3c.h - E3c control pages for bare-metal C (docs/soc/e3c_integration.md).
 * DSP 0x1000_2000, CNN 0x1000_5000, replay 0x1000_6000 (4 KB each; SoC built with E3C = 1). */
#ifndef RV32_E3C_H
#define RV32_E3C_H

#include <stdint.h>
#include "rv32_soc.h"

#define REG32(a)          (*(volatile uint32_t *)(a))

#define DSP_BASE          0x10002000u
#define DSP_ID            REG32(DSP_BASE + 0x000)
#define DSP_VERSION       REG32(DSP_BASE + 0x004)
#define DSP_CTRL          REG32(DSP_BASE + 0x008)
#define DSP_STATUS        REG32(DSP_BASE + 0x00C)
#define DSP_SEC_EN        REG32(DSP_BASE + 0x010)
#define DSP_COEF_ADDR     REG32(DSP_BASE + 0x014)
#define DSP_COEF_DATA     REG32(DSP_BASE + 0x018)
#define DSP_FRAMES_IN     REG32(DSP_BASE + 0x01C)
#define DSP_FRAMES_OUT    REG32(DSP_BASE + 0x020)
#define DSP_OUT_SAT       REG32(DSP_BASE + 0x024)
#define DSP_RESETS        REG32(DSP_BASE + 0x02C)
#define DSP_SOS_CNT       REG32(DSP_BASE + 0x030)
#define DSP_GAP_CNT       REG32(DSP_BASE + 0x034)
#define DSP_LAST_SEQ      REG32(DSP_BASE + 0x038)
#define DSP_COEF_REJ      REG32(DSP_BASE + 0x03C)
#define DSP_CTRL_ENABLE   0x1u
#define DSP_CTRL_RESET    0x2u
#define DSP_CTRL_CLEAR    0x4u
#define DSP_ST_IDLE       0x1u
#define DSP_ST_COEF_PEND  0x8u

#define CNN_BASE          0x10005000u
#define CNN_ID            REG32(CNN_BASE + 0x000)
#define CNN_VERSION       REG32(CNN_BASE + 0x004)
#define CNN_CTRL          REG32(CNN_BASE + 0x008)
#define CNN_STATUS        REG32(CNN_BASE + 0x00C)
#define CNN_RESULT_ACK    REG32(CNN_BASE + 0x010)
#define CNN_RES_INDEX     REG32(CNN_BASE + 0x020)
#define CNN_RES_SEQ       REG32(CNN_BASE + 0x024)
#define CNN_RES_END       REG32(CNN_BASE + 0x028)
#define CNN_RES_TS        REG32(CNN_BASE + 0x02C)
#define CNN_RES_SEG       REG32(CNN_BASE + 0x030)
#define CNN_ARGMAX        REG32(CNN_BASE + 0x034)
#define CNN_LAT_CYCLES    REG32(CNN_BASE + 0x038)
#define CNN_SCORE(k)      REG32(CNN_BASE + 0x040 + 4u * (k))
#define CNN_RESULT_CNT    REG32(CNN_BASE + 0x060)
#define CNN_DROP_CNT      REG32(CNN_BASE + 0x064)
#define CNN_ACK_CNT       REG32(CNN_BASE + 0x068)
#define CNN_FRAMES        REG32(CNN_BASE + 0x06C)
#define CNN_RESETS        REG32(CNN_BASE + 0x070)
#define CNN_NOT_POST_DSP  REG32(CNN_BASE + 0x074)
#define CNN_WSEL          REG32(CNN_BASE + 0x090)
#define CNN_WADDR         REG32(CNN_BASE + 0x094)
#define CNN_WDATA         REG32(CNN_BASE + 0x098)
#define CNN_WREJ          REG32(CNN_BASE + 0x09C)
#define CNN_CTRL_ENABLE   0x1u
#define CNN_CTRL_RESET    0x2u
#define CNN_CTRL_CLR      0x4u
#define CNN_ST_PENDING    0x1u
#define CNN_ST_BUSY       0x2u
#define CNN_ST_FULL       0x4u

#define RPL_BASE          0x10006000u
#define RPL_ID            REG32(RPL_BASE + 0x000)
#define RPL_VERSION       REG32(RPL_BASE + 0x004)
#define RPL_CTRL          REG32(RPL_BASE + 0x008)
#define RPL_STATUS        REG32(RPL_BASE + 0x00C)
#define RPL_NFRAMES       REG32(RPL_BASE + 0x010)
#define RPL_PERIOD        REG32(RPL_BASE + 0x014)
#define RPL_FRAME_CNT     REG32(RPL_BASE + 0x018)
#define RPL_LATE_CNT      REG32(RPL_BASE + 0x01C)
#define RPL_SEQ_BASE      REG32(RPL_BASE + 0x020)
#define RPL_RPTR          REG32(RPL_BASE + 0x024)
#define RPL_WADDR         REG32(RPL_BASE + 0x028)
#define RPL_WDATA_LO      REG32(RPL_BASE + 0x02C)
#define RPL_WDATA_HI      REG32(RPL_BASE + 0x030)
#define RPL_WFLAGS        REG32(RPL_BASE + 0x034)
#define RPL_WREJ          REG32(RPL_BASE + 0x038)
#define RPL_DEPTH         REG32(RPL_BASE + 0x03C)
#define RPL_CTRL_RUN      0x1u
#define RPL_CTRL_LOOP     0x2u
#define RPL_CTRL_RESET    0x4u
#define RPL_ST_ACTIVE     0x1u
#define RPL_ST_DONE       0x2u
#define RPL_ST_VALID      0x4u

#define ID_DSP1           0x44535031u
#define ID_CNN1           0x434E4E31u
#define ID_RPL1           0x52504C31u
#define E3C_VERSION       0x00010000u

/* one CNN result as read from the snapshot registers */
typedef struct {
    uint32_t index, seq, end, ts, seg, argmax, lat;
    int32_t  score[6];
} cnn_result_t;

/* cycle counter based timeout (SYSCTRL CYCLE_LO) */
static inline uint32_t cycles(void) { return SYSCTRL_CYCLE_LO; }

/* wait for a pending CNN result; 0 = ok, 1 = timeout */
static inline int cnn_wait(uint32_t max_cycles)
{
    uint32_t t0 = cycles();
    while (!(CNN_STATUS & CNN_ST_PENDING))
        if (cycles() - t0 > max_cycles)
            return 1;
    return 0;
}

static inline void cnn_read(cnn_result_t *r)
{
    r->index = CNN_RES_INDEX;
    r->seq = CNN_RES_SEQ;
    r->end = CNN_RES_END;
    r->ts = CNN_RES_TS;
    r->seg = CNN_RES_SEG;
    r->argmax = CNN_ARGMAX;
    r->lat = CNN_LAT_CYCLES;
    for (int k = 0; k < 6; k++)
        r->score[k] = (int32_t)CNN_SCORE(k);
}

/* stop and reset the whole pipeline (consumers and producer together, so no
 * stream handshake is cut on one side only) */
static inline void e3c_reset(void)
{
    RPL_CTRL = RPL_CTRL_RESET;
    DSP_CTRL = DSP_CTRL_RESET;
    CNN_CTRL = CNN_CTRL_RESET;
}

#endif
