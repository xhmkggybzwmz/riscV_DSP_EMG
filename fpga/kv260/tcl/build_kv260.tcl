# build_kv260.tcl - stage 6A: KV260 hardware project of the RV32I SoC (Vivado 2026.1).
#
#   vivado -mode batch -source fpga/kv260/tcl/build_kv260.tcl \
#          -tclargs <repo root> <project dir> <INIT_FILE hex> [bd|bit] [<jobs>] [jtag|ps|ps_spi]
#
#   bd  : project + block design + validation + wrapper (no synthesis)
#   bit : additionally synthesis, opt/place/route, timing, bitstream (default)
#   jtag: VIO/ILA debug variant (default); ps: PS debug path (kv260_dbg_axil), no JTAG cores;
#   ps_spi: ps + STM32 EMG1 SPI monitor at PS 0xA013_0000 and constrained J2 Pmod I/O;
#   ps_e3c: ps + the E3c DSP/CNN pipeline (needs build/e3c/e3c_paths.vh, scripts/sim/gen_e3c.py)
#   ps_e4a: ps_e3c + the E4a PS mailbox (shared BRAM, fpga/kv260/tcl/mbx_bd.tcl); additionally
#           writes out/kv260_soc.hwh (block design hardware handoff) and reports/cdc.rpt
#
# Board part xilinx.com:kv260_som:part0:1.4 (part xck26-sfvc784-2LV-c). Block
# design: fpga/kv260/tcl/kv260_bd.tcl. The only clock is PS PL_CLK0; its
# create_clock comes from the PS IP constraints (period from the PS clock
# configuration) and is checked below. Only ps_spi uses board I/O.
#
# Outputs in <project dir>/out/: kv260_soc.bit, kv260_soc.bin, kv260_soc.ltx,
# summary.txt (key = value, parsed by scripts/fpga/kv260_6a.py) and reports/*.rpt.

if {[llength $argv] < 3} {
    error "usage: -tclargs <repo root> <project dir> <INIT_FILE hex> \[bd|bit\] \[<jobs>\]"
}
set repo     [file normalize [lindex $argv 0]]
set proj_dir [file normalize [lindex $argv 1]]
set init     [file normalize [lindex $argv 2]]
set mode     [expr {[llength $argv] > 3 ? [lindex $argv 3] : "bit"}]
set jobs     [expr {[llength $argv] > 4 ? [lindex $argv 4] : 8}]
set dbg_mode [expr {[llength $argv] > 5 ? [lindex $argv 5] : "jtag"}]
set e3c_paths ""
set mbx 0
set spi 0
if {$dbg_mode eq "ps_spi"} {
    set dbg_mode ps
    set spi 1
}
set spilive 0
if {$dbg_mode eq "ps_e6a"} {
    set dbg_mode ps_e4a
    set spilive 1
}
set dma 0
if {$dbg_mode eq "ps_e5a"} {
    set dbg_mode ps_e4a
    set dma 1
}
if {$dbg_mode eq "ps_e4a"} {
    set dbg_mode ps_e3c
    set mbx 1
}
if {$dbg_mode eq "ps_e3c"} {
    set dbg_mode ps
    set e3c_paths $repo/build/e3c/e3c_paths.vh
    if {![file isfile $e3c_paths]} { error "missing $e3c_paths: run scripts/sim/gen_e3c.py" }
}
set part     xck26-sfvc784-2LV-c
set board    xilinx.com:kv260_som:part0:1.4

if {![string match 2026.1* [version -short]]} { error "Vivado 2026.1 required, got [version -short]" }
if {![file isfile $init]} { error "INIT_FILE not found: $init" }

create_project kv260_soc $proj_dir -part $part -force
set_property board_part $board [current_project]
set_property target_language Verilog [current_project]

