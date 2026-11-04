`timescale 1ns / 1ps
`default_nettype none

// tb_e3c_axil - AXI4-Lite unit test of the three E3c control pages (DSP, CNN,
// replay) inside emg_e3c_subsys, driven by tb_axil_master BFMs.
// Plusargs: +seed=S +order=0|1|2|3 (AW/W order, 3 = random) +bmax=N +rmax=N
// Checks: reset behaviour, ID/VERSION, first transaction after reset, read-only
// and reserved offsets, WSTRB, random RW register traffic against a model under
// back-pressure, exactly one side effect per write (auto-increment registers),
// rejection rules of the write ports, OKAY responses, protocol (tb_axil_monitor).

module tb_e3c_axil;
    localparam TB_TIMEOUT_NS = 50000000;
    `include "tb_check.vh"

    reg clk = 1'b0;
    always #5 clk = ~clk;
    reg aresetn = 1'b0;

    // ---- three masters ----
    `define AXIL_WIRES(p) \
        wire [11:0] p``_awaddr, p``_araddr; wire [31:0] p``_wdata, p``_rdata; wire [3:0] p``_wstrb; \
        wire [1:0] p``_bresp, p``_rresp; \
        wire p``_awvalid, p``_awready, p``_wvalid, p``_wready, p``_bvalid, p``_bready, \
             p``_arvalid, p``_arready, p``_rvalid, p``_rready;
    `AXIL_WIRES(rp)
    `AXIL_WIRES(ds)
    `AXIL_WIRES(cn)

    tb_axil_master #(.AW(12)) m_rp (.clk(clk), .awaddr(rp_awaddr), .awvalid(rp_awvalid), .awready(rp_awready),
        .wdata(rp_wdata), .wstrb(rp_wstrb), .wvalid(rp_wvalid), .wready(rp_wready), .bresp(rp_bresp),
        .bvalid(rp_bvalid), .bready(rp_bready), .araddr(rp_araddr), .arvalid(rp_arvalid), .arready(rp_arready),
        .rdata(rp_rdata), .rresp(rp_rresp), .rvalid(rp_rvalid), .rready(rp_rready));
    tb_axil_master #(.AW(12)) m_ds (.clk(clk), .awaddr(ds_awaddr), .awvalid(ds_awvalid), .awready(ds_awready),
        .wdata(ds_wdata), .wstrb(ds_wstrb), .wvalid(ds_wvalid), .wready(ds_wready), .bresp(ds_bresp),
        .bvalid(ds_bvalid), .bready(ds_bready), .araddr(ds_araddr), .arvalid(ds_arvalid), .arready(ds_arready),
        .rdata(ds_rdata), .rresp(ds_rresp), .rvalid(ds_rvalid), .rready(ds_rready));
    tb_axil_master #(.AW(12)) m_cn (.clk(clk), .awaddr(cn_awaddr), .awvalid(cn_awvalid), .awready(cn_awready),
        .wdata(cn_wdata), .wstrb(cn_wstrb), .wvalid(cn_wvalid), .wready(cn_wready), .bresp(cn_bresp),
        .bvalid(cn_bvalid), .bready(cn_bready), .araddr(cn_araddr), .arvalid(cn_arvalid), .arready(cn_arready),
        .rdata(cn_rdata), .rresp(cn_rresp), .rvalid(cn_rvalid), .rready(cn_rready));

    emg_e3c_subsys #(.REPLAY_DEPTH_LOG2(11), .REPLAY_FRAMES(16)) dut (
        .aclk(clk), .aresetn(aresetn),
        .s_axi_rep_awaddr(rp_awaddr), .s_axi_rep_awprot(3'd0), .s_axi_rep_awvalid(rp_awvalid), .s_axi_rep_awready(rp_awready),
        .s_axi_rep_wdata(rp_wdata), .s_axi_rep_wstrb(rp_wstrb), .s_axi_rep_wvalid(rp_wvalid), .s_axi_rep_wready(rp_wready),
        .s_axi_rep_bresp(rp_bresp), .s_axi_rep_bvalid(rp_bvalid), .s_axi_rep_bready(rp_bready),
        .s_axi_rep_araddr(rp_araddr), .s_axi_rep_arprot(3'd0), .s_axi_rep_arvalid(rp_arvalid), .s_axi_rep_arready(rp_arready),
        .s_axi_rep_rdata(rp_rdata), .s_axi_rep_rresp(rp_rresp), .s_axi_rep_rvalid(rp_rvalid), .s_axi_rep_rready(rp_rready),
        .s_axi_dsp_awaddr(ds_awaddr), .s_axi_dsp_awprot(3'd0), .s_axi_dsp_awvalid(ds_awvalid), .s_axi_dsp_awready(ds_awready),
        .s_axi_dsp_wdata(ds_wdata), .s_axi_dsp_wstrb(ds_wstrb), .s_axi_dsp_wvalid(ds_wvalid), .s_axi_dsp_wready(ds_wready),
        .s_axi_dsp_bresp(ds_bresp), .s_axi_dsp_bvalid(ds_bvalid), .s_axi_dsp_bready(ds_bready),
        .s_axi_dsp_araddr(ds_araddr), .s_axi_dsp_arprot(3'd0), .s_axi_dsp_arvalid(ds_arvalid), .s_axi_dsp_arready(ds_arready),
        .s_axi_dsp_rdata(ds_rdata), .s_axi_dsp_rresp(ds_rresp), .s_axi_dsp_rvalid(ds_rvalid), .s_axi_dsp_rready(ds_rready),
        .s_axi_cnn_awaddr(cn_awaddr), .s_axi_cnn_awprot(3'd0), .s_axi_cnn_awvalid(cn_awvalid), .s_axi_cnn_awready(cn_awready),
        .s_axi_cnn_wdata(cn_wdata), .s_axi_cnn_wstrb(cn_wstrb), .s_axi_cnn_wvalid(cn_wvalid), .s_axi_cnn_wready(cn_wready),
        .s_axi_cnn_bresp(cn_bresp), .s_axi_cnn_bvalid(cn_bvalid), .s_axi_cnn_bready(cn_bready),
        .s_axi_cnn_araddr(cn_araddr), .s_axi_cnn_arprot(3'd0), .s_axi_cnn_arvalid(cn_arvalid), .s_axi_cnn_arready(cn_arready),
        .s_axi_cnn_rdata(cn_rdata), .s_axi_cnn_rresp(cn_rresp), .s_axi_cnn_rvalid(cn_rvalid), .s_axi_cnn_rready(cn_rready),
        .tap_rep_valid(), .tap_rep_ready(), .tap_rep_data(), .tap_rep_user(), .tap_rep_last(), .tap_rep_rst(),
        .tap_dsp_valid(), .tap_dsp_ready(), .tap_dsp_data(), .tap_dsp_user(), .tap_dsp_last(), .tap_dsp_rst(),
        .tap_cnn_rst(), .tap_res_event(),
        .dbg_valid(), .dbg_layer(), .dbg_ch(), .dbg_step(), .dbg_seg(), .dbg_acc(), .dbg_act());

    // ---- AXI4-Lite protocol monitors (one per port) ----
    `define AXIL_MON(p, nm) \
        tb_axil_monitor #(.NAME(nm)) mon_``p (.aclk(clk), .aresetn(aresetn), \
            .awaddr({20'd0, p``_awaddr}), .awprot(3'd0), .awvalid(p``_awvalid), .awready(p``_awready), \
            .wdata(p``_wdata), .wstrb(p``_wstrb), .wvalid(p``_wvalid), .wready(p``_wready), \
            .bresp(p``_bresp), .bvalid(p``_bvalid), .bready(p``_bready), \
            .araddr({20'd0, p``_araddr}), .arprot(3'd0), .arvalid(p``_arvalid), .arready(p``_arready), \
            .rdata(p``_rdata), .rresp(p``_rresp), .rvalid(p``_rvalid), .rready(p``_rready),             .errors(), .n_aw(), .n_w(), .n_b(), .n_ar(), .n_r(), .n_berr(), .n_rerr());
    `AXIL_MON(rp, "replay")
    `AXIL_MON(ds, "dsp")
    `AXIL_MON(cn, "cnn")

    // ---- during reset no slave output may be active ----
    integer rst_viol = 0;
    always @(posedge clk)
        if (!aresetn && (rp_awready || rp_wready || rp_bvalid || rp_arready || rp_rvalid ||
                         ds_awready || ds_wready || ds_bvalid || ds_arready || ds_rvalid ||
                         cn_awready || cn_wready || cn_bvalid || cn_arready || cn_rvalid))
            rst_viol = rst_viol + 1;

    reg [31:0] v, model [0:3];
    integer i, k, order, bmax, rmax, n_err;
    reg [11:0] a;
    reg [31:0] d;
    reg [3:0]  s;
    reg [8*64-1:0] msg;

    task expect_rd(input integer port, input [11:0] addr, input [31:0] exp, input [8*48-1:0] what);
        begin
            case (port)
                0: m_rp.read(addr, v);
                1: m_ds.read(addr, v);
                default: m_cn.read(addr, v);
            endcase
            tb_check(what, v, exp);
        end
    endtask

    function [31:0] apply_strb(input [31:0] old, input [31:0] nw, input [3:0] st);
        integer b;
        begin
            apply_strb = old;
            for (b = 0; b < 4; b = b + 1) if (st[b]) apply_strb[8*b +: 8] = nw[8*b +: 8];
        end
    endfunction

    initial begin
        tb_init;
        if (!$value$plusargs("order=%d", order)) order = 3;
        if (!$value$plusargs("bmax=%d", bmax)) bmax = 3;
        if (!$value$plusargs("rmax=%d", rmax)) rmax = 3;
        m_rp.configure(order, bmax, rmax, tb_seed);
        m_ds.configure(order, bmax, rmax, tb_seed + 1);
        m_cn.configure(order, bmax, rmax, tb_seed + 2);
        repeat (8) @(posedge clk);
        #1 aresetn = 1'b1;
        repeat (2) @(posedge clk);                  // AXI: no master VALID in the first cycle after reset

        // first transaction after reset, ID / VERSION
        expect_rd(0, 12'h000, 32'h5250_4C31, "replay ID (first transaction)");
        expect_rd(1, 12'h000, 32'h4453_5031, "dsp ID (first transaction)");
        expect_rd(2, 12'h000, 32'h434E_4E31, "cnn ID (first transaction)");
        expect_rd(0, 12'h004, 32'h0001_0000, "replay VERSION");
        expect_rd(1, 12'h004, 32'h0001_0000, "dsp VERSION");
        expect_rd(2, 12'h004, 32'h0001_0000, "cnn VERSION");
        // reset values
        expect_rd(0, 12'h010, 32'd16, "replay NFRAMES reset = REPLAY_FRAMES");
        expect_rd(0, 12'h014, 32'd50000, "replay PERIOD reset");
        expect_rd(0, 12'h03C, 32'd2048, "replay DEPTH");
        expect_rd(1, 12'h010, 32'hD, "dsp SEC_EN reset");
        expect_rd(1, 12'h00C, 32'h1, "dsp STATUS reset (IDLE)");
        expect_rd(2, 12'h00C, 32'h0, "cnn STATUS reset");
        expect_rd(2, 12'h020, 32'h0, "cnn snapshot empty");

        // read-only and reserved offsets
        m_rp.write(12'h000, 32'hFFFF_FFFF, 4'hF); expect_rd(0, 12'h000, 32'h5250_4C31, "replay ID read-only");
        m_ds.write(12'h004, 32'hFFFF_FFFF, 4'hF); expect_rd(1, 12'h004, 32'h0001_0000, "dsp VERSION read-only");
        m_cn.write(12'h060, 32'h1234_5678, 4'hF); expect_rd(2, 12'h060, 32'h0, "cnn RESULT_CNT read-only");
        m_ds.write(12'h01C, 32'h5555_5555, 4'hF); expect_rd(1, 12'h01C, 32'h0, "dsp FRAMES_IN read-only");
        for (i = 0; i < 4; i = i + 1) begin
            a = (i == 0) ? 12'h0FC : (i == 1) ? 12'h400 : (i == 2) ? 12'h800 : 12'hFFC;
            m_rp.write(a, 32'hDEAD_BEEF, 4'hF); expect_rd(0, a, 32'h0, "replay reserved offset reads 0");
            m_ds.write(a, 32'hDEAD_BEEF, 4'hF); expect_rd(1, a, 32'h0, "dsp reserved offset reads 0");
            m_cn.write(a, 32'hDEAD_BEEF, 4'hF); expect_rd(2, a, 32'h0, "cnn reserved offset reads 0");
        end
        expect_rd(1, 12'h010, 32'hD, "dsp SEC_EN unchanged by reserved writes");

        // WSTRB on full 32-bit RW registers (replay SEQ_BASE, PERIOD, WDATA_LO/HI)
        model[0] = 32'd0; model[1] = 32'd50000; model[2] = 32'd0; model[3] = 32'd0;
        n_err = 0;
        for (i = 0; i < 400; i = i + 1) begin
            k = $random(tb_seed); if (k < 0) k = -k; k = k % 4;
            a = (k == 0) ? 12'h020 : (k == 1) ? 12'h014 : (k == 2) ? 12'h02C : 12'h030;
            d = $random(tb_seed);
            s = $random(tb_seed);
            if (($random(tb_seed) & 1) != 0) begin
                m_rp.write(a, d, s);
                model[k] = apply_strb(model[k], d, s);
            end else begin
                m_rp.read(a, v);
                if (v !== model[k]) begin
                    n_err = n_err + 1;
                    if (n_err <= 5) $display("ERROR: replay reg %h read %h model %h", a, v, model[k]);
                end
            end
        end
        tb_check("random replay RW register traffic vs model (WSTRB, AW/W order, back-pressure)", n_err, 0);
        // byte strobes on narrow fields
        m_ds.write(12'h010, 32'h0000_0003, 4'h0); expect_rd(1, 12'h010, 32'hD, "dsp SEC_EN, WSTRB = 0 ignored");
        m_ds.write(12'h010, 32'h0000_0003, 4'h1); expect_rd(1, 12'h010, 32'h3, "dsp SEC_EN, WSTRB byte 0");
        m_ds.write(12'h010, 32'h0000_000D, 4'h1);
        m_cn.write(12'h094, 32'h0000_1234, 4'h2); expect_rd(2, 12'h094, 32'h0000_1200, "cnn WADDR, WSTRB byte 1 only");
        m_cn.write(12'h094, 32'h0000_0000, 4'h3);

        // exactly one side effect per write: auto-increment registers
        m_ds.write(12'h014, 32'd2, 4'hF);
        for (i = 0; i < 6; i = i + 1) begin
            m_ds.write(12'h018, 32'h0000_1000 + i, 4'hF);
            m_ds.read(12'h00C, v);
            while (v[3]) m_ds.read(12'h00C, v);         // wait for the held write (core idle)
        end
        expect_rd(1, 12'h014, 32'd8, "dsp COEF_ADDR +1 per COEF_DATA write");
        expect_rd(1, 12'h018, 32'h0000_1005, "dsp COEF_DATA reads last written value");
        expect_rd(1, 12'h03C, 32'd0, "dsp COEF_REJ = 0");
        m_ds.write(12'h014, 32'd20, 4'hF); m_ds.write(12'h018, 32'h1, 4'hF);
        expect_rd(1, 12'h03C, 32'd1, "dsp COEF_DATA with COEF_ADDR > 19 rejected");
        m_ds.write(12'h014, 32'd0, 4'hF);
        // replay frame memory writes
        m_rp.write(12'h028, 32'd5, 4'hF);
        for (i = 0; i < 7; i = i + 1) m_rp.write(12'h034, 32'h41, 4'h1);
        expect_rd(0, 12'h028, 32'd12, "replay WADDR +1 per WFLAGS write");
        m_rp.write(12'h034, 32'h41, 4'h0);
        expect_rd(0, 12'h028, 32'd12, "replay WFLAGS with WSTRB[0] = 0 does nothing");
        m_rp.write(12'h010, 32'd16, 4'hF);
        m_rp.write(12'h008, 32'h1, 4'hF);                  // RUN (DSP disabled: frame stays valid)
        m_rp.write(12'h034, 32'h41, 4'h1);
        expect_rd(0, 12'h038, 32'd1, "replay frame write while RUN rejected");
        expect_rd(0, 12'h028, 32'd12, "replay WADDR unchanged by rejected write");
        expect_rd(0, 12'h00C, 32'h5, "replay STATUS ACTIVE + VALID (DSP not enabled)");
        m_rp.write(12'h008, 32'h4, 4'hF);                  // SOFT_RESET (consumers idle)
        expect_rd(0, 12'h00C, 32'h0, "replay STATUS after SOFT_RESET");
        expect_rd(0, 12'h038, 32'd0, "replay WREJ cleared by SOFT_RESET");
        // CNN parameter writes: rejected while ENABLE = 1, accepted (one each) while 0
        m_cn.write(12'h090, 32'd13, 4'hF); m_cn.write(12'h094, 32'd0, 4'hF);
        m_cn.write(12'h008, 32'h1, 4'hF);
        m_cn.write(12'h098, 32'h11, 4'hF);
        expect_rd(2, 12'h09C, 32'd1, "cnn WDATA while ENABLE = 1 rejected");
        expect_rd(2, 12'h094, 32'd0, "cnn WADDR unchanged by rejected write");
        m_cn.write(12'h008, 32'h0, 4'hF);
        for (i = 0; i < 3; i = i + 1) begin
            m_cn.write(12'h098, 32'h100 + i, 4'hF);
            m_cn.read(12'h00C, v);
            while (v[8]) m_cn.read(12'h00C, v);
        end
        expect_rd(2, 12'h094, 32'd3, "cnn WADDR +1 per accepted WDATA");
        tb_check("cnn fcb[0..2] written through the port", dut.u_cnn.u_cnn.fcb[1], 32'h101);
        tb_check("cnn fcb[2] written through the port", dut.u_cnn.u_cnn.fcb[2], 32'h102);
        // RESULT_ACK on an empty FIFO has no effect
        m_cn.write(12'h010, 32'h0, 4'hF);
        expect_rd(2, 12'h068, 32'd0, "cnn ACK_CNT unchanged by ACK with no result");
        // DSP CLEAR / SOFT_RESET are pulses (read 0)
        m_ds.write(12'h008, 32'h7, 4'hF); expect_rd(1, 12'h008, 32'h1, "dsp CTRL reads ENABLE only");
        m_ds.write(12'h008, 32'h0, 4'hF);

        tb_check("no slave activity during reset", rst_viol, 0);
        tb_check("BRESP errors", m_rp.bresp_err + m_ds.bresp_err + m_cn.bresp_err, 0);
        tb_check("RRESP errors", m_rp.rresp_err + m_ds.rresp_err + m_cn.rresp_err, 0);
        tb_check("protocol errors", mon_rp.errors + mon_ds.errors + mon_cn.errors, 0);
        $display("INFO: writes %0d reads %0d", m_rp.n_wr + m_ds.n_wr + m_cn.n_wr, m_rp.n_rd + m_ds.n_rd + m_cn.n_rd);
        tb_finish;
    end
endmodule

`default_nettype wire
