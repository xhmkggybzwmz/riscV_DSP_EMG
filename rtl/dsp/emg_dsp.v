`timescale 1ns / 1ps
`default_nettype none

`include "esi_defs.vh"

// emg_dsp - fixed-point EMG DSP front end (E3a), ESI v1 in / out.
// Arithmetic: docs/ml/e2_fixed_point_spec.md section 3 (golden model reference/emg_fixed/dsp/fixed_dsp.py).
//
//   per lane: u = x << 8 (int27, 8 fraction bits)
//   per enabled section slot (order DC, notch, BP0, BP1):
//       acc = b0*u + b1*u1 + b2*u2 - a1*v1 - a2*v2         int27 x Q2.16 int18 products, exact 48-bit sum
//       v   = sat27(round_shift(acc, 16))                   round half up: (acc + 2^15) >>> 16
//       u2 <= u1, u1 <= u, v2 <= v1, v1 <= v ; next section input u = v
//   y = sat16(round_shift(u, 8))                            POST_DSP int16 (disabled slots pass u through)
//
// Micro-architecture: one registered 27x18 multiplier and one 48-bit accumulator, time-multiplexed
// over N_CH lanes x N_SEC slots x 5 products (2 cycles per product, 1 for round/saturate/state update).
// One frame is latched on the input handshake; s_tready stays low until the result frame has been
// accepted downstream, so a frame can never be overwritten or consumed twice.
//
// Reset contract (docs/soc/esi_v1.md): SOS or GAP in the input flags, a change of cfg_sec_en, a coefficient
// write or cfg_clear -> all filter state is cleared before the next frame is processed; for the last three
// the output frame also gets SOS so that the downstream CNN clears too.
// Output flags = input flags | POST_DSP | OVR (if any lane saturated at the int16 output in this frame).

module emg_dsp #(
    parameter N_CH      = 4,
    parameter N_SEC     = 4,                 // slots: 0 DC, 1 notch, 2 band-pass 0, 3 band-pass 1
    parameter COEF_FILE = "",                // N_SEC x {b0 b1 b2 a1 a2}, 18-bit hex per line
    parameter [N_SEC-1:0] SEC_EN_DEFAULT = 4'b1101
) (
    input  wire                 clk,
    input  wire                 rst,                  // synchronous, active high

    input  wire [N_SEC-1:0]     cfg_sec_en,
    input  wire                 cfg_clear,
    input  wire                 coef_we,
    input  wire [7:0]           coef_addr,
    input  wire [17:0]          coef_wdata,

    input  wire                 s_tvalid,
    output wire                 s_tready,
    input  wire [127:0]         s_tdata,
    input  wire [79:0]          s_tuser,
    input  wire                 s_tlast,

    output wire                 m_tvalid,             // = m_tvalid_r & ~rst (ESI rule 1)
    input  wire                 m_tready,
    output reg  [127:0]         m_tdata,
    output reg  [79:0]          m_tuser,
    output reg                  m_tlast,

    output reg  [31:0]          stat_frames_in,
    output reg  [31:0]          stat_frames_out,
    output reg  [31:0]          stat_out_sat,
    output reg  [31:0]          stat_int_sat,
    output reg  [31:0]          stat_resets,
    output reg                  stat_ovf_sticky,
    output wire                 busy
);
    localparam NS = N_CH * N_SEC;
    localparam [2:0] S_IDLE = 3'd0, S_SEC = 3'd1, S_MUL = 3'd2, S_ACC = 3'd3, S_RND = 3'd4,
                     S_CHEND = 3'd5, S_OUT = 3'd6;
    localparam signed [26:0] D_MAX = 27'sd67108863, D_MIN = -27'sd67108864;

    reg signed [17:0] coef [0:N_SEC*5-1];
    reg signed [26:0] su1 [0:NS-1];
    reg signed [26:0] su2 [0:NS-1];
    reg signed [26:0] sv1 [0:NS-1];
    reg signed [26:0] sv2 [0:NS-1];

    integer n;
    initial begin
        for (n = 0; n < N_SEC * 5; n = n + 1) coef[n] = 18'sd0;
        if (COEF_FILE != "") $readmemh(COEF_FILE, coef);
    end

    reg  [2:0]  state;
    reg  [127:0] x_lat;
    reg  [79:0]  user_lat;
    reg          last_lat;
    reg  [N_SEC-1:0] en_act, en_last;
    reg          pending, force_sos, frame_sat;
    reg  [3:0]   ch;
    reg  [3:0]   sec;
    reg  [2:0]   p;
    reg  signed [26:0] u;
    reg  signed [47:0] acc;
    reg  signed [44:0] prod;
    reg  [15:0]  y [0:N_CH-1];

    reg m_tvalid_r;
    assign m_tvalid = m_tvalid_r & ~rst;
    assign s_tready = (state == S_IDLE) && !rst;
    assign busy = (state != S_IDLE);

    wire [7:0] idx = ch * N_SEC + sec;

    // multiplier operands
    wire signed [17:0] opc = coef[sec * 5 + p];
    wire signed [26:0] s_u1 = su1[idx];
    wire signed [26:0] s_u2 = su2[idx];
    wire signed [26:0] s_v1 = sv1[idx];
    wire signed [26:0] s_v2 = sv2[idx];
    wire signed [26:0] opd = (p == 3'd0) ? u : (p == 3'd1) ? s_u1 : (p == 3'd2) ? s_u2 :
                             (p == 3'd3) ? s_v1 : s_v2;

    // section output: v = sat27(round_shift(acc, 16))
    wire signed [47:0] acc_r = acc + 48'sd32768;
    wire signed [31:0] v_raw = acc_r >>> 16;          // |acc| < 2^47 -> fits 32 bits
    wire               v_hi  = (v_raw > $signed({{5{1'b0}}, D_MAX}));
    wire               v_lo  = (v_raw < $signed({{5{1'b1}}, D_MIN}));
    wire signed [26:0] v_sat = v_hi ? D_MAX : (v_lo ? D_MIN : v_raw[26:0]);

    // lane output: y = sat16(round_shift(u, 8))
    wire signed [27:0] u_r   = {u[26], u} + 28'sd128;
    wire signed [19:0] y_raw = u_r >>> 8;
    wire               y_hi  = (y_raw > 20'sd32767);
    wire               y_lo  = (y_raw < -20'sd32768);
    wire [15:0]        y_sat = y_hi ? 16'h7FFF : (y_lo ? 16'h8000 : y_raw[15:0]);

    wire [15:0] in_flags = s_tuser[`ESI_FLAGS];
    wire        do_reset = in_flags[`ESI_F_SOS] | in_flags[`ESI_F_GAP];

    integer i;
    always @(posedge clk) begin
        if (rst) begin
            state <= S_IDLE;
            m_tvalid_r <= 1'b0;
            m_tdata <= 128'd0; m_tuser <= 80'd0; m_tlast <= 1'b0;
            stat_frames_in <= 32'd0; stat_frames_out <= 32'd0; stat_out_sat <= 32'd0;
            stat_int_sat <= 32'd0; stat_resets <= 32'd0; stat_ovf_sticky <= 1'b0;
            pending <= 1'b0; force_sos <= 1'b0; frame_sat <= 1'b0;
            en_last <= SEC_EN_DEFAULT; en_act <= SEC_EN_DEFAULT;
            ch <= 4'd0; sec <= 4'd0; p <= 3'd0; u <= 27'sd0; acc <= 48'sd0; prod <= 45'sd0;
            for (i = 0; i < NS; i = i + 1) begin
                su1[i] <= 27'sd0; su2[i] <= 27'sd0; sv1[i] <= 27'sd0; sv2[i] <= 27'sd0;
            end
        end else begin
            if (coef_we && state == S_IDLE) begin
                coef[coef_addr] <= $signed(coef_wdata);
                pending <= 1'b1;
            end
            if (cfg_clear) pending <= 1'b1;
            case (state)
                S_IDLE: if (s_tvalid) begin                     // s_tready = 1 in S_IDLE
                    x_lat <= s_tdata; user_lat <= s_tuser; last_lat <= s_tlast;
                    stat_frames_in <= stat_frames_in + 1;
                    en_act <= cfg_sec_en;
                    en_last <= cfg_sec_en;
                    force_sos <= pending || (cfg_sec_en != en_last) || cfg_clear || coef_we;
                    if (do_reset || pending || (cfg_sec_en != en_last) || cfg_clear || coef_we) begin
                        for (i = 0; i < NS; i = i + 1) begin
                            su1[i] <= 27'sd0; su2[i] <= 27'sd0; sv1[i] <= 27'sd0; sv2[i] <= 27'sd0;
                        end
                        stat_resets <= stat_resets + 1;
                        pending <= 1'b0;
                    end
                    frame_sat <= 1'b0;
                    ch <= 4'd0; sec <= 4'd0;
                    u <= {{3{s_tdata[15]}}, s_tdata[15:0], 8'd0};
                    state <= S_SEC;
                end
                S_SEC: begin
                    if (sec == N_SEC) state <= S_CHEND;
                    else if (!en_act[sec]) sec <= sec + 1'b1;     // disabled slot: pass through
                    else begin p <= 3'd0; state <= S_MUL; end
                end
                S_MUL: begin
                    prod <= opc * opd;
                    state <= S_ACC;
                end
                S_ACC: begin
                    if (p == 3'd0)      acc <= prod;
                    else if (p < 3'd3)  acc <= acc + prod;
                    else                acc <= acc - prod;
                    if (p == 3'd4) state <= S_RND;
                    else begin p <= p + 1'b1; state <= S_MUL; end
                end
                S_RND: begin
                    if (v_hi || v_lo) begin
                        stat_int_sat <= stat_int_sat + 1;
                        stat_ovf_sticky <= 1'b1;
                    end
                    su2[idx] <= su1[idx];
                    su1[idx] <= u;
                    sv2[idx] <= sv1[idx];
                    sv1[idx] <= v_sat;
                    u <= v_sat;
                    sec <= sec + 1'b1;
                    state <= S_SEC;
                end
                S_CHEND: begin
                    y[ch] <= y_sat;
                    if (y_hi || y_lo) begin
                        stat_out_sat <= stat_out_sat + 1;
                        stat_ovf_sticky <= 1'b1;
                        frame_sat <= 1'b1;
                    end
                    if (ch == N_CH - 1) state <= S_OUT;
                    else begin
                        ch <= ch + 1'b1;
                        sec <= 4'd0;
                        u <= {{3{x_lat[16*(ch+1)+15]}}, x_lat[16*(ch+1) +: 16], 8'd0};
                        state <= S_SEC;
                    end
                end
                S_OUT: begin
                    if (!m_tvalid_r) begin
                        m_tvalid_r <= 1'b1;
                        for (i = 0; i < 8; i = i + 1)
                            m_tdata[16*i +: 16] <= (i < N_CH) ? y[i] : 16'd0;
                        m_tuser <= {user_lat[79:16], user_lat[15:0] | (16'd1 << `ESI_F_POST_DSP)
                                    | (frame_sat ? (16'd1 << `ESI_F_OVR) : 16'd0)
                                    | (force_sos ? (16'd1 << `ESI_F_SOS) : 16'd0)};
                        m_tlast <= last_lat;
                    end else if (m_tready) begin
                        m_tvalid_r <= 1'b0;
                        stat_frames_out <= stat_frames_out + 1;
                        state <= S_IDLE;
                    end
                end
                default: state <= S_IDLE;
            endcase
        end
    end
endmodule

`default_nettype wire
