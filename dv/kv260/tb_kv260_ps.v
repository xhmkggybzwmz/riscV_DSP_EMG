`timescale 1ns / 1ps
`default_nettype none

// tb_kv260_ps - stage 6A PS-variant debug path (Icarus): kv260_cpu_dbg (the
// unchanged rv32_soc_top) -> soc_sysctrl, kv260_dbg_obs, and kv260_dbg_axil
// driven by an AXI4-Lite master task (in place of the PS through the SmartConnect).
// proc_sys_reset is modelled as a 4-cycle synchroniser: soc reset = PS reset |
// CTRL.cpu_reset.
//
// Plusargs: +hex=<image> +exp_retired=N (ISS count incl. the TOHOST store, 0 = skip)
//           +trace_start=S +dump=<file> (register/trace dump in the format of
//           scripts/fpga/kv260_ps_board.py)
// Checks: ID/VERSION/SCRATCH/TRACE_DEPTH; power-on run and a re-run through
// CTRL.cpu_reset (TOHOST 1 once, no trap, tohost_retired, equal run_cycles,
// reset_releases); every trace record equals the signals of its cycle; the
// first record is the retire of instruction S.

module tb_kv260_ps;
    localparam TB_TIMEOUT_NS = 50000000;
    `include "tb_check.vh"

    reg clk = 1'b0;
    always #5 clk = ~clk;
    reg ps_rst_n = 1'b0;

    // ---- CPU subsystem, SYSCTRL, obs ----
    wire [31:0] awaddr, wdata, araddr, rdata;
    wire [2:0]  awprot, arprot;
    wire [3:0]  wstrb;
    wire [1:0]  bresp, rresp;
    wire        awvalid, awready, wvalid, wready, bvalid, bready, arvalid, arready, rvalid, rready;
    wire        halted, retire_valid, tohost_wr, tohost_valid;
    wire [3:0]  trap_cause;
    wire [31:0] trap_pc, trap_tval, imem_addr, tohost_value;
    wire [7:0]  led;
    wire [63:0]  ila_ctrl;
    wire [159:0] ila_cpu;
    wire [95:0]  ila_trap;
    wire [127:0] ila_axi;
    wire        cpu_reset, ps_rst_override;

    reg  [3:0]  soc_sync = 4'd0;
    always @(posedge clk) soc_sync <= {soc_sync[2:0], ps_rst_n & ~cpu_reset};
    wire soc_rst_n = soc_sync[3];

    kv260_cpu_dbg #(.INIT_FILE("")) cpu (
        .aclk(clk), .aresetn(soc_rst_n),
        .m_axi_awaddr(awaddr), .m_axi_awprot(awprot), .m_axi_awvalid(awvalid), .m_axi_awready(awready),
        .m_axi_wdata(wdata), .m_axi_wstrb(wstrb), .m_axi_wvalid(wvalid), .m_axi_wready(wready),
        .m_axi_bresp(bresp), .m_axi_bvalid(bvalid), .m_axi_bready(bready),
        .m_axi_araddr(araddr), .m_axi_arprot(arprot), .m_axi_arvalid(arvalid), .m_axi_arready(arready),
        .m_axi_rdata(rdata), .m_axi_rresp(rresp), .m_axi_rvalid(rvalid), .m_axi_rready(rready),
        .mbx_bram_clk(), .mbx_bram_rst(), .mbx_bram_en(), .mbx_bram_we(), .mbx_bram_addr(),
        .mbx_bram_wrdata(), .mbx_bram_rddata(32'h0),
        .halted(halted), .trap_cause(trap_cause), .trap_pc(trap_pc), .trap_tval(trap_tval),
        .dbg_retire_valid(retire_valid), .dbg_imem_req_addr(imem_addr),
        .tohost_wr(tohost_wr), .tohost_value(tohost_value), .pl_resetn0(1'b1),
        .ila_ctrl(ila_ctrl), .ila_cpu(ila_cpu), .ila_trap(ila_trap), .ila_axi(ila_axi));

    soc_sysctrl sys (
        .aclk(clk), .aresetn(soc_rst_n),
        .s_axi_awaddr(awaddr[11:0]), .s_axi_awprot(awprot), .s_axi_awvalid(awvalid), .s_axi_awready(awready),
        .s_axi_wdata(wdata), .s_axi_wstrb(wstrb), .s_axi_wvalid(wvalid), .s_axi_wready(wready),
        .s_axi_bresp(bresp), .s_axi_bvalid(bvalid), .s_axi_bready(bready),
        .s_axi_araddr(araddr[11:0]), .s_axi_arprot(arprot), .s_axi_arvalid(arvalid), .s_axi_arready(arready),
        .s_axi_rdata(rdata), .s_axi_rresp(rresp), .s_axi_rvalid(rvalid), .s_axi_rready(rready),
        .led(led), .tohost_valid(tohost_valid), .tohost_value(tohost_value), .tohost_wr(tohost_wr));

    wire        ext_reset_n, dcm_locked;
    wire [31:0] run_cycles, retired, tohost_retired, first_tohost, free_cycles;
    wire [7:0]  tohost_writes;
    wire [15:0] reset_releases;
    kv260_dbg_obs obs (
        .aclk(clk), .rst_n(soc_rst_n), .pl_resetn0(1'b1), .ps_rst_override(ps_rst_override),
        .ext_reset_n(ext_reset_n), .dcm_locked(dcm_locked),
        .retire_valid(retire_valid), .tohost_wr(tohost_wr), .tohost_value(tohost_value),
        .run_cycles(run_cycles), .retired(retired), .tohost_retired(tohost_retired),
        .tohost_writes(tohost_writes), .first_tohost(first_tohost), .reset_releases(reset_releases),
        .free_cycles(free_cycles));

    // ---- debug slave + AXI4-Lite master ----
    reg  [17:0] d_awaddr = 0, d_araddr = 0;
    reg  [31:0] d_wdata = 0;
    reg         d_awvalid = 0, d_wvalid = 0, d_bready = 0, d_arvalid = 0, d_rready = 0;
    wire        d_awready, d_wready, d_bvalid, d_arready, d_rvalid;
    wire [1:0]  d_bresp, d_rresp;
    wire [31:0] d_rdata;

    kv260_dbg_axil #(.DEPTH_LOG2(12)) dbg (
        .aclk(clk), .aresetn(ps_rst_n),
        .s_axi_awaddr(d_awaddr), .s_axi_awprot(3'd0), .s_axi_awvalid(d_awvalid), .s_axi_awready(d_awready),
        .s_axi_wdata(d_wdata), .s_axi_wstrb(4'hF), .s_axi_wvalid(d_wvalid), .s_axi_wready(d_wready),
        .s_axi_bresp(d_bresp), .s_axi_bvalid(d_bvalid), .s_axi_bready(d_bready),
        .s_axi_araddr(d_araddr), .s_axi_arprot(3'd0), .s_axi_arvalid(d_arvalid), .s_axi_arready(d_arready),
        .s_axi_rdata(d_rdata), .s_axi_rresp(d_rresp), .s_axi_rvalid(d_rvalid), .s_axi_rready(d_rready),
        .cpu_reset(cpu_reset), .ps_rst_override(ps_rst_override),
        .ila_ctrl(ila_ctrl), .ila_cpu(ila_cpu), .ila_trap(ila_trap), .retired(retired),
        .run_cycles(run_cycles), .tohost_retired(tohost_retired), .tohost_writes(tohost_writes),
        .first_tohost(first_tohost), .reset_releases(reset_releases), .free_cycles(free_cycles),
        .tohost_valid(tohost_valid), .led(led), .fetch_pc(imem_addr), .soc_aresetn(soc_rst_n),
        .pl_resetn0(1'b1));

    integer axi_err = 0;
    reg     aw_hs, w_hs;
    task axi_write(input [17:0] a, input [31:0] v);
        begin
            @(negedge clk); d_awaddr = a; d_awvalid = 1; d_wdata = v; d_wvalid = 1; d_bready = 1;
            while (d_awvalid || d_wvalid) begin
                @(posedge clk);
                aw_hs = d_awvalid && d_awready;       // sample both before changing anything
                w_hs  = d_wvalid && d_wready;
                #1;
                if (aw_hs) d_awvalid = 0;
                if (w_hs)  d_wvalid = 0;
            end
            while (!d_bvalid) @(posedge clk);
            if (d_bresp !== 2'b00) axi_err = axi_err + 1;
            @(negedge clk) d_bready = 0;
        end
    endtask
    task axi_read(input [17:0] a, output [31:0] v);
        begin
            @(negedge clk); d_araddr = a; d_arvalid = 1;
            @(posedge clk); while (!d_arready) @(posedge clk);
            #1 d_arvalid = 0; d_rready = 1;
            @(posedge clk); while (!d_rvalid) @(posedge clk);
            v = d_rdata;
            if (d_rresp !== 2'b00) axi_err = axi_err + 1;
            @(negedge clk) d_rready = 0;
        end
    endtask

    // ---- reference copy of every recorded cycle (same condition as the slave) ----
    reg [255:0] ref_rec [0:4095];
    integer     ref_n = 0;
    always @(posedge clk)
        if (dbg.rec) begin
            ref_rec[ref_n] = {2'b00, ila_ctrl[45:32], dbg.cyc, ila_cpu, retired, ila_ctrl[31:0]};
            ref_n = ref_n + 1;
        end
    always @(posedge clk) if (!soc_rst_n) ref_n = 0;

    // ---- test ----
    reg [8*256-1:0] hexfile, dumpfile;
    integer exp_retired, trace_start, fd, i, w, n, cyc0, nerr, run;
    reg [31:0] v, rec_w [0:7];
    reg [255:0] got;
    reg [8*64-1:0] msg;

    task wait_tohost;
        begin
            n = 0;
            while (tohost_writes == 0 && !halted && n < 200000) begin @(posedge clk); n = n + 1; end
            repeat (300) @(posedge clk);
        end
    endtask

    task check_run(input integer r);
        begin
            axi_read(18'h00010, v); $sformat(msg, "run %0d FIRST_TOHOST", r); tb_check(msg, v, 32'd1);
            axi_read(18'h00014, v); $sformat(msg, "run %0d TOHOST_WRITES", r); tb_check(msg, v, 32'd1);
            axi_read(18'h0000C, v); $sformat(msg, "run %0d STATUS tohost/halted/aresetn", r);
            tb_check(msg, v[3:0], 4'b1101);
            axi_read(18'h00034, v); $sformat(msg, "run %0d RESET_RELEASES", r); tb_check(msg, v, r);
            if (exp_retired > 0) begin
                axi_read(18'h00030, v); $sformat(msg, "run %0d TOHOST_RETIRED", r); tb_check(msg, v, exp_retired - 1);
            end
            axi_read(18'h00028, v);
            if (cyc0 < 0) cyc0 = v;
            else begin $sformat(msg, "run %0d RUN_CYCLES equal", r); tb_check(msg, v, cyc0); end
            $display("PERF: run %0d run_cycles=%0d", r, v);
        end
    endtask

    initial begin
        tb_init;
        if (!$value$plusargs("hex=%s", hexfile)) begin tb_fail("no +hex"); tb_finish; end
        if (!$value$plusargs("exp_retired=%d", exp_retired)) exp_retired = 0;
        if (!$value$plusargs("trace_start=%d", trace_start)) trace_start = 0;
        $readmemh(hexfile, cpu.u_soc.u_ram.mem);
        cyc0 = -1;
        repeat (10) @(posedge clk);
        #1 ps_rst_n = 1'b1;                      // "configuration done": CPU runs (CTRL = 0)
        wait_tohost;
        axi_read(18'h00000, v); tb_check("ID", v, 32'h5256_3641);
        axi_read(18'h00004, v); tb_check("VERSION", v, 32'h0001_0000);
        axi_read(18'h00088, v); tb_check("TRACE_DEPTH", v, 32'd4096);
        axi_write(18'h00044, 32'hA5C3_0F1E); axi_read(18'h00044, v); tb_check("SCRATCH", v, 32'hA5C3_0F1E);
        axi_write(18'h00000, 32'hFFFF_FFFF); axi_read(18'h00000, v); tb_check("ID read-only", v, 32'h5256_3641);
        check_run(1);
        // re-run through CTRL.cpu_reset with TRACE_START = S
        axi_write(18'h00080, trace_start);
        axi_write(18'h00008, 32'd1);
        repeat (20) @(posedge clk);
        tb_check("CPU held in reset", soc_rst_n, 1'b0);
        axi_write(18'h00008, 32'd0);
        wait_tohost;
        check_run(2);
        // trace read-back and comparison
        // the CPU keeps retiring in its final loop, so recording continues until the
        // buffer is full: the count read here may be just behind the reference
        axi_read(18'h00084, v);
        tb_check("TRACE_COUNT within 2 of the reference", (v <= ref_n) && (ref_n - v <= 2), 1'b1);
        nerr = 0;
        if (!$value$plusargs("dump=%s", dumpfile)) dumpfile = "";
        fd = (dumpfile != "") ? $fopen(dumpfile, "w") : 0;
        if (fd) begin
            $fdisplay(fd, "REG TRACE_START %08x", trace_start);
            $fdisplay(fd, "REG TRACE_COUNT %08x", v);
        end
        for (i = 0; i < v; i = i + 1) begin
            for (w = 0; w < 8; w = w + 1) axi_read(18'h20000 + 32 * i + 4 * w, rec_w[w]);
            got = {rec_w[7], rec_w[6], rec_w[5], rec_w[4], rec_w[3], rec_w[2], rec_w[1], rec_w[0]};
            if (got !== ref_rec[i]) begin
                nerr = nerr + 1;
                if (nerr <= 3) $display("ERROR: record %0d got %h exp %h", i, got, ref_rec[i]);
            end
            if (fd) $fdisplay(fd, "REC %08x %08x %08x %08x %08x %08x %08x %08x", rec_w[0], rec_w[1], rec_w[2],
                              rec_w[3], rec_w[4], rec_w[5], rec_w[6], rec_w[7]);
        end
        if (fd) $fclose(fd);
        tb_check("trace records equal the observed signals", nerr, 0);
        if (v > 0) begin
            axi_read(18'h20004, v); tb_check("first record = instruction TRACE_START", v, trace_start);
            axi_read(18'h20000, v); tb_check("first record is a retire", v[0], 1'b1);
        end
        tb_check("AXI error responses", axi_err, 0);
        tb_finish;
    end
endmodule

`default_nettype wire
