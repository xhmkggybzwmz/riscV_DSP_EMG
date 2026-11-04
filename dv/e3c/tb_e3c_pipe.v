`timescale 1ns / 1ps
`default_nettype none

// tb_e3c_pipe - E3c data path through the control pages (no CPU): replay -> DSP ->
// CNN inside emg_e3c_subsys, configured and read by AXI4-Lite BFMs, checked bit
// by bit against one build/e3/vec/chain_* case (dv/e3c/e3c_golden.vh: every DSP
// frame, every conv1..3 activation/accumulator, GAP sum, FC score) and every
// result snapshot read over AXI (index, seq, end, ts, segment, argmax, scores).
//
// Plusargs:
//   +vec=<case dir> +acc=0|1 (compare accumulators) +seq_base=N
//   +period=P       replay PERIOD (0 = flow); +interval_check=1 checks that frame
//                   handshakes are exactly P cycles apart (no back-pressure from the core)
//   +nframes=N      frames to play (default: all of the case)
//   +noack=1        never acknowledge during the run: FIFO keeps results 1..4, the rest
//                   is dropped (DROP_CNT), the snapshot must not change while new
//                   results arrive; afterwards the kept results are read and acked
//   +rerun=1        after the run: SOFT_RESET replay/DSP/CNN, run again, same results
//   +loops=L        LOOP mode, L passes; results of pass p: segment + p*nseg,
//                   seq/ts + p*nframes (layer checks for pass 0 only)
//   +coefport=1     (compile with NO_COEF_INIT) DSP starts with zero coefficients,
//                   the TB writes all 20 through COEF_ADDR/COEF_DATA first
//   +order= +bmax= +rmax=  AXI BFM behaviour; +seed=S
// Also: ESI protocol monitors on both links, statistics registers at the end.

module tb_e3c_pipe;
    localparam TB_TIMEOUT_NS = 1000000000;   // 2 kHz run: 1400 x 50000 cycles = 0.7 s
    `include "tb_check.vh"
    `include "model_paths.vh"

    reg clk = 1'b0;
    always #5 clk = ~clk;
    reg aresetn = 1'b0;

    `define AXIL_WIRES(p) \
        wire [11:0] p``_awaddr, p``_araddr; wire [31:0] p``_wdata, p``_rdata; wire [3:0] p``_wstrb; \
        wire [1:0] p``_bresp, p``_rresp; \
        wire p``_awvalid, p``_awready, p``_wvalid, p``_wready, p``_bvalid, p``_bready, \
             p``_arvalid, p``_arready, p``_rvalid, p``_rready;
    `AXIL_WIRES(rp)
    `AXIL_WIRES(ds)
    `AXIL_WIRES(cn)
    `define AXIL_M(p) \
        tb_axil_master #(.AW(12)) m_``p (.clk(clk), .awaddr(p``_awaddr), .awvalid(p``_awvalid), .awready(p``_awready), \
            .wdata(p``_wdata), .wstrb(p``_wstrb), .wvalid(p``_wvalid), .wready(p``_wready), .bresp(p``_bresp), \
            .bvalid(p``_bvalid), .bready(p``_bready), .araddr(p``_araddr), .arvalid(p``_arvalid), .arready(p``_arready), \
            .rdata(p``_rdata), .rresp(p``_rresp), .rvalid(p``_rvalid), .rready(p``_rready));
    `AXIL_M(rp)
    `AXIL_M(ds)
    `AXIL_M(cn)

