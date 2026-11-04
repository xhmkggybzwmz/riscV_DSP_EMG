# mbx_bd.tcl - E4a mailbox block-design part (Vivado 2026.1, docs/soc/e4a_mailbox.md).
#
#   cpu_subsys/MBX_BRAM (rv32_soc_top / kv260_cpu_dbg MBOX = 1, cmi_bram_port)
#       -> mbx_mem/BRAM_PORTA      Block Memory Generator, true dual port, BRAM_Controller
#                                  mode, 4 KB (32 bit x 1024), both ports READ_FIRST
#   <PS AXI> -> ps_smc -> mbx_ctrl/S_AXI   AXI BRAM Controller, AXI4-Lite, one BRAM port
#       mbx_ctrl/BRAM_PORTA -> mbx_mem/BRAM_PORTB
# Both BRAM ports run on the same clock (pl_clk0 / aclk): no clock-domain crossing.
# The PS address of the window is KV260_MBX_PS_BASE / _SIZE from rtl/soc/soc_addr_map.vh
# (the CPU side SOC_MBX_* is decoded by the CPU's router, not by an address editor).
# Requires soc_bd.tcl (soc_map_value) and e3c_bd.tcl to be sourced.
#
#   add_mbx_cells <repo> <ps_smc cell> <ps_smc master index> <ps clk> <ps resetn> <address space>
#       adds mbx_mem and mbx_ctrl, connects them to cpu_subsys/MBX_BRAM and to
#       <ps_smc>/M<idx>_AXI, assigns the PS address
#   create_mbx_sim_bd <repo> <design name> <paths.vh>
#       stand-alone BD for xsim: ports aclk (100 MHz), aresetn (SoC), ps_aresetn (PS side,
#       survives SoC resets like rst_ps on the board) and the AXI4-Lite slave port S_AXI_PS
#       (-> ps_smc 1 SI / 1 MI -> mbx_ctrl) that the test bench drives as the PS

proc add_mbx_cells {repo smc idx clk rstn space} {
    set mem [create_bd_cell -type ip -vlnv xilinx.com:ip:blk_mem_gen:8.4 mbx_mem]
    set_property -dict [list CONFIG.Memory_Type {True_Dual_Port_RAM} CONFIG.use_bram_block {BRAM_Controller} \
        CONFIG.Operating_Mode_A {READ_FIRST} CONFIG.Operating_Mode_B {READ_FIRST} \
        CONFIG.Enable_32bit_Address {true} CONFIG.Use_Byte_Write_Enable {true} CONFIG.Byte_Size {8} \
        CONFIG.Register_PortA_Output_of_Memory_Primitives {false} \
        CONFIG.Register_PortB_Output_of_Memory_Primitives {false} \
        CONFIG.Use_RSTA_Pin {true} CONFIG.Use_RSTB_Pin {true}] $mem
    set ctl [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_bram_ctrl:4.1 mbx_ctrl]
    set_property -dict [list CONFIG.PROTOCOL {AXI4LITE} CONFIG.SINGLE_PORT_BRAM {1} CONFIG.DATA_WIDTH {32} \
        CONFIG.ECC_TYPE {0}] $ctl
    connect_bd_intf_net [get_bd_intf_pins cpu_subsys/MBX_BRAM] [get_bd_intf_pins mbx_mem/BRAM_PORTA]
    connect_bd_intf_net [get_bd_intf_pins mbx_ctrl/BRAM_PORTA] [get_bd_intf_pins mbx_mem/BRAM_PORTB]
    connect_bd_intf_net [get_bd_intf_pins $smc/M[format %02d $idx]_AXI] [get_bd_intf_pins mbx_ctrl/S_AXI]
    connect_bd_net $clk [get_bd_pins mbx_ctrl/s_axi_aclk]
    connect_bd_net $rstn [get_bd_pins mbx_ctrl/s_axi_aresetn]
    set base [soc_map_value $repo KV260_MBX_PS_BASE]
    set size [soc_map_value $repo KV260_MBX_PS_SIZE]
    assign_bd_address -offset [format 0x%08X $base] -range "[expr {$size / 1024}]K" \
        -target_address_space $space [get_bd_addr_segs -of_objects [get_bd_intf_pins mbx_ctrl/S_AXI]]
}

proc create_mbx_sim_bd {repo design_name paths} {
    create_bd_design $design_name
    set aclk [create_bd_port -dir I -type clk -freq_hz 100000000 aclk]
    set aresetn [create_bd_port -dir I -type rst aresetn]
    set ps_aresetn [create_bd_port -dir I -type rst ps_aresetn]
    set_property CONFIG.POLARITY ACTIVE_LOW $aresetn
    set_property CONFIG.POLARITY ACTIVE_LOW $ps_aresetn
    set_property CONFIG.ASSOCIATED_RESET {aresetn:ps_aresetn} $aclk
    add_e3c_cells $repo $paths $aclk $aresetn $aresetn
    set_property CONFIG.MBOX 1 [get_bd_cells cpu_subsys]

    set s_axi [create_bd_intf_port -mode Slave -vlnv xilinx.com:interface:aximm_rtl:1.0 S_AXI_PS]
    set_property -dict [list CONFIG.PROTOCOL {AXI4LITE} CONFIG.ADDR_WIDTH {32} CONFIG.DATA_WIDTH {32} \
        CONFIG.FREQ_HZ {100000000}] $s_axi
    set_property CONFIG.ASSOCIATED_BUSIF {S_AXI_PS} $aclk
    set psmc [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 ps_smc]
    set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {1}] $psmc
    connect_bd_intf_net $s_axi [get_bd_intf_pins ps_smc/S00_AXI]
    connect_bd_net $aclk [get_bd_pins ps_smc/aclk]
    connect_bd_net $ps_aresetn [get_bd_pins ps_smc/aresetn]
    add_mbx_cells $repo ps_smc 0 $aclk $ps_aresetn [get_bd_addr_spaces S_AXI_PS]
    validate_bd_design
    save_bd_design
}
