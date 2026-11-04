// soc_addr_map.vh - implemented address windows of the RV32I SoC
// (docs/soc/memory_map.md). Single source for the RTL (rv32_soc_top ->
// soc_dmem_router) and for the Vivado block design (fpga/kv260/tcl/soc_bd.tcl
// reads the SOC_SYSCTRL_* values from this file for the SmartConnect address
// map), so the CPU-side router and the Address Editor cannot disagree.
//
// Only windows listed here are reachable from the CPU data port; every other
// address is answered by soc_dmem_router with a CMI error and never reaches
// the RAM or the AXI interconnect.
//
// Adding a peripheral (e.g. SPI at 0x1000_1000, DSP at 0x1000_2000, 4 KB each):
// add its BASE/SIZE here, add the window to rv32_soc_top's router parameters,
// add the slave and its segment in soc_bd.tcl, and extend the regression.
// Format of the value lines is fixed (`define NAME 32'hXXXX_XXXX): soc_bd.tcl
// parses them.

`ifndef SOC_ADDR_MAP_VH
`define SOC_ADDR_MAP_VH

// local RAM: always at 0x0000_0000; its size is the MEM_BYTES parameter of
// rv32_soc_top (default below)
`define SOC_RAM_BYTES_DEFAULT 32'h0001_0000

// SYSCTRL (AXI4-Lite, via cmi2axil and the SmartConnect)
`define SOC_SYSCTRL_BASE 32'h1000_0000
`define SOC_SYSCTRL_SIZE 32'h0000_1000

// E3c recognition pipeline (docs/soc/e3c_integration.md). Implemented only when
// rv32_soc_top is built with E3C = 1 (the stage 6A design keeps E3C = 0 and
// forwards SYSCTRL only). 0x1000_1000 (SPI), 0x1000_3000 (IRQ/timer) and
// 0x1000_4000 stay unmapped.
`define SOC_DSP_BASE 32'h1000_2000
`define SOC_DSP_SIZE 32'h0000_1000
`define SOC_CNN_BASE 32'h1000_5000
`define SOC_CNN_SIZE 32'h0000_1000
`define SOC_REPLAY_BASE 32'h1000_6000
`define SOC_REPLAY_SIZE 32'h0000_1000

// E4a PS mailbox (docs/soc/e4a_mailbox.md): one shared dual-port block RAM.
// CPU side (rv32_soc_top MBOX = 1): router window -> soc_io_split -> cmi_bram_port
// -> BRAM port A. 0x2000_0000-0x2001_FFFF and everything after the 4 KB stay unmapped.
`define SOC_MBX_BASE 32'h2002_0000
`define SOC_MBX_SIZE 32'h0000_1000
// PS side: physical address of the same BRAM behind M_AXI_HPM0_FPD -> ps_smc -> AXI
// BRAM Controller (port B) in the KV260 block design (ps_e4a). Outside the 256 KB
// debug window at 0xA000_0000.
`define KV260_MBX_PS_BASE 32'hA010_0000
`define KV260_MBX_PS_SIZE 32'h0000_1000

// E5a PS DMA data source (docs/system/e5a_ps_dma.md), PS addresses behind M_AXI_HPM0_FPD -> ps_smc:
// AMD AXI DMA registers and the control page of esi_dma_src_axil. The CPU has no route to either
// (rv32_soc_top E3C_REPLAY = 0 also removes SOC_REPLAY_* from the router).
`define KV260_DMA_PS_BASE 32'hA011_0000
`define KV260_DMA_PS_SIZE 32'h0001_0000
`define KV260_ESRC_PS_BASE 32'hA012_0000
`define KV260_ESRC_PS_SIZE 32'h0000_1000

`endif
