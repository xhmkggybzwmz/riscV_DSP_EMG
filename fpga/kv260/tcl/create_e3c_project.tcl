# create_e3c_project.tcl - Vivado 2026.1 project for the E3c system simulation with
# the real AMD AXI SmartConnect (no board pins, no PS, no bitstream).
#
#   vivado -mode batch -source fpga/kv260/tcl/create_e3c_project.tcl -tclargs <repo root> <project dir>
#
# Creates <project dir>/e3c_sim.xpr:
#   sources_1 : rtl/cpu, rtl/soc (rv32_soc_top E3C = 1), rtl/dsp, rtl/infer, rtl/e3c,
#               block design e3c_bd (fpga/kv260/tcl/e3c_bd.tcl) and its wrapper
#   sim_1     : dv/e3c/tb_e3c_soc.v (define E3C_BD) + AXI monitor; xsim scripts exported
# Needs build/e3c/e3c_paths.vh and the model files (scripts/sim/gen_e3c.py).
# Writes <project dir>/reports/bd_summary.txt (cells, SmartConnect config, address map).

if {[llength $argv] < 2} { error "usage: -tclargs <repo root> <project dir>" }
set repo     [file normalize [lindex $argv 0]]
set proj_dir [file normalize [lindex $argv 1]]
set part     xck26-sfvc784-2LV-c
set paths    $repo/build/e3c/e3c_paths.vh
if {![file isfile $paths]} { error "missing $paths: run scripts/sim/gen_e3c.py" }

puts "INFO: Vivado [version -short], part $part"
create_project e3c_sim $proj_dir -part $part -force
set_property target_language Verilog [current_project]

set rtl [list]
foreach f {rv32i_regfile rv32i_alu rv32i_decoder rv32i_lsu rv32i_core} { lappend rtl $repo/rtl/cpu/$f.v }
foreach f {soc_ram_dp soc_dmem_router cmi2axil soc_sysctrl rv32_soc_top} { lappend rtl $repo/rtl/soc/$f.v }
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
create_e3c_sim_bd $repo e3c_bd $paths
set bd_file [get_files e3c_bd.bd]
generate_target all $bd_file
set wrapper [make_wrapper -files $bd_file -top]
add_files -norecurse -fileset sources_1 $wrapper
set_property top e3c_bd_wrapper [get_filesets sources_1]
update_compile_order -fileset sources_1

file mkdir $proj_dir/reports
set fh [open $proj_dir/reports/bd_summary.txt w]
puts $fh "Vivado [version -short]  part $part"
foreach c [lsort [get_bd_cells]] { puts $fh "cell [get_property NAME $c]  vlnv=[get_property VLNV $c]" }
puts $fh "cpu_subsys E3C = [get_property CONFIG.E3C [get_bd_cells cpu_subsys]]"
foreach p {CONFIG.NUM_SI CONFIG.NUM_MI CONFIG.STRATEGY} {
    puts $fh "axi_smc $p = [get_property $p [get_bd_cells axi_smc]]"
}
foreach s [get_bd_addr_segs -of_objects [get_bd_addr_spaces cpu_subsys/m_axi]] {
    puts $fh "addr [get_property NAME $s] offset=[get_property OFFSET $s] range=[get_property RANGE $s]"
}
foreach i [get_bd_intf_pins -of_objects [get_bd_cells e3c]] {
    puts $fh "e3c intf [get_property NAME $i] vlnv=[get_property VLNV $i] mode=[get_property MODE $i]"
}
close $fh

set sim [list $repo/dv/e3c/tb_e3c_soc.v $repo/dv/common/tb_axil_monitor.v]
add_files -norecurse -fileset sim_1 $sim
add_files -norecurse -fileset sim_1 [list $repo/dv/common/tb_check.vh $repo/dv/e3c/e3c_golden.vh     $repo/dv/e3c/e3c_timing.vh $paths]
set_property file_type {Verilog Header} [get_files {tb_check.vh e3c_golden.vh e3c_timing.vh e3c_paths.vh}]
set_property include_dirs [list $repo/rtl/cpu $repo/rtl/soc $repo/rtl/esi $repo/dv/common $repo/dv/e3c \
    $repo/build/e3c] [get_filesets sim_1]
set_property verilog_define {E3C_BD=1} [get_filesets sim_1]
set_property top tb_e3c_soc [get_filesets sim_1]
set_property top_lib xil_defaultlib [get_filesets sim_1]
update_compile_order -fileset sim_1
launch_simulation -simset sim_1 -mode behavioral -scripts_only
puts "INFO: project ready: $proj_dir/e3c_sim.xpr"
