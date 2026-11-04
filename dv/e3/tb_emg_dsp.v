`timescale 1ns / 1ps
`default_nettype none

// tb_emg_dsp - E3a DSP RTL vs E2 Python fixed-point reference, frame by frame, bit by bit.
// Plusargs: +vec=<case dir> +frames=N +mask=M +cfgchg_at=I +cfgchg_mask=M2 +coefport=0|1 +coef=<file>
//           +vpct=<source valid %> +rpct=<sink ready %> +seed=S +exp_int_sat=A +exp_out_sat=B
// Compile-time: model_paths.vh gives DSP_COEF_FILE (parameter init); with NO_COEF_INIT defined the
// DUT starts with zero coefficients and the TB writes them through the write port (+coefport=1).

module tb_emg_dsp;
    localparam TB_TIMEOUT_NS = 200000000;
    `include "tb_check.vh"

    reg clk = 1'b0;
    reg rst = 1'b1;
    always #5 clk = ~clk;

    reg  [3:0]   cfg_sec_en = 4'b1101;
    reg          cfg_clear = 1'b0;
    reg          coef_we = 1'b0;
    reg  [7:0]   coef_addr = 8'd0;
    reg  [17:0]  coef_wdata = 18'd0;
    wire         s_tvalid, s_tready, s_tlast, m_tvalid, m_tready, m_tlast;
    wire [127:0] s_tdata, m_tdata;
    wire [79:0]  s_tuser, m_tuser;
    wire [31:0]  st_in, st_out, st_osat, st_isat, st_res;
    wire         st_ovf, busy;
    wire         src_done;
    wire [31:0]  src_sent, snk_count;

`ifdef NO_COEF_INIT
    localparam COEF = "";                    // coefficients only through the write port
