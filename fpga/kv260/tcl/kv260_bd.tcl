# kv260_bd.tcl - stage 6A block design: the RV32I SoC on the KV260 PL with the
# Zynq UltraScale+ PS as clock/reset source (Vivado 2026.1). Two debug variants:
#
#   dbg_mode jtag : JTAG debug cores (VIO + ILA), no PS-PL AXI port
#   dbg_mode ps   : no JTAG debug cores; the PS reads the results through
#                   M_AXI_HPM0_FPD -> ps_smc -> dbg (kv260_dbg_axil) at 0xA000_0000.
#                   Needed because any JTAG session hangs the KV260 Linux
#                   (docs/fpga/kv260_stage6a.md section 9).
#
#   zynq_ultra_ps_e_0  PS8 (board preset kv260_som), PL_CLK0 100 MHz from IOPLL,
#                      one fabric reset (pl_resetn0); ps: M_AXI_HPM0_FPD 32 bit
#                      (must equal the PS-side width the running system has set in
#                      FPD_SLCR AFI_FS: the PYNQ overlay writes config-afi <14 0> = 32 bit;
#                      loading a bitstream from Linux does not change it)
#   rst_pl0            proc_sys_reset of the SoC: slowest_sync_clk = pl_clk0,
#                      ext_reset_in = obs/ext_reset_n (pl_resetn0, active low),
#                      aux_reset_in = CPU re-run request (jtag: vio/probe_out0,
#                      active low, init 1; ps: dbg/cpu_reset, active high, reset 0),
#                      dcm_locked = 1 (no MMCM)
#                      interconnect_aresetn -> axi_smc
#                      peripheral_aresetn   -> cpu_subsys, sysctrl, obs
#   rst_ps (ps)        proc_sys_reset of the PS debug path: ext_reset_in = pl_resetn0
#                      only, so dbg/CTRL survives the CPU resets it requests
#                      interconnect_aresetn -> ps_smc, peripheral_aresetn -> dbg
#   cpu_subsys         kv260_cpu_dbg (module reference): the unchanged rv32_soc_top
#                      (CPU, 64 KB RAM with INIT_FILE, CMI router, cmi2axil) with
#                      m_axi passed through and the debug outputs packed
#   axi_smc            AMD AXI SmartConnect, 1 SI / 1 MI (CPU -> SYSCTRL)
#   sysctrl            soc_sysctrl (module reference), window from soc_addr_map.vh
#   obs                kv260_dbg_obs (module reference): counters, reset gating
#   vio, ila (jtag)    VIO: run/reset control and readout; ILA 6.2 with 5 packed
#                      probes (BASIC license limit)
#   ps_smc, dbg (ps)   AMD AXI SmartConnect 1 SI / 1 MI (PS AXI4 -> AXI4-Lite);
#                      kv260_dbg_axil: status/control registers + 4096-record trace
#   spi (ps_spi)       ps_smc M01 -> emg_spi_monitor_axil at 0xA013_0000;
#                      external SCLK/MOSI/CS_N/READY are constrained to J2 Pmod
#
#   mbx (E4a, needs e3c): cpu_subsys MBOX = 1; ps_smc 1 SI / 2 MI: M01 -> mbx_ctrl (AXI BRAM
#                      Controller) -> mbx_mem port B, cpu_subsys/MBX_BRAM -> port A
#                      (fpga/kv260/tcl/mbx_bd.tcl), PS address KV260_MBX_PS_BASE (0xA010_0000, 4 KB);
#                      mbx_ctrl reset = rst_ps (PS side survives CPU re-runs)
#   dma (E5a, needs mbx): the replay page is gone. cpu_subsys E3C_REPLAY = 0, axi_smc 3 MI, e3c = emg_e5_subsys;
#                      ps_smc 1 SI / 4 MI: M02 -> axi_dma, M03 -> source page (fpga/kv260/tcl/dma_bd.tcl);
#                      axi_dma/M_AXI_MM2S -> S_AXI_HP0_FPD, DDR segments only. 128 bit is what the PS side is
#                      assumed to run at: a load from Linux does not set AFIFM2 RDCTRL, host/ps_dma/device.py checks
#                      it read-only before any transfer (not verified on the board yet)
#   e3c (e3c_paths given, dbg_mode ps): cpu_subsys E3C = 1, axi_smc 1 SI / 4 MI and the
#                      E3c pipeline emg_e3c_subsys (fpga/kv260/tcl/e3c_bd.tcl address map:
#                      DSP 0x1000_2000, CNN 0x1000_5000, replay 0x1000_6000); reset =
#                      peripheral_aresetn (re-run through dbg/CTRL.cpu_reset resets it too)
#
# Usage (from build_kv260.tcl, after soc_bd.tcl has been sourced for soc_map_value):
#   create_kv260_bd <repo root> <design name> <INIT_FILE> [<ila depth>] [jtag|ps] [<e3c_paths.vh>] [<mbx 0|1>] [<dma 0|1>] [<spi 0|1>] [<spilive 0|1>]

