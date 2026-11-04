# soc_bd.tcl - Vivado 2026.1 IP Integrator block design of the RV32I SoC.
#
#   aclk, aresetn (external ports)
#   cpu_subsys : rv32_soc_top  (module reference, self-developed RTL)
#   axi_smc    : AMD AXI SmartConnect (xilinx.com:ip:smartconnect:1.0)
#   sysctrl    : soc_sysctrl   (module reference, self-developed RTL)
#
#   cpu_subsys/m_axi -> axi_smc/S00_AXI ; axi_smc/M00_AXI -> sysctrl/s_axi
#   Address map: SYSCTRL base/size are read from rtl/soc/soc_addr_map.vh, the
#   same file that sets the CPU-side router windows, so both always agree.
#   The router (soc_dmem_router) forwards only the SYSCTRL window to m_axi;
#   unmapped addresses get a CMI error and never reach the SmartConnect. The
#   SmartConnect's DECERR is not relied on: with one address segment it does
#   not decode (docs/soc/soc_bus_architecture.md section 10).
#
# Usage (from create_soc_project.tcl):
#   create_soc_bd <repo root> <design name> <INIT_FILE or ""> [<STRATEGY>] [<decode_probe 0|1>]
# decode_probe = 1 builds a VERIFICATION-ONLY variant: a second slave (another
# soc_sysctrl instance) at the reserved page 0x1000_F000 gives two address
# segments, and cpu_subsys gets AXI_DECODE_TEST=1 so that its router forwards
# all of 0x1000_0000-0x2FFF_FFFF to m_axi. That makes the SmartConnect's own
# DECERR path reachable from the CPU. Never used for the production design.

# `define NAME 32'hXXXX_XXXX from rtl/soc/soc_addr_map.vh
proc soc_map_value {repo name} {
    set fh [open $repo/rtl/soc/soc_addr_map.vh r]
    set text [read $fh]
    close $fh
    if {![regexp "`define\\s+$name\\s+32'h(\[0-9A-Fa-f_\]+)" $text -> hex]} {
        error "soc_addr_map.vh: $name not found"
    }
    return [expr {"0x[string map {_ {}} $hex]" + 0}]
}

proc create_soc_bd {repo design_name init_file {strategy AUTOMATIC} {decode_probe 0}} {
    set sys_base [soc_map_value $repo SOC_SYSCTRL_BASE]
    set sys_size [soc_map_value $repo SOC_SYSCTRL_SIZE]
    if {$sys_size % 1024 != 0} { error "SOC_SYSCTRL_SIZE must be a multiple of 1 KB" }
    set sys_range "[expr {$sys_size / 1024}]K"
    puts [format "INFO: SYSCTRL window from soc_addr_map.vh: base 0x%08X range %s" $sys_base $sys_range]

    create_bd_design $design_name

    # clock and reset ports
    set aclk [create_bd_port -dir I -type clk -freq_hz 100000000 aclk]
    set aresetn [create_bd_port -dir I -type rst aresetn]
    set_property CONFIG.POLARITY ACTIVE_LOW $aresetn
    set_property CONFIG.ASSOCIATED_RESET aresetn $aclk

    # self-developed RTL as module references
    set cpu [create_bd_cell -type module -reference rv32_soc_top cpu_subsys]
    if {$init_file ne ""} {
        set_property CONFIG.INIT_FILE $init_file $cpu
    }
    if {$decode_probe} {
        set_property CONFIG.AXI_DECODE_TEST 1 $cpu
    }
    set sys [create_bd_cell -type module -reference soc_sysctrl sysctrl]

    # AMD AXI SmartConnect: 1 slave interface, 1 master interface
    set smc [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 axi_smc]
    set num_mi [expr {$decode_probe ? 2 : 1}]
    set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI $num_mi CONFIG.STRATEGY $strategy] $smc

    connect_bd_net [get_bd_ports aclk] [get_bd_pins cpu_subsys/aclk] [get_bd_pins sysctrl/aclk] \
                   [get_bd_pins axi_smc/aclk]
    connect_bd_net [get_bd_ports aresetn] [get_bd_pins cpu_subsys/aresetn] [get_bd_pins sysctrl/aresetn] \
                   [get_bd_pins axi_smc/aresetn]
    connect_bd_intf_net [get_bd_intf_pins cpu_subsys/m_axi] [get_bd_intf_pins axi_smc/S00_AXI]
    connect_bd_intf_net [get_bd_intf_pins axi_smc/M00_AXI] [get_bd_intf_pins sysctrl/s_axi]
    if {$decode_probe} {
        create_bd_cell -type module -reference soc_sysctrl probe_slave
        connect_bd_net [get_bd_ports aclk] [get_bd_pins probe_slave/aclk]
        connect_bd_net [get_bd_ports aresetn] [get_bd_pins probe_slave/aresetn]
        connect_bd_intf_net [get_bd_intf_pins axi_smc/M01_AXI] [get_bd_intf_pins probe_slave/s_axi]
    }

    # address map
    set seg [get_bd_addr_segs -of_objects [get_bd_intf_pins sysctrl/s_axi]]
    assign_bd_address -offset [format 0x%08X $sys_base] -range $sys_range \
        -target_address_space [get_bd_addr_spaces cpu_subsys/m_axi] $seg
    if {$decode_probe} {
        assign_bd_address -offset 0x1000F000 -range 4K \
            -target_address_space [get_bd_addr_spaces cpu_subsys/m_axi] \
            [get_bd_addr_segs -of_objects [get_bd_intf_pins probe_slave/s_axi]]
    }

    validate_bd_design
    save_bd_design
}
