`timescale 1ns / 1ps
`default_nettype none

// tb_soc - program-level test bench for the RV32I SoC.
//
// Two configurations of the same checks:
//   default   (Icarus Verilog) rv32_soc_top with its m_axi connected DIRECTLY to
//             soc_sysctrl (one slave, no interconnect). This is a subsystem test:
//             it does NOT verify the AMD AXI SmartConnect, and AXI addresses
//             outside SYSCTRL alias into it, so such programs are not run here.
//   SOC_BD    (Vivado xsim) the Vivado block design soc_bd (rv32_soc_top ->
//             AMD AXI SmartConnect -> soc_sysctrl) through its wrapper.
// Observation uses hierarchical paths to the CPU-subsystem and SYSCTRL instances
// (`SOC / `SYS), so the block design needs no debug ports.
//
// The program image is loaded into the real BRAM array (soc_ram_dp.mem) at
// time 0 by the testbench (simulation backdoor); the RAM's INIT_FILE parameter
// is the synthesis path and is tested separately.
//
// Plusargs and output lines are those of dv/cpu/core/tb_rv32i_core.v:
//   +hex=<file>  tb_* expectation symbols  +max_cycles=N  +trace_file=F  +mem_dump=F
//   +reset_test=1 +reset_mode=dmem|imem_pending|... +reset_after=N
// The memory has no error-injection windows: programs that need tb_i_err_* /
// tb_d_err_* are not used with this bench. TOHOST is the SYSCTRL register.
// Extra checks: TOHOST write events (exactly one for a PASS program, none for
// a trap program), AXI4-Lite protocol on the CPU's m_axi (and on the SYSCTRL
// port in the SOC_BD configuration).

`ifdef SOC_BD
  `define SOC u_dut.soc_bd_i.cpu_subsys.inst
  `define SYS u_dut.soc_bd_i.sysctrl.inst
`else
  `define SOC u_soc
  `define SYS u_sys
`endif

module tb_soc;

    localparam TB_TIMEOUT_NS = 200000000;
    `include "tb_check.vh"

    reg clk = 1'b0;
    reg aresetn = 1'b0;
    always #5 clk = ~clk;
    wire rst = ~aresetn;

`ifdef DUMP_WAVES
    initial begin $dumpfile("wave.vcd"); $dumpvars(0, tb_soc); end
`endif

    // ------------------------------------------------------------------
    // DUT
    // ------------------------------------------------------------------