proc kv260_net {pin_paths} {
    set pins {}
    foreach p $pin_paths { lappend pins [get_bd_pins $p] }
    connect_bd_net {*}$pins
}

# debug slave window for the PS (dbg_mode ps)
set KV260_DBG_BASE 0xA0000000
set KV260_DBG_RANGE 256K
set KV260_SPI_BASE 0xA0130000
set KV260_SPI_RANGE 4K

proc create_kv260_bd {repo design_name init_file {ila_depth 4096} {dbg_mode jtag} {e3c_paths ""} {mbx 0} {dma 0} {spi 0} {spilive 0}} {
    global KV260_DBG_BASE KV260_DBG_RANGE KV260_SPI_BASE KV260_SPI_RANGE
    if {$dbg_mode ni {jtag ps}} { error "dbg_mode must be jtag or ps" }
    set e3c [expr {$e3c_paths ne ""}]
    if {$e3c && $dbg_mode ne "ps"} { error "the E3c variant is built on the ps debug variant" }
    if {$mbx && !$e3c} { error "the mailbox variant (E4a) needs the E3c variant" }
    if {$spi && $dbg_mode ne "ps"} { error "the SPI monitor needs the PS AXI debug path" }
    if {$dma && !$mbx} { error "the DMA variant (E5a) needs the mailbox variant" }
    if {$spilive && !$mbx} { error "the live SPI variant needs the mailbox variant" }
    set sys_base [soc_map_value $repo SOC_SYSCTRL_BASE]
    set sys_size [soc_map_value $repo SOC_SYSCTRL_SIZE]
    set sys_range "[expr {$sys_size / 1024}]K"

    create_bd_design $design_name

    # ---------------- PS: clock and reset (+ HPM0 for the ps variant) ----------------
    set ps [create_bd_cell -type ip -vlnv xilinx.com:ip:zynq_ultra_ps_e:3.5 zynq_ultra_ps_e_0]
    apply_bd_automation -rule xilinx.com:bd_rule:zynq_ultra_ps_e \
        -config {apply_board_preset "1"} $ps
    set_property -dict [list \
        CONFIG.PSU__USE__M_AXI_GP0 [expr {$dbg_mode eq "ps" ? 1 : 0}] \
        CONFIG.PSU__USE__M_AXI_GP1 {0} \
        CONFIG.PSU__USE__M_AXI_GP2 {0} \
        CONFIG.PSU__USE__IRQ0 {0} \
        CONFIG.PSU__USE__IRQ1 {0} \
        CONFIG.PSU__FPGA_PL0_ENABLE {1} \
        CONFIG.PSU__FPGA_PL1_ENABLE {0} \
        CONFIG.PSU__FPGA_PL2_ENABLE {0} \
        CONFIG.PSU__FPGA_PL3_ENABLE {0} \
        CONFIG.PSU__CRL_APB__PL0_REF_CTRL__SRCSEL {IOPLL} \
        CONFIG.PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ {100} \
        CONFIG.PSU__NUM_FABRIC_RESETS {1} \
    ] $ps
    if {$dbg_mode eq "ps"} {
        set_property CONFIG.PSU__MAXIGP0__DATA_WIDTH {32} $ps
    }
    if {$dma} {
        set_property -dict [list CONFIG.PSU__USE__S_AXI_GP2 {1} CONFIG.PSU__SAXIGP2__DATA_WIDTH {128}] $ps
    }

    # ---------------- self-developed RTL ----------------
    # cpu_subsys: kv260_cpu_dbg = rv32_soc_top (unchanged) + debug signal packing
    set cpu [create_bd_cell -type module -reference kv260_cpu_dbg cpu_subsys]
    set_property CONFIG.INIT_FILE $init_file $cpu
    if {$e3c} { set_property CONFIG.E3C 1 $cpu }
    if {$mbx} { set_property CONFIG.MBOX 1 $cpu }
    if {$dma || $spilive} { set_property CONFIG.E3C_REPLAY 0 $cpu }
    create_bd_cell -type module -reference soc_sysctrl sysctrl
    create_bd_cell -type module -reference kv260_dbg_obs obs

    # ---------------- reset generator of the SoC ----------------
    set rst [create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 rst_pl0]
    # C_EXT_RESET_HIGH follows the POLARITY of obs/ext_reset_n (ACTIVE_LOW, read-only here)
    set_property CONFIG.C_AUX_RESET_HIGH [expr {$dbg_mode eq "ps" ? 1 : 0}] $rst

    # ---------------- AMD AXI SmartConnect (CPU -> SYSCTRL) ----------------
    set smc [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 axi_smc]
    set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI [expr {($dma || $spilive) ? 3 : ($e3c ? 4 : 1)}]] $smc
    if {$e3c} {
        set def [e3c_read_defines $e3c_paths]
        if {$dma} {
            set ec [create_bd_cell -type module -reference emg_e5_subsys e3c]
            set cfg [list CONFIG.DSP_COEF_FILE [dict get $def DSP_COEF_FILE]]
        } elseif {$spilive} {
            set ec [create_bd_cell -type module -reference emg_spi_subsys e3c]
            set cfg [list CONFIG.DSP_COEF_FILE [dict get $def DSP_COEF_FILE]]
        } else {
            set ec [create_bd_cell -type module -reference emg_e3c_subsys e3c]
            set cfg [list CONFIG.REPLAY_FILE [dict get $def E3C_REPLAY_FILE]                       CONFIG.REPLAY_FRAMES [dict get $def E3C_REPLAY_FRAMES]                       CONFIG.DSP_COEF_FILE [dict get $def DSP_COEF_FILE]]
        }
        foreach p {W1 B1 M1 SH1 W2 B2 M2 SH2 W3 B3 M3 SH3 FCW FCB} {
            lappend cfg CONFIG.${p}_FILE [dict get $def ${p}_FILE]
        }
        set_property -dict $cfg $ec
    }

    set vio_in {}
    set ila_in {}
    set clk_pins {zynq_ultra_ps_e_0/pl_clk0 rst_pl0/slowest_sync_clk cpu_subsys/aclk sysctrl/aclk
                  axi_smc/aclk obs/aclk}
    if {$e3c} { lappend clk_pins e3c/aclk }
    if {$dma} { lappend clk_pins zynq_ultra_ps_e_0/saxihp0_fpd_aclk }
    if {$dbg_mode eq "jtag"} {
        # ---------------- VIO ----------------
        # outputs: 0 cpu_rst_n (aux reset, active low, init 1 = run)
        #          1 ps_rst_override (init 0 = use pl_resetn0)
        set vio_in {
            obs/first_tohost obs/tohost_writes sysctrl/tohost_valid sysctrl/tohost_value
            cpu_subsys/halted cpu_subsys/trap_cause cpu_subsys/trap_pc cpu_subsys/trap_tval
            obs/run_cycles obs/retired obs/tohost_retired obs/reset_releases
            rst_pl0/peripheral_aresetn zynq_ultra_ps_e_0/pl_resetn0 sysctrl/led
            cpu_subsys/dbg_imem_req_addr obs/free_cycles
        }
        set vio [create_bd_cell -type ip -vlnv xilinx.com:ip:vio:3.0 vio]
        set_property -dict [list CONFIG.C_NUM_PROBE_OUT {2} CONFIG.C_NUM_PROBE_IN [llength $vio_in] \
            CONFIG.C_PROBE_OUT0_INIT_VAL {0x1} CONFIG.C_PROBE_OUT1_INIT_VAL {0x0}] $vio

        # ---------------- ILA (5 probes: BASIC license limit, see kv260_cpu_dbg.v) ----------------
        # probe0 ila_ctrl[63:0], probe1 retired[31:0], probe2 ila_cpu[159:0],
        # probe3 ila_trap[95:0], probe4 ila_axi[127:0]
        set ila_in {cpu_subsys/ila_ctrl obs/retired cpu_subsys/ila_cpu cpu_subsys/ila_trap cpu_subsys/ila_axi}
        set ila_w  {64 32 160 96 128}
        set ila [create_bd_cell -type ip -vlnv xilinx.com:ip:ila:6.2 ila]
        set cfg [list CONFIG.C_MONITOR_TYPE {Native} CONFIG.C_NUM_OF_PROBES [llength $ila_in] \
            CONFIG.C_DATA_DEPTH $ila_depth CONFIG.C_EN_STRG_QUAL {1} CONFIG.C_INPUT_PIPE_STAGES {1}]
        for {set i 0} {$i < [llength $ila_in]} {incr i} {
            lappend cfg CONFIG.C_PROBE${i}_WIDTH [lindex $ila_w $i] CONFIG.C_PROBE${i}_MU_CNT {2}
        }
        set_property -dict $cfg $ila
        lappend clk_pins vio/clk ila/clk
    } else {
        # ---------------- PS debug path ----------------
        set rps [create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 rst_ps]
        set psmc [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 ps_smc]
        set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI [expr {$dma ? 4 : (1 + $mbx + $spi + $spilive)}]] $psmc
        create_bd_cell -type module -reference kv260_dbg_axil dbg
        lappend clk_pins zynq_ultra_ps_e_0/maxihpm0_fpd_aclk rst_ps/slowest_sync_clk ps_smc/aclk dbg/aclk
        if {$spi} {
            create_bd_cell -type module -reference emg_spi_monitor_axil spi_mon
            foreach {name dir} {spi_sclk I spi_mosi I spi_cs_n I spi_ready O} {
                create_bd_port -dir $dir $name
                connect_bd_net [get_bd_ports $name] [get_bd_pins spi_mon/$name]
            }
            lappend clk_pins spi_mon/aclk
        }
    }

    # ---------------- clock ----------------
    kv260_net $clk_pins

    # ---------------- reset ----------------
    kv260_net {zynq_ultra_ps_e_0/pl_resetn0 obs/pl_resetn0}
    kv260_net {obs/ext_reset_n rst_pl0/ext_reset_in}
    kv260_net {obs/dcm_locked rst_pl0/dcm_locked}
    kv260_net {rst_pl0/interconnect_aresetn axi_smc/aresetn}
    kv260_net {rst_pl0/peripheral_aresetn cpu_subsys/aresetn sysctrl/aresetn obs/rst_n}
    if {$e3c} { kv260_net {rst_pl0/peripheral_aresetn e3c/aresetn} }
    if {$dbg_mode eq "jtag"} {
        kv260_net {vio/probe_out0 rst_pl0/aux_reset_in}
        kv260_net {vio/probe_out1 obs/ps_rst_override}
    } else {
        kv260_net {dbg/cpu_reset rst_pl0/aux_reset_in}
        kv260_net {dbg/ps_rst_override obs/ps_rst_override}
        kv260_net {zynq_ultra_ps_e_0/pl_resetn0 rst_ps/ext_reset_in}
        kv260_net {obs/dcm_locked rst_ps/dcm_locked}
        kv260_net {rst_ps/interconnect_aresetn ps_smc/aresetn}
        kv260_net {rst_ps/peripheral_aresetn dbg/aresetn}
        if {$spi} { kv260_net {rst_ps/peripheral_aresetn spi_mon/aresetn} }
    }

    # ---------------- AXI: CPU -> SmartConnect -> SYSCTRL ----------------
    connect_bd_intf_net [get_bd_intf_pins cpu_subsys/m_axi] [get_bd_intf_pins axi_smc/S00_AXI]
    connect_bd_intf_net [get_bd_intf_pins axi_smc/M00_AXI] [get_bd_intf_pins sysctrl/s_axi]
    if {$e3c} {
        connect_bd_intf_net [get_bd_intf_pins axi_smc/M01_AXI] [get_bd_intf_pins e3c/s_axi_dsp]
        connect_bd_intf_net [get_bd_intf_pins axi_smc/M02_AXI] [get_bd_intf_pins e3c/s_axi_cnn]
        if {!$dma && !$spilive} { connect_bd_intf_net [get_bd_intf_pins axi_smc/M03_AXI] [get_bd_intf_pins e3c/s_axi_rep] }
    }

    # ---------------- observation ----------------
    kv260_net {cpu_subsys/dbg_retire_valid obs/retire_valid}
    kv260_net {sysctrl/tohost_wr obs/tohost_wr cpu_subsys/tohost_wr}
    kv260_net {sysctrl/tohost_value obs/tohost_value cpu_subsys/tohost_value}
    kv260_net {zynq_ultra_ps_e_0/pl_resetn0 cpu_subsys/pl_resetn0}
    if {$dbg_mode eq "jtag"} {
        for {set i 0} {$i < [llength $vio_in]} {incr i} {
            connect_bd_net [get_bd_pins [lindex $vio_in $i]] [get_bd_pins vio/probe_in$i]
        }
        for {set i 0} {$i < [llength $ila_in]} {incr i} {
            connect_bd_net [get_bd_pins [lindex $ila_in $i]] [get_bd_pins ila/probe$i]
        }
    } else {
        connect_bd_intf_net [get_bd_intf_pins zynq_ultra_ps_e_0/M_AXI_HPM0_FPD] [get_bd_intf_pins ps_smc/S00_AXI]
        connect_bd_intf_net [get_bd_intf_pins ps_smc/M00_AXI] [get_bd_intf_pins dbg/s_axi]
        if {$spi} {
            set spi_mi [format "M%02d_AXI" [expr {1 + $mbx}]]
            connect_bd_intf_net [get_bd_intf_pins ps_smc/$spi_mi] [get_bd_intf_pins spi_mon/s_axi]
        }
        foreach {src dst} {
            cpu_subsys/ila_ctrl ila_ctrl  cpu_subsys/ila_cpu ila_cpu  cpu_subsys/ila_trap ila_trap
            obs/retired retired  obs/run_cycles run_cycles  obs/tohost_retired tohost_retired
            obs/tohost_writes tohost_writes  obs/first_tohost first_tohost
            obs/reset_releases reset_releases  obs/free_cycles free_cycles
            sysctrl/tohost_valid tohost_valid  sysctrl/led led  cpu_subsys/dbg_imem_req_addr fetch_pc
            rst_pl0/peripheral_aresetn soc_aresetn  zynq_ultra_ps_e_0/pl_resetn0 pl_resetn0
        } {
            connect_bd_net [get_bd_pins $src] [get_bd_pins dbg/$dst]
        }
    }

    # ---------------- address map ----------------
    assign_bd_address -offset [format 0x%08X $sys_base] -range $sys_range \
        -target_address_space [get_bd_addr_spaces cpu_subsys/m_axi] \
        [get_bd_addr_segs -of_objects [get_bd_intf_pins sysctrl/s_axi]]
    if {$e3c} {
        foreach {name pin} [expr {($dma || $spilive) ? {DSP e3c/s_axi_dsp CNN e3c/s_axi_cnn}
                                       : {DSP e3c/s_axi_dsp CNN e3c/s_axi_cnn REPLAY e3c/s_axi_rep}}] {
            set base [soc_map_value $repo SOC_${name}_BASE]
            set size [soc_map_value $repo SOC_${name}_SIZE]
            assign_bd_address -offset [format 0x%08X $base] -range "[expr {$size / 1024}]K"                 -target_address_space [get_bd_addr_spaces cpu_subsys/m_axi]                 [get_bd_addr_segs -of_objects [get_bd_intf_pins $pin]]
        }
    }
    if {$mbx} {
        add_mbx_cells $repo ps_smc 1 [get_bd_pins zynq_ultra_ps_e_0/pl_clk0] \
            [get_bd_pins rst_ps/peripheral_aresetn] [get_bd_addr_spaces zynq_ultra_ps_e_0/Data]
    }
    if {$spilive} {
        # STM32 -> J2 Pmod -> e3c (esi_spi_src_axil); its page at KV260_SPI_BASE, PS reset domain (as the E5a source)
        foreach {name dir} {spi_sclk I spi_mosi I spi_cs_n I spi_ready O} {
            create_bd_port -dir $dir $name
            connect_bd_net [get_bd_ports $name] [get_bd_pins e3c/$name]
        }
        connect_bd_net [get_bd_pins rst_ps/peripheral_aresetn] [get_bd_pins e3c/src_aresetn]
        connect_bd_intf_net [get_bd_intf_pins ps_smc/M02_AXI] [get_bd_intf_pins e3c/s_axi_rep]
        assign_bd_address -offset $KV260_SPI_BASE -range $KV260_SPI_RANGE \
            -target_address_space [get_bd_addr_spaces zynq_ultra_ps_e_0/Data] \
            [get_bd_addr_segs -of_objects [get_bd_intf_pins e3c/s_axi_rep]]
    }
    if {$dma} {
        add_dma_cells $repo ps_smc 2 3 [get_bd_pins zynq_ultra_ps_e_0/pl_clk0]             [get_bd_pins rst_ps/peripheral_aresetn] [get_bd_addr_spaces zynq_ultra_ps_e_0/Data]
        connect_bd_intf_net [get_bd_intf_pins axi_dma/M_AXI_MM2S] [get_bd_intf_pins zynq_ultra_ps_e_0/S_AXI_HP0_FPD]
        foreach seg {HP0_DDR_LOW HP0_DDR_HIGH} {
            assign_bd_address -target_address_space [get_bd_addr_spaces axi_dma/Data_MM2S]                 [get_bd_addr_segs zynq_ultra_ps_e_0/SAXIGP2/$seg]
        }
    }
    if {$dbg_mode eq "ps"} {
        assign_bd_address -offset $KV260_DBG_BASE -range $KV260_DBG_RANGE \
            -target_address_space [get_bd_addr_spaces zynq_ultra_ps_e_0/Data] \
            [get_bd_addr_segs -of_objects [get_bd_intf_pins dbg/s_axi]]
        if {$spi} {
            assign_bd_address -offset $KV260_SPI_BASE -range $KV260_SPI_RANGE \
                -target_address_space [get_bd_addr_spaces zynq_ultra_ps_e_0/Data] \
                [get_bd_addr_segs -of_objects [get_bd_intf_pins spi_mon/s_axi]]
        }
    }

    validate_bd_design
    save_bd_design
    return [list $vio_in $ila_in]
}