# ---- design sources ----
set rtl [list]
foreach f {rv32i_regfile rv32i_alu rv32i_decoder rv32i_lsu rv32i_core} { lappend rtl $repo/rtl/cpu/$f.v }
foreach f {soc_ram_dp soc_dmem_router cmi2axil soc_sysctrl rv32_soc_top} { lappend rtl $repo/rtl/soc/$f.v }
if {$mbx} { lappend rtl $repo/rtl/soc/soc_io_split.v $repo/rtl/soc/cmi_bram_port.v }
lappend rtl $repo/fpga/kv260/rtl/kv260_dbg_obs.v $repo/fpga/kv260/rtl/kv260_cpu_dbg.v
if {$dbg_mode eq "ps"} { lappend rtl $repo/fpga/kv260/rtl/kv260_dbg_axil.v }
if {$spi} { lappend rtl $repo/rtl/spi/emg_spi_monitor_axil.v }
if {$e3c_paths ne ""} {
    lappend rtl $repo/rtl/dsp/emg_dsp.v $repo/rtl/infer/emg_cnn.v
    set e3c_src [expr {$spilive ? {} : $dma ? {esi_dma_src_axil emg_e5_subsys} : {esi_replay_axil emg_e3c_subsys}}]
    if {$spilive} { lappend rtl $repo/rtl/spi/emg1_spi_rx.v $repo/rtl/spi/esi_spi_src_axil.v $repo/rtl/spi/emg_spi_subsys.v }
    foreach f [list e3c_axil_if emg_dsp_axil emg_cnn_axil {*}$e3c_src] { lappend rtl $repo/rtl/e3c/$f.v }
}
add_files -norecurse -fileset sources_1 $rtl
add_files -norecurse -fileset sources_1 [list $repo/rtl/cpu/rv32i_defs.vh $repo/rtl/soc/soc_addr_map.vh]
set_property file_type {Verilog Header} [get_files {rv32i_defs.vh soc_addr_map.vh}]
set incs [list $repo/rtl/cpu $repo/rtl/soc]
if {$e3c_paths ne ""} {
    add_files -norecurse -fileset sources_1 $repo/rtl/esi/esi_defs.vh
    set_property file_type {Verilog Header} [get_files esi_defs.vh]
    lappend incs $repo/rtl/esi
}
set_property include_dirs $incs [get_filesets sources_1]
if {$spi || $spilive} { add_files -norecurse -fileset constrs_1 $repo/fpga/kv260/xdc/spi_pmod.xdc }
update_compile_order -fileset sources_1

# ---- block design ----
source $repo/fpga/kv260/tcl/soc_bd.tcl
source $repo/fpga/kv260/tcl/kv260_bd.tcl
source $repo/fpga/kv260/tcl/e3c_bd.tcl
source $repo/fpga/kv260/tcl/mbx_bd.tcl
source $repo/fpga/kv260/tcl/dma_bd.tcl
set probes [create_kv260_bd $repo kv260_bd $init 4096 $dbg_mode $e3c_paths $mbx $dma $spi $spilive]
set bd_file [get_files kv260_bd.bd]
generate_target all $bd_file
set wrapper [make_wrapper -files $bd_file -top]
add_files -norecurse -fileset sources_1 $wrapper
set_property top kv260_bd_wrapper [get_filesets sources_1]
update_compile_order -fileset sources_1

