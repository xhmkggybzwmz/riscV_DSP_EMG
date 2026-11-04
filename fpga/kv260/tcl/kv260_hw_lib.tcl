# kv260_hw_lib.tcl - Vivado 2026.1 Hardware Manager helpers for the stage 6A
# board test (used by the Tcl that scripts/fpga/kv260_6a.py generates).
#
# Only volatile JTAG operations: PL configuration (program_hw_devices), VIO
# reads/writes and ILA captures. Nothing is written to QSPI/eMMC/SD, and the
# PS is not touched.
#
# Output lines for the Python side start with "KV:" (key value ...).

proc kv {args} { puts "KV: [join $args { }]" }

proc hw_open {} {
    open_hw_manager
    connect_hw_server -url localhost:3121 -allow_non_jtag
    set t [lindex [get_hw_targets] 0]
    open_hw_target $t
    set d [lindex [get_hw_devices xck26_0] 0]
    current_hw_device $d
    kv target $t
    kv device $d [get_property PART $d] [get_property IDCODE_HEX $d]
    return $d
}

proc hw_close {} {
    catch {close_hw_target}
    catch {disconnect_hw_server}
}

proc hw_program {bit ltx} {
    set d [current_hw_device]
    set_property PROGRAM.FILE $bit $d
    set_property PROBES.FILE $ltx $d
    set_property FULL_PROBES.FILE $ltx $d
    program_hw_devices $d
    refresh_hw_device $d
    kv programmed [file normalize $bit] done=[get_property REGISTER.CONFIG_STATUS.BIT\[14\]_DONE_PIN $d]
    kv ilas [llength [get_hw_ilas -quiet]] vios [llength [get_hw_vios -quiet]]
}

# probe of a debug core whose name ends with <suffix>, e.g. obs_retired
proc hw_probe {core suffix} {
    set m {}
    foreach p [get_hw_probes -quiet -of_objects $core] {
        if {[string match *$suffix [get_property NAME $p]]} { lappend m $p }
    }
    if {[llength $m] != 1} { error "probe *$suffix: [llength $m] matches on $core" }
    return [lindex $m 0]
}

proc vio_dump {tag} {
    set v [lindex [get_hw_vios] 0]
    refresh_hw_vio $v
    foreach p [lsort [get_hw_probes -of_objects $v]] {
        if {[get_property TYPE $p] eq "vio_input"} {
            set_property INPUT_VALUE_RADIX HEX $p
            kv vio $tag [get_property NAME $p] [get_property INPUT_VALUE $p]
        } else {
            kv vio $tag [get_property NAME $p] out=[get_property OUTPUT_VALUE $p]
        }
    }
}

proc vio_out {suffix value} {
    set v [lindex [get_hw_vios] 0]
    set p [hw_probe $v $suffix]
    set_property OUTPUT_VALUE $value $p
    commit_hw_vio $p
}

# assert the CPU reset through proc_sys_reset aux_reset_in for a moment
proc cpu_reset_pulse {} {
    vio_out probe_out0 0
    after 20
    vio_out probe_out0 1
    after 50
}

proc ila_list {} {
    set ila [lindex [get_hw_ilas] 0]
    foreach p [lsort [get_hw_probes -of_objects $ila]] { kv ila_probe [get_property NAME $p] [get_property WIDTH $p] }
}

# compare value "eq<width>'b..." with one bit = 1 and all other bits don't care
proc bit_is_1 {width bit} {
    set s [string repeat X $width]
    set i [expr {$width - 1 - $bit}]
    return "eq${width}'b[string replace $s $i $i 1]"
}

# ILA probes (fpga/kv260/rtl/kv260_cpu_dbg.v): ila_ctrl bit 0 retire_valid,
# bit 9 tohost_wr, bit 12 trace_event; obs_retired = retired-instruction counter
# (the ILA probe is named obs_retired_1: the net also feeds the VIO).
#
# Trace capture: trigger on the retire of instruction number <start>
# (retire_valid && obs_retired == start), storage qualification
# trace_event = retire | data request handshake | data response | trap |
# TOHOST write | AXI handshake. Trigger and capture conditions are AND over
# the probes (unused probes compare as don't care). The CPU is reset after
# arming; returns when the ILA is full or after the timeout.
proc ila_trace {csv start} {
    set ila [lindex [get_hw_ilas] 0]
    reset_hw_ila $ila
    set ctrl [hw_probe $ila ila_ctrl]
    set w [get_property WIDTH $ctrl]
    set_property CONTROL.TRIGGER_POSITION 0 $ila
    set_property CONTROL.TRIGGER_CONDITION AND $ila
    set_property CONTROL.CAPTURE_MODE BASIC $ila
    set_property CONTROL.CAPTURE_CONDITION AND $ila
    set_property TRIGGER_COMPARE_VALUE [bit_is_1 $w 0] $ctrl
    set_property TRIGGER_COMPARE_VALUE [format "eq32'h%08X" $start] [hw_probe $ila obs_retired*]
    set_property CAPTURE_COMPARE_VALUE [bit_is_1 $w 12] $ctrl
    run_hw_ila $ila
    after 200
    cpu_reset_pulse
    if {[catch {wait_on_hw_ila -timeout 1 $ila} e]} { kv ila_wait $e }
    set data [upload_hw_ila_data $ila]
    write_hw_ila_data -force -csv_file $csv $data
    kv ila_csv $csv start=$start
}

# Cycle-accurate window around the first TOHOST write, no qualification:
# shows the AXI4-Lite AW/W/B (and AR/R) handshakes through the SmartConnect.
proc ila_tohost {csv {position 2048}} {
    set ila [lindex [get_hw_ilas] 0]
    reset_hw_ila $ila
    set ctrl [hw_probe $ila ila_ctrl]
    set_property CONTROL.TRIGGER_POSITION $position $ila
    set_property CONTROL.TRIGGER_CONDITION AND $ila
    set_property CONTROL.CAPTURE_MODE ALWAYS $ila
    set_property TRIGGER_COMPARE_VALUE [bit_is_1 [get_property WIDTH $ctrl] 9] $ctrl
    run_hw_ila $ila
    after 200
    cpu_reset_pulse
    if {[catch {wait_on_hw_ila -timeout 1 $ila} e]} { kv ila_wait $e }
    set data [upload_hw_ila_data $ila]
    write_hw_ila_data -force -csv_file $csv $data
    kv ila_csv $csv tohost
}