`ifdef NO_COEF_INIT
    localparam COEF = "";
`else
    localparam COEF = `DSP_COEF_FILE;
`endif

    wire         r_valid, r_ready, r_last, r_rst, d_valid, d_ready, d_last, d_rst, c_rst, res_event;
    wire [127:0] r_data, dsp_data;
    wire [79:0]  r_user, d_user;
    wire         dbg_valid;
    wire [2:0]   dbg_layer;
    wire [5:0]   dbg_ch;
    wire [23:0]  dbg_step;
    wire [7:0]   dbg_seg, dbg_act;
    wire [31:0]  dbg_acc;

    emg_e3c_subsys #(.REPLAY_DEPTH_LOG2(11), .REPLAY_FRAMES(0), .DSP_COEF_FILE(COEF),
        .W1_FILE(`W1_FILE), .B1_FILE(`B1_FILE), .M1_FILE(`M1_FILE), .SH1_FILE(`SH1_FILE),
        .W2_FILE(`W2_FILE), .B2_FILE(`B2_FILE), .M2_FILE(`M2_FILE), .SH2_FILE(`SH2_FILE),
        .W3_FILE(`W3_FILE), .B3_FILE(`B3_FILE), .M3_FILE(`M3_FILE), .SH3_FILE(`SH3_FILE),
        .FCW_FILE(`FCW_FILE), .FCB_FILE(`FCB_FILE)) dut (
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
        .tap_rep_valid(r_valid), .tap_rep_ready(r_ready), .tap_rep_data(r_data), .tap_rep_user(r_user),
        .tap_rep_last(r_last), .tap_rep_rst(r_rst),
        .tap_dsp_valid(d_valid), .tap_dsp_ready(d_ready), .tap_dsp_data(dsp_data), .tap_dsp_user(d_user),
        .tap_dsp_last(d_last), .tap_dsp_rst(d_rst), .tap_cnn_rst(c_rst), .tap_res_event(res_event),
        .dbg_valid(dbg_valid), .dbg_layer(dbg_layer), .dbg_ch(dbg_ch), .dbg_step(dbg_step), .dbg_seg(dbg_seg),
        .dbg_acc(dbg_acc), .dbg_act(dbg_act));

    // ESI protocol on both links; a link's reset = reset of either endpoint
    wire [31:0] e1_err, e1_hs, e2_err, e2_hs;
    esi_monitor #(.NAME("replay->dsp")) mon_e1 (.clk(clk), .rst(r_rst | d_rst), .tvalid(r_valid), .tready(r_ready),
        .tdata(r_data), .tuser(r_user), .tlast(r_last), .errors(e1_err), .handshakes(e1_hs));
    esi_monitor #(.NAME("dsp->cnn")) mon_e2 (.clk(clk), .rst(d_rst | c_rst), .tvalid(d_valid), .tready(d_ready),
        .tdata(dsp_data), .tuser(d_user), .tlast(d_last), .errors(e2_err), .handshakes(e2_hs));

    // layer/DSP checks cover pass 0 only: in LOOP mode they stop once the CNN has accepted
    // nframes frames (the dbg events of pass 0 all happen before that handshake)
    reg  g_check_en = 1'b1;
    integer n_dsp_hs = 0;
    wire dsp_hs = d_valid & d_ready;
    always @(posedge clk) if (dsp_hs) n_dsp_hs <= n_dsp_hs + 1;
    integer loops;
    wire g_check_on;
    `include "e3c_golden.vh"
    assign g_check_on = g_check_en && (loops <= 1 || n_dsp_hs <= g_nframes);

    // frame interval (real-time mode)
    integer cyc = 0, t_last = -1, iv_bad = 0, iv_n = 0, period, interval_check;
    always @(posedge clk) begin
        cyc = cyc + 1;
        if (r_valid && r_ready) begin
            if (interval_check && t_last >= 0 && (cyc - t_last) != period) iv_bad = iv_bad + 1;
            if (t_last >= 0) iv_n = iv_n + 1;
            t_last = cyc;
        end
    end
    integer n_res_event = 0;
    always @(posedge clk) if (res_event) n_res_event = n_res_event + 1;

    reg [8*512-1:0] vec, fname;
    reg [31:0] v, w, snap [0:15], cf [0:19];
    integer i, j, k, nfr, seq_base, noack, rerun, coefport, acc, pass, got, n_exp, fidx, segoff, ev_base;
    reg [8*64-1:0] msg;

    // CNN register offsets
    localparam C_CTRL = 12'h008, C_STATUS = 12'h00C, C_ACK = 12'h010, C_INDEX = 12'h020, C_SEQ = 12'h024,
               C_END = 12'h028, C_TS = 12'h02C, C_SEG = 12'h030, C_ARG = 12'h034, C_LAT = 12'h038,
               C_SCORE = 12'h040, C_RCNT = 12'h060, C_DROP = 12'h064, C_ACKC = 12'h068, C_FRAMES = 12'h06C;

    task read_snapshot;                     // fields 0 index 1 seq 2 end 3 ts 4 seg 5 arg 6 lat 7..12 scores
        begin
            m_cn.read(C_INDEX, snap[0]); m_cn.read(C_SEQ, snap[1]); m_cn.read(C_END, snap[2]);
            m_cn.read(C_TS, snap[3]);    m_cn.read(C_SEG, snap[4]); m_cn.read(C_ARG, snap[5]);
            m_cn.read(C_LAT, snap[6]);
            for (k = 0; k < 6; k = k + 1) m_cn.read(C_SCORE + 4 * k, snap[7 + k]);
        end
    endtask

    // compare the snapshot with golden result r of pass p, core result number idx
    task check_result(input integer r, input integer p, input integer idx);
        begin
            segoff = p * g_nseg;
            fidx = seg_start[g_dec[r][71:64]] + g_dec[r][63:40] + p * nfr;
            if (snap[0] !== idx) begin $sformat(msg, "result %0d.%0d RES_INDEX", p, r); tb_check(msg, snap[0], idx); end
            if (snap[1] !== seq_base + fidx) begin $sformat(msg, "result %0d.%0d RES_SEQ", p, r); tb_check(msg, snap[1], seq_base + fidx); end
            if (snap[2] !== g_dec[r][63:40]) begin $sformat(msg, "result %0d.%0d RES_END", p, r); tb_check(msg, snap[2], g_dec[r][63:40]); end
            if (snap[3] !== fidx) begin $sformat(msg, "result %0d.%0d RES_TS", p, r); tb_check(msg, snap[3], fidx); end
            if (snap[4] !== (g_dec[r][71:64] + segoff) % 256) begin $sformat(msg, "result %0d.%0d RES_SEG", p, r); tb_check(msg, snap[4], (g_dec[r][71:64] + segoff) % 256); end
            if (snap[5] !== g_dec[r][3:0]) begin $sformat(msg, "result %0d.%0d ARGMAX", p, r); tb_check(msg, snap[5], g_dec[r][3:0]); end
            for (k = 0; k < 6; k = k + 1)
                if (snap[7 + k] !== g_log[r][32 * k +: 32]) begin
                    $sformat(msg, "result %0d.%0d SCORE%0d", p, r, k); tb_check(msg, snap[7 + k], g_log[r][32 * k +: 32]);
                end
            if (snap[6] < 32'd100 || snap[6] > 32'd20000) begin
                $sformat(msg, "result %0d.%0d LAT_CYCLES plausible", p, r); tb_check(msg, 0, 1);
            end
            got = got + 1;
        end
    endtask

    task wait_pending;
        begin
            m_cn.read(C_STATUS, v);
            while (!v[0]) begin
                repeat (50) @(posedge clk);
                m_cn.read(C_STATUS, v);
            end
        end
    endtask

    task configure_and_run(input integer lp);
        begin
            m_rp.write(12'h010, nfr, 4'hF);
            m_rp.write(12'h014, period, 4'hF);
            m_rp.write(12'h020, seq_base, 4'hF);
            m_ds.write(12'h010, 32'hD, 4'hF);
            m_ds.write(12'h008, 32'h1, 4'hF);                  // DSP ENABLE
            m_cn.write(C_CTRL, 32'h1, 4'hF);                   // CNN ENABLE
            m_rp.write(12'h008, (lp > 1) ? 32'h3 : 32'h1, 4'hF);  // RUN (+LOOP)
        end
    endtask

    task soft_reset_all;
        begin
            m_rp.write(12'h008, 32'h4, 4'hF);
            m_ds.write(12'h008, 32'h2, 4'hF);
            m_cn.write(C_CTRL, 32'h2, 4'hF);
        end
    endtask

    initial begin
        tb_init;
        if (!$value$plusargs("vec=%s", vec)) begin tb_fail("no +vec"); tb_finish; end
        if (!$value$plusargs("acc=%d", acc)) acc = 0;
        if (!$value$plusargs("seq_base=%d", seq_base)) seq_base = 32'h0001_0000;
        if (!$value$plusargs("period=%d", period)) period = 0;
        if (!$value$plusargs("interval_check=%d", interval_check)) interval_check = 0;
        if (!$value$plusargs("noack=%d", noack)) noack = 0;
        if (!$value$plusargs("rerun=%d", rerun)) rerun = 0;
        if (!$value$plusargs("loops=%d", loops)) loops = 1;
        if (!$value$plusargs("coefport=%d", coefport)) coefport = 0;
        if (!$value$plusargs("order=%d", i)) i = 3;
        if (!$value$plusargs("bmax=%d", j)) j = 2;
        if (!$value$plusargs("rmax=%d", k)) k = 2;
        m_rp.configure(i, j, k, tb_seed); m_ds.configure(i, j, k, tb_seed + 1); m_cn.configure(i, j, k, tb_seed + 2);
        g_load(vec, acc);
        if (!$value$plusargs("nframes=%d", nfr)) nfr = g_nframes;
        n_exp = 0;                                         // results whose window ends inside nfr frames
        for (i = 0; i < g_nlog; i = i + 1)
            if (seg_start[g_dec[i][71:64]] + g_dec[i][63:40] < nfr) n_exp = n_exp + 1;
        repeat (8) @(posedge clk);
        #1 aresetn = 1'b1;
        repeat (4) @(posedge clk);

        // frame memory through the AXI write port: {flags[7:0], lane3..lane0}
        m_rp.write(12'h028, 32'd0, 4'hF);
        for (i = 0; i < nfr; i = i + 1) begin
            m_rp.write(12'h02C, frames[i][31:0], 4'hF);
            m_rp.write(12'h030, frames[i][63:32], 4'hF);
            m_rp.write(12'h034, {24'd0, frames[i][135:128]}, 4'h1);
        end
        tb_check("replay WADDR after loading", dut.u_rep.waddr, nfr % 2048);
        if (coefport) begin
            $readmemh(`DSP_COEF_FILE, cf);
            m_ds.write(12'h014, 32'd0, 4'hF);
            for (i = 0; i < 20; i = i + 1) m_ds.write(12'h018, {{14{cf[i][17]}}, cf[i][17:0]}, 4'hF);
            m_ds.read(12'h00C, v);
            while (v[3]) m_ds.read(12'h00C, v);
            for (i = 0; i < 20; i = i + 1)
                if (dut.u_dsp.u_dsp.coef[i] !== cf[i][17:0]) begin
                    $sformat(msg, "DSP coefficient %0d written through the port", i);
                    tb_check(msg, dut.u_dsp.u_dsp.coef[i], cf[i][17:0]);
                end
        end

        for (pass = 0; pass <= rerun; pass = pass + 1) begin
            if (pass > 0) begin                            // re-run after SOFT_RESET of all three pages
                g_check_en = 1'b0;
                soft_reset_all;
                repeat (5) @(posedge clk);
            end
            got = 0;
            ev_base = n_res_event;
            configure_and_run(loops);
            if (noack) begin
                // never ack: wait until all results are produced, snapshot must stay result #1
                wait_pending;
                read_snapshot;
                for (j = 0; j < 13; j = j + 1) cf[j] = snap[j];
                while (n_res_event - ev_base < n_exp * loops) begin
                    @(posedge clk);
                    if (res_event) begin
                        repeat (3) @(posedge clk);
                        read_snapshot;
                        for (i = 0; i < 13; i = i + 1)
                            if (snap[i] !== cf[i]) begin
                                $sformat(msg, "snapshot field %0d unchanged while results arrive", i);
                                tb_check(msg, snap[i], cf[i]);
                            end
                    end
                end
                m_cn.read(C_DROP, v);
                tb_check("DROP_CNT = results - FIFO depth", v, (n_exp * loops > 4) ? n_exp * loops - 4 : 0);
                for (i = 0; i < 4 && i < n_exp * loops; i = i + 1) begin
                    read_snapshot;
                    check_result(i % n_exp, i / n_exp, i + 1);
                    m_cn.write(C_ACK, 32'd0, 4'hF);
                end
                m_cn.read(C_STATUS, v);
                tb_check("FIFO empty after acking the kept results", v[0], 1'b0);
            end else begin
                for (i = 0; i < n_exp * loops; i = i + 1) begin
                    wait_pending;
                    read_snapshot;
                    check_result(i % n_exp, i / n_exp, i + 1);
                    m_cn.write(C_ACK, 32'd0, 4'hF);
                end
                m_cn.read(C_DROP, v); tb_check("DROP_CNT", v, 0);
                m_cn.read(C_ACKC, v); tb_check("ACK_CNT", v, n_exp * loops);
            end
            // wait for the stream to drain, then statistics
            m_rp.read(12'h00C, v);
            while (loops == 1 && (!v[1] || v[2])) begin repeat (100) @(posedge clk); m_rp.read(12'h00C, v); end
            if (loops > 1) begin
                while (dut.u_rep.frame_cnt < nfr * loops) @(posedge clk);
                m_rp.write(12'h008, 32'h0, 4'hF);          // stop
            end
            repeat (12000) @(posedge clk);
            m_rp.read(12'h018, v); tb_check("replay FRAME_CNT", (loops > 1) ? (v >= nfr * loops) : (v == nfr), 1'b1);
            m_rp.read(12'h01C, v);
            if (period == 0 || interval_check) tb_check("replay LATE_CNT", v, 0);
            if (loops == 1) begin
                m_ds.read(12'h01C, v); tb_check("DSP FRAMES_IN", v, nfr);
                m_ds.read(12'h020, v); tb_check("DSP FRAMES_OUT", v, nfr);
                m_ds.read(12'h038, v); tb_check("DSP LAST_SEQ", v, seq_base + nfr - 1);
                m_cn.read(C_FRAMES, v); tb_check("CNN FRAMES", v, nfr);
                m_cn.read(C_RCNT, v); tb_check("CNN RESULT_CNT", v, n_exp);
            end
            if (pass == 0 && loops == 1 && nfr == g_nframes) g_check_complete;
            if (pass == 0) g_check_en = 1'b0;
        end
        if (interval_check) begin
            tb_check("frame intervals == PERIOD", iv_bad, 0);
            $display("INFO: %0d frame intervals of %0d cycles checked", iv_n, period);
        end
        tb_check("ESI replay->dsp protocol errors", e1_err, 0);
        tb_check("ESI dsp->cnn protocol errors", e2_err, 0);
        tb_check("AXI response errors", m_rp.bresp_err + m_rp.rresp_err + m_ds.bresp_err + m_ds.rresp_err +
                 m_cn.bresp_err + m_cn.rresp_err, 0);
        $display("PERF: results=%0d frames=%0d cycles=%0d", n_res_event, nfr, cyc);
        tb_finish;
    end
endmodule

`default_nettype wire
