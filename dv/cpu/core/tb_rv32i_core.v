`timescale 1ns / 1ps
`default_nettype none

// tb_rv32i_core - program-level test bench for rtl/cpu/rv32i_core.v
//
// Runs one RV32I program (built by scripts/sw/build_asm_tests.py) on the core
// connected to tb_mem_model through both CMI ports, with a CMI protocol
// monitor on each port. The program reports PASS/FAIL through TOHOST
// (0x1000_0008); trap tests instead expect a halt with a given cause/pc/tval.
//
// Plusargs
//   +hex=<file>                       program image ($readmemh, words from address 0)
//   +ready_pct=N +lat_min=N +lat_max=N  memory timing (default 100 / 1 / 1 = zero wait)
//   +seed=N                           random seed (memory timing)
//   +max_cycles=N                     timeout (default 200000)
//   +reset_test=1 [+reset_after=N] [+reset_mode=M]
//                                     assert rst at a chosen bus condition, then run the
//                                     program again from the reset vector. M:
//                                       dmem          data access accepted, response pending (default)
//                                       dmem_wait     data request waiting for ready
//                                       dmem_rsp      in the cycle the data response is presented
//                                       imem_wait     fetch request waiting for ready
//                                       imem_pending  fetch accepted, response pending
//                                       imem_rsp      in the cycle the fetch response is presented
//   +trace_file=<file>                write the architectural event trace (see below)
//   +mem_dump=<file>                  write the final RAM image (one word per line)
//   tb_* symbols from the program (hex values), see tests/cpu/common/test_macros.h
//
// Checks on every cycle / event (in addition to the monitors):
//   - pc (= imem_req_addr) changes only together with a retire event
//   - every retired pc is the address of the most recent instruction fetch
//   - no retire / trap after halt; at most one trap event
//   - when an instruction retires, no CMI transaction is outstanding
//   - an error response is followed by a trap and never by a retire
//   - retire never reports a write to x0
//   - committed data-port writes == retired stores (+1 if the trap is a store
//     access fault: that store was accepted but did not retire)
// Trace file (compared with reference/rv32i_iss by scripts/sim/trace_compare.py):
//   M <addr> <we> <wstrb> <wdata> <rdata> <err>  data-port transaction (request fields
//                                                 at the handshake, response at completion)
//   R <pc> <insn> <rd_we> <rd> <wdata>           retire event
//   T <cause> <pc> <tval>                        trap event
//   X                                            rst asserted (earlier events are void)
//   E TOHOST <value> | E HALT | E TIMEOUT        end of the run
// Output lines parsed by scripts/sim/run_sim.py:
//   PERF: cycles=.. retired=.. cpi=.. loads=.. stores=.. branches=.. taken=..
//   TRACE: retired=.. hash=..        (independent of memory timing)
//   MEMSTAT: stall and response-latency histograms of both ports

module tb_rv32i_core;

    localparam TB_TIMEOUT_NS = 100000000;
    `include "tb_check.vh"

    reg clk = 1'b0;
    reg rst = 1'b1;
    always #5 clk = ~clk;

`ifdef DUMP_WAVES
    initial begin $dumpfile("wave.vcd"); $dumpvars(0, tb_rv32i_core); end
