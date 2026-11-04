# create_e5_project.tcl - Vivado 2026.1 project for the E5a system simulation with the real AMD AXI
# DMA, AXI SmartConnects, Block Memory Generator and AXI BRAM Controller (no board pins, no PS, no
# bitstream; a test-bench memory stands in for the PS DDR).
#
#   vivado -mode batch -source fpga/kv260/tcl/create_e5_project.tcl -tclargs <repo root> <project dir>
#
# Creates <project dir>/e5_sim.xpr:
#   sources_1 : rtl/cpu, rtl/soc (rv32_soc_top E3C = 1, E3C_REPLAY = 0, MBOX = 1), rtl/dsp, rtl/infer,
#               rtl/e3c (emg_e5_subsys, esi_dma_src_axil), block design e5_bd (dma_bd.tcl) and its wrapper
#   sim_1     : dv/e5/tb_e5_soc.v (define E5_BD) + models; xsim scripts exported
# Needs build/e3c/e3c_paths.vh and the model files (scripts/sim/gen_e3c.py).
# Writes <project dir>/reports/bd_summary.txt.

if {[llength $argv] < 2} { error "usage: -tclargs <repo root> <project dir>" }
set repo     [file normalize [lindex $argv 0]]
set proj_dir [file normalize [lindex $argv 1]]
set part     xck26-sfvc784-2LV-c
set paths    $repo/build/e3c/e3c_paths.vh
if {![file isfile $paths]} { error "missing $paths: run scripts/sim/gen_e3c.py" }

puts "INFO: Vivado [version -short], part $part"
create_project e5_sim $proj_dir -part $part -force
set_property target_language Verilog [current_project]

set rtl [list]
foreach f {rv32i_regfile rv32i_alu rv32i_decoder rv32i_lsu rv32i_core} { lappend rtl $repo/rtl/cpu/$f.v }
foreach f {soc_ram_dp soc_dmem_router cmi2axil soc_io_split cmi_bram_port soc_sysctrl rv32_soc_top} {
    lappend rtl $repo/rtl/soc/$f.v
}
lappend rtl $repo/rtl/dsp/emg_dsp.v $repo/rtl/infer/emg_cnn.v
foreach f {e3c_axil_if emg_dsp_axil emg_cnn_axil esi_dma_src_axil emg_e5_subsys} { lappend rtl $repo/rtl/e3c/$f.v }
add_files -norecurse -fileset sources_1 $rtl
add_files -norecurse -fileset sources_1 [list $repo/rtl/cpu/rv32i_defs.vh $repo/rtl/soc/soc_addr_map.vh \
    $repo/rtl/esi/esi_defs.vh]
set_property file_type {Verilog Header} [get_files {rv32i_defs.vh soc_addr_map.vh esi_defs.vh}]
set_property include_dirs [list $repo/rtl/cpu $repo/rtl/soc $repo/rtl/esi] [get_filesets sources_1]
update_compile_order -fileset sources_1

source $repo/fpga/kv260/tcl/soc_bd.tcl
source $repo/fpga/kv260/tcl/e3c_bd.tcl
source $repo/fpga/kv260/tcl/mbx_bd.tcl
source $repo/fpga/kv260/tcl/dma_bd.tcl
create_e5_sim_bd $repo e5_bd $paths
set bd_file [get_files e5_bd.bd]
generate_target all $bd_file
set wrapper [make_wrapper -files $bd_file -top]
add_files -norecurse -fileset sources_1 $wrapper
set_property top e5_bd_wrapper [get_filesets sources_1]
update_compile_order -fileset sources_1

file mkdir $proj_dir/reports
set fh [open $proj_dir/reports/bd_summary.txt w]
puts $fh "Vivado [version -short]  part $part"
foreach c [lsort [get_bd_cells]] { puts $fh "cell [get_property NAME $c]  vlnv=[get_property VLNV $c]" }
foreach p {CONFIG.c_include_sg CONFIG.c_include_mm2s CONFIG.c_include_s2mm CONFIG.c_addr_width CONFIG.c_sg_length_width \
           CONFIG.c_m_axi_mm2s_data_width CONFIG.c_m_axis_mm2s_tdata_width CONFIG.c_mm2s_burst_size CONFIG.c_include_mm2s_dre} {
    puts $fh "axi_dma $p = [get_property -quiet $p [get_bd_cells axi_dma]]"
}
foreach sp {cpu_subsys/m_axi S_AXI_PS axi_dma/Data_MM2S} {
    foreach s [get_bd_addr_segs -of_objects [get_bd_addr_spaces $sp]] {
        puts $fh "addr $sp [get_property NAME $s] offset=[get_property OFFSET $s] range=[get_property RANGE $s]"
    }
}
close $fh

set sim [list $repo/dv/e5/tb_e5_soc.v $repo/dv/e5/tb_axi_ddr_model.v $repo/dv/common/tb_axil_monitor.v \
    $repo/dv/common/tb_axil_master.v]
add_files -norecurse -fileset sim_1 $sim
add_files -norecurse -fileset sim_1 [list $repo/dv/common/tb_check.vh $repo/dv/e3c/e3c_golden.vh \
    $repo/dv/e3c/e3c_timing.vh $repo/dv/mbx/mbx_layout.vh $repo/dv/mbx/mbx_monitor.vh \
    $repo/dv/mbx/mbx_ps_reader.vh $paths]
set_property file_type {Verilog Header} [get_files {tb_check.vh e3c_golden.vh e3c_timing.vh e3c_paths.vh \
    mbx_layout.vh mbx_monitor.vh mbx_ps_reader.vh}]
set_property include_dirs [list $repo/rtl/cpu $repo/rtl/soc $repo/rtl/esi $repo/dv/common $repo/dv/e3c \
    $repo/dv/mbx $repo/build/e3c] [get_filesets sim_1]
set_property verilog_define {E5_BD=1} [get_filesets sim_1]
set_property top tb_e5_soc [get_filesets sim_1]
set_property top_lib xil_defaultlib [get_filesets sim_1]
update_compile_order -fileset sim_1
launch_simulation -simset sim_1 -mode behavioral -scripts_only
puts "INFO: project ready: $proj_dir/e5_sim.xpr"
