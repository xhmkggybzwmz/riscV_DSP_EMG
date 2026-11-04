# e3c_bd.tcl - E3c block-design part (Vivado 2026.1): the RV32I CPU subsystem with
# E3C = 1, the AMD AXI SmartConnect with 4 master interfaces and the E3c pipeline.
#
#   cpu_subsys/m_axi -> axi_smc/S00_AXI
#   axi_smc/M00_AXI -> sysctrl/s_axi      SOC_SYSCTRL_* (0x1000_0000, 4 KB)
#   axi_smc/M01_AXI -> e3c/s_axi_dsp      SOC_DSP_*     (0x1000_2000, 4 KB)
#   axi_smc/M02_AXI -> e3c/s_axi_cnn      SOC_CNN_*     (0x1000_5000, 4 KB)
#   axi_smc/M03_AXI -> e3c/s_axi_rep      SOC_REPLAY_*  (0x1000_6000, 4 KB)
# All windows come from rtl/soc/soc_addr_map.vh, the file that also sets the CPU
# router windows (rv32_soc_top E3C = 1), so the router and the address editor agree.
# The model / replay files are the module parameters given by build/e3c/e3c_paths.vh
# (scripts/sim/gen_e3c.py). Requires soc_bd.tcl (soc_map_value) to be sourced.
#
#   add_e3c_cells <repo> <paths.vh> <clk> <periph resetn> <interconnect resetn>
#       adds cpu_subsys, axi_smc, sysctrl, e3c to the current BD; clock and resets
#       are BD objects (ports or pins)
#   create_e3c_sim_bd <repo> <design name> <paths.vh>
#       stand-alone BD for xsim: ports aclk (100 MHz) and aresetn

# `define NAME "value" | `define NAME number  from a Verilog header -> dict
proc e3c_read_defines {file} {
    set fh [open $file r]
    set d [dict create]
    foreach line [split [read $fh] "\n"] {
        if {[regexp {^`define\s+(\w+)\s+"([^"]*)"} $line -> n v]} { dict set d $n $v }
        if {[regexp {^`define\s+(\w+)\s+(\d+)\s*$} $line -> n v]} { dict set d $n $v }
    }
    close $fh
    return $d
}

proc add_e3c_cells {repo paths clk rstn icrstn} {
    set def [e3c_read_defines $paths]
    set cpu [create_bd_cell -type module -reference rv32_soc_top cpu_subsys]
    set_property CONFIG.E3C 1 $cpu
    create_bd_cell -type module -reference soc_sysctrl sysctrl
    set e3c [create_bd_cell -type module -reference emg_e3c_subsys e3c]
    set cfg [list CONFIG.REPLAY_FILE [dict get $def E3C_REPLAY_FILE] \
                  CONFIG.REPLAY_FRAMES [dict get $def E3C_REPLAY_FRAMES] \
                  CONFIG.DSP_COEF_FILE [dict get $def DSP_COEF_FILE]]
    foreach p {W1 B1 M1 SH1 W2 B2 M2 SH2 W3 B3 M3 SH3 FCW FCB} {
        lappend cfg CONFIG.${p}_FILE [dict get $def ${p}_FILE]
    }
    set_property -dict $cfg $e3c
    set smc [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 axi_smc]
    set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {4}] $smc

    connect_bd_net $clk [get_bd_pins cpu_subsys/aclk] [get_bd_pins sysctrl/aclk] \
        [get_bd_pins e3c/aclk] [get_bd_pins axi_smc/aclk]
    connect_bd_net $rstn [get_bd_pins cpu_subsys/aresetn] [get_bd_pins sysctrl/aresetn] [get_bd_pins e3c/aresetn]
    connect_bd_net $icrstn [get_bd_pins axi_smc/aresetn]
    connect_bd_intf_net [get_bd_intf_pins cpu_subsys/m_axi] [get_bd_intf_pins axi_smc/S00_AXI]
    connect_bd_intf_net [get_bd_intf_pins axi_smc/M00_AXI] [get_bd_intf_pins sysctrl/s_axi]
    connect_bd_intf_net [get_bd_intf_pins axi_smc/M01_AXI] [get_bd_intf_pins e3c/s_axi_dsp]
    connect_bd_intf_net [get_bd_intf_pins axi_smc/M02_AXI] [get_bd_intf_pins e3c/s_axi_cnn]
    connect_bd_intf_net [get_bd_intf_pins axi_smc/M03_AXI] [get_bd_intf_pins e3c/s_axi_rep]

    set space [get_bd_addr_spaces cpu_subsys/m_axi]
    foreach {name pin} {SYSCTRL sysctrl/s_axi DSP e3c/s_axi_dsp CNN e3c/s_axi_cnn REPLAY e3c/s_axi_rep} {
        set base [soc_map_value $repo SOC_${name}_BASE]
        set size [soc_map_value $repo SOC_${name}_SIZE]
        assign_bd_address -offset [format 0x%08X $base] -range "[expr {$size / 1024}]K" \
            -target_address_space $space [get_bd_addr_segs -of_objects [get_bd_intf_pins $pin]]
    }
}

proc create_e3c_sim_bd {repo design_name paths} {
    create_bd_design $design_name
    set aclk [create_bd_port -dir I -type clk -freq_hz 100000000 aclk]
    set aresetn [create_bd_port -dir I -type rst aresetn]
    set_property CONFIG.POLARITY ACTIVE_LOW $aresetn
    set_property CONFIG.ASSOCIATED_RESET aresetn $aclk
    add_e3c_cells $repo $paths $aclk $aresetn $aresetn
    validate_bd_design
    save_bd_design
}
