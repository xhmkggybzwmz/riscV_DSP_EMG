`timescale 1ns / 1ps
`default_nettype none

`include "esi_defs.vh"

// emg_cnn - INT8 streaming 1D-CNN inference core (E3b), ESI v1 POST_DSP input.
// Integer semantics: docs/ml/e2_fixed_point_spec.md section 4 (golden model reference/emg_fixed/cnn/int_model.py).
//
//   Conv1 N_CH->C1 k K1 s S1 : int16 x int8 ; Conv2 C1->C2 k K2 s S2 ; Conv3 C2->C3 k K3 s S3 : uint8 x int8
//   acc = bias + sum w*x (int32) ; a = acc <= 0 ? 0 : min(255, (acc*M + 2^(s-1)) >> s)   (uint8)
//   GAP: running sum of the last MGAP conv3 outputs (add new, subtract the one leaving)
//   FC : score[k] = fcb[k] + sum_c fcw[k][c] * gsum[c] (int32) ; argmax, ties -> lowest index
//   decision schedule: conv3 step j = FIRST + EVERY*n, FIRST = (SETTLE + WINDOW - RF)/D (SETTLE = 0 ->
//   first decision at sample index 399 after SOS/GAP), EVERY = STEP/D.
//
// Micro-architecture (P = 1): one MAC pipeline (issue -> registered weight/activation read ->
// registered multiply -> int32 accumulate), time-multiplexed over all layers. Each accepted frame
// writes one sample into the conv1 line buffer and runs the layers that become due (at most
// conv1 -> conv2 -> conv3 -> FC). s_tready is low while a frame is being processed.
// SOS/GAP: all counters, stride phases, buffer heads, the GAP ring and sums are cleared before the
// frame is used as sample 0. The dbg_* port emits every accumulator / activation / GAP sum / score.

module emg_cnn #(
    parameter N_CH = 4,
    parameter C1 = 16, parameter K1 = 8, parameter S1 = 2,
    parameter C2 = 32, parameter K2 = 7, parameter S2 = 2,
    parameter C3 = 32, parameter K3 = 6, parameter S3 = 5,
    parameter NCLS = 6, parameter MGAP = 19,
    parameter WINDOW = 400, parameter STEP = 100, parameter SETTLE = 0,
    parameter W1_FILE = "", parameter B1_FILE = "", parameter M1_FILE = "", parameter SH1_FILE = "",
    parameter W2_FILE = "", parameter B2_FILE = "", parameter M2_FILE = "", parameter SH2_FILE = "",
    parameter W3_FILE = "", parameter B3_FILE = "", parameter M3_FILE = "", parameter SH3_FILE = "",
    parameter FCW_FILE = "", parameter FCB_FILE = ""
) (
    input  wire                 clk,
    input  wire                 rst,

    input  wire                 s_tvalid,
    output wire                 s_tready,
    input  wire [127:0]         s_tdata,
    input  wire [79:0]          s_tuser,
    input  wire                 s_tlast,

    output wire                 res_valid,            // = res_valid_r & ~rst
    input  wire                 res_ready,
    output reg  [NCLS*32-1:0]   res_scores,
    output reg  [3:0]           res_argmax,
    output reg  [31:0]          res_end,          // sample index (since SOS/GAP) of the window end
    output reg  [31:0]          res_seq,
    output reg  [31:0]          res_ts,
    output reg  [7:0]           res_seg,
    output reg  [31:0]          res_count,

    output reg                  dbg_valid,
    output reg  [2:0]           dbg_layer,        // 0..2 conv, 3 GAP sum, 4 FC score
    output reg  [5:0]           dbg_ch,
    output reg  [23:0]          dbg_step,         // conv: output index j ; GAP/FC: window end sample index
    output reg  [7:0]           dbg_seg,
    output reg  [31:0]          dbg_acc,
    output reg  [7:0]           dbg_act,

    input  wire                 wr_en,            // future weight update (only while idle)
    input  wire [3:0]           wr_mem,
    input  wire [12:0]          wr_addr,
    input  wire [31:0]          wr_data,

    output reg  [31:0]          stat_frames,
    output reg  [31:0]          stat_results,
    output reg  [31:0]          stat_resets,
    output reg  [31:0]          stat_not_post_dsp,
    output reg  [31:0]          stat_sat1,
    output reg  [31:0]          stat_sat2,
    output reg  [31:0]          stat_sat3,
    output wire                 busy
);
    // ---------------- geometry ----------------
    localparam D     = S1 * S2 * S3;
    localparam RF    = K1 + (K2 - 1) * S1 + (K3 - 1) * S1 * S2;
    localparam FIRST = (SETTLE + WINDOW - RF) / D;
    localparam EVERY = STEP / D;
    localparam NW1 = C1 * N_CH * K1, NW2 = C2 * C1 * K2, NW3 = C3 * C2 * K3, NWF = NCLS * C3;

    // ---------------- parameter memories ----------------
    reg [7:0]  w1 [0:NW1-1];
    reg [7:0]  w2 [0:NW2-1];
    reg [7:0]  w3 [0:NW3-1];
    reg [7:0]  fcw [0:NWF-1];
    reg [31:0] b1 [0:C1-1];
    reg [31:0] b2 [0:C2-1];
    reg [31:0] b3 [0:C3-1];
    reg [31:0] fcb [0:NCLS-1];
    reg [15:0] m1 [0:C1-1];
    reg [15:0] m2 [0:C2-1];
    reg [15:0] m3 [0:C3-1];
    reg [7:0]  sh1 [0:C1-1];
    reg [7:0]  sh2 [0:C2-1];
    reg [7:0]  sh3 [0:C3-1];
    initial begin
        if (W1_FILE != "")  $readmemh(W1_FILE, w1);
        if (W2_FILE != "")  $readmemh(W2_FILE, w2);
        if (W3_FILE != "")  $readmemh(W3_FILE, w3);
        if (FCW_FILE != "") $readmemh(FCW_FILE, fcw);
        if (B1_FILE != "")  $readmemh(B1_FILE, b1);
        if (B2_FILE != "")  $readmemh(B2_FILE, b2);
        if (B3_FILE != "")  $readmemh(B3_FILE, b3);
        if (FCB_FILE != "") $readmemh(FCB_FILE, fcb);
        if (M1_FILE != "")  $readmemh(M1_FILE, m1);
        if (M2_FILE != "")  $readmemh(M2_FILE, m2);
        if (M3_FILE != "")  $readmemh(M3_FILE, m3);
        if (SH1_FILE != "") $readmemh(SH1_FILE, sh1);
        if (SH2_FILE != "") $readmemh(SH2_FILE, sh2);
        if (SH3_FILE != "") $readmemh(SH3_FILE, sh3);
    end

    // ---------------- stream state ----------------
    reg [15:0] lb1 [0:K1*N_CH-1];
    reg [7:0]  lb2 [0:K2*C1-1];
    reg [7:0]  lb3 [0:K3*C2-1];
    reg [7:0]  ring [0:MGAP*C3-1];
    reg [15:0] gsum [0:C3-1];
    reg [3:0]  head1, head2, head3;
    reg [4:0]  rhead;
    reg [3:0]  fill1, fill2, fill3;
    reg [4:0]  rfill;
    reg [3:0]  ph1, ph2, ph3, dph;
    reg [23:0] j1, j2, j3;
    reg [31:0] n_samp;
    reg [7:0]  seg;
    reg [31:0] seq_lat, ts_lat;
    reg [31:0] scores [0:NCLS-1];

    // ---------------- engine ----------------
    localparam [3:0] S_IDLE = 4'd0, S_OINIT = 4'd1, S_ISSUE = 4'd2, S_DRAIN = 4'd3, S_F1 = 4'd4, S_F2 = 4'd5,
                     S_F3 = 4'd6, S_LDONE = 4'd7, S_GAPDBG = 4'd8, S_ARG = 4'd9, S_RES = 4'd10;
    reg [3:0]  state;
    reg [2:0]  L;                 // 1..3 conv layers, 4 = FC
    reg [5:0]  o;                 // output channel / class
    reg [5:0]  c;                 // input channel
    reg [3:0]  k;                 // tap
    reg [12:0] wcnt;
    reg [12:0] waddr;
    reg [9:0]  iaddr;
    reg        p1, p2, p3;
    reg [7:0]  w_q1, w_q2, w_q3, w_qf;
    reg [15:0] in_q1;
    reg [7:0]  in_q2, in_q3;
    reg [15:0] in_qf;
    reg signed [24:0] prod;
    reg signed [31:0] acc;
    reg [47:0] rq_prod;
    reg [63:0] rq_shift;
    reg        rq_pos;
    reg [3:0]  best;
    reg [31:0] best_v;

    reg res_valid_r;
    assign res_valid = res_valid_r & ~rst;
    assign s_tready = (state == S_IDLE) && !rst;
    assign busy = (state != S_IDLE);

    // per-layer views
    wire [5:0]  CIN  = (L == 3'd1) ? N_CH : (L == 3'd2) ? C1 : C2;
    wire [5:0]  CINX = (L == 3'd4) ? C3 : CIN;
    wire [5:0]  COUT = (L == 3'd1) ? C1 : (L == 3'd2) ? C2 : (L == 3'd3) ? C3 : NCLS;
    wire [3:0]  KL   = (L == 3'd1) ? K1 : (L == 3'd2) ? K2 : (L == 3'd3) ? K3 : 4'd1;
    wire [3:0]  HB   = (L == 3'd1) ? head1 : (L == 3'd2) ? head2 : head3;
    wire [4:0]  slot_sum = HB + k;
    wire [3:0]  slot = (slot_sum >= KL) ? (slot_sum - KL) : slot_sum[3:0];
    wire [9:0]  iaddr_next = (L == 3'd4) ? c : slot * CIN + c;
    wire        last_mac = (c == CINX - 1) && (k == KL - 1);

    wire [31:0] bias_o = (L == 3'd1) ? b1[o] : (L == 3'd2) ? b2[o] : (L == 3'd3) ? b3[o] : fcb[o];
    wire [15:0] m_o    = (L == 3'd1) ? m1[o] : (L == 3'd2) ? m2[o] : m3[o];
    wire [7:0]  s_o    = (L == 3'd1) ? sh1[o] : (L == 3'd2) ? sh2[o] : sh3[o];

    // multiplier operands (selected by layer, registered reads)
    wire signed [16:0] opa = (L == 3'd1) ? {in_q1[15], in_q1} :
                             (L == 3'd2) ? {9'd0, in_q2} :
                             (L == 3'd3) ? {9'd0, in_q3} : {1'b0, in_qf};
    wire signed [7:0]  opw = (L == 3'd1) ? w_q1 : (L == 3'd2) ? w_q2 : (L == 3'd3) ? w_q3 : w_qf;

    wire [7:0]  act = rq_pos ? ((rq_shift > 64'd255) ? 8'd255 : rq_shift[7:0]) : 8'd0;
    wire        act_clip = rq_pos && (rq_shift > 64'd255);

    wire [15:0] in_flags = s_tuser[`ESI_FLAGS];
    wire        do_reset = in_flags[`ESI_F_SOS] | in_flags[`ESI_F_GAP];

    // registered memory / buffer reads (BRAM-friendly)
    always @(posedge clk) begin
        w_q1 <= w1[waddr[8:0]];
        w_q2 <= w2[waddr[11:0]];
        w_q3 <= w3[waddr];
        w_qf <= fcw[waddr[7:0]];
        in_q1 <= lb1[iaddr[4:0]];
        in_q2 <= lb2[iaddr[6:0]];
        in_q3 <= lb3[iaddr[7:0]];
        in_qf <= gsum[iaddr[4:0]];
    end

    // future weight-update port (writes only while idle)
    always @(posedge clk) begin
        if (wr_en && state == S_IDLE) begin
            case (wr_mem)
                4'd0:  w1[wr_addr[8:0]]  <= wr_data[7:0];
                4'd1:  b1[wr_addr[3:0]]  <= wr_data;
                4'd2:  m1[wr_addr[3:0]]  <= wr_data[15:0];
                4'd3:  sh1[wr_addr[3:0]] <= wr_data[7:0];
                4'd4:  w2[wr_addr[11:0]] <= wr_data[7:0];
                4'd5:  b2[wr_addr[4:0]]  <= wr_data;
                4'd6:  m2[wr_addr[4:0]]  <= wr_data[15:0];
                4'd7:  sh2[wr_addr[4:0]] <= wr_data[7:0];
                4'd8:  w3[wr_addr]       <= wr_data[7:0];
                4'd9:  b3[wr_addr[4:0]]  <= wr_data;
                4'd10: m3[wr_addr[4:0]]  <= wr_data[15:0];
                4'd11: sh3[wr_addr[4:0]] <= wr_data[7:0];
                4'd12: fcw[wr_addr[7:0]] <= wr_data[7:0];
                4'd13: fcb[wr_addr[2:0]] <= wr_data;
                default: ;
            endcase
        end
    end

    integer i;
    always @(posedge clk) begin
        dbg_valid <= 1'b0;
        p1 <= (state == S_ISSUE);
        p2 <= p1;
        p3 <= p2;
        if (p2) prod <= opa * opw;
        if (p3) acc <= acc + {{7{prod[24]}}, prod};
        if (rst) begin
            state <= S_IDLE;
            res_valid_r <= 1'b0; res_count <= 32'd0;
            res_scores <= {NCLS*32{1'b0}}; res_argmax <= 4'd0; res_end <= 32'd0; res_seq <= 32'd0;
            res_ts <= 32'd0; res_seg <= 8'd0;
            stat_frames <= 32'd0; stat_results <= 32'd0; stat_resets <= 32'd0; stat_not_post_dsp <= 32'd0;
            stat_sat1 <= 32'd0; stat_sat2 <= 32'd0; stat_sat3 <= 32'd0;
            head1 <= 4'd0; head2 <= 4'd0; head3 <= 4'd0; rhead <= 5'd0;
            fill1 <= 4'd0; fill2 <= 4'd0; fill3 <= 4'd0; rfill <= 5'd0;
            ph1 <= 4'd0; ph2 <= 4'd0; ph3 <= 4'd0; dph <= 4'd0;
            j1 <= 24'd0; j2 <= 24'd0; j3 <= 24'd0; n_samp <= 32'd0; seg <= 8'hFF;
            p1 <= 1'b0; p2 <= 1'b0; p3 <= 1'b0;
            L <= 3'd1; o <= 6'd0; c <= 6'd0; k <= 4'd0; wcnt <= 13'd0; waddr <= 13'd0; iaddr <= 10'd0;
            for (i = 0; i < C3; i = i + 1) gsum[i] <= 16'd0;
        end else begin
            if (res_valid_r && res_ready) res_valid_r <= 1'b0;
            case (state)
                S_IDLE: if (s_tvalid) begin
                    stat_frames <= stat_frames + 1;
                    if (!in_flags[`ESI_F_POST_DSP]) stat_not_post_dsp <= stat_not_post_dsp + 1;
                    seq_lat <= s_tuser[`ESI_SEQ];
                    ts_lat  <= s_tuser[`ESI_TS];
                    // SOS / GAP: clear the stream state, this frame becomes sample 0
                    if (do_reset) begin
                        stat_resets <= stat_resets + 1;
                        seg <= seg + 1'b1;
                        for (i = 0; i < C3; i = i + 1) gsum[i] <= 16'd0;
                        head2 <= 4'd0; head3 <= 4'd0; rhead <= 5'd0;
                        fill2 <= 4'd0; fill3 <= 4'd0; rfill <= 5'd0;
                        ph2 <= 4'd0; ph3 <= 4'd0; dph <= 4'd0;
                        j1 <= 24'd0; j2 <= 24'd0; j3 <= 24'd0;
                    end
                    for (i = 0; i < N_CH; i = i + 1)
                        lb1[(do_reset ? 0 : head1) * N_CH + i] <= s_tdata[16*i +: 16];
                    head1  <= ((do_reset ? 4'd0 : head1) == K1 - 1) ? 4'd0 : (do_reset ? 4'd0 : head1) + 1'b1;
                    n_samp <= (do_reset ? 32'd0 : n_samp) + 1;
                    // conv1 due? (fill reaches K1, then every S1 inputs)
                    if ((do_reset ? 4'd1 : ((fill1 < K1) ? fill1 + 1'b1 : K1)) == K1) begin
                        fill1 <= K1;
                        ph1 <= ((do_reset ? 4'd0 : ph1) == S1 - 1) ? 4'd0 : (do_reset ? 4'd0 : ph1) + 1'b1;
                        if ((do_reset ? 4'd0 : ph1) == 4'd0) begin
                            L <= 3'd1; o <= 6'd0; wcnt <= 13'd0; state <= S_OINIT;
                        end
                    end else begin
                        fill1 <= do_reset ? 4'd1 : fill1 + 1'b1;
                        if (do_reset) ph1 <= 4'd0;
                    end
                end
                S_OINIT: begin
                    acc <= bias_o;
                    c <= 6'd0; k <= 4'd0;
                    state <= S_ISSUE;
                end
                S_ISSUE: begin
                    waddr <= wcnt;
                    iaddr <= iaddr_next;
                    wcnt  <= wcnt + 1'b1;
                    if (last_mac) state <= S_DRAIN;
                    else if (k == KL - 1) begin k <= 4'd0; c <= c + 1'b1; end
                    else k <= k + 1'b1;
                end
                S_DRAIN: if (!p1 && !p2 && !p3) state <= (L == 3'd4) ? S_F3 : S_F1;
                S_F1: begin
                    rq_pos  <= (acc > 0);
                    rq_prod <= acc[31:0] * m_o;           // acc > 0 is the only case used
                    state <= S_F2;
                end
                S_F2: begin
                    rq_shift <= ({16'd0, rq_prod} + (64'd1 << (s_o - 1))) >> s_o;   // s in [1, 62]
                    state <= S_F3;
                end
                S_F3: begin
                    dbg_valid <= 1'b1;
                    dbg_layer <= (L == 3'd4) ? 3'd4 : L - 1'b1;     // 0..2 conv, 4 FC (3 = GAP, S_GAPDBG)
                    dbg_ch <= o;
                    dbg_seg <= seg;
                    dbg_acc <= acc;
                    dbg_act <= (L == 3'd4) ? 8'd0 : act;
                    case (L)
                        3'd1: begin dbg_step <= j1; lb2[head2 * C1 + o] <= act; if (act_clip) stat_sat1 <= stat_sat1 + 1; end
                        3'd2: begin dbg_step <= j2; lb3[head3 * C2 + o] <= act; if (act_clip) stat_sat2 <= stat_sat2 + 1; end
                        3'd3: begin
                            dbg_step <= j3;
                            ring[rhead * C3 + o] <= act;
                            gsum[o] <= gsum[o] + act - ((rfill == MGAP) ? ring[rhead * C3 + o] : 8'd0);
                            if (act_clip) stat_sat3 <= stat_sat3 + 1;
                        end
                        default: begin dbg_step <= n_samp - 1; scores[o] <= acc; end
                    endcase
                    if (o == COUT - 1) state <= S_LDONE;
                    else begin o <= o + 1'b1; state <= S_OINIT; end
                end
                S_LDONE: begin
                    o <= 6'd0;
                    wcnt <= 13'd0;
                    case (L)
                        3'd1: begin
                            j1 <= j1 + 1'b1;
                            head2 <= (head2 == K2 - 1) ? 4'd0 : head2 + 1'b1;
                            if (((fill2 < K2) ? fill2 + 1'b1 : K2) == K2) begin
                                fill2 <= K2;
                                ph2 <= (ph2 == S2 - 1) ? 4'd0 : ph2 + 1'b1;
                                if (ph2 == 4'd0) begin L <= 3'd2; state <= S_OINIT; end
                                else state <= S_IDLE;
                            end else begin fill2 <= fill2 + 1'b1; state <= S_IDLE; end
                        end
                        3'd2: begin
                            j2 <= j2 + 1'b1;
                            head3 <= (head3 == K3 - 1) ? 4'd0 : head3 + 1'b1;
                            if (((fill3 < K3) ? fill3 + 1'b1 : K3) == K3) begin
                                fill3 <= K3;
                                ph3 <= (ph3 == S3 - 1) ? 4'd0 : ph3 + 1'b1;
                                if (ph3 == 4'd0) begin L <= 3'd3; state <= S_OINIT; end
                                else state <= S_IDLE;
                            end else begin fill3 <= fill3 + 1'b1; state <= S_IDLE; end
                        end
                        3'd3: begin
                            j3 <= j3 + 1'b1;
                            rhead <= (rhead == MGAP - 1) ? 5'd0 : rhead + 1'b1;
                            rfill <= (rfill < MGAP) ? rfill + 1'b1 : MGAP;
                            if (j3 >= FIRST) begin
                                dph <= (dph == EVERY - 1) ? 4'd0 : dph + 1'b1;
                                if (dph == 4'd0) begin c <= 6'd0; state <= S_GAPDBG; end
                                else state <= S_IDLE;
                            end else state <= S_IDLE;
                        end
                        default: begin best <= 4'd0; best_v <= scores[0]; c <= 6'd1; state <= S_ARG; end
                    endcase
                end
                S_GAPDBG: begin                                // emit the GAP sums, then run the FC layer
                    dbg_valid <= 1'b1;
                    dbg_layer <= 3'd3;
                    dbg_ch <= c;
                    dbg_step <= n_samp - 1;
                    dbg_seg <= seg;
                    dbg_acc <= {16'd0, gsum[c]};
                    dbg_act <= 8'd0;
                    if (c == C3 - 1) begin L <= 3'd4; o <= 6'd0; wcnt <= 13'd0; state <= S_OINIT; end
                    else c <= c + 1'b1;
                end
                S_ARG: begin                                   // argmax, ties -> lowest index (strict >)
                    if (c == NCLS) state <= S_RES;
                    else begin
                        if ($signed(scores[c]) > $signed(best_v)) begin best <= c[3:0]; best_v <= scores[c]; end
                        c <= c + 1'b1;
                    end
                end
                S_RES: if (!res_valid_r) begin
                    res_valid_r <= 1'b1;
                    for (i = 0; i < NCLS; i = i + 1) res_scores[32*i +: 32] <= scores[i];
                    res_argmax <= best;
                    res_end <= n_samp - 1;
                    res_seq <= seq_lat;
                    res_ts <= ts_lat;
                    res_seg <= seg;
                    res_count <= res_count + 1;
                    stat_results <= stat_results + 1;
                    L <= 3'd1;
                    state <= S_IDLE;
                end
                default: state <= S_IDLE;
            endcase
        end
    end
endmodule

`default_nettype wire
