# create_soc_project.tcl - minimal Vivado 2026.1 project for the RV32I SoC
# (simulation and synthesis only: no board pins, no PS, no bitstream).
#
#   vivado -mode batch -source fpga/kv260/tcl/create_soc_project.tcl \
#          -tclargs <repo root> <project dir> [<INIT_FILE hex> | -] [<SmartConnect STRATEGY>] [<decode_probe 0|1>]
#
# Creates <project dir>/soc_sim.xpr with
#   - sources_1 : rtl/cpu/*.v, rtl/soc/*.v (include dir rtl/cpu), block design soc_bd
#                 (fpga/kv260/tcl/soc_bd.tcl) and its wrapper as top
#   - sim_1     : dv/soc/tb_soc.v + monitors, define SOC_BD, top tb_soc;
#                 xsim scripts exported with launch_simulation -scripts_only
# and writes <project dir>/reports/ (address map, BD validation, IP versions).
# Part: xck26-sfvc784-2LV-c (the part of the KV260 board files kv260_som:part0).

if {[llength $argv] < 2} {
    error "usage: -tclargs <repo root> <project dir> \[<INIT_FILE hex>\]"
}
set repo     [file normalize [lindex $argv 0]]
set proj_dir [file normalize [lindex $argv 1]]
set init     [expr {[llength $argv] > 2 && [lindex $argv 2] ne "-" ? [file normalize [lindex $argv 2]] : ""}]
set strategy [expr {[llength $argv] > 3 ? [lindex $argv 3] : "AUTOMATIC"}]
set probe    [expr {[llength $argv] > 4 ? [lindex $argv 4] : 0}]
set part     xck26-sfvc784-2LV-c

puts "INFO: Vivado [version -short], part $part"
create_project soc_sim $proj_dir -part $part -force
set_property target_language Verilog [current_project]
# simulator language stays at the default (Mixed): the SmartConnect simulation
# model is SystemVerilog and is left out of the xsim scripts with 'Verilog'

# ---- design sources (self-developed RTL) ----
set rtl [list]
foreach f {rv32i_regfile rv32i_alu rv32i_decoder rv32i_lsu rv32i_core} { lappend rtl $repo/rtl/cpu/$f.v }
foreach f {soc_ram_dp soc_dmem_router cmi2axil soc_sysctrl rv32_soc_top} { lappend rtl $repo/rtl/soc/$f.v }
add_files -norecurse -fileset sources_1 $rtl
add_files -norecurse -fileset sources_1 [list $repo/rtl/cpu/rv32i_defs.vh $repo/rtl/soc/soc_addr_map.vh]
set_property file_type {Verilog Header} [get_files {rv32i_defs.vh soc_addr_map.vh}]
set_property include_dirs [list $repo/rtl/cpu $repo/rtl/soc] [get_filesets sources_1]
update_compile_order -fileset sources_1

# ---- block design with the AMD AXI SmartConnect ----
source $repo/fpga/kv260/tcl/soc_bd.tcl
create_soc_bd $repo soc_bd $init $strategy $probe
puts "INFO: SmartConnect STRATEGY = $strategy"
set bd_file [get_files soc_bd.bd]
generate_target all $bd_file
set wrapper [make_wrapper -files $bd_file -top]
add_files -norecurse -fileset sources_1 $wrapper
set_property top soc_bd_wrapper [get_filesets sources_1]
update_compile_order -fileset sources_1

# ---- reports ----
file mkdir $proj_dir/reports
set fh [open $proj_dir/reports/bd_summary.txt w]
puts $fh "Vivado [version -short]  part $part"
foreach c [get_bd_cells] {
    puts $fh "cell [get_property NAME $c]  vlnv=[get_property VLNV $c]"
}
puts $fh "--- SmartConnect configuration"
foreach p [lsort [list_property [get_bd_cells axi_smc] CONFIG.*]] {
    puts $fh "$p = [get_property $p [get_bd_cells axi_smc]]"
}
puts $fh "--- cpu_subsys parameters"
foreach p {CONFIG.MEM_BYTES CONFIG.AXI_DECODE_TEST} {
    puts $fh "$p = [get_property $p [get_bd_cells cpu_subsys]]"
}
puts $fh "--- address map (cpu_subsys/m_axi)"
foreach s [get_bd_addr_segs -of_objects [get_bd_addr_spaces cpu_subsys/m_axi]] {
    puts $fh "[get_property NAME $s]  offset=[get_property OFFSET $s]  range=[get_property RANGE $s]"
}
puts $fh "--- interfaces inferred on the module references"
foreach i [get_bd_intf_pins -of_objects [get_bd_cells -filter {VLNV =~ *module_ref*}]] {
    puts $fh "[get_property PATH $i]  vlnv=[get_property VLNV $i]  mode=[get_property MODE $i]"
}
close $fh

# ---- simulation set: SoC test bench with the block design ----
set sim [list $repo/dv/soc/tb_soc.v $repo/dv/common/tb_cmi_monitor.v $repo/dv/common/tb_axil_monitor.v]
add_files -norecurse -fileset sim_1 $sim
add_files -norecurse -fileset sim_1 $repo/dv/common/tb_check.vh
set_property file_type {Verilog Header} [get_files tb_check.vh]
set_property include_dirs [list $repo/rtl/cpu $repo/rtl/soc $repo/dv/common] [get_filesets sim_1]
set_property verilog_define {SOC_BD=1} [get_filesets sim_1]
set_property top tb_soc [get_filesets sim_1]
set_property top_lib xil_defaultlib [get_filesets sim_1]
update_compile_order -fileset sim_1
launch_simulation -simset sim_1 -mode behavioral -scripts_only
puts "INFO: project ready: $proj_dir/soc_sim.xpr"
