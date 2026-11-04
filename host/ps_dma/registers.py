"""Registers of the E5a data path (PS addresses: rtl/soc/soc_addr_map.vh, checked by tests/test_registers.py)."""

DMA_BASE, DMA_SIZE = 0xA0110000, 0x10000      # AMD AXI DMA (PG021), direct register mode, MM2S only
SRC_BASE, SRC_SIZE = 0xA0120000, 0x1000       # esi_dma_src_axil control page
AFI_HP0_BASE, AFI_SIZE = 0xFD380000, 0x1000   # AFIFM2 = S_AXI_HP0_FPD (read-only check of the fabric width)

# AXI DMA MM2S
MM2S_DMACR, MM2S_DMASR, MM2S_SA, MM2S_SA_MSB, MM2S_LENGTH = 0x00, 0x04, 0x18, 0x1C, 0x28
CR_RS, CR_RESET = 0x1, 0x4
SR_HALTED, SR_IDLE, SR_IOC = 0x1, 0x2, 0x1000
SR_ERRORS = 0x770                             # DMAIntErr, DMASlvErr, DMADecErr, SGIntErr, SGSlvErr, SGDecErr
LENGTH_MAX = (1 << 26) - 1                    # c_sg_length_width = 26
ADDR_ALIGN = 16                               # c_m_axi_mm2s_data_width = 128 and no DRE: aligned start addresses

# esi_dma_src_axil
SRC_ID, SRC_VERSION, SRC_CTRL, SRC_STATUS = 0x000, 0x004, 0x008, 0x00C
SRC_PERIOD, SRC_FRAME_CNT, SRC_LATE_CNT, SRC_SEQ_BASE = 0x014, 0x018, 0x01C, 0x020
SRC_SEG_CNT, SRC_UNDERRUN_CNT, SRC_KEEP_ERR_CNT = 0x024, 0x028, 0x02C
SRC_ID_VALUE = 0x45534431                     # "ESD1"
CTRL_RUN, CTRL_SOFT_RESET = 0x1, 0x4
ST_RUN, ST_IN_SEGMENT, ST_VALID, ST_INPUT, ST_SINK_RESET = 0x1, 0x2, 0x4, 0x8, 0x10
PERIOD_2KHZ = 50000                           # cycles per frame at 100 MHz
