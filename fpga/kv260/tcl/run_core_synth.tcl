# run_core_synth.tcl - out-of-context synthesis of the rv32i_core alone (Vivado 2026.1).
#
#   vivado -mode batch -source fpga/kv260/tcl/run_core_synth.tcl \
#          -tclargs <repo root> <output dir>
#
# Non-project flow, part xck26-sfvc784-2LV-c, 100 MHz on clk, no I/O buffers
# (out_of_context). Gives the resource cost of the core itself and a
# post-synthesis timing estimate; no implementation, no pins.

if {[llength $argv] < 2} { error "usage: -tclargs <repo root> <output dir>" }
set repo [file normalize [lindex $argv 0]]
set out  [file normalize [lindex $argv 1]]
file mkdir $out

foreach f {rv32i_regfile rv32i_alu rv32i_decoder rv32i_lsu rv32i_core} {
    read_verilog $repo/rtl/cpu/$f.v
}
set xdc $out/core_timing.xdc
set fh [open $xdc w]
puts $fh "create_clock -name clk -period 10.000 \[get_ports clk\]"
close $fh
read_xdc -mode out_of_context $xdc
synth_design -top rv32i_core -part xck26-sfvc784-2LV-c -mode out_of_context \
    -include_dirs [list $repo/rtl/cpu]

report_utilization -file $out/core_utilization.rpt
report_utilization -hierarchical -file $out/core_utilization_hier.rpt
report_timing_summary -max_paths 10 -file $out/core_timing_summary.rpt
report_timing -max_paths 3 -nworst 1 -file $out/core_critical_paths.rpt
puts "INFO: core synthesis done, reports in $out"
