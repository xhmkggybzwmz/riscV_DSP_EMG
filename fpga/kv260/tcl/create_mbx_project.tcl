# create_mbx_project.tcl - Vivado 2026.1 project for the E4a mailbox system simulation
# with the real AMD AXI SmartConnects, Block Memory Generator and AXI BRAM Controller
# (no board pins, no PS, no bitstream).
#
#   vivado -mode batch -source fpga/kv260/tcl/create_mbx_project.tcl -tclargs <repo root> <project dir>
#
# Creates <project dir>/mbx_sim.xpr:
#   sources_1 : rtl/cpu, rtl/soc (rv32_soc_top E3C = 1, MBOX = 1), rtl/dsp, rtl/infer, rtl/e3c,
#               block design mbx_bd (e3c_bd.tcl + mbx_bd.tcl) and its wrapper
#   sim_1     : dv/mbx/tb_mbx_soc.v (define MBX_BD) + AXI monitor / master; xsim scripts exported
# Needs build/e3c/e3c_paths.vh and the model files (scripts/sim/gen_e3c.py).
# Writes <project dir>/reports/bd_summary.txt (cells, IP configuration, address maps).

if {[llength $argv] < 2} { error "usage: -tclargs <repo root> <project dir>" }
set repo     [file normalize [lindex $argv 0]]
set proj_dir [file normalize [lindex $argv 1]]
set part     xck26-sfvc784-2LV-c
set paths    $repo/build/e3c/e3c_paths.vh
if {![file isfile $paths]} { error "missing $paths: run scripts/sim/gen_e3c.py" }

puts "INFO: Vivado [version -short], part $part"
create_project mbx_sim $proj_dir -part $part -force
set_property target_language Verilog [current_project]

set rtl [list]
foreach f {rv32i_regfile rv32i_alu rv32i_decoder rv32i_lsu rv32i_core} { lappend rtl $repo/rtl/cpu/$f.v }
foreach f {soc_ram_dp soc_dmem_router cmi2axil soc_io_split cmi_bram_port soc_sysctrl rv32_soc_top} {
    lappend rtl $repo/rtl/soc/$f.v
}
lappend rtl $repo/rtl/dsp/emg_dsp.v $repo/rtl/infer/emg_cnn.v
foreach f {e3c_axil_if emg_dsp_axil emg_cnn_axil esi_replay_axil emg_e3c_subsys} { lappend rtl $repo/rtl/e3c/$f.v }
add_files -norecurse -fileset sources_1 $rtl
add_files -norecurse -fileset sources_1 [list $repo/rtl/cpu/rv32i_defs.vh $repo/rtl/soc/soc_addr_map.vh \
    $repo/rtl/esi/esi_defs.vh]
set_property file_type {Verilog Header} [get_files {rv32i_defs.vh soc_addr_map.vh esi_defs.vh}]
set_property include_dirs [list $repo/rtl/cpu $repo/rtl/soc $repo/rtl/esi] [get_filesets sources_1]
update_compile_order -fileset sources_1

source $repo/fpga/kv260/tcl/soc_bd.tcl
source $repo/fpga/kv260/tcl/e3c_bd.tcl
source $repo/fpga/kv260/tcl/mbx_bd.tcl
create_mbx_sim_bd $repo mbx_bd $paths
set bd_file [get_files mbx_bd.bd]
generate_target all $bd_file
set wrapper [make_wrapper -files $bd_file -top]
add_files -norecurse -fileset sources_1 $wrapper
set_property top mbx_bd_wrapper [get_filesets sources_1]
update_compile_order -fileset sources_1

file mkdir $proj_dir/reports
set fh [open $proj_dir/reports/bd_summary.txt w]
puts $fh "Vivado [version -short]  part $part"
foreach c [lsort [get_bd_cells]] { puts $fh "cell [get_property NAME $c]  vlnv=[get_property VLNV $c]" }
puts $fh "cpu_subsys E3C = [get_property CONFIG.E3C [get_bd_cells cpu_subsys]] MBOX = [get_property CONFIG.MBOX [get_bd_cells cpu_subsys]]"
foreach p {CONFIG.Memory_Type CONFIG.use_bram_block CONFIG.Operating_Mode_A CONFIG.Operating_Mode_B \
           CONFIG.Write_Width_A CONFIG.Write_Depth_A CONFIG.Write_Width_B CONFIG.Use_Byte_Write_Enable \
           CONFIG.Register_PortA_Output_of_Memory_Primitives CONFIG.Register_PortB_Output_of_Memory_Primitives \
           CONFIG.Enable_32bit_Address CONFIG.EN_SAFETY_CKT} {
    puts $fh "mbx_mem $p = [get_property -quiet $p [get_bd_cells mbx_mem]]"
}
foreach p {CONFIG.PROTOCOL CONFIG.SINGLE_PORT_BRAM CONFIG.DATA_WIDTH CONFIG.MEM_DEPTH CONFIG.READ_LATENCY} {
    puts $fh "mbx_ctrl $p = [get_property -quiet $p [get_bd_cells mbx_ctrl]]"
}
foreach p {CONFIG.MEM_SIZE CONFIG.MEM_WIDTH CONFIG.READ_LATENCY CONFIG.MASTER_TYPE} {
    puts $fh "cpu_subsys/MBX_BRAM $p = [get_property -quiet $p [get_bd_intf_pins cpu_subsys/MBX_BRAM]]"
}
foreach s [get_bd_addr_segs -of_objects [get_bd_addr_spaces cpu_subsys/m_axi]] {
    puts $fh "addr cpu [get_property NAME $s] offset=[get_property OFFSET $s] range=[get_property RANGE $s]"
}
foreach s [get_bd_addr_segs -of_objects [get_bd_addr_spaces S_AXI_PS]] {
    puts $fh "addr ps [get_property NAME $s] offset=[get_property OFFSET $s] range=[get_property RANGE $s]"
}
close $fh

set sim [list $repo/dv/mbx/tb_mbx_soc.v $repo/dv/common/tb_axil_monitor.v $repo/dv/common/tb_axil_master.v]
add_files -norecurse -fileset sim_1 $sim
add_files -norecurse -fileset sim_1 [list $repo/dv/common/tb_check.vh $repo/dv/e3c/e3c_golden.vh \
    $repo/dv/e3c/e3c_timing.vh $repo/dv/mbx/mbx_layout.vh $repo/dv/mbx/mbx_monitor.vh \
    $repo/dv/mbx/mbx_ps_reader.vh $paths]
set_property file_type {Verilog Header} [get_files {tb_check.vh e3c_golden.vh e3c_timing.vh e3c_paths.vh \
    mbx_layout.vh mbx_monitor.vh mbx_ps_reader.vh}]
set_property include_dirs [list $repo/rtl/cpu $repo/rtl/soc $repo/rtl/esi $repo/dv/common $repo/dv/e3c \
    $repo/dv/mbx $repo/build/e3c] [get_filesets sim_1]
set_property verilog_define {MBX_BD=1} [get_filesets sim_1]
set_property top tb_mbx_soc [get_filesets sim_1]
set_property top_lib xil_defaultlib [get_filesets sim_1]
update_compile_order -fileset sim_1
launch_simulation -simset sim_1 -mode behavioral -scripts_only
puts "INFO: project ready: $proj_dir/mbx_sim.xpr"
