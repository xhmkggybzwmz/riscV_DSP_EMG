# dma_bd.tcl - E5a block-design part (Vivado 2026.1, docs/system/e5a_ps_dma.md): the PS feeds the
# pipeline from its memory through the AMD AXI DMA.
#
#   <PS memory> -> axi_dma/M_AXI_MM2S (128 bit, 40-bit addresses, simple mode, MM2S only, no DRE)
#   axi_dma/M_AXIS_MM2S (64 bit) -> e3c/s_axis (esi_dma_src_axil in emg_e5_subsys)
#   <PS AXI> -> ps_smc -> axi_dma/S_AXI_LITE    KV260_DMA_PS_* (0xA011_0000, 64 KB)
#            -> ps_smc -> e3c/s_axi_rep         KV260_ESRC_PS_* (0xA012_0000, 4 KB), source control page
# DMA and source control are in the PS reset domain (rst_ps / ps_aresetn): they survive a re-run of the
# SoC, so a PS access never meets a slave in reset (as for the mailbox controller, mbx_bd.tcl). The CPU
# has no route to either: axi_smc has 3 MI (SYSCTRL, DSP, CNN), cpu_subsys E3C_REPLAY = 0.
# Requires soc_bd.tcl (soc_map_value), e3c_bd.tcl (e3c_read_defines) and mbx_bd.tcl to be sourced.
#
#   add_dma_cells <repo> <ps_smc> <dma master index> <src master index> <clk> <ps resetn> <PS address space>
#   create_e5_sim_bd <repo> <design name> <paths.vh>
#       stand-alone BD for xsim: ports aclk (100 MHz), aresetn (SoC), ps_aresetn (PS side), S_AXI_PS (AXI4-Lite,
#       the test bench as the PS) and M_AXI_DDR (the test bench's memory instead of S_AXI_HP0_FPD)

proc add_dma_cells {repo smc idx_dma idx_src clk rstn space} {
    set dma [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_dma:7.1 axi_dma]
    set_property -dict [list CONFIG.c_include_sg {0} CONFIG.c_include_mm2s {1} CONFIG.c_include_s2mm {0} \
        CONFIG.c_sg_include_stscntrl_strm {0} CONFIG.c_sg_length_width {26} CONFIG.c_addr_width {40} \
        CONFIG.c_m_axi_mm2s_data_width {128} CONFIG.c_m_axis_mm2s_tdata_width {64} \
        CONFIG.c_mm2s_burst_size {16} CONFIG.c_include_mm2s_dre {0}] $dma
    connect_bd_intf_net [get_bd_intf_pins axi_dma/M_AXIS_MM2S] [get_bd_intf_pins e3c/s_axis]
    connect_bd_intf_net [get_bd_intf_pins $smc/M[format %02d $idx_dma]_AXI] [get_bd_intf_pins axi_dma/S_AXI_LITE]
    connect_bd_intf_net [get_bd_intf_pins $smc/M[format %02d $idx_src]_AXI] [get_bd_intf_pins e3c/s_axi_rep]
    connect_bd_net $clk [get_bd_pins axi_dma/s_axi_lite_aclk] [get_bd_pins axi_dma/m_axi_mm2s_aclk]
    connect_bd_net $rstn [get_bd_pins axi_dma/axi_resetn] [get_bd_pins e3c/src_aresetn]
    foreach {name pin} {DMA axi_dma/S_AXI_LITE ESRC e3c/s_axi_rep} {
        set base [soc_map_value $repo KV260_${name}_PS_BASE]
        set size [soc_map_value $repo KV260_${name}_PS_SIZE]
        assign_bd_address -offset [format 0x%08X $base] -range "[expr {$size / 1024}]K" \
            -target_address_space $space [get_bd_addr_segs -of_objects [get_bd_intf_pins $pin]]
    }
}