`else
    `include "model_paths.vh"
    localparam COEF = `DSP_COEF_FILE;
`endif

    emg_dsp #(.N_CH(4), .N_SEC(4), .COEF_FILE(COEF)) dut (
        .clk(clk), .rst(rst), .cfg_sec_en(cfg_sec_en), .cfg_clear(cfg_clear),
        .coef_we(coef_we), .coef_addr(coef_addr), .coef_wdata(coef_wdata),
        .s_tvalid(s_tvalid), .s_tready(s_tready), .s_tdata(s_tdata), .s_tuser(s_tuser), .s_tlast(s_tlast),
        .m_tvalid(m_tvalid), .m_tready(m_tready), .m_tdata(m_tdata), .m_tuser(m_tuser), .m_tlast(m_tlast),
        .stat_frames_in(st_in), .stat_frames_out(st_out), .stat_out_sat(st_osat), .stat_int_sat(st_isat),
        .stat_resets(st_res), .stat_ovf_sticky(st_ovf), .busy(busy));

    esi_src_bfm src (.clk(clk), .rst(rst), .tvalid(s_tvalid), .tready(s_tready), .tdata(s_tdata),
                     .tuser(s_tuser), .tlast(s_tlast), .done(src_done), .sent(src_sent));
    esi_sink_bfm snk (.clk(clk), .rst(rst), .tvalid(m_tvalid), .tready(m_tready), .tdata(m_tdata),
                      .tuser(m_tuser), .tlast(m_tlast), .count(snk_count));
    wire [31:0] mon_in_err, mon_in_hs, mon_out_err, mon_out_hs;
    esi_monitor #(.NAME("dsp in"))  mon_in  (.clk(clk), .rst(rst), .tvalid(s_tvalid), .tready(s_tready),
        .tdata(s_tdata), .tuser(s_tuser), .tlast(s_tlast), .errors(mon_in_err), .handshakes(mon_in_hs));
    esi_monitor #(.NAME("dsp out")) mon_out (.clk(clk), .rst(rst), .tvalid(m_tvalid), .tready(m_tready),
        .tdata(m_tdata), .tuser(m_tuser), .tlast(m_tlast), .errors(mon_out_err), .handshakes(mon_out_hs));

    reg [175:0] expv [0:39999];
    reg [17:0]  coefs [0:19];
    reg [8*512-1:0] vec, fname;
    integer nframes, mask, cfgchg_at, cfgchg_mask, coefport, vpct, rpct, exp_isat, exp_osat, k, l, nerr;

    // processing latency: input handshake -> first output valid (DUT internal, independent of the sink)
    integer t_acc, lat, lat_min, lat_max, lat_sum, nlat, cyc;
    reg     m_prev;
    initial begin lat_min = 1 << 30; lat_max = 0; lat_sum = 0; nlat = 0; cyc = 0; m_prev = 0; end
    always @(posedge clk) begin
        cyc = cyc + 1;
        if (s_tvalid && s_tready) t_acc = cyc;
        if (m_tvalid && !m_prev) begin
            lat = cyc - t_acc;
            nlat = nlat + 1; lat_sum = lat_sum + lat;
            if (lat < lat_min) lat_min = lat;
            if (lat > lat_max) lat_max = lat;
        end
        m_prev = m_tvalid;
    end

    initial begin
        tb_init;
        if (!$value$plusargs("vec=%s", vec)) begin tb_fail("no +vec"); tb_finish; end
        if (!$value$plusargs("frames=%d", nframes)) nframes = 0;
        if (!$value$plusargs("mask=%d", mask)) mask = 13;
        if (!$value$plusargs("cfgchg_at=%d", cfgchg_at)) cfgchg_at = -1;
        if (!$value$plusargs("cfgchg_mask=%d", cfgchg_mask)) cfgchg_mask = mask;
        if (!$value$plusargs("coefport=%d", coefport)) coefport = 0;
        if (!$value$plusargs("vpct=%d", vpct)) vpct = 100;
        if (!$value$plusargs("rpct=%d", rpct)) rpct = 100;
        if (!$value$plusargs("exp_int_sat=%d", exp_isat)) exp_isat = -1;
        if (!$value$plusargs("exp_out_sat=%d", exp_osat)) exp_osat = -1;
        $sformat(fname, "%0s/frames.hex", vec);
        src.load(fname, nframes);
        $sformat(fname, "%0s/expect.hex", vec);
        $readmemh(fname, expv, 0, nframes - 1);
        src.configure(vpct, 97, tb_seed);
        snk.configure(rpct, tb_seed + 11);
        if (cfgchg_at >= 0) src.pause_at = cfgchg_at;
        cfg_sec_en = mask[3:0];
        repeat (5) @(posedge clk);
        #1 rst = 1'b0;
        @(posedge clk);
        if (coefport) begin
            if (!$value$plusargs("coef=%s", fname)) begin tb_fail("no +coef"); tb_finish; end
            $readmemh(fname, coefs);
            for (k = 0; k < 20; k = k + 1) begin
                @(negedge clk);
                coef_we = 1'b1; coef_addr = k; coef_wdata = coefs[k];
            end
            @(negedge clk) coef_we = 1'b0;
        end
        src.start;
        if (cfgchg_at >= 0) begin
            wait (snk_count == cfgchg_at);
            @(negedge clk) cfg_sec_en = cfgchg_mask[3:0];
            src.release_pause;
        end
        wait (snk_count == nframes);
        repeat (50) @(posedge clk);
        // ---- frame-by-frame comparison ----
        nerr = 0;
        for (k = 0; k < nframes; k = k + 1) begin
            if (snk.cap_data[k] !== expv[k][127:0] || snk.cap_user[k][15:0] !== expv[k][143:128]
                || snk.cap_user[k][47:16] !== expv[k][175:144] || snk.cap_user[k][79:48] !== k
                || snk.cap_last[k] !== (((k + 1) % 97 == 0) || (k == nframes - 1))) begin
                nerr = nerr + 1;
                if (nerr <= 5) begin
                    for (l = 0; l < 8; l = l + 1)
                        if (snk.cap_data[k][16*l +: 16] !== expv[k][16*l +: 16])
                            $display("ERROR: frame %0d lane %0d got %h exp %h", k, l,
                                     snk.cap_data[k][16*l +: 16], expv[k][16*l +: 16]);
                    $display("ERROR: frame %0d flags %h/%h seq %h/%h ts %0d last %b", k, snk.cap_user[k][15:0],
                             expv[k][143:128], snk.cap_user[k][47:16], expv[k][175:144], snk.cap_user[k][79:48],
                             snk.cap_last[k]);
                end
            end
        end
        tb_check("mismatching output frames", nerr, 0);
        tb_check("output frames", snk_count, nframes);
        tb_check("stat frames in", st_in, nframes);
        tb_check("stat frames out", st_out, nframes);
        if (exp_isat >= 0) tb_check("internal saturations", st_isat, exp_isat);
        if (exp_osat >= 0) tb_check("output saturations", st_osat, exp_osat);
        tb_check("sticky overflow consistent", st_ovf, (st_isat != 0) || (st_osat != 0));
        tb_check("input protocol errors", mon_in_err, 0);
        tb_check("output protocol errors", mon_out_err, 0);
        tb_check("input handshakes", mon_in_hs, nframes);
        $display("PERF: dsp frame latency cycles min=%0d max=%0d mean=%0d frames=%0d resets=%0d",
                 lat_min, lat_max, lat_sum / (nlat > 0 ? nlat : 1), nlat, st_res);
        tb_finish;
    end
endmodule

`default_nettype wire