file mkdir $proj_dir/out/reports
set out $proj_dir/out
set fh [open $out/bd_summary.txt w]
puts $fh "Vivado [version -short]  part $part  board [get_property board_part [current_project]]"
puts $fh "INIT_FILE $init"
puts $fh "dbg_mode $dbg_mode e3c=[expr {$e3c_paths ne {}}]"
puts $fh "spi=$spi"
if {$mbx} {
    puts $fh "mbx=1 cpu_subsys MBOX = [get_property CONFIG.MBOX [get_bd_cells cpu_subsys]]"
    foreach p {CONFIG.Memory_Type CONFIG.use_bram_block CONFIG.Operating_Mode_A CONFIG.Operating_Mode_B
               CONFIG.Write_Width_A CONFIG.Write_Depth_A CONFIG.Use_Byte_Write_Enable} {
        puts $fh "mbx_mem $p = [get_property -quiet $p [get_bd_cells mbx_mem]]"
    }
    foreach p {CONFIG.PROTOCOL CONFIG.SINGLE_PORT_BRAM CONFIG.DATA_WIDTH CONFIG.MEM_DEPTH} {
        puts $fh "mbx_ctrl $p = [get_property -quiet $p [get_bd_cells mbx_ctrl]]"
    }
}
if {$mbx || $spi} {
    set hwh [glob -nocomplain $proj_dir/kv260_soc.gen/sources_1/bd/kv260_bd/hw_handoff/kv260_bd.hwh]
    if {[llength $hwh]} { file copy -force [lindex $hwh 0] $out/kv260_soc.hwh }
}
foreach c [lsort [get_bd_cells]] { puts $fh "cell [get_property NAME $c]  vlnv=[get_property VLNV $c]" }
set ps [get_bd_cells zynq_ultra_ps_e_0]
foreach p {PSU__FPGA_PL0_ENABLE PSU__CRL_APB__PL0_REF_CTRL__SRCSEL PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ
           PSU__CRL_APB__PL0_REF_CTRL__ACT_FREQMHZ PSU__CRL_APB__PL0_REF_CTRL__DIVISOR0
           PSU__CRL_APB__PL0_REF_CTRL__DIVISOR1 PSU__CRL_APB__IOPLL_CTRL__FBDIV PSU__CRL_APB__IOPLL_CTRL__DIV2
           PSU__PSS_REF_CLK__FREQMHZ PSU__NUM_FABRIC_RESETS PSU__USE__M_AXI_GP0 PSU__USE__M_AXI_GP1
           PSU__USE__M_AXI_GP2 PSU__MAXIGP0__DATA_WIDTH PSU__USE__S_AXI_GP2 PSU__SAXIGP2__DATA_WIDTH} {
    puts $fh "ps $p = [get_property -quiet CONFIG.$p $ps]"
}
puts $fh "pl_clk0 FREQ_HZ = [get_property CONFIG.FREQ_HZ [get_bd_pins zynq_ultra_ps_e_0/pl_clk0]]"
if {$dma} {
    foreach p {c_include_sg c_include_mm2s c_include_s2mm c_addr_width c_sg_length_width c_m_axi_mm2s_data_width
               c_m_axis_mm2s_tdata_width c_mm2s_burst_size c_include_mm2s_dre} {
        puts $fh "axi_dma CONFIG.$p = [get_property -quiet CONFIG.$p [get_bd_cells axi_dma]]"
    }
    foreach s [get_bd_addr_segs -of_objects [get_bd_addr_spaces axi_dma/Data_MM2S]] {
        puts $fh "dma_addr [get_property NAME $s] offset=[get_property OFFSET $s] range=[get_property RANGE $s]"
    }
}
foreach p {cpu_subsys/aclk sysctrl/aclk obs/aclk axi_smc/aclk ila/clk vio/clk} {
    if {[llength [get_bd_pins -quiet $p]] == 0} continue
    puts $fh "$p FREQ_HZ = [get_property -quiet CONFIG.FREQ_HZ [get_bd_pins $p]]"
}
foreach c [get_bd_cells -quiet {rst_pl0 rst_ps}] {
    foreach p {C_EXT_RESET_HIGH C_AUX_RESET_HIGH} { puts $fh "[get_property NAME $c] $p = [get_property CONFIG.$p $c]" }
}
if {$dbg_mode eq "jtag"} {
    foreach p {C_MONITOR_TYPE C_DATA_DEPTH C_NUM_OF_PROBES C_EN_STRG_QUAL C_PROBE0_WIDTH C_PROBE1_WIDTH
               C_PROBE2_WIDTH C_PROBE3_WIDTH C_PROBE4_WIDTH} {
        puts $fh "ila $p = [get_property -quiet CONFIG.$p [get_bd_cells ila]]"
    }
} else {
    foreach p {zynq_ultra_ps_e_0/maxihpm0_fpd_aclk dbg/aclk ps_smc/aclk} {
        puts $fh "$p FREQ_HZ = [get_property -quiet CONFIG.FREQ_HZ [get_bd_pins $p]]"
    }
    foreach s [get_bd_addr_segs -of_objects [get_bd_addr_spaces zynq_ultra_ps_e_0/Data]] {
        puts $fh "ps_addr [get_property NAME $s] offset=[get_property OFFSET $s] range=[get_property RANGE $s]"
    }
}
puts $fh "vio_in [lindex $probes 0]"
puts $fh "ila_in [lindex $probes 1]"
foreach s [get_bd_addr_segs -of_objects [get_bd_addr_spaces cpu_subsys/m_axi]] {
    puts $fh "addr [get_property NAME $s] offset=[get_property OFFSET $s] range=[get_property RANGE $s]"
}
close $fh
if {$mode eq "bd"} { puts "INFO: block design done: $out/bd_summary.txt"; return }