`ifdef SOC_BD
    soc_bd_wrapper u_dut (.aclk(clk), .aresetn(aresetn));
`else
    wire [31:0] awaddr, wdata, araddr, rdata;
    wire [2:0]  awprot, arprot;
    wire [3:0]  wstrb;
    wire [1:0]  bresp, rresp;
    wire        awvalid, awready, wvalid, wready, bvalid, bready, arvalid, arready, rvalid, rready;
    wire        u0;  wire [3:0] u1;  wire [31:0] u2, u3;
    wire [7:0]  led;
    wire        th_valid, th_wr;  wire [31:0] th_value;
    rv32_soc_top u_soc (
        .aclk(clk), .aresetn(aresetn),
        .m_axi_awaddr(awaddr), .m_axi_awprot(awprot), .m_axi_awvalid(awvalid), .m_axi_awready(awready),
        .m_axi_wdata(wdata), .m_axi_wstrb(wstrb), .m_axi_wvalid(wvalid), .m_axi_wready(wready),
        .m_axi_bresp(bresp), .m_axi_bvalid(bvalid), .m_axi_bready(bready),
        .m_axi_araddr(araddr), .m_axi_arprot(arprot), .m_axi_arvalid(arvalid), .m_axi_arready(arready),
        .m_axi_rdata(rdata), .m_axi_rresp(rresp), .m_axi_rvalid(rvalid), .m_axi_rready(rready),
        .mbx_bram_clk(), .mbx_bram_rst(), .mbx_bram_en(), .mbx_bram_we(), .mbx_bram_addr(),
        .mbx_bram_wrdata(), .mbx_bram_rddata(32'h0),
        .halted(), .trap_cause(), .trap_pc(), .trap_tval(),
        .dbg_trap_valid(), .dbg_retire_valid(), .dbg_retire_pc(), .dbg_retire_insn(),
        .dbg_retire_rd_we(), .dbg_retire_rd(), .dbg_retire_rd_wdata(),
        .dbg_imem_req_valid(), .dbg_imem_req_ready(), .dbg_imem_req_addr(), .dbg_imem_rsp_valid(),
        .dbg_imem_rsp_err(), .dbg_dmem_req_valid(), .dbg_dmem_req_ready(), .dbg_dmem_req_addr(),
        .dbg_dmem_req_we(), .dbg_dmem_req_wstrb(), .dbg_dmem_req_wdata(), .dbg_dmem_rsp_valid(),
        .dbg_dmem_rsp_rdata(), .dbg_dmem_rsp_err()
    );
    soc_sysctrl u_sys (
        .aclk(clk), .aresetn(aresetn),
        .s_axi_awaddr(awaddr[11:0]), .s_axi_awprot(awprot), .s_axi_awvalid(awvalid), .s_axi_awready(awready),
        .s_axi_wdata(wdata), .s_axi_wstrb(wstrb), .s_axi_wvalid(wvalid), .s_axi_wready(wready),
        .s_axi_bresp(bresp), .s_axi_bvalid(bvalid), .s_axi_bready(bready),
        .s_axi_araddr(araddr[11:0]), .s_axi_arprot(arprot), .s_axi_arvalid(arvalid), .s_axi_arready(arready),
        .s_axi_rdata(rdata), .s_axi_rresp(rresp), .s_axi_rvalid(rvalid), .s_axi_rready(rready),
        .led(led), .tohost_valid(th_valid), .tohost_value(th_value), .tohost_wr(th_wr)
    );
`endif

    // observation (hierarchical, identical in both configurations)
    wire        halted      = `SOC.halted;
    wire [3:0]  trap_cause  = `SOC.trap_cause;
    wire [31:0] trap_pc     = `SOC.trap_pc;
    wire [31:0] trap_tval   = `SOC.trap_tval;
    wire        trap_valid  = `SOC.dbg_trap_valid;
    wire        rt_valid    = `SOC.dbg_retire_valid;
    wire [31:0] rt_pc       = `SOC.dbg_retire_pc;
    wire [31:0] rt_insn     = `SOC.dbg_retire_insn;
    wire        rt_rd_we    = `SOC.dbg_retire_rd_we;
    wire [4:0]  rt_rd       = `SOC.dbg_retire_rd;
    wire [31:0] rt_wdata    = `SOC.dbg_retire_rd_wdata;
    wire        i_req_valid = `SOC.dbg_imem_req_valid;
    wire        i_req_ready = `SOC.dbg_imem_req_ready;
    wire [31:0] i_req_addr  = `SOC.dbg_imem_req_addr;
    wire        i_rsp_valid = `SOC.dbg_imem_rsp_valid;
    wire        i_rsp_err   = `SOC.dbg_imem_rsp_err;
    wire        d_req_valid = `SOC.dbg_dmem_req_valid;
    wire        d_req_ready = `SOC.dbg_dmem_req_ready;
    wire [31:0] d_req_addr  = `SOC.dbg_dmem_req_addr;
    wire        d_req_we    = `SOC.dbg_dmem_req_we;
    wire [3:0]  d_req_wstrb = `SOC.dbg_dmem_req_wstrb;
    wire [31:0] d_req_wdata = `SOC.dbg_dmem_req_wdata;
    wire        d_rsp_valid = `SOC.dbg_dmem_rsp_valid;
    wire [31:0] d_rsp_rdata = `SOC.dbg_dmem_rsp_rdata;
    wire        d_rsp_err   = `SOC.dbg_dmem_rsp_err;
    wire        tohost_valid = `SYS.tohost_valid;
    wire [31:0] tohost_value = `SYS.tohost_value;
    wire        tohost_wr    = `SYS.tohost_wr;

    // ------------------------------------------------------------------
    // monitors
    // ------------------------------------------------------------------
    wire [31:0] im_err, im_hs, im_whs, im_rsp, im_ersp, dm_err, dm_hs, dm_whs, dm_rsp, dm_ersp;
    wire        im_out, dm_out;
    tb_cmi_monitor #(.NAME("soc imem"), .CHECK_ALIGN(1)) u_imon (
        .clk(clk), .rst(rst), .halted(halted),
        .req_valid(i_req_valid), .req_ready(i_req_ready), .req_addr(i_req_addr),
        .req_we(1'b0), .req_wstrb(4'b0000), .req_wdata(32'd0),
        .rsp_valid(i_rsp_valid), .rsp_err(i_rsp_err),
        .errors(im_err), .n_handshakes(im_hs), .n_write_handshakes(im_whs),
        .n_responses(im_rsp), .n_error_responses(im_ersp), .outstanding(im_out));
    tb_cmi_monitor #(.NAME("soc dmem"), .CHECK_ALIGN(0)) u_dmon (
        .clk(clk), .rst(rst), .halted(halted),
        .req_valid(d_req_valid), .req_ready(d_req_ready), .req_addr(d_req_addr),
        .req_we(d_req_we), .req_wstrb(d_req_wstrb), .req_wdata(d_req_wdata),
        .rsp_valid(d_rsp_valid), .rsp_err(d_rsp_err),
        .errors(dm_err), .n_handshakes(dm_hs), .n_write_handshakes(dm_whs),
        .n_responses(dm_rsp), .n_error_responses(dm_ersp), .outstanding(dm_out));

    wire [31:0] ax_err, ax_aw, ax_w, ax_b, ax_ar, ax_r, ax_berr, ax_rerr;
    tb_axil_monitor #(.NAME("cpu m_axi"), .SINGLE(1)) u_axmon (
        .aclk(clk), .aresetn(aresetn),
        .awaddr(`SOC.m_axi_awaddr), .awprot(`SOC.m_axi_awprot), .awvalid(`SOC.m_axi_awvalid), .awready(`SOC.m_axi_awready),
        .wdata(`SOC.m_axi_wdata), .wstrb(`SOC.m_axi_wstrb), .wvalid(`SOC.m_axi_wvalid), .wready(`SOC.m_axi_wready),
        .bresp(`SOC.m_axi_bresp), .bvalid(`SOC.m_axi_bvalid), .bready(`SOC.m_axi_bready),
        .araddr(`SOC.m_axi_araddr), .arprot(`SOC.m_axi_arprot), .arvalid(`SOC.m_axi_arvalid), .arready(`SOC.m_axi_arready),
        .rdata(`SOC.m_axi_rdata), .rresp(`SOC.m_axi_rresp), .rvalid(`SOC.m_axi_rvalid), .rready(`SOC.m_axi_rready),
        .errors(ax_err), .n_aw(ax_aw), .n_w(ax_w), .n_b(ax_b), .n_ar(ax_ar), .n_r(ax_r),
        .n_berr(ax_berr), .n_rerr(ax_rerr));

