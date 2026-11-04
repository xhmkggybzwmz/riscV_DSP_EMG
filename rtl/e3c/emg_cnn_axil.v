`timescale 1ns / 1ps
`default_nettype none

// emg_cnn_axil - AXI4-Lite control page around the unchanged E3b INT8 CNN core
// emg_cnn (docs/soc/e3c_integration.md section 6). Base 0x1000_5000, 4 KB.
//
// Results: the core's result interface is always accepted (res_ready = 1), so the
// CPU never stalls inference. Each result is stored as one record in a FIFO of
// RES_DEPTH entries. The CPU-visible SNAPSHOT is the oldest stored record; all its
// fields (RES_INDEX .. SCORE5) belong to the same window and do not change until
// the CPU writes RESULT_ACK, which removes it (the next record becomes the
// snapshot). A new result that finds the FIFO full is discarded and counted in
// DROP_CNT; RES_INDEX (the core's result number since reset) then shows a gap.
//
// Offset  Name          Access  Reset        Description
// 0x000   ID            RO      0x434E_4E31  "CNN1"
// 0x004   VERSION       RO      0x0001_0000
// 0x008   CTRL          RW      0x0          [0] ENABLE: input handshake allowed (0: s_tready = 0)
//                                            [1] SOFT_RESET (write 1: one-cycle core reset, FIFO and
//                                            all counters cleared; reads 0)
//                                            [2] CLR_RESULTS (write 1: FIFO emptied; reads 0)
// 0x00C   STATUS        RO      -            [0] RESULT_PENDING (snapshot valid) [1] BUSY (core)
//                                            [2] FIFO_FULL [6:4] FIFO level [8] WR_PENDING
// 0x010   RESULT_ACK    W       -            any write: remove the snapshot (no effect if empty)
// 0x020   RES_INDEX     RO      0            \  snapshot (0 when RESULT_PENDING = 0)
// 0x024   RES_SEQ       RO      0             | ESI sequence number of the window's last frame
// 0x028   RES_END       RO      0             | sample index of the window end since SOS/GAP
// 0x02C   RES_TS        RO      0             | ESI timestamp of the window's last frame
// 0x030   RES_SEG       RO      0             | [7:0] stream segment number (SOS/GAP count - 1)
// 0x034   ARGMAX        RO      0             | [3:0] class index, ties -> lowest index
// 0x038   LAT_CYCLES    RO      0             | cycles from the input handshake of the decision
//                                             | frame to the core's result (core latency)
// 0x040+4k SCORE0..5    RO      0            /  int32 class scores (k = 0..5)
// 0x060   RESULT_CNT    RO      0            results produced by the core     \
// 0x064   DROP_CNT      RO      0            results discarded (FIFO full)     |  32-bit, wrap,
// 0x068   ACK_CNT       RO      0            snapshots removed by RESULT_ACK   |  cleared by reset /
// 0x06C   FRAMES        RO      0            frames accepted by the core       |  SOFT_RESET
// 0x070   RESETS        RO      0            stream resets (SOS/GAP)           |
// 0x074   NOT_POST_DSP  RO      0            input frames without POST_DSP     |
// 0x078   SAT1/2/3      RO      0            conv1/2/3 activation clips (0x078, 0x07C, 0x080)
// 0x090   WSEL          RW      0            [3:0] parameter memory (core wr_mem: 0 w1 .. 13 fcb)
// 0x094   WADDR         RW      0            [12:0] element index, auto-increment after WDATA
// 0x098   WDATA         W       -            parameter value. Accepted only while CTRL.ENABLE = 0
//                                            and no write is pending; held until the core is idle,
//                                            then written in one cycle (core wr_en only in S_IDLE).
//                                            Otherwise ignored and counted in WREJ.
// 0x09C   WREJ          RO      0            rejected WDATA writes
// other offsets: read 0, writes ignored, OKAY. Byte strobes: CTRL/WSEL use byte 0, WADDR bytes 0..1,
// WDATA all four bytes as given (absent bytes write 0); RESULT_ACK ignores WDATA and WSTRB.
// Reads have no side effects.