# ---- synthesis and implementation ----
set_property STEPS.WRITE_BITSTREAM.ARGS.BIN_FILE true [get_runs impl_1]
launch_runs synth_1 -jobs $jobs
wait_on_run synth_1
if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} { error "synth_1 failed" }
launch_runs impl_1 -to_step write_bitstream -jobs $jobs
wait_on_run impl_1
if {[get_property PROGRESS [get_runs impl_1]] ne "100%"} { error "impl_1 failed: [get_property STATUS [get_runs impl_1]]" }

open_run impl_1
set r $out/reports
report_utilization -file $r/utilization.rpt
report_utilization -hierarchical -hierarchical_depth 3 -file $r/utilization_hier.rpt
report_ram_utilization -file $r/ram_utilization.rpt
report_clocks -file $r/clocks.rpt
report_timing_summary -max_paths 10 -report_unconstrained -file $r/timing_summary.rpt
report_timing -max_paths 5 -nworst 1 -file $r/critical_paths.rpt
report_clock_interaction -file $r/clock_interaction.rpt
check_timing -verbose -file $r/check_timing.rpt
report_drc -file $r/drc.rpt
report_methodology -file $r/methodology.rpt
report_debug_core -file $r/debug_core.rpt
report_power -file $r/power.rpt

# ---- summary ----
set fh [open $out/summary.txt w]
puts $fh "vivado = [version -short]"
puts $fh "init_file = $init"
foreach c [get_clocks] {
    puts $fh "clock = [get_property NAME $c] period=[get_property PERIOD $c] source=[get_property SOURCE_PINS $c]"
}
set wns [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -setup]]
set whs [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -hold]]
puts $fh "wns = $wns"
puts $fh "whs = $whs"
set tns 0.0
foreach p [get_timing_paths -max_paths 1000 -nworst 1 -setup -slack_lesser_than 0] {
    set tns [expr {$tns + [get_property SLACK $p]}]
}
puts $fh "tns = $tns"
set ths 0.0
foreach p [get_timing_paths -max_paths 1000 -nworst 1 -hold -slack_lesser_than 0] {
    set ths [expr {$ths + [get_property SLACK $p]}]
}
puts $fh "ths = $ths"
set drc_err 0; set drc_warn 0
foreach v [get_drc_violations -quiet] {
    if {[get_property SEVERITY $v] in {Error {Critical Warning}}} { incr drc_err } else { incr drc_warn }
}
puts $fh "drc_errors = $drc_err"
puts $fh "drc_warnings = $drc_warn"
puts $fh "ramb36 = [llength [get_cells -hier -quiet -filter {REF_NAME =~ RAMB36*}]]"
puts $fh "ramb18 = [llength [get_cells -hier -quiet -filter {REF_NAME =~ RAMB18*}]]"
puts $fh "dsp = [llength [get_cells -hier -quiet -filter {REF_NAME =~ DSP48E2*}]]"
puts $fh "bram_cpu_ram = [llength [get_cells -hier -quiet -filter {REF_NAME =~ RAMB* && NAME =~ *cpu_subsys*}]]"
puts $fh "bram_ila = [llength [get_cells -hier -quiet -filter {REF_NAME =~ RAMB* && NAME =~ *ila*}]]"
puts $fh "bram_dbg = [llength [get_cells -hier -quiet -filter {REF_NAME =~ RAMB* && NAME =~ */dbg/*}]]"
puts $fh "bram_e3c = [llength [get_cells -hier -quiet -filter {REF_NAME =~ RAMB* && NAME =~ */e3c/*}]]"
if {$mbx} {
    puts $fh "bram_mbx = [llength [get_cells -hier -quiet -filter {REF_NAME =~ RAMB* && NAME =~ *mbx_mem*}]]"
    report_cdc -details -file $r/cdc.rpt
    puts $fh "cdc_report = reports/cdc.rpt"
}
puts $fh "uram = [llength [get_cells -hier -quiet -filter {REF_NAME =~ URAM*}]]"
puts $fh "dsp_e3c = [llength [get_cells -hier -quiet -filter {REF_NAME =~ DSP48E2* && NAME =~ */e3c/*}]]"
close $fh

file copy -force [glob $proj_dir/kv260_soc.runs/impl_1/kv260_bd_wrapper.bit] $out/kv260_soc.bit
file copy -force [glob $proj_dir/kv260_soc.runs/impl_1/kv260_bd_wrapper.bin] $out/kv260_soc.bin
if {$dbg_mode eq "jtag"} { write_debug_probes -force $out/kv260_soc.ltx }
puts "INFO: bitstream done: $out"
