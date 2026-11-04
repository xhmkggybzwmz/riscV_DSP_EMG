`timescale 1ns / 1ps
`default_nettype none

// tb_e3c_soc - E3c system test: the real RV32I V1 runs a firmware image
// (firmware/rv32/e3c) that configures the replay source, DSP and CNN pages through
// CMI -> cmi2axil -> interconnect and reads the class-score snapshots.
//   default   (Icarus) rv32_soc_top #(E3C = 1) -> tb_axil_xbar (behavioural decoder)
//             -> soc_sysctrl + emg_e3c_subsys (replay BRAM initialised from
//             build/e3c/replay_*.hex, model from build/e3)
//   E3C_BD    (Vivado xsim) the block design e3c_bd: the same RTL behind the real
//             AMD AXI SmartConnect (4 master interfaces, 4 address segments)
// Independent checks (do not rely on the firmware):
//   - golden (+golden=1): every DSP output frame, conv/GAP/FC debug event and every
//     core result (scores, argmax, end, segment) against build/e3/vec/<case>, for the
//     first replay pass (e3c_golden.vh); later passes / reruns: results only
//   - program end: TOHOST written once with 1 (PASS), or for +exp_trap=1 a trap with
//     +exp_cause / +exp_tval, no TOHOST write and no AXI transaction at all
//   - AXI4-Lite protocol on the CPU's m_axi (tb_axil_monitor)
// Plusargs: +hex=<image> +vec=<golden case dir> +golden=0|1 +max_cycles=N
//           +exp_trap=1 +exp_cause=N +exp_tval=<hex> +exp_results=N
//           +period_check=P   real-time measurements and stream checks (dv/e3c/e3c_timing.vh)

`ifdef E3C_BD
  `define SOC u_dut.e3c_bd_i.cpu_subsys.inst
  `define SYS u_dut.e3c_bd_i.sysctrl.inst
  `define E3C u_dut.e3c_bd_i.e3c.inst
`else
  `define SOC u_soc
  `define SYS u_sys
  `define E3C u_e3c
`endif

module tb_e3c_soc;
    localparam TB_TIMEOUT_NS = 1000000000;   // 2 kHz run: 1400 x 50000 cycles = 0.7 s
    `include "tb_check.vh"
    `include "e3c_paths.vh"

    reg clk = 1'b0;
    always #5 clk = ~clk;
    reg aresetn = 1'b0;

`ifdef E3C_BD
    e3c_bd_wrapper u_dut (.aclk(clk), .aresetn(aresetn));
