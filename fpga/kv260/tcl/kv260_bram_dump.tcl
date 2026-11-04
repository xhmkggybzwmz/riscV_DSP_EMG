# kv260_bram_dump.tcl - dump the block RAMs of the CPU subsystem RAM from a
# routed checkpoint (Vivado 2026.1), for scripts/fpga/kv260_6a.py bramcheck.
#
#   vivado -mode batch -source fpga/kv260/tcl/kv260_bram_dump.tcl -tclargs <routed.dcp> <out.txt> [<name pattern>]
#
# For every RAMB* cell whose name matches the pattern (default *cpu_subsys*): one
# CELL line with the mapping properties (ram_addr_begin/end = word addresses,
# ram_slice_begin/end = data bits, READ_WIDTH_A, RTL_RAM_NAME) followed by its
# INIT_xx and INITP_xx strings.

set dcp [lindex $argv 0]
set out [lindex $argv 1]
set pat [expr {[llength $argv] > 2 ? [lindex $argv 2] : "*cpu_subsys*"}]
open_checkpoint $dcp
set fh [open $out w]
foreach c [lsort [get_cells -hier -filter "REF_NAME =~ RAMB* && NAME =~ $pat"]] {
    set props {}
    foreach p {REF_NAME READ_WIDTH_A WRITE_WIDTH_B ram_addr_begin ram_addr_end ram_slice_begin ram_slice_end RTL_RAM_NAME} {
        lappend props "$p=[get_property -quiet $p $c]"
    }
    puts $fh "CELL $c [join $props { }]"
    set n [expr {[string match RAMB36* [get_property REF_NAME $c]] ? 128 : 64}]
    for {set i 0} {$i < $n} {incr i} { puts $fh "INIT [format %02X $i] [get_property [format INIT_%02X $i] $c]" }
    for {set i 0} {$i < $n / 8} {incr i} { puts $fh "INITP [format %02X $i] [get_property [format INITP_%02X $i] $c]" }
}
close $fh
puts "INFO: BRAM dump written to $out"