proc create_e5_sim_bd {repo design_name paths} {
    create_bd_design $design_name
    set aclk [create_bd_port -dir I -type clk -freq_hz 100000000 aclk]
    set aresetn [create_bd_port -dir I -type rst aresetn]
    set ps_aresetn [create_bd_port -dir I -type rst ps_aresetn]
    set_property CONFIG.POLARITY ACTIVE_LOW $aresetn
    set_property CONFIG.POLARITY ACTIVE_LOW $ps_aresetn
    set_property CONFIG.ASSOCIATED_RESET {aresetn:ps_aresetn} $aclk

    # ---- RV32I, SYSCTRL, DSP / CNN (no replay page on the CPU side) ----
    set def [e3c_read_defines $paths]
    set cpu [create_bd_cell -type module -reference rv32_soc_top cpu_subsys]
    set_property -dict [list CONFIG.E3C 1 CONFIG.E3C_REPLAY 0 CONFIG.MBOX 1] $cpu
    create_bd_cell -type module -reference soc_sysctrl sysctrl
    set e3c [create_bd_cell -type module -reference emg_e5_subsys e3c]
    set cfg [list CONFIG.DSP_COEF_FILE [dict get $def DSP_COEF_FILE]]
    foreach p {W1 B1 M1 SH1 W2 B2 M2 SH2 W3 B3 M3 SH3 FCW FCB} {
        lappend cfg CONFIG.${p}_FILE [dict get $def ${p}_FILE]
    }
    set_property -dict $cfg $e3c
    set smc [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 axi_smc]
    set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {3}] $smc
    connect_bd_net $aclk [get_bd_pins cpu_subsys/aclk] [get_bd_pins sysctrl/aclk] \
        [get_bd_pins e3c/aclk] [get_bd_pins axi_smc/aclk]
    connect_bd_net $aresetn [get_bd_pins cpu_subsys/aresetn] [get_bd_pins sysctrl/aresetn] \
        [get_bd_pins e3c/aresetn] [get_bd_pins axi_smc/aresetn]
    connect_bd_intf_net [get_bd_intf_pins cpu_subsys/m_axi] [get_bd_intf_pins axi_smc/S00_AXI]
    connect_bd_intf_net [get_bd_intf_pins axi_smc/M00_AXI] [get_bd_intf_pins sysctrl/s_axi]
    connect_bd_intf_net [get_bd_intf_pins axi_smc/M01_AXI] [get_bd_intf_pins e3c/s_axi_dsp]
    connect_bd_intf_net [get_bd_intf_pins axi_smc/M02_AXI] [get_bd_intf_pins e3c/s_axi_cnn]
    set cspace [get_bd_addr_spaces cpu_subsys/m_axi]
    foreach {name pin} {SYSCTRL sysctrl/s_axi DSP e3c/s_axi_dsp CNN e3c/s_axi_cnn} {
        set base [soc_map_value $repo SOC_${name}_BASE]
        set size [soc_map_value $repo SOC_${name}_SIZE]
        assign_bd_address -offset [format 0x%08X $base] -range "[expr {$size / 1024}]K" \
            -target_address_space $cspace [get_bd_addr_segs -of_objects [get_bd_intf_pins $pin]]
    }

    # ---- PS side: mailbox, AXI DMA, source page ----
    set s_axi [create_bd_intf_port -mode Slave -vlnv xilinx.com:interface:aximm_rtl:1.0 S_AXI_PS]
    set_property -dict [list CONFIG.PROTOCOL {AXI4LITE} CONFIG.ADDR_WIDTH {32} CONFIG.DATA_WIDTH {32} \
        CONFIG.FREQ_HZ {100000000}] $s_axi
    set psmc [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 ps_smc]
    set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {3}] $psmc
    connect_bd_intf_net $s_axi [get_bd_intf_pins ps_smc/S00_AXI]
    connect_bd_net $aclk [get_bd_pins ps_smc/aclk]
    connect_bd_net $ps_aresetn [get_bd_pins ps_smc/aresetn]
    add_mbx_cells $repo ps_smc 0 $aclk $ps_aresetn [get_bd_addr_spaces S_AXI_PS]
    add_dma_cells $repo ps_smc 1 2 $aclk $ps_aresetn [get_bd_addr_spaces S_AXI_PS]

    # ---- the test bench's memory instead of the PS DDR ----
    set m_axi [create_bd_intf_port -mode Master -vlnv xilinx.com:interface:aximm_rtl:1.0 M_AXI_DDR]
    set_property -dict [list CONFIG.PROTOCOL {AXI4} CONFIG.ADDR_WIDTH {40} CONFIG.DATA_WIDTH {128} \
        CONFIG.READ_WRITE_MODE {READ_ONLY} CONFIG.FREQ_HZ {100000000}] $m_axi
    connect_bd_intf_net [get_bd_intf_pins axi_dma/M_AXI_MM2S] $m_axi
    set_property CONFIG.ASSOCIATED_BUSIF {S_AXI_PS:M_AXI_DDR} $aclk
    assign_bd_address -offset 0x0 -range 4G -target_address_space [get_bd_addr_spaces axi_dma/Data_MM2S] \
        [get_bd_addr_segs M_AXI_DDR/Reg]
    validate_bd_design
    save_bd_design
}