module emg_cnn_axil #(
    parameter N_CH = 4,
    parameter RES_DEPTH_LOG2 = 2,
    parameter W1_FILE = "", parameter B1_FILE = "", parameter M1_FILE = "", parameter SH1_FILE = "",
    parameter W2_FILE = "", parameter B2_FILE = "", parameter M2_FILE = "", parameter SH2_FILE = "",
    parameter W3_FILE = "", parameter B3_FILE = "", parameter M3_FILE = "", parameter SH3_FILE = "",
    parameter FCW_FILE = "", parameter FCB_FILE = ""
) (
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF s_axi, ASSOCIATED_RESET aresetn" *)
    input  wire         aclk,
    input  wire         aresetn,

    input  wire [11:0]  s_axi_awaddr,
    input  wire [2:0]   s_axi_awprot,
    input  wire         s_axi_awvalid,
    output wire         s_axi_awready,
    input  wire [31:0]  s_axi_wdata,
    input  wire [3:0]   s_axi_wstrb,
    input  wire         s_axi_wvalid,
    output wire         s_axi_wready,
    output wire [1:0]   s_axi_bresp,
    output wire         s_axi_bvalid,
    input  wire         s_axi_bready,
    input  wire [11:0]  s_axi_araddr,
    input  wire [2:0]   s_axi_arprot,
    input  wire         s_axi_arvalid,
    output wire         s_axi_arready,
    output wire [31:0]  s_axi_rdata,
    output wire [1:0]   s_axi_rresp,
    output wire         s_axi_rvalid,
    input  wire         s_axi_rready,

    // ESI in (POST_DSP)
    input  wire         s_tvalid,
    output wire         s_tready,
    input  wire [127:0] s_tdata,
    input  wire [79:0]  s_tuser,
    input  wire         s_tlast,

    output wire         core_rst,
    // core debug port, brought out for the bit-exact layer checks (unused in the block design)
    output wire         dbg_valid,
    output wire [2:0]   dbg_layer,
    output wire [5:0]   dbg_ch,
    output wire [23:0]  dbg_step,
    output wire [7:0]   dbg_seg,
    output wire [31:0]  dbg_acc,
    output wire [7:0]   dbg_act,
    output wire         res_event          // one-cycle pulse per core result (testbench / statistics)
);
    localparam NCLS = 6;
    localparam [31:0] ID_VALUE = 32'h434E_4E31, VERSION_VALUE = 32'h0001_0000;
    localparam DEPTH = 1 << RES_DEPTH_LOG2;
    localparam RW = 32 * 5 + 8 + 4 + 32 + 32 * NCLS;      // record width

    wire        wr_en, rd_en;
    wire [9:0]  wr_word, rd_word;
    wire [31:0] wr_data;
    wire [3:0]  wr_strb;
    reg  [31:0] rd_data;

    e3c_axil_if u_if (
        .aclk(aclk), .aresetn(aresetn),
        .s_axi_awaddr(s_axi_awaddr), .s_axi_awvalid(s_axi_awvalid), .s_axi_awready(s_axi_awready),
        .s_axi_wdata(s_axi_wdata), .s_axi_wstrb(s_axi_wstrb), .s_axi_wvalid(s_axi_wvalid),
        .s_axi_wready(s_axi_wready), .s_axi_bresp(s_axi_bresp), .s_axi_bvalid(s_axi_bvalid),
        .s_axi_bready(s_axi_bready), .s_axi_araddr(s_axi_araddr), .s_axi_arvalid(s_axi_arvalid),
        .s_axi_arready(s_axi_arready), .s_axi_rdata(s_axi_rdata), .s_axi_rresp(s_axi_rresp),
        .s_axi_rvalid(s_axi_rvalid), .s_axi_rready(s_axi_rready),
        .wr_en(wr_en), .wr_word(wr_word), .wr_data(wr_data), .wr_strb(wr_strb),
        .rd_en(rd_en), .rd_word(rd_word), .rd_data(rd_data));

    reg        enable, soft_rst, clr_res, w_pend;
    reg [3:0]  wsel, w_hold_sel;
    reg [12:0] waddr, w_hold_addr;
    reg [31:0] w_hold_data, wrej;
    wire rst = ~aresetn | soft_rst;
    assign core_rst = rst;

    // ---------------- core ----------------
    wire               core_ready, busy, res_valid;
    wire [NCLS*32-1:0] res_scores;
    wire [3:0]         res_argmax;
    wire [31:0]        res_end, res_seq, res_ts, res_count;
    wire [7:0]         res_seg;
    wire [31:0]        st_frames, st_results, st_resets, st_npd, st_sat1, st_sat2, st_sat3;
    wire in_valid = s_tvalid & enable;
    assign s_tready = core_ready & enable;
    wire wr_core = w_pend & ~busy & ~rst;               // core takes wr_en only in S_IDLE

    emg_cnn #(.N_CH(N_CH), .SETTLE(0),
        .W1_FILE(W1_FILE), .B1_FILE(B1_FILE), .M1_FILE(M1_FILE), .SH1_FILE(SH1_FILE),
        .W2_FILE(W2_FILE), .B2_FILE(B2_FILE), .M2_FILE(M2_FILE), .SH2_FILE(SH2_FILE),
        .W3_FILE(W3_FILE), .B3_FILE(B3_FILE), .M3_FILE(M3_FILE), .SH3_FILE(SH3_FILE),
        .FCW_FILE(FCW_FILE), .FCB_FILE(FCB_FILE)) u_cnn (
        .clk(aclk), .rst(rst),
        .s_tvalid(in_valid), .s_tready(core_ready), .s_tdata(s_tdata), .s_tuser(s_tuser), .s_tlast(s_tlast),
        .res_valid(res_valid), .res_ready(1'b1), .res_scores(res_scores), .res_argmax(res_argmax),
        .res_end(res_end), .res_seq(res_seq), .res_ts(res_ts), .res_seg(res_seg), .res_count(res_count),
        .dbg_valid(dbg_valid), .dbg_layer(dbg_layer), .dbg_ch(dbg_ch), .dbg_step(dbg_step), .dbg_seg(dbg_seg),
        .dbg_acc(dbg_acc), .dbg_act(dbg_act),
        .wr_en(wr_core), .wr_mem(w_hold_sel), .wr_addr(w_hold_addr), .wr_data(w_hold_data),
        .stat_frames(st_frames), .stat_results(st_results), .stat_resets(st_resets),
        .stat_not_post_dsp(st_npd), .stat_sat1(st_sat1), .stat_sat2(st_sat2), .stat_sat3(st_sat3),
        .busy(busy));

    assign res_event = res_valid;                       // res_ready = 1: valid for one cycle per result

    // ---------------- latency: decision frame handshake -> result ----------------
    reg [31:0] cyc, t_in, lat;
    always @(posedge aclk) begin
        if (rst) begin
            cyc <= 32'd0; t_in <= 32'd0;
        end else begin
            cyc <= cyc + 32'd1;
            if (in_valid && core_ready) t_in <= cyc;    // s_tready is low while a frame is processed
        end
    end
    wire [31:0] lat_now = cyc - t_in;

    // ---------------- result FIFO ----------------
    reg [RW-1:0] fifo [0:DEPTH-1];
    reg [RES_DEPTH_LOG2:0] rd_p, wr_p;
    wire [RES_DEPTH_LOG2:0] level = wr_p - rd_p;
    wire full = (level == DEPTH);
    wire pending = (level != 0);
    wire [RW-1:0] snap = pending ? fifo[rd_p[RES_DEPTH_LOG2-1:0]] : {RW{1'b0}};
    reg  [31:0] result_cnt, drop_cnt, ack_cnt;
    wire ack = wr_en && wr_word == 10'h004;

    always @(posedge aclk) begin
        if (res_valid && !full)
            fifo[wr_p[RES_DEPTH_LOG2-1:0]] <= {res_scores, lat_now, res_argmax, res_seg, res_ts, res_end,
                                              res_seq, res_count, 32'd0};
    end

    always @(posedge aclk) begin
        if (rst) begin
            rd_p <= 0; wr_p <= 0;
            result_cnt <= 32'd0; drop_cnt <= 32'd0; ack_cnt <= 32'd0;
        end else begin
            if (res_valid) begin
                result_cnt <= result_cnt + 32'd1;
                if (full) drop_cnt <= drop_cnt + 32'd1;
                else      wr_p <= wr_p + 1'b1;
            end
            if (clr_res) begin
                rd_p <= wr_p + ((res_valid && !full) ? 1'b1 : 1'b0);
            end else if (ack && pending) begin
                rd_p    <= rd_p + 1'b1;
                ack_cnt <= ack_cnt + 32'd1;
            end
        end
    end

    // record fields: [31:0] reserved 0, then index, seq, end, ts, seg, argmax, lat, scores
    wire [31:0] s_index = snap[63:32];
    wire [31:0] s_seq   = snap[95:64];
    wire [31:0] s_end   = snap[127:96];
    wire [31:0] s_ts    = snap[159:128];
    wire [7:0]  s_seg   = snap[167:160];
    wire [3:0]  s_arg   = snap[171:168];
    wire [31:0] s_lat   = snap[203:172];
    wire [NCLS*32-1:0] s_scores = snap[RW-1:204];

    // ---------------- control registers ----------------
    always @(posedge aclk) begin
        if (!aresetn) begin
            enable <= 1'b0; soft_rst <= 1'b0; clr_res <= 1'b0; w_pend <= 1'b0;
            wsel <= 4'd0; waddr <= 13'd0; w_hold_sel <= 4'd0; w_hold_addr <= 13'd0; w_hold_data <= 32'd0;
            wrej <= 32'd0;
        end else begin
            soft_rst <= 1'b0;
            clr_res  <= 1'b0;
            if (wr_core) w_pend <= 1'b0;
            if (soft_rst) begin
                w_pend <= 1'b0;
                wrej   <= 32'd0;
            end
            if (wr_en) begin
                case (wr_word)
                    10'h002: if (wr_strb[0]) begin
                        enable   <= wr_data[0];
                        soft_rst <= wr_data[1];
                        clr_res  <= wr_data[2];
                    end
                    10'h024: if (wr_strb[0]) wsel <= wr_data[3:0];
                    10'h025: waddr <= {wr_strb[1] ? wr_data[12:8] : waddr[12:8], wr_strb[0] ? wr_data[7:0] : waddr[7:0]};
                    10'h026: begin
                        if (enable || w_pend) begin
                            wrej <= wrej + 32'd1;
                        end else begin
                            w_hold_sel  <= wsel;
                            w_hold_addr <= waddr;
                            w_hold_data <= {wr_strb[3] ? wr_data[31:24] : 8'd0, wr_strb[2] ? wr_data[23:16] : 8'd0,
                                            wr_strb[1] ? wr_data[15:8]  : 8'd0, wr_strb[0] ? wr_data[7:0]   : 8'd0};
                            waddr       <= waddr + 13'd1;
                            w_pend      <= 1'b1;
                        end
                    end
                    default: ;
                endcase
            end
        end
    end

    always @* begin
        rd_data = 32'd0;
        if (rd_word >= 10'h010 && rd_word <= 10'h015) begin
            rd_data = s_scores[32 * (rd_word - 10'h010) +: 32];
        end else begin
            case (rd_word)
                10'h000: rd_data = ID_VALUE;
                10'h001: rd_data = VERSION_VALUE;
                10'h002: rd_data = {31'd0, enable};
                10'h003: rd_data = {23'd0, w_pend, 1'b0, level[2:0] & 3'h7, 1'b0, full, busy, pending};
                10'h008: rd_data = s_index;
                10'h009: rd_data = s_seq;
                10'h00A: rd_data = s_end;
                10'h00B: rd_data = s_ts;
                10'h00C: rd_data = {24'd0, s_seg};
                10'h00D: rd_data = {28'd0, s_arg};
                10'h00E: rd_data = s_lat;
                10'h018: rd_data = result_cnt;
                10'h019: rd_data = drop_cnt;
                10'h01A: rd_data = ack_cnt;
                10'h01B: rd_data = st_frames;
                10'h01C: rd_data = st_resets;
                10'h01D: rd_data = st_npd;
                10'h01E: rd_data = st_sat1;
                10'h01F: rd_data = st_sat2;
                10'h020: rd_data = st_sat3;
                10'h024: rd_data = {28'd0, wsel};
                10'h025: rd_data = {19'd0, waddr};
                10'h027: rd_data = wrej;
                default: rd_data = 32'd0;
            endcase
        end
    end

endmodule

`default_nettype wire
