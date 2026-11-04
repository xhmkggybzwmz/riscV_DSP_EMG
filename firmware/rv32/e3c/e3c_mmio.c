/* e3c_mmio.c - MMIO view of the E3c pages from the CPU: last word of every
 * implemented window is reachable (reads 0, writes ignored), read-only and
 * reserved offsets, byte/half stores (WSTRB), write-port rejection rules, SYSCTRL
 * still intact. Unmapped addresses are tested by the e3c_trap_* programs.
 * TOHOST: 1 = PASS. */
#include "rv32_e3c.h"

int main(void)
{
    /* window boundaries: last word inside each implemented page */
    if (REG32(DSP_BASE + 0xFFC) != 0) return 1;
    if (REG32(CNN_BASE + 0xFFC) != 0) return 2;
    if (REG32(RPL_BASE + 0xFFC) != 0) return 3;
    REG32(DSP_BASE + 0xFFC) = 0xFFFFFFFFu;
    REG32(CNN_BASE + 0xFFC) = 0xFFFFFFFFu;
    REG32(RPL_BASE + 0xFFC) = 0xFFFFFFFFu;
    if (DSP_SEC_EN != 0xDu) return 4;           /* reserved writes did not alias */
    if (SYSCTRL_STATUS != 0) return 5;          /* nothing reached TOHOST */
    /* read-only registers */
    DSP_ID = 0;
    CNN_RESULT_CNT = 0x55;
    RPL_DEPTH = 1;
    if (DSP_ID != ID_DSP1 || CNN_RESULT_CNT != 0 || RPL_DEPTH != 2048u) return 6;
    /* byte and half stores (WSTRB) on a 32-bit RW register */
    RPL_SEQ_BASE = 0x11223344u;
    *(volatile uint8_t *)(RPL_BASE + 0x021) = 0xAB;
    if (RPL_SEQ_BASE != 0x1122AB44u) return 7;
    *(volatile uint16_t *)(RPL_BASE + 0x022) = 0xCDEF;
    if (RPL_SEQ_BASE != 0xCDEFAB44u) return 8;
    RPL_SEQ_BASE = 0;
    /* CNN parameter port: rejected while enabled */
    CNN_CTRL = CNN_CTRL_ENABLE;
    CNN_WSEL = 13;
    CNN_WADDR = 0;
    CNN_WDATA = 0x1234;
    if (CNN_WREJ != 1 || CNN_WADDR != 0) return 9;
    CNN_CTRL = CNN_CTRL_RESET;                  /* also clears WREJ */
    if (CNN_WREJ != 0 || CNN_CTRL != 0) return 10;
    /* replay frame memory: rejected while running */
    RPL_NFRAMES = 4;
    RPL_CTRL = RPL_CTRL_RUN;                    /* DSP not enabled: the first frame waits */
    RPL_WADDR = 100;
    RPL_WDATA_LO = 1; RPL_WDATA_HI = 2;
    RPL_WFLAGS = 0;
    if (RPL_WREJ != 1 || RPL_WADDR != 100) return 11;
    if ((RPL_STATUS & (RPL_ST_ACTIVE | RPL_ST_VALID)) != (RPL_ST_ACTIVE | RPL_ST_VALID)) return 12;
    e3c_reset();
    if (RPL_STATUS != 0 || RPL_FRAME_CNT != 0) return 13;
    /* SYSCTRL unchanged */
    if (SYSCTRL_ID != 0x53595331u) return 14;
    SYSCTRL_SCRATCH = 0xA5A5A5A5u;
    if (SYSCTRL_SCRATCH != 0xA5A5A5A5u) return 15;
    return 0;
}