`endif

    // ------------------------------------------------------------------
    // DUT, memory, monitors
    // ------------------------------------------------------------------
    wire        i_req_valid, i_req_ready, i_rsp_valid, i_rsp_err;
    wire [31:0] i_req_addr, i_rsp_rdata;
    wire        d_req_valid, d_req_ready, d_req_we, d_rsp_valid, d_rsp_err;
    wire [31:0] d_req_addr, d_req_wdata, d_rsp_rdata;
    wire [3:0]  d_req_wstrb;
    wire        halted, trap_valid;
    wire [3:0]  trap_cause;
    wire [31:0] trap_pc, trap_tval;
    wire        rt_valid, rt_rd_we;
    wire [31:0] rt_pc, rt_insn, rt_wdata;
    wire [4:0]  rt_rd;

    rv32i_core #(.RESET_VECTOR(32'h0000_0000)) dut (
        .clk(clk), .rst(rst),
        .imem_req_valid(i_req_valid), .imem_req_ready(i_req_ready), .imem_req_addr(i_req_addr),
        .imem_rsp_valid(i_rsp_valid), .imem_rsp_rdata(i_rsp_rdata), .imem_rsp_err(i_rsp_err),
        .dmem_req_valid(d_req_valid), .dmem_req_ready(d_req_ready), .dmem_req_addr(d_req_addr),
        .dmem_req_we(d_req_we), .dmem_req_wstrb(d_req_wstrb), .dmem_req_wdata(d_req_wdata),
        .dmem_rsp_valid(d_rsp_valid), .dmem_rsp_rdata(d_rsp_rdata), .dmem_rsp_err(d_rsp_err),
        .halted_o(halted), .trap_cause_o(trap_cause), .trap_pc_o(trap_pc), .trap_tval_o(trap_tval),
        .dbg_trap_valid_o(trap_valid),
        .dbg_retire_valid_o(rt_valid), .dbg_retire_pc_o(rt_pc), .dbg_retire_insn_o(rt_insn),
        .dbg_retire_rd_we_o(rt_rd_we), .dbg_retire_rd_o(rt_rd), .dbg_retire_rd_wdata_o(rt_wdata)
    );

    tb_mem_model #(.MEM_BYTES(65536), .TOHOST_ADDR(32'h1000_0008)) u_mem (
        .clk(clk), .rst(rst),
        .i_req_valid(i_req_valid), .i_req_ready(i_req_ready), .i_req_addr(i_req_addr),
        .i_rsp_valid(i_rsp_valid), .i_rsp_rdata(i_rsp_rdata), .i_rsp_err(i_rsp_err),
        .d_req_valid(d_req_valid), .d_req_ready(d_req_ready), .d_req_addr(d_req_addr),
        .d_req_we(d_req_we), .d_req_wstrb(d_req_wstrb), .d_req_wdata(d_req_wdata),
        .d_rsp_valid(d_rsp_valid), .d_rsp_rdata(d_rsp_rdata), .d_rsp_err(d_rsp_err)
    );

    wire [31:0] im_err, im_hs, im_whs, im_rsp, im_ersp;
    wire [31:0] dm_err, dm_hs, dm_whs, dm_rsp, dm_ersp;
    wire        im_out, dm_out;

    tb_cmi_monitor #(.NAME("imem"), .CHECK_ALIGN(1)) u_imon (
        .clk(clk), .rst(rst), .halted(halted),
        .req_valid(i_req_valid), .req_ready(i_req_ready), .req_addr(i_req_addr),
        .req_we(1'b0), .req_wstrb(4'b0000), .req_wdata(32'd0),
        .rsp_valid(i_rsp_valid), .rsp_err(i_rsp_err),
        .errors(im_err), .n_handshakes(im_hs), .n_write_handshakes(im_whs),
        .n_responses(im_rsp), .n_error_responses(im_ersp), .outstanding(im_out)
    );

    tb_cmi_monitor #(.NAME("dmem"), .CHECK_ALIGN(0)) u_dmon (
        .clk(clk), .rst(rst), .halted(halted),
        .req_valid(d_req_valid), .req_ready(d_req_ready), .req_addr(d_req_addr),
        .req_we(d_req_we), .req_wstrb(d_req_wstrb), .req_wdata(d_req_wdata),
        .rsp_valid(d_rsp_valid), .rsp_err(d_rsp_err),
        .errors(dm_err), .n_handshakes(dm_hs), .n_write_handshakes(dm_whs),
        .n_responses(dm_rsp), .n_error_responses(dm_ersp), .outstanding(dm_out)
    );

    // ------------------------------------------------------------------
    // Configuration from plusargs
    // ------------------------------------------------------------------
    reg [8*512-1:0] hexfile;
    integer ready_pct, lat_min, lat_max, max_cycles, reset_test, reset_after;
    reg        exp_trap;
    reg [31:0] exp_cause, exp_pc, exp_tval;
    reg        chk_reg0_en, chk_reg1_en, chk_mem_en, probe_en, i_err_en, d_err_en;
    reg [31:0] chk_reg0, chk_val0, chk_reg1, chk_val1, chk_mem_addr, chk_mem_val;
    reg [31:0] probe_addr, exp_probe_reads, i_err_lo, i_err_hi, d_err_lo, d_err_hi;
    reg [31:0] tmp;
    reg [8*512-1:0] trace_file, mem_dump_file;
    reg        trace_en, mem_dump_en;
    reg [8*16-1:0] reset_mode;
    integer    tfd;

    task read_config;
        begin
            if (!$value$plusargs("hex=%s", hexfile)) begin
                tb_fail("no +hex=<file> given");
                tb_finish;
            end
            if (!$value$plusargs("ready_pct=%d", ready_pct))   ready_pct   = 100;
            if (!$value$plusargs("lat_min=%d", lat_min))       lat_min     = 1;
            if (!$value$plusargs("lat_max=%d", lat_max))       lat_max     = 1;
            if (!$value$plusargs("max_cycles=%d", max_cycles)) max_cycles  = 200000;
            if (!$value$plusargs("reset_test=%d", reset_test)) reset_test  = 0;
            if (!$value$plusargs("reset_after=%d", reset_after)) reset_after = 400;
            if (!$value$plusargs("reset_mode=%s", reset_mode))   reset_mode  = "dmem";
            trace_en    = $value$plusargs("trace_file=%s", trace_file);
            mem_dump_en = $value$plusargs("mem_dump=%s", mem_dump_file);
            if (trace_en) begin
                tfd = $fopen(trace_file, "w");
                if (tfd == 0) tb_fail("cannot open +trace_file for writing");
            end

            exp_trap    = $value$plusargs("tb_expect_cause=%h", exp_cause);
            if (exp_trap) begin
                if (!$value$plusargs("tb_expect_pc=%h", exp_pc))     exp_pc   = 32'hxxxx_xxxx;
                if (!$value$plusargs("tb_expect_tval=%h", exp_tval)) exp_tval = 32'hxxxx_xxxx;
            end
            chk_reg0_en = $value$plusargs("tb_check_reg0=%h", chk_reg0);
            if (chk_reg0_en) tmp = $value$plusargs("tb_check_val0=%h", chk_val0);
            chk_reg1_en = $value$plusargs("tb_check_reg1=%h", chk_reg1);
            if (chk_reg1_en) tmp = $value$plusargs("tb_check_val1=%h", chk_val1);
            chk_mem_en  = $value$plusargs("tb_check_mem_addr=%h", chk_mem_addr);
            if (chk_mem_en) tmp = $value$plusargs("tb_check_mem_val=%h", chk_mem_val);
            probe_en    = $value$plusargs("tb_probe_addr=%h", probe_addr);
            if (probe_en && !$value$plusargs("tb_expect_probe_reads=%h", exp_probe_reads))
                exp_probe_reads = 32'hFFFF_FFFF;
            i_err_en = $value$plusargs("tb_i_err_lo=%h", i_err_lo);
            if (i_err_en) tmp = $value$plusargs("tb_i_err_hi=%h", i_err_hi);
            d_err_en = $value$plusargs("tb_d_err_lo=%h", d_err_lo);
            if (d_err_en) tmp = $value$plusargs("tb_d_err_hi=%h", d_err_hi);

            $display("INFO: program %0s", hexfile);
            $display("INFO: memory ready_pct=%0d lat=[%0d,%0d] reset_test=%0d reset_mode=%0s expect=%0s",
                     ready_pct, lat_min, lat_max, reset_test, reset_mode, exp_trap ? "trap" : "pass");
        end
    endtask

    // ------------------------------------------------------------------
    // Run-time bookkeeping (cleared when rst is asserted)
    // ------------------------------------------------------------------
    integer    cycles, retired, n_stores, n_loads, n_branches, n_taken, n_traps;
    reg [31:0] trace_hash;
    reg [31:0] last_fetch_addr;
    reg        fetch_seen;
    reg [31:0] prev_pc;
    reg        prev_i_err, prev_d_err;
    reg        tohost_seen, store_retire_after_tohost, end_pass, end_halt;
    reg        first_fetch_checked;
    reg        exp_pc_retired;
    reg [8*64-1:0] msg;
    // bus statistics and the data transaction in flight (for the trace)
    integer    i_stall, d_stall, i_hs_cycle, d_hs_cycle, lat, k;
    integer    i_lat_hist [0:15];
    integer    d_lat_hist [0:15];
    reg [31:0] d_hs_addr, d_hs_wdata;
    reg        d_hs_we;
    reg [3:0]  d_hs_wstrb;

    task clear_counters;
        begin
            cycles = 0; retired = 0; n_stores = 0; n_loads = 0; n_branches = 0;
            n_taken = 0; n_traps = 0; trace_hash = 32'h1234_5678;
            fetch_seen = 1'b0; prev_pc = 32'd0; prev_i_err = 1'b0; prev_d_err = 1'b0;
            tohost_seen = 1'b0; end_pass = 1'b0; end_halt = 1'b0;
            first_fetch_checked = 1'b0; exp_pc_retired = 1'b0;
            i_stall = 0; d_stall = 0; i_hs_cycle = 0; d_hs_cycle = 0;
            for (k = 0; k < 16; k = k + 1) begin i_lat_hist[k] = 0; d_lat_hist[k] = 0; end
        end
    endtask

    // All checks sample pre-edge values (the DUT updates with non-blocking assignments).
    always @(posedge clk) begin
        if (rst) begin
            prev_pc <= i_req_addr;
        end else if (!end_pass && !end_halt) begin
            cycles = cycles + 1;

            // first instruction fetch after reset comes from the reset vector
            if (i_req_valid && i_req_ready && !first_fetch_checked) begin
                tb_check("first fetch after reset is at RESET_VECTOR", i_req_addr, 32'h0000_0000);
                first_fetch_checked = 1'b1;
            end
            // (last_fetch_addr is updated at the end of this block: a retire
            //  event and the next fetch handshake can fall in the same cycle)

            // bus statistics; data transactions go to the trace when they complete
            if (i_req_valid && !i_req_ready) i_stall = i_stall + 1;
            if (d_req_valid && !d_req_ready) d_stall = d_stall + 1;
            if (i_req_valid && i_req_ready) i_hs_cycle = cycles;
            if (d_req_valid && d_req_ready) begin
                d_hs_cycle = cycles;
                d_hs_addr = d_req_addr; d_hs_we = d_req_we;
                d_hs_wstrb = d_req_wstrb; d_hs_wdata = d_req_wdata;
            end
            if (i_rsp_valid) begin
                lat = cycles - i_hs_cycle;
                i_lat_hist[(lat > 15) ? 15 : lat] = i_lat_hist[(lat > 15) ? 15 : lat] + 1;
            end
            if (d_rsp_valid) begin
                lat = cycles - d_hs_cycle;
                d_lat_hist[(lat > 15) ? 15 : lat] = d_lat_hist[(lat > 15) ? 15 : lat] + 1;
                if (trace_en)
                    $fdisplay(tfd, "M %h %0d %h %h %h %0d", d_hs_addr, d_hs_we, d_hs_wstrb,
                              d_hs_wdata, d_rsp_rdata, d_rsp_err);
            end

            // pc changes only together with a retire event
            if (cycles > 1 && i_req_addr !== prev_pc && !rt_valid)
                tb_fail("pc changed without a retire event");
            prev_pc <= i_req_addr;

            // an error response must be followed by a trap, never a retire
            if (prev_i_err || prev_d_err) begin
                if (rt_valid)   tb_fail("instruction retired after an error response");
                if (!trap_valid) tb_fail("no trap event after an error response");
            end
            prev_i_err <= i_rsp_valid && i_rsp_err;
            prev_d_err <= d_rsp_valid && d_rsp_err;

            if (rt_valid) begin
                if (halted) tb_fail("retire event while halted");
                if (!fetch_seen || rt_pc !== last_fetch_addr) begin
                    $sformat(msg, "retired pc %h is not the last fetch address %h", rt_pc, last_fetch_addr);
                    tb_fail(msg);
                end
                if (im_out || dm_out) tb_fail("instruction retired with a CMI transaction outstanding");
                if (rt_rd_we && rt_rd == 5'd0) tb_fail("retire reports a write to x0");
                if (!rt_rd_we && rt_wdata !== 32'd0) tb_fail("retire wdata not 0 without rd write");
                if (exp_trap && rt_pc === exp_pc) exp_pc_retired = 1'b1;
                if (trace_en)
                    $fdisplay(tfd, "R %h %h %0d %02d %h", rt_pc, rt_insn, rt_rd_we, rt_rd, rt_wdata);
                retired = retired + 1;
                if (rt_insn[6:0] == 7'b0100011) n_stores = n_stores + 1;
                if (rt_insn[6:0] == 7'b0000011) n_loads  = n_loads + 1;
                if (rt_insn[6:0] == 7'b1100011) begin
                    n_branches = n_branches + 1;
                    if (i_req_addr != rt_pc + 32'd4) n_taken = n_taken + 1;
                end
                trace_hash = {trace_hash[26:0], trace_hash[31:27]}
                             ^ rt_pc ^ {rt_insn[15:0], rt_insn[31:16]}
                             ^ {rt_rd_we, 21'd0, rt_rd, 5'd0} ^ rt_wdata;
                if (tohost_seen) begin
                    // the TOHOST store itself retires one cycle after the memory saw it
                    end_pass = 1'b1;
                end
            end

            if (trap_valid) begin
                if (trace_en)
                    $fdisplay(tfd, "T %0d %h %h", trap_cause, trap_pc, trap_tval);
                n_traps = n_traps + 1;
                if (n_traps > 1) tb_fail("more than one trap event");
                end_halt = 1'b1;
            end
            if (u_mem.tohost_valid && !tohost_seen) tohost_seen = 1'b1;

            if (i_req_valid && i_req_ready) begin
                last_fetch_addr = i_req_addr;
                fetch_seen = 1'b1;
            end
        end
    end

    // ------------------------------------------------------------------
    // Main sequence
    // ------------------------------------------------------------------
    integer    wait_n;
    reg        reset_hit_pending;
    real       cpi;
    integer    exp_writes, hold;

    function reset_cond;
        input [8*16-1:0] mode;
        begin
            if      (mode == "imem_wait")    reset_cond = i_req_valid && !i_req_ready;
            else if (mode == "imem_pending") reset_cond = im_out && !i_rsp_valid;
            else if (mode == "imem_rsp")     reset_cond = i_rsp_valid;
            else if (mode == "dmem_wait")    reset_cond = d_req_valid && !d_req_ready;
            else if (mode == "dmem_rsp")     reset_cond = d_rsp_valid;
            else                             reset_cond = dm_out && !d_rsp_valid;   // "dmem"
        end
    endfunction

    task run_to_end;
        begin
            wait_n = 0;
            while (!end_pass && !end_halt && wait_n < max_cycles) begin
                @(posedge clk);
                wait_n = wait_n + 1;
            end
        end
    endtask

    initial begin
        tb_init;
        read_config;
        clear_counters;
        u_mem.clear_ram;
        u_mem.load_hex(hexfile);
        u_mem.configure(ready_pct, lat_min, lat_max, tb_seed);
        if (i_err_en) u_mem.set_i_error_window(i_err_lo, i_err_hi);
        if (d_err_en) u_mem.set_d_error_window(d_err_lo, d_err_hi);
        if (probe_en) u_mem.set_probe(probe_addr);

        rst = 1'b1;
        repeat (5) @(negedge clk);
        rst = 1'b0;

        // ---- optional reset at a chosen bus condition ----
        if (reset_test) begin
            repeat (reset_after) @(posedge clk);
            wait_n = 0;
            @(negedge clk);
            while (!reset_cond(reset_mode) && wait_n < 20000 && !end_pass && !end_halt) begin
                @(negedge clk);
                wait_n = wait_n + 1;
            end
            reset_hit_pending = reset_cond(reset_mode);
            $sformat(msg, "reset test: condition '%0s' reached before rst", reset_mode);
            tb_check(msg, reset_hit_pending, 1'b1);
            $display("INFO: reset (%0s) at t=%0t: imem valid=%0d ready=%0d outstanding=%0d rsp=%0d | dmem valid=%0d ready=%0d outstanding=%0d rsp=%0d",
                     reset_mode, $time, i_req_valid, i_req_ready, im_out, i_rsp_valid,
                     d_req_valid, d_req_ready, dm_out, d_rsp_valid);
            rst = 1'b1;                       // set between edges: sampled at the next rising edge
            if (trace_en) $fdisplay(tfd, "X");
            repeat (3) @(negedge clk);
            clear_counters;
            rst = 1'b0;
        end

        run_to_end;

        // let registered outputs and monitors settle, then check that nothing
        // more happens (no requests after halt: monitor check C6)
        repeat (2) @(posedge clk);

        if (!end_pass && !end_halt) begin
            $sformat(msg, "timeout after %0d cycles (pc=%h)", max_cycles, i_req_addr);
            tb_fail(msg);
        end

        // ---- outcome ----
        if (exp_trap) begin
            tb_check("program ended in a trap (not TOHOST)", {end_halt, tohost_seen}, 2'b10);
            tb_check("halted_o", halted, 1'b1);
            tb_check("trap_cause", trap_cause, exp_cause);
            if (exp_pc !== 32'hxxxx_xxxx)   tb_check("trap_pc", trap_pc, exp_pc);
            if (exp_tval !== 32'hxxxx_xxxx) tb_check("trap_tval", trap_tval, exp_tval);
            tb_check("trapping instruction did not retire", exp_pc_retired, 1'b0);
        end else begin
            tb_check("program ended through TOHOST (not a trap)", {end_pass, end_halt}, 2'b10);
            if (end_halt)
                $display("ERROR: unexpected trap cause=%0d pc=%h tval=%h", trap_cause, trap_pc, trap_tval);
            tb_check("TOHOST value (1 = PASS, else (testnum<<1)|1)", u_mem.tohost_value, 32'd1);
            if (u_mem.tohost_value !== 32'd1 && u_mem.tohost_value[0])
                $display("ERROR: program reported failing check #%0d", u_mem.tohost_value >> 1);
        end

        // ---- side-effect accounting ----
        exp_writes = n_stores + ((end_halt && trap_cause == 4'd7) ? 1 : 0);
        tb_check("data-port write handshakes == retired stores (+faulting store)", dm_whs, exp_writes);
        tb_check("committed + errored writes == write handshakes",
                 u_mem.d_writes_ok + u_mem.d_writes_err, dm_whs);
        if (chk_reg0_en) begin
            $sformat(msg, "register x%0d after end", chk_reg0);
            tb_check(msg, dut.u_regfile.regs[chk_reg0[4:0]], chk_val0);
        end
        if (chk_reg1_en) begin
            $sformat(msg, "register x%0d after end", chk_reg1);
            tb_check(msg, dut.u_regfile.regs[chk_reg1[4:0]], chk_val1);
        end
        if (chk_mem_en) begin
            $sformat(msg, "memory word %h after end", chk_mem_addr);
            tb_check(msg, u_mem.mem[chk_mem_addr[15:2]], chk_mem_val);
        end
        if (probe_en && exp_probe_reads != 32'hFFFF_FFFF)
            tb_check("reads of tb_probe_addr (loads to x0 are performed)", u_mem.probe_reads, exp_probe_reads);

        // ---- quiet after halt: no requests for a while (monitor C6 checks) ----
        if (end_halt) begin
            hold = 0;
            repeat (50) begin
                @(posedge clk);
                if (i_req_valid || d_req_valid || rt_valid || trap_valid) hold = hold + 1;
            end
            tb_check("no activity during 50 cycles after halt", hold, 0);
        end

        tb_check("imem monitor violations", im_err, 0);
        tb_check("dmem monitor violations", dm_err, 0);

        if (trace_en) begin
            if (end_pass)      $fdisplay(tfd, "E TOHOST %h", u_mem.tohost_value);
            else if (end_halt) $fdisplay(tfd, "E HALT");
            else               $fdisplay(tfd, "E TIMEOUT");
            $fclose(tfd);
        end
        if (mem_dump_en) u_mem.dump_hex(mem_dump_file);
        $write("MEMSTAT: imem_stall=%0d dmem_stall=%0d imem_lat=", i_stall, d_stall);
        for (k = 1; k < 16; k = k + 1) $write("%0d%0s", i_lat_hist[k], (k < 15) ? "," : "");
        $write(" dmem_lat=");
        for (k = 1; k < 16; k = k + 1) $write("%0d%0s", d_lat_hist[k], (k < 15) ? "," : "");
        $write("\n");

        cpi = (retired > 0) ? (1.0 * cycles) / retired : 0.0;
        $display("PERF: cycles=%0d retired=%0d cpi=%0.3f loads=%0d stores=%0d branches=%0d taken=%0d",
                 cycles, retired, cpi, n_loads, n_stores, n_branches, n_taken);
        $display("TRACE: retired=%0d hash=%h", retired, trace_hash);
        if (end_halt)
            $display("INFO: halted cause=%0d pc=%h tval=%h", trap_cause, trap_pc, trap_tval);
        tb_finish;
    end

endmodule

`default_nettype wire
