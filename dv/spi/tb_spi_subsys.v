`timescale 1ns / 1ps
`default_nettype none

// tb_spi_subsys - SPI -> esi_spi_src_axil -> emg_dsp_axil -> emg_cnn_axil with the real cores and BRAM images of
// build/e3/model_paths.vh (scripts/sim/gen_spi_live.py). The arithmetic of DSP and CNN has its own bit-exact regressions
// (run_e3.py, run_e5.py); this test proves the wiring and the stream contract of the live source:
//   first result at frame 399 after SOS, then every 100; seq = EMG1 index; a lost packet restarts DSP and CNN (GAP, new
//   CNN segment); every frame reaches the CNN as POST_DSP; the activity counter follows the signal.

`include "model_paths.vh"

module tb_spi_subsys;
    localparam [63:0] TB_TIMEOUT_NS = 64'd2000000000;
    `include "tb_check.vh"

    reg clk = 1'b0;
    always #5 clk = ~clk;
    reg aresetn = 1'b0, src_aresetn = 1'b0;
    reg sclk = 1'b0, mosi = 1'b0, cs_n = 1'b1;
    wire ready;

    wire [11:0] aw_s, ar_s, aw_d, ar_d, aw_c, ar_c;
    wire [31:0] wd_s, rd_s, wd_d, rd_d, wd_c, rd_c;
    wire [3:0]  ws_s, ws_d, ws_c;
    wire [1:0]  br_s, rr_s, br_d, rr_d, br_c, rr_c;
    wire awv_s, awr_s, wv_s, wr_s, bv_s, brd_s, arv_s, arr_s, rv_s, rrd_s;
    wire awv_d, awr_d, wv_d, wr_d, bv_d, brd_d, arv_d, arr_d, rv_d, rrd_d;
    wire awv_c, awr_c, wv_c, wr_c, bv_c, brd_c, arv_c, arr_c, rv_c, rrd_c;

    tb_axil_master #(.AW(12)) m_s (.clk(clk), .awaddr(aw_s), .awvalid(awv_s), .awready(awr_s), .wdata(wd_s), .wstrb(ws_s),
        .wvalid(wv_s), .wready(wr_s), .bresp(br_s), .bvalid(bv_s), .bready(brd_s), .araddr(ar_s), .arvalid(arv_s),
        .arready(arr_s), .rdata(rd_s), .rresp(rr_s), .rvalid(rv_s), .rready(rrd_s));
    tb_axil_master #(.AW(12)) m_d (.clk(clk), .awaddr(aw_d), .awvalid(awv_d), .awready(awr_d), .wdata(wd_d), .wstrb(ws_d),
        .wvalid(wv_d), .wready(wr_d), .bresp(br_d), .bvalid(bv_d), .bready(brd_d), .araddr(ar_d), .arvalid(arv_d),
        .arready(arr_d), .rdata(rd_d), .rresp(rr_d), .rvalid(rv_d), .rready(rrd_d));
    tb_axil_master #(.AW(12)) m_c (.clk(clk), .awaddr(aw_c), .awvalid(awv_c), .awready(awr_c), .wdata(wd_c), .wstrb(ws_c),
        .wvalid(wv_c), .wready(wr_c), .bresp(br_c), .bvalid(bv_c), .bready(brd_c), .araddr(ar_c), .arvalid(arv_c),
        .arready(arr_c), .rdata(rd_c), .rresp(rr_c), .rvalid(rv_c), .rready(rrd_c));

    wire         tv1, tr1, tl1, trst1, tv2, tr2, tl2, trst2, trst3, tev, dv;
    wire [127:0] td1, td2;
    wire [79:0]  tu1, tu2;
    wire [2:0]   dl;
    wire [5:0]   dc;
    wire [23:0]  ds;
    wire [7:0]   dse, da;
    wire [31:0]  dac;

    emg_spi_subsys #(.DSP_COEF_FILE(`DSP_COEF_FILE),
        .W1_FILE(`W1_FILE), .B1_FILE(`B1_FILE), .M1_FILE(`M1_FILE), .SH1_FILE(`SH1_FILE),
        .W2_FILE(`W2_FILE), .B2_FILE(`B2_FILE), .M2_FILE(`M2_FILE), .SH2_FILE(`SH2_FILE),
        .W3_FILE(`W3_FILE), .B3_FILE(`B3_FILE), .M3_FILE(`M3_FILE), .SH3_FILE(`SH3_FILE),
        .FCW_FILE(`FCW_FILE), .FCB_FILE(`FCB_FILE)) u_dut (
        .aclk(clk), .aresetn(aresetn), .src_aresetn(src_aresetn),
        .spi_sclk(sclk), .spi_mosi(mosi), .spi_cs_n(cs_n), .spi_ready(ready),
        .s_axi_rep_awaddr(aw_s), .s_axi_rep_awprot(3'd0), .s_axi_rep_awvalid(awv_s), .s_axi_rep_awready(awr_s),
        .s_axi_rep_wdata(wd_s), .s_axi_rep_wstrb(ws_s), .s_axi_rep_wvalid(wv_s), .s_axi_rep_wready(wr_s),
        .s_axi_rep_bresp(br_s), .s_axi_rep_bvalid(bv_s), .s_axi_rep_bready(brd_s), .s_axi_rep_araddr(ar_s),
        .s_axi_rep_arprot(3'd0), .s_axi_rep_arvalid(arv_s), .s_axi_rep_arready(arr_s), .s_axi_rep_rdata(rd_s),
        .s_axi_rep_rresp(rr_s), .s_axi_rep_rvalid(rv_s), .s_axi_rep_rready(rrd_s),
        .s_axi_dsp_awaddr(aw_d), .s_axi_dsp_awprot(3'd0), .s_axi_dsp_awvalid(awv_d), .s_axi_dsp_awready(awr_d),
        .s_axi_dsp_wdata(wd_d), .s_axi_dsp_wstrb(ws_d), .s_axi_dsp_wvalid(wv_d), .s_axi_dsp_wready(wr_d),
        .s_axi_dsp_bresp(br_d), .s_axi_dsp_bvalid(bv_d), .s_axi_dsp_bready(brd_d), .s_axi_dsp_araddr(ar_d),
        .s_axi_dsp_arprot(3'd0), .s_axi_dsp_arvalid(arv_d), .s_axi_dsp_arready(arr_d), .s_axi_dsp_rdata(rd_d),
        .s_axi_dsp_rresp(rr_d), .s_axi_dsp_rvalid(rv_d), .s_axi_dsp_rready(rrd_d),
        .s_axi_cnn_awaddr(aw_c), .s_axi_cnn_awprot(3'd0), .s_axi_cnn_awvalid(awv_c), .s_axi_cnn_awready(awr_c),
        .s_axi_cnn_wdata(wd_c), .s_axi_cnn_wstrb(ws_c), .s_axi_cnn_wvalid(wv_c), .s_axi_cnn_wready(wr_c),
        .s_axi_cnn_bresp(br_c), .s_axi_cnn_bvalid(bv_c), .s_axi_cnn_bready(brd_c), .s_axi_cnn_araddr(ar_c),
        .s_axi_cnn_arprot(3'd0), .s_axi_cnn_arvalid(arv_c), .s_axi_cnn_arready(arr_c), .s_axi_cnn_rdata(rd_c),
        .s_axi_cnn_rresp(rr_c), .s_axi_cnn_rvalid(rv_c), .s_axi_cnn_rready(rrd_c),
        .tap_rep_valid(tv1), .tap_rep_ready(tr1), .tap_rep_data(td1), .tap_rep_user(tu1), .tap_rep_last(tl1), .tap_rep_rst(trst1),
        .tap_dsp_valid(tv2), .tap_dsp_ready(tr2), .tap_dsp_data(td2), .tap_dsp_user(tu2), .tap_dsp_last(tl2), .tap_dsp_rst(trst2),
        .tap_cnn_rst(trst3), .tap_res_event(tev), .dbg_valid(dv), .dbg_layer(dl), .dbg_ch(dc), .dbg_step(ds), .dbg_seg(dse),
        .dbg_acc(dac), .dbg_act(da));

    integer me1, mh1, me2, mh2;
    esi_monitor #(.NAME("src")) u_mon1 (.clk(clk), .rst(trst1 | ~aresetn), .tvalid(tv1), .tready(tr1), .tdata(td1), .tuser(tu1),
        .tlast(tl1), .errors(me1), .handshakes(mh1));
    esi_monitor #(.NAME("dsp")) u_mon2 (.clk(clk), .rst(trst2 | ~aresetn), .tvalid(tv2), .tready(tr2), .tdata(td2), .tuser(tu2),
        .tlast(tl2), .errors(me2), .handshakes(mh2));

    // ---------------- SPI master, respects READY like the STM32 ----------------
    integer hp = 50;
    reg [15:0] crc;
    function [15:0] crc_byte(input [15:0] c0, input [7:0] d);
        integer j;
        reg [15:0] c;
        begin
            c = c0 ^ {d, 8'h00};
            for (j = 0; j < 8; j = j + 1) c = c[15] ? ((c << 1) ^ 16'h1021) : (c << 1);
            crc_byte = c;
        end
    endfunction
    task send_raw(input [7:0] b);
        integer j;
        begin for (j = 7; j >= 0; j = j - 1) begin mosi = b[j]; #(hp); sclk = 1'b1; #(hp); sclk = 1'b0; end end
    endtask
    task send_crc(input [7:0] b); begin crc = crc_byte(crc, b); send_raw(b); end endtask

    function [15:0] sample_value(input [31:0] seq, input integer amp);
        reg [31:0] h;
        begin
            h = seq * 32'h9E37_79B1;
            sample_value = 16'd2048 + ((amp == 0) ? 16'd0 : (h[31:20] % (2 * amp + 1)) - amp);
        end
    endfunction

    task send_packet(input [31:0] idx, input integer amp, input bad_crc);
        integer i;
        reg [15:0] v;
        begin
            while (ready !== 1'b1) @(posedge clk);
            crc = 16'hFFFF;
            cs_n = 1'b0; #(100);
            send_crc("E"); send_crc("M"); send_crc("G"); send_crc("1");
            send_crc(8'd1); send_crc(8'd1); send_crc(8'd1); send_crc(8'd0);
            send_crc(8'hD0); send_crc(8'h07); send_crc(8'd0); send_crc(8'd0);
            send_crc(idx[7:0]); send_crc(idx[15:8]); send_crc(idx[23:16]); send_crc(idx[31:24]);
            send_crc(8'h80); send_crc(8'h00); send_crc(8'h00); send_crc(8'h01);
            for (i = 0; i < 128; i = i + 1) begin
                v = sample_value(idx + i, amp);
                send_crc(v[7:0]); send_crc(v[15:8]);
            end
            send_raw(crc[7:0] ^ (bad_crc ? 8'h01 : 8'h00));
            send_raw(crc[15:8]);
            #(100); cs_n = 1'b1; #(300);
        end
    endtask

    // ---------------- results, taken as the firmware takes them ----------------
    integer n_res = 0, seg_prev = -1, seg_no = -1, in_seg = 0;
    reg [31:0] seg_base = 0;
    reg [31:0] r, r_end, r_seq, r_seg;
    task take_results;
        begin
            m_c.read(12'h00C, r);
            while (r & 32'h1) begin
                m_c.read(12'h024, r_seq);
                m_c.read(12'h028, r_end);
                m_c.read(12'h030, r_seg);
                if (r_seg != seg_prev) begin seg_prev = r_seg; seg_no = seg_no + 1; in_seg = 0; end
                tb_check("result end", r_end, 399 + 100 * in_seg);
                tb_check("result seq", r_seq, ((seg_no == 0) ? 32'd128 : 32'd1536) + 399 + 100 * in_seg);
                m_c.write(12'h010, 32'd0, 4'hF);
                in_seg = in_seg + 1;
                n_res = n_res + 1;
                m_c.read(12'h00C, r);
            end
        end
    endtask

    task wait_frames(input [31:0] n);
        integer guard;
        begin
            guard = 0;
            m_c.read(12'h06C, r);
            while (r != n && guard < 400000) begin
                take_results;
                repeat (200) @(posedge clk);
                m_c.read(12'h06C, r);
                guard = guard + 1;
            end
            tb_check("frames at the CNN", r, n);
            repeat (30000) @(posedge clk);
            take_results;
        end
    endtask

    integer i;
    reg [31:0] e_quiet, e_loud, q;
    initial begin
        tb_init;
        repeat (10) @(posedge clk);
        src_aresetn = 1'b1;
        repeat (5) @(posedge clk);
        aresetn = 1'b1;
        repeat (10) @(posedge clk);
        m_c.write(12'h008, 32'h1, 4'hF);    // CNN enable
        m_d.write(12'h008, 32'h1, 4'hF);    // DSP enable
        m_s.write(12'h030, 32'h1, 4'hF);    // source RUN

        // five quiet packets, five loud: 1280 frames
        for (i = 0; i < 5; i = i + 1) begin send_packet(32'd128 + 128 * i, 0, 1'b0); take_results; end
        wait_frames(32'd640);
        m_s.read(12'h058, e_quiet);
        for (i = 5; i < 10; i = i + 1) begin send_packet(32'd128 + 128 * i, 1500, 1'b0); take_results; end
        wait_frames(32'd1280);
        m_s.read(12'h058, e_loud);
        $display("INFO: LEVEL_ENERGY quiet %0d loud %0d", e_quiet, e_loud);
        tb_check("quiet is near zero", e_quiet < 32'd10, 1);
        tb_check("loud is far above quiet", e_loud > 32'd1000000, 1);
        m_s.read(12'h05C, q);
        tb_check("LEVEL_SEQ", q, 10);
        tb_check("results of the first segment", n_res, 9);

        // a corrupt packet: GAP restarts DSP and CNN, the results of the next segment count from its first frame
        send_packet(32'd1408, 1500, 1'b1);
        for (i = 0; i < 5; i = i + 1) begin send_packet(32'd1536 + 128 * i, 1500, 1'b0); take_results; end
        wait_frames(32'd1920);
        tb_check("results in total", n_res, 12);
        m_s.read(12'h044, q);
        tb_check("source GAP", q, 1);
        m_s.read(12'h010, q);
        tb_check("source CRC", q, 1);
        m_d.read(12'h034, q);
        tb_check("DSP GAP", q, 1);
        m_d.read(12'h030, q);
        tb_check("DSP SOS", q, 1);
        m_c.read(12'h074, q);
        tb_check("all frames POST_DSP", q, 0);
        m_c.read(12'h064, q);
        tb_check("no CNN drop", q, 0);
        m_s.read(12'h038, q);
        tb_check("no packet dropped", q, 0);

        // a re-run of the SoC: the page survives, DSP and CNN restart with SOS, nothing older than the reset
        aresetn = 1'b0;
        repeat (20) @(posedge clk);
        m_s.read(12'h000, q);
        tb_check("page alive during the SoC reset", q, 32'h4553_5331);
        aresetn = 1'b1;
        repeat (10) @(posedge clk);
        m_c.write(12'h008, 32'h1, 4'hF);
        m_d.write(12'h008, 32'h1, 4'hF);
        send_packet(32'd5000, 1500, 1'b0);
        repeat (400000) @(posedge clk);
        m_c.read(12'h06C, q);
        tb_check("frames after the re-run", q, 128);
        m_d.read(12'h030, q);
        tb_check("SOS after the re-run", q, 1);

        tb_check("source monitor", me1, 0);
        tb_check("DSP monitor", me2, 0);
        tb_finish;
    end
endmodule

`default_nettype wire