`ifdef SOC_BD
    wire [31:0] sx_err, sx_aw, sx_w, sx_b, sx_ar, sx_r, sx_berr, sx_rerr;
    tb_axil_monitor #(.NAME("sysctrl s_axi"), .SINGLE(1)) u_sxmon (
        .aclk(clk), .aresetn(aresetn),
        .awaddr({20'd0, `SYS.s_axi_awaddr}), .awprot(`SYS.s_axi_awprot), .awvalid(`SYS.s_axi_awvalid), .awready(`SYS.s_axi_awready),
        .wdata(`SYS.s_axi_wdata), .wstrb(`SYS.s_axi_wstrb), .wvalid(`SYS.s_axi_wvalid), .wready(`SYS.s_axi_wready),
        .bresp(`SYS.s_axi_bresp), .bvalid(`SYS.s_axi_bvalid), .bready(`SYS.s_axi_bready),
        .araddr({20'd0, `SYS.s_axi_araddr}), .arprot(`SYS.s_axi_arprot), .arvalid(`SYS.s_axi_arvalid), .arready(`SYS.s_axi_arready),
        .rdata(`SYS.s_axi_rdata), .rresp(`SYS.s_axi_rresp), .rvalid(`SYS.s_axi_rvalid), .rready(`SYS.s_axi_rready),
        .errors(sx_err), .n_aw(sx_aw), .n_w(sx_w), .n_b(sx_b), .n_ar(sx_ar), .n_r(sx_r),
        .n_berr(sx_berr), .n_rerr(sx_rerr));
`endif

    // ------------------------------------------------------------------
    // configuration
    // ------------------------------------------------------------------
    reg [8*512-1:0] hexfile, trace_file, mem_dump_file;
    reg [8*16-1:0]  reset_mode;
    integer max_cycles, reset_test, reset_after, tfd;
    reg        exp_trap, trace_en, mem_dump_en;
    reg [31:0] exp_cause, exp_pc, exp_tval, tmp;
    reg        chk_reg0_en, chk_reg1_en, chk_mem_en, probe_en, err_win;
    reg [31:0] chk_reg0, chk_val0, chk_reg1, chk_val1, chk_mem_addr, chk_mem_val, probe_addr, exp_probe_reads;

    task read_config;
        begin
            if (!$value$plusargs("hex=%s", hexfile)) begin tb_fail("no +hex=<file> given"); tb_finish; end
            if (!$value$plusargs("max_cycles=%d", max_cycles))   max_cycles  = 400000;
            if (!$value$plusargs("reset_test=%d", reset_test))   reset_test  = 0;
            if (!$value$plusargs("reset_after=%d", reset_after)) reset_after = 400;
            if (!$value$plusargs("reset_mode=%s", reset_mode))   reset_mode  = "dmem";
            exp_trap = $value$plusargs("tb_expect_cause=%h", exp_cause);
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
            probe_en = $value$plusargs("tb_probe_addr=%h", probe_addr);
            if (probe_en && !$value$plusargs("tb_expect_probe_reads=%h", exp_probe_reads))
                exp_probe_reads = 32'hFFFF_FFFF;
            err_win = $test$plusargs("tb_i_err_lo") || $test$plusargs("tb_d_err_lo");
            if (err_win) tb_fail("program needs memory error windows, which the SoC has not");
            trace_en    = $value$plusargs("trace_file=%s", trace_file);
            mem_dump_en = $value$plusargs("mem_dump=%s", mem_dump_file);
            if (trace_en) begin
                tfd = $fopen(trace_file, "w");
                if (tfd == 0) tb_fail("cannot open +trace_file for writing");
            end
`ifdef SOC_BD
            $display("INFO: configuration SOC_BD (block design with AMD AXI SmartConnect)");
`else
            $display("INFO: configuration DIRECT (cpu m_axi -> sysctrl, no interconnect)");
`endif
            $display("INFO: program %0s reset_test=%0d reset_mode=%0s expect=%0s",
                     hexfile, reset_test, reset_mode, exp_trap ? "trap" : "pass");
        end
    endtask

    // ------------------------------------------------------------------
    // per-cycle checks, trace, statistics
    // ------------------------------------------------------------------
    integer    cycles, retired, n_stores, n_loads, n_traps, tohost_events, probe_reads;
    reg [31:0] trace_hash, last_fetch_addr, prev_pc, d_hs_addr, d_hs_wdata;
    reg        fetch_seen, prev_i_err, prev_d_err, tohost_seen, end_pass, end_halt;
    reg        first_fetch_checked, exp_pc_retired, d_hs_we;
    reg [3:0]  d_hs_wstrb;
    reg [8*64-1:0] msg;

    // Router rejection check (docs/soc/memory_map.md): a data access outside the
    // implemented windows (RAM 0x0000_0000-0x0000_FFFF, SYSCTRL 0x1000_0000-
    // 0x1000_0FFF; TB's own copy of the map) must get exactly one CMI error
    // response and cause no AXI activity at all: no AW/W/AR VALID while it is
    // outstanding, and no AW/W/AR handshake on the CPU m_axi or (block design)
    // on the SYSCTRL s_axi.
    // +axi_decode_test: verification-only block design (AXI_DECODE_TEST=1), the
    // router forwards all of 0x1000_0000-0x2FFF_FFFF and the SmartConnect decodes.
    reg axi_decode_test = 1'b0;
    initial axi_decode_test = $test$plusargs("axi_decode_test");
    function mapped_addr;
        input [31:0] a;
        mapped_addr = (a <= 32'h0000_FFFF) || (a >= 32'h1000_0000 && a <= 32'h1000_0FFF) ||
                      (axi_decode_test && a >= 32'h1000_0000 && a <= 32'h2FFF_FFFF);
    endfunction
    integer    n_rejected, rej_aw, rej_w, rej_ar, rej_saw, rej_sw, rej_sar;
    reg        rej_pending;

    task clear_counters;
        begin
            n_rejected = 0; rej_pending = 1'b0;
            cycles = 0; retired = 0; n_stores = 0; n_loads = 0; n_traps = 0; tohost_events = 0;
            probe_reads = 0; trace_hash = 32'h1234_5678; fetch_seen = 1'b0; prev_pc = 32'd0;
            prev_i_err = 1'b0; prev_d_err = 1'b0; tohost_seen = 1'b0; end_pass = 1'b0; end_halt = 1'b0;
            first_fetch_checked = 1'b0; exp_pc_retired = 1'b0;
        end
    endtask

    always @(posedge clk) begin
        if (rst) begin
            prev_pc <= i_req_addr;
            rej_pending = 1'b0;
        end else if (!end_pass && !end_halt) begin
            if (rej_pending) begin
                if (`SOC.m_axi_awvalid || `SOC.m_axi_wvalid || `SOC.m_axi_arvalid)
                    tb_fail("AXI VALID during a router-rejected access");
                if (d_rsp_valid) begin
                    $sformat(msg, "rejected access %h: CMI error response", d_hs_addr);
                    tb_check(msg, d_rsp_err, 1'b1);
                    $sformat(msg, "rejected access %h: cpu m_axi aw/w/ar unchanged", d_hs_addr);
                    tb_check(msg, (ax_aw - rej_aw) + (ax_w - rej_w) + (ax_ar - rej_ar), 0);
`ifdef SOC_BD
                    $sformat(msg, "rejected access %h: sysctrl s_axi aw/w/ar unchanged", d_hs_addr);
                    tb_check(msg, (sx_aw - rej_saw) + (sx_w - rej_sw) + (sx_ar - rej_sar), 0);
`endif
                    n_rejected = n_rejected + 1;
                    rej_pending = 1'b0;
                end
            end
            cycles = cycles + 1;
            if (tohost_wr) tohost_events = tohost_events + 1;
            if (i_req_valid && i_req_ready && !first_fetch_checked) begin
                tb_check("first fetch after reset is at RESET_VECTOR", i_req_addr, 32'h0000_0000);
                first_fetch_checked = 1'b1;
            end
            if (d_req_valid && d_req_ready) begin
                d_hs_addr = d_req_addr; d_hs_we = d_req_we; d_hs_wstrb = d_req_wstrb; d_hs_wdata = d_req_wdata;
                if (!mapped_addr(d_req_addr)) begin
                    rej_pending = 1'b1;
                    rej_aw = ax_aw; rej_w = ax_w; rej_ar = ax_ar;
`ifdef SOC_BD
                    rej_saw = sx_aw; rej_sw = sx_w; rej_sar = sx_ar;
`endif
                end
                if (probe_en && !d_req_we && {d_req_addr[31:2], 2'b00} == probe_addr)
                    probe_reads = probe_reads + 1;
            end
            if (d_rsp_valid && trace_en)
                $fdisplay(tfd, "M %h %0d %h %h %h %0d", d_hs_addr, d_hs_we, d_hs_wstrb,
                          d_hs_wdata, d_rsp_rdata, d_rsp_err);
            if (cycles > 1 && i_req_addr !== prev_pc && !rt_valid)
                tb_fail("pc changed without a retire event");
            prev_pc <= i_req_addr;
            if (prev_i_err || prev_d_err) begin
                if (rt_valid)    tb_fail("instruction retired after an error response");
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
                if (exp_trap && rt_pc === exp_pc) exp_pc_retired = 1'b1;
                if (trace_en) $fdisplay(tfd, "R %h %h %0d %02d %h", rt_pc, rt_insn, rt_rd_we, rt_rd, rt_wdata);
                retired = retired + 1;
                if (rt_insn[6:0] == 7'b0100011) n_stores = n_stores + 1;
                if (rt_insn[6:0] == 7'b0000011) n_loads  = n_loads + 1;
                trace_hash = {trace_hash[26:0], trace_hash[31:27]} ^ rt_pc ^ {rt_insn[15:0], rt_insn[31:16]}
                             ^ {rt_rd_we, 21'd0, rt_rd, 5'd0} ^ rt_wdata;
                if (tohost_seen) end_pass = 1'b1;
            end
            if (trap_valid) begin
                if (trace_en) $fdisplay(tfd, "T %0d %h %h", trap_cause, trap_pc, trap_tval);
                n_traps = n_traps + 1;
                end_halt = 1'b1;
            end
            if (tohost_valid && !tohost_seen) tohost_seen = 1'b1;
            if (i_req_valid && i_req_ready) begin last_fetch_addr = i_req_addr; fetch_seen = 1'b1; end
        end
    end

    function reset_cond;
        input [8*16-1:0] mode;
        begin
            if      (mode == "imem_wait")    reset_cond = i_req_valid && !i_req_ready;
            else if (mode == "imem_pending") reset_cond = im_out && !i_rsp_valid;
            else if (mode == "imem_rsp")     reset_cond = i_rsp_valid;
            else if (mode == "dmem_wait")    reset_cond = d_req_valid && !d_req_ready;
            else if (mode == "dmem_rsp")     reset_cond = d_rsp_valid;
            else                             reset_cond = dm_out && !d_rsp_valid;
        end
    endfunction

    // ------------------------------------------------------------------
    // main sequence
    // ------------------------------------------------------------------
    integer wait_n, exp_writes, hold, k, fd;
    real    cpi;

    initial begin
        tb_init;
        read_config;
        clear_counters;
        for (k = 0; k < 16384; k = k + 1) `SOC.u_ram.mem[k] = 32'd0;
        $readmemh(hexfile, `SOC.u_ram.mem);
        aresetn = 1'b0;
        repeat (5) @(negedge clk);
        aresetn = 1'b1;

        if (reset_test) begin
            repeat (reset_after) @(posedge clk);
            wait_n = 0;
            @(negedge clk);
            while (!reset_cond(reset_mode) && wait_n < 20000 && !end_pass && !end_halt) begin
                @(negedge clk); wait_n = wait_n + 1;
            end
            $sformat(msg, "reset test: condition '%0s' reached before reset", reset_mode);
            tb_check(msg, reset_cond(reset_mode), 1'b1);
            $display("INFO: reset (%0s) at t=%0t", reset_mode, $time);
            aresetn = 1'b0;
            if (trace_en) $fdisplay(tfd, "X");
            repeat (3) @(negedge clk);
            clear_counters;
            aresetn = 1'b1;
        end

        wait_n = 0;
        while (!end_pass && !end_halt && wait_n < max_cycles) begin @(posedge clk); wait_n = wait_n + 1; end
        repeat (2) @(posedge clk);
        if (!end_pass && !end_halt) begin
            $sformat(msg, "timeout after %0d cycles (pc=%h)", max_cycles, i_req_addr);
            tb_fail(msg);
        end

        if (exp_trap) begin
            tb_check("program ended in a trap (not TOHOST)", {end_halt, tohost_seen}, 2'b10);
            tb_check("trap_cause", trap_cause, exp_cause);
            if (exp_pc !== 32'hxxxx_xxxx)   tb_check("trap_pc", trap_pc, exp_pc);
            if (exp_tval !== 32'hxxxx_xxxx) tb_check("trap_tval", trap_tval, exp_tval);
            tb_check("trapping instruction did not retire", exp_pc_retired, 1'b0);
            tb_check("no TOHOST write in a trap program", tohost_events, 0);
        end else begin
            tb_check("program ended through TOHOST (not a trap)", {end_pass, end_halt}, 2'b10);
            if (end_halt) $display("ERROR: unexpected trap cause=%0d pc=%h tval=%h", trap_cause, trap_pc, trap_tval);
            tb_check("SYSCTRL TOHOST value (1 = PASS)", tohost_value, 32'd1);
            if (tohost_value !== 32'd1 && tohost_value[0])
                $display("ERROR: program reported failing check #%0d", tohost_value >> 1);
            tb_check("exactly one TOHOST write event", tohost_events, 1);
        end
        exp_writes = n_stores + ((end_halt && trap_cause == 4'd7) ? 1 : 0);
        tb_check("data-port write handshakes == retired stores (+faulting store)", dm_whs, exp_writes);
        if (chk_reg0_en) tb_check("register check 0", `SOC.u_core.u_regfile.regs[chk_reg0[4:0]], chk_val0);
        if (chk_reg1_en) tb_check("register check 1", `SOC.u_core.u_regfile.regs[chk_reg1[4:0]], chk_val1);
        if (chk_mem_en)  tb_check("memory check", `SOC.u_ram.mem[chk_mem_addr[15:2]], chk_mem_val);
        if (probe_en && exp_probe_reads != 32'hFFFF_FFFF)
            tb_check("reads of tb_probe_addr", probe_reads, exp_probe_reads);
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
        tb_check("cpu m_axi AXI monitor violations", ax_err, 0);
`ifdef SOC_BD
        tb_check("sysctrl s_axi AXI monitor violations", sx_err, 0);
        $display("INFO: AXI cpu side: aw=%0d w=%0d b=%0d (err %0d) ar=%0d r=%0d (err %0d); sysctrl side: aw=%0d ar=%0d",
                 ax_aw, ax_w, ax_b, ax_berr, ax_ar, ax_r, ax_rerr, sx_aw, sx_ar);
`else
        $display("INFO: AXI cpu side: aw=%0d w=%0d b=%0d (err %0d) ar=%0d r=%0d (err %0d)",
                 ax_aw, ax_w, ax_b, ax_berr, ax_ar, ax_r, ax_rerr);
`endif
        if (trace_en) begin
            if (end_pass)      $fdisplay(tfd, "E TOHOST %h", tohost_value);
            else if (end_halt) $fdisplay(tfd, "E HALT");
            else               $fdisplay(tfd, "E TIMEOUT");
            $fclose(tfd);
        end
        if (mem_dump_en) begin
            fd = $fopen(mem_dump_file, "w");
            for (k = 0; k < 16384; k = k + 1) $fdisplay(fd, "%h", `SOC.u_ram.mem[k]);
            $fclose(fd);
        end
        cpi = (retired > 0) ? (1.0 * cycles) / retired : 0.0;
        $display("INFO: router-rejected data accesses: %0d", n_rejected);
        $display("PERF: cycles=%0d retired=%0d cpi=%0.3f loads=%0d stores=%0d", cycles, retired, cpi, n_loads, n_stores);
        $display("TRACE: retired=%0d hash=%h", retired, trace_hash);
        if (end_halt) $display("INFO: halted cause=%0d pc=%h tval=%h", trap_cause, trap_pc, trap_tval);
        tb_finish;
    end

endmodule

`default_nettype wire