`else
    wire [31:0] awaddr, wdata, araddr, rdata;
    wire [2:0]  awprot, arprot;
    wire [3:0]  wstrb;
    wire [1:0]  bresp, rresp;
    wire        awvalid, awready, wvalid, wready, bvalid, bready, arvalid, arready, rvalid, rready;
    rv32_soc_top #(.E3C(1)) u_soc (
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
        .dbg_dmem_rsp_rdata(), .dbg_dmem_rsp_err());

    // slaves: 0 SYSCTRL, 1 DSP, 2 CNN, 3 replay (soc_addr_map.vh)
    wire [127:0] x_awaddr, x_wdata, x_araddr, x_rdata;
    wire [11:0]  x_awprot, x_arprot;
    wire [15:0]  x_wstrb;
    wire [7:0]   x_bresp, x_rresp;
    wire [3:0]   x_awvalid, x_awready, x_wvalid, x_wready, x_bvalid, x_bready, x_arvalid, x_arready, x_rvalid, x_rready;
    tb_axil_xbar #(.N(4),
        .BASE({32'h1000_6000, 32'h1000_5000, 32'h1000_2000, 32'h1000_0000}),
        .SIZE({32'h0000_1000, 32'h0000_1000, 32'h0000_1000, 32'h0000_1000})) u_xbar (
        .clk(clk), .aresetn(aresetn),
        .s_awaddr(awaddr), .s_awprot(awprot), .s_awvalid(awvalid), .s_awready(awready),
        .s_wdata(wdata), .s_wstrb(wstrb), .s_wvalid(wvalid), .s_wready(wready),
        .s_bresp(bresp), .s_bvalid(bvalid), .s_bready(bready),
        .s_araddr(araddr), .s_arprot(arprot), .s_arvalid(arvalid), .s_arready(arready),
        .s_rdata(rdata), .s_rresp(rresp), .s_rvalid(rvalid), .s_rready(rready),
        .m_awaddr(x_awaddr), .m_awprot(x_awprot), .m_awvalid(x_awvalid), .m_awready(x_awready),
        .m_wdata(x_wdata), .m_wstrb(x_wstrb), .m_wvalid(x_wvalid), .m_wready(x_wready),
        .m_bresp(x_bresp), .m_bvalid(x_bvalid), .m_bready(x_bready),
        .m_araddr(x_araddr), .m_arprot(x_arprot), .m_arvalid(x_arvalid), .m_arready(x_arready),
        .m_rdata(x_rdata), .m_rresp(x_rresp), .m_rvalid(x_rvalid), .m_rready(x_rready));

    wire [7:0] led; wire th_valid, th_wr; wire [31:0] th_value;
    soc_sysctrl u_sys (
        .aclk(clk), .aresetn(aresetn),
        .s_axi_awaddr(x_awaddr[11:0]), .s_axi_awprot(x_awprot[2:0]), .s_axi_awvalid(x_awvalid[0]), .s_axi_awready(x_awready[0]),
        .s_axi_wdata(x_wdata[31:0]), .s_axi_wstrb(x_wstrb[3:0]), .s_axi_wvalid(x_wvalid[0]), .s_axi_wready(x_wready[0]),
        .s_axi_bresp(x_bresp[1:0]), .s_axi_bvalid(x_bvalid[0]), .s_axi_bready(x_bready[0]),
        .s_axi_araddr(x_araddr[11:0]), .s_axi_arprot(x_arprot[2:0]), .s_axi_arvalid(x_arvalid[0]), .s_axi_arready(x_arready[0]),
        .s_axi_rdata(x_rdata[31:0]), .s_axi_rresp(x_rresp[1:0]), .s_axi_rvalid(x_rvalid[0]), .s_axi_rready(x_rready[0]),
        .led(led), .tohost_valid(th_valid), .tohost_value(th_value), .tohost_wr(th_wr));

    `define XS(i) \
        .s_axi_``i``_awaddr(x_awaddr[32*IDX_``i +: 12]), .s_axi_``i``_awprot(x_awprot[3*IDX_``i +: 3]), \
        .s_axi_``i``_awvalid(x_awvalid[IDX_``i]), .s_axi_``i``_awready(x_awready[IDX_``i]), \
        .s_axi_``i``_wdata(x_wdata[32*IDX_``i +: 32]), .s_axi_``i``_wstrb(x_wstrb[4*IDX_``i +: 4]), \
        .s_axi_``i``_wvalid(x_wvalid[IDX_``i]), .s_axi_``i``_wready(x_wready[IDX_``i]), \
        .s_axi_``i``_bresp(x_bresp[2*IDX_``i +: 2]), .s_axi_``i``_bvalid(x_bvalid[IDX_``i]), .s_axi_``i``_bready(x_bready[IDX_``i]), \
        .s_axi_``i``_araddr(x_araddr[32*IDX_``i +: 12]), .s_axi_``i``_arprot(x_arprot[3*IDX_``i +: 3]), \
        .s_axi_``i``_arvalid(x_arvalid[IDX_``i]), .s_axi_``i``_arready(x_arready[IDX_``i]), \
        .s_axi_``i``_rdata(x_rdata[32*IDX_``i +: 32]), .s_axi_``i``_rresp(x_rresp[2*IDX_``i +: 2]), \
        .s_axi_``i``_rvalid(x_rvalid[IDX_``i]), .s_axi_``i``_rready(x_rready[IDX_``i])
    localparam IDX_dsp = 1, IDX_cnn = 2, IDX_rep = 3;
    emg_e3c_subsys #(.REPLAY_DEPTH_LOG2(11), .REPLAY_FRAMES(`E3C_REPLAY_FRAMES), .REPLAY_FILE(`E3C_REPLAY_FILE),
        .DSP_COEF_FILE(`DSP_COEF_FILE),
        .W1_FILE(`W1_FILE), .B1_FILE(`B1_FILE), .M1_FILE(`M1_FILE), .SH1_FILE(`SH1_FILE),
        .W2_FILE(`W2_FILE), .B2_FILE(`B2_FILE), .M2_FILE(`M2_FILE), .SH2_FILE(`SH2_FILE),
        .W3_FILE(`W3_FILE), .B3_FILE(`B3_FILE), .M3_FILE(`M3_FILE), .SH3_FILE(`SH3_FILE),
        .FCW_FILE(`FCW_FILE), .FCB_FILE(`FCB_FILE)) u_e3c (
        .aclk(clk), .aresetn(aresetn),
        `XS(rep), `XS(dsp), `XS(cnn),
        .tap_rep_valid(), .tap_rep_ready(), .tap_rep_data(), .tap_rep_user(), .tap_rep_last(), .tap_rep_rst(),
        .tap_dsp_valid(), .tap_dsp_ready(), .tap_dsp_data(), .tap_dsp_user(), .tap_dsp_last(), .tap_dsp_rst(),
        .tap_cnn_rst(), .tap_res_event(),
        .dbg_valid(), .dbg_layer(), .dbg_ch(), .dbg_step(), .dbg_seg(), .dbg_acc(), .dbg_act());
