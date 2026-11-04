# run_soc_synth.tcl - synthesis of the RV32I SoC block design (Vivado 2026.1).
#
#   vivado -mode batch -source fpga/kv260/tcl/run_soc_synth.tcl \
#          -tclargs <repo root> <project dir> <INIT_FILE hex>
#
# Builds a fresh project (create_soc_project.tcl, production topology:
# one SmartConnect SI/MI, SYSCTRL only) with the BRAM initialised from
# <INIT_FILE>, then runs out-of-context synthesis of soc_bd_wrapper with a
# single 100 MHz clock constraint on aclk. No pin constraints, no
# implementation, no bitstream: the timing numbers are post-synthesis
# estimates only.
# Reports in <project dir>/reports/: utilization (flat and hierarchical),
# RAM utilization, timing summary, critical path, DRC/methodology.

if {[llength $argv] < 3} { error "usage: -tclargs <repo root> <project dir> <INIT_FILE hex>" }
set repo     [file normalize [lindex $argv 0]]
set proj_dir [file normalize [lindex $argv 1]]
set init     [file normalize [lindex $argv 2]]

set argv [list $repo $proj_dir $init AUTOMATIC 0]
source $repo/fpga/kv260/tcl/create_soc_project.tcl

# 100 MHz on aclk (initial target, docs/soc/soc_bus_architecture.md section 7)
set xdc $proj_dir/soc_timing.xdc
set fh [open $xdc w]
puts $fh "create_clock -name aclk -period 10.000 \[get_ports aclk\]"
close $fh
add_files -fileset constrs_1 -norecurse $xdc

set_property -name {STEPS.SYNTH_DESIGN.ARGS.MORE OPTIONS} -value {-mode out_of_context} -objects [get_runs synth_1]
launch_runs synth_1 -jobs 4
wait_on_run synth_1
if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} { error "synth_1 did not complete" }
open_run synth_1

set r $proj_dir/reports
report_utilization -file $r/utilization.rpt
report_utilization -hierarchical -hierarchical_depth 4 -file $r/utilization_hier.rpt
report_ram_utilization -file $r/ram_utilization.rpt
report_timing_summary -max_paths 10 -file $r/timing_summary.rpt
report_timing -max_paths 3 -nworst 1 -file $r/critical_paths.rpt
report_methodology -file $r/methodology.rpt
report_drc -file $r/drc.rpt
puts "INFO: synthesis done, reports in $r"
