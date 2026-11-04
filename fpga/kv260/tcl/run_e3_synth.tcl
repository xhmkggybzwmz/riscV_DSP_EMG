# run_e3_synth.tcl - out-of-context synthesis of the E3a DSP, E3b CNN and DSP+CNN (Vivado 2026.1).
#
#   vivado -mode batch -source fpga/kv260/tcl/run_e3_synth.tcl -tclargs <repo root> <output dir> <top>
#   <top> = emg_dsp_top | emg_cnn_top | emg_e3_top
#
# Non-project flow, part xck26-sfvc784-2LV-c, one 10 ns clock on clk, no I/O, no implementation:
# results are post-synthesis estimates only. model_paths.vh must exist in <repo>/build/e3 (run_e3.py).

if {[llength $argv] < 3} { error "usage: -tclargs <repo root> <output dir> <top>" }
set repo [file normalize [lindex $argv 0]]
set out  [file normalize [lindex $argv 1]]
set top  [lindex $argv 2]
file mkdir $out

read_verilog [list $repo/rtl/dsp/emg_dsp.v $repo/rtl/infer/emg_cnn.v $repo/fpga/kv260/e3/emg_e3_tops.v]
set xdc $out/clk.xdc
set fh [open $xdc w]
puts $fh "create_clock -name clk -period 10.000 \[get_ports clk\]"
close $fh
read_xdc -mode out_of_context $xdc
synth_design -top $top -part xck26-sfvc784-2LV-c -mode out_of_context \
    -include_dirs [list $repo/rtl/esi $repo/build/e3]

report_utilization -file $out/utilization.rpt
report_utilization -hierarchical -file $out/utilization_hier.rpt
report_ram_utilization -file $out/ram_utilization.rpt
report_timing_summary -max_paths 10 -file $out/timing_summary.rpt
report_timing -max_paths 5 -nworst 1 -file $out/critical_paths.rpt
report_methodology -file $out/methodology.rpt
report_drc -file $out/drc.rpt

# BRAM contents: first INIT word of every block RAM (weights must come from the reference model files)
set fh [open $out/bram_init.txt w]
foreach c [lsort [get_cells -hier -filter {REF_NAME =~ RAMB*}]] {
    puts $fh "$c [get_property REF_NAME $c] INIT_00=[get_property INIT_00 $c]"
}
close $fh
set fh [open $out/dsp48.txt w]
foreach c [lsort [get_cells -hier -filter {REF_NAME =~ DSP48*}]] { puts $fh "$c [get_property REF_NAME $c]" }
close $fh
puts "INFO: E3 synthesis of $top done, reports in $out"