`endif

    // ---------------- observation (hierarchical) ----------------
    wire        halted       = `SOC.halted;
    wire [3:0]  trap_cause   = `SOC.trap_cause;
    wire [31:0] trap_tval    = `SOC.trap_tval;
    wire        tohost_wr    = `SYS.tohost_wr;
    wire [31:0] tohost_value = `SYS.tohost_value;
    wire        dbg_valid    = `E3C.dbg_valid;
    wire [2:0]  dbg_layer    = `E3C.dbg_layer;
    wire [5:0]  dbg_ch       = `E3C.dbg_ch;
    wire [23:0] dbg_step     = `E3C.dbg_step;
    wire [7:0]  dbg_seg      = `E3C.dbg_seg;
    wire [31:0] dbg_acc      = `E3C.dbg_acc;
    wire [7:0]  dbg_act      = `E3C.dbg_act;
    wire        res_event    = `E3C.tap_res_event;
    wire        dsp_hs       = `E3C.tap_dsp_valid & `E3C.tap_dsp_ready;
    wire [127:0] dsp_data    = `E3C.tap_dsp_data;
    wire        cnn_rst      = `E3C.tap_cnn_rst;

    wire [31:0] m_awaddr = `SOC.m_axi_awaddr, m_wdata = `SOC.m_axi_wdata, m_araddr = `SOC.m_axi_araddr,
                m_rdata = `SOC.m_axi_rdata;
    wire [31:0] mon_err, n_aw, n_w, n_b, n_ar, n_r, n_berr, n_rerr;
    tb_axil_monitor #(.NAME("cpu m_axi")) u_mon (.aclk(clk), .aresetn(aresetn),
        .awaddr(m_awaddr), .awprot(`SOC.m_axi_awprot), .awvalid(`SOC.m_axi_awvalid), .awready(`SOC.m_axi_awready),
        .wdata(m_wdata), .wstrb(`SOC.m_axi_wstrb), .wvalid(`SOC.m_axi_wvalid), .wready(`SOC.m_axi_wready),
        .bresp(`SOC.m_axi_bresp), .bvalid(`SOC.m_axi_bvalid), .bready(`SOC.m_axi_bready),
        .araddr(m_araddr), .arprot(`SOC.m_axi_arprot), .arvalid(`SOC.m_axi_arvalid), .arready(`SOC.m_axi_arready),
        .rdata(m_rdata), .rresp(`SOC.m_axi_rresp), .rvalid(`SOC.m_axi_rvalid), .rready(`SOC.m_axi_rready),
        .errors(mon_err), .n_aw(n_aw), .n_w(n_w), .n_b(n_b), .n_ar(n_ar), .n_r(n_r), .n_berr(n_berr), .n_rerr(n_rerr));

    // golden checks: first replay pass after the first CNN start only
    reg  golden_en = 1'b0;
    integer n_dsp_hs = 0, n_res = 0, n_cnn_rst = 0, res_errs = 0, rk;
    always @(posedge clk) if (dsp_hs) n_dsp_hs <= n_dsp_hs + 1;
    wire g_check_on;
    `include "e3c_golden.vh"
    assign g_check_on = golden_en && n_dsp_hs <= g_nframes;

    // every core result (all passes): scores / argmax / end / segment vs golden (cyclic)
    always @(posedge clk) if (golden_en && res_event) begin
        rk = n_res % g_nlog;
        if (`E3C.u_cnn.res_scores !== g_log[rk][191:0] || `E3C.u_cnn.res_argmax !== g_dec[rk][3:0] ||
            `E3C.u_cnn.res_end !== g_dec[rk][63:40]) begin
            res_errs = res_errs + 1;
            if (res_errs <= 5) $display("ERROR: core result %0d differs from golden %0d (t=%0t)", n_res, rk, $time);
        end
        n_res = n_res + 1;
    end
    // a CNN soft reset restarts the result numbering of the next run
    reg cnn_rst_q = 1'b0;
    always @(posedge clk) begin
        cnn_rst_q <= cnn_rst;
        if (aresetn && cnn_rst && !cnn_rst_q) n_cnn_rst = n_cnn_rst + 1;
    end

    `include "e3c_timing.vh"

    integer tohost_events = 0;
    always @(posedge clk) if (aresetn && tohost_wr) tohost_events = tohost_events + 1;

    reg [8*512-1:0] hexfile, vec;
    integer max_cycles, exp_trap, exp_cause, exp_results, use_golden, n;
    reg [31:0] exp_tval;
    initial begin
        tb_init;
        if (!$value$plusargs("hex=%s", hexfile)) begin tb_fail("no +hex"); tb_finish; end
        if (!$value$plusargs("max_cycles=%d", max_cycles)) max_cycles = 5000000;
        if (!$value$plusargs("exp_trap=%d", exp_trap)) exp_trap = 0;
        if (!$value$plusargs("exp_cause=%d", exp_cause)) exp_cause = 0;
        if (!$value$plusargs("exp_tval=%h", exp_tval)) exp_tval = 32'h0;
        if (!$value$plusargs("exp_results=%d", exp_results)) exp_results = -1;
        if (!$value$plusargs("golden=%d", use_golden)) use_golden = 0;
        if (use_golden) begin
            if (!$value$plusargs("vec=%s", vec)) begin tb_fail("+golden needs +vec"); tb_finish; end
            g_load(vec, 1);
            golden_en = 1'b1;
        end
        $readmemh(hexfile, `SOC.u_ram.mem);
        repeat (10) @(posedge clk);
        #1 aresetn = 1'b1;
        n = 0;
        while (tohost_events == 0 && !halted && n < max_cycles) begin @(posedge clk); n = n + 1; end
        repeat (200) @(posedge clk);
        if (exp_trap) begin
            tb_check("program ended in a trap", halted, 1'b1);
            tb_check("trap cause", trap_cause, exp_cause);
            tb_check("trap tval", trap_tval, exp_tval);
            tb_check("no TOHOST write", tohost_events, 0);
            tb_check("no AXI AW handshake", n_aw, 0);
            tb_check("no AXI W handshake", n_w, 0);
            tb_check("no AXI AR handshake", n_ar, 0);
        end else begin
            tb_check("no trap", halted, 1'b0);
            if (halted) $display("ERROR: trap cause=%0d pc=%h tval=%h", trap_cause, `SOC.trap_pc, trap_tval);
            tb_check("TOHOST written once", tohost_events, 1);
            tb_check("TOHOST = 1 (PASS)", tohost_value, 32'd1);
            if (tohost_value != 1 && tohost_value[0])
                $display("ERROR: firmware reported failing check #%0d", tohost_value >> 1);
        end
        if (use_golden) begin
            if (exp_results >= 0) tb_check("core results", n_res, exp_results);
            tb_check("core results == golden", res_errs, 0);
            tb_check("golden mismatches (DSP frames, layers)", g_errs, 0);
            tb_check("DSP frames compared (first pass)", pd, g_ndsp);
            tb_check("FC vectors compared (first pass)", pf, g_nlog);
        end
        if (tm_period > 0) tm_report((exp_results >= 0) ? exp_results : 11);
        tb_check("AXI protocol errors (cpu m_axi)", mon_err, 0);
        tb_check("AXI error responses", n_berr + n_rerr, 0);
        $display("PERF: cycles=%0d axi aw=%0d ar=%0d results=%0d", n, n_aw, n_ar, n_res);
        tb_finish;
    end
endmodule

`default_nettype wire
