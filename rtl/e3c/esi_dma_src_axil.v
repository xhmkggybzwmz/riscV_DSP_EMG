`timescale 1ns / 1ps
`default_nettype none

`include "esi_defs.vh"

// esi_dma_src_axil - ESI v1 source fed from PS memory through an AXI4-Stream master (AMD AXI DMA
// MM2S), with an AXI4-Lite control page for the PS (docs/system/e5a_ps_dma.md). The frames are
// loaded by the PS at run time: nothing of the data set is in the bitstream.
//
// Input stream: one beat = one frame = 4 int16 lanes, {lane3, lane2, lane1, lane0} (the little-endian
// int16 array [T, 4] in memory). TLAST (end of a DMA transfer) ends a segment. ESI lanes 4..7 are 0.
// Output frame k (k = 0, 1, ... since SOFT_RESET), n = index inside its segment:
//   tdata  = {64'd0, beat}
//   flags  = SOS on the first frame of a segment (and after a sink reset), UNDERRUN on the first frame
//            after a starved period inside a segment (the samples are late, none is lost: no GAP)
//   seq    = SEQ_BASE + k        timestamp = n
//   tlast  = (n mod 100 == 99) or the last frame of a segment
// Timing: PERIOD = 0 -> flow mode, the next frame as soon as the previous one was accepted.
// PERIOD = P > 0 -> at least P cycles between frames, exactly P while the input keeps up and the
// sink accepts (2 kHz at 100 MHz: P = 50000). No catching up: a late frame delays the following ones.
//   LATE_CNT      + 1 when a frame is due and the previous one still waits for TREADY
//   UNDERRUN_CNT  + 1 when a frame is due inside a segment and the input has none
// VALID is never withdrawn, except by SOFT_RESET or while the sink is in reset.
//
// Reset domains: aresetn and the control page belong to the PS side, which survives a re-run of the
// SoC, so a PS access never meets a slave in reset. sink_aresetn is the reset of DSP / CNN (the SoC):
// while it is low no frame is offered, and the next frame carries SOS (a waiting one is restarted
// as frame 0 of a segment), since the sink has lost its stream state.
//
// Offset  Name          Access  Reset        Description
// 0x000   ID            RO      0x4553_4431  "ESD1"
// 0x004   VERSION       RO      0x0001_0000
// 0x008   CTRL          RW      0x0          [0] RUN (0: input not accepted, nothing launched)
//                                            [2] SOFT_RESET (write 1: stop, k = 0, counters 0, held and
//                                            waiting frames dropped, next frame SOS; reads 0)
// 0x00C   STATUS        RO      -            [0] RUN [1] IN_SEGMENT [2] VALID (frame waiting for TREADY)
//                                            [3] INPUT (frame held, not launched yet) [4] SINK_RESET
// 0x014   PERIOD        RW      50000        cycles per frame, 0 = flow mode
// 0x018   FRAME_CNT     RO      0            frames delivered (handshakes) since SOFT_RESET
// 0x01C   LATE_CNT      RO      0
// 0x020   SEQ_BASE      RW      0            sequence number of frame k = 0
// 0x024   SEG_CNT       RO      0            segments finished (last frame launched)
// 0x028   UNDERRUN_CNT  RO      0
// 0x02C   KEEP_ERR_CNT  RO      0            input beats with TKEEP != 0xFF (passed on unchanged)
// Writes use all four strobes as given; other offsets read 0; responses OKAY; reads have no side effects.

module esi_dma_src_axil (
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF s_axi:s_axis, ASSOCIATED_RESET aresetn" *)
    input  wire         aclk,
    input  wire         aresetn,
    input  wire         sink_aresetn,

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

    input  wire [63:0]  s_axis_tdata,
    input  wire [7:0]   s_axis_tkeep,
    input  wire         s_axis_tvalid,
    output wire         s_axis_tready,
    input  wire         s_axis_tlast,

    output wire         m_tvalid,
    input  wire         m_tready,
    output reg  [127:0] m_tdata,
    output reg  [79:0]  m_tuser,
    output reg          m_tlast,

    output wire         src_rst           // for stream monitors
);
    localparam [31:0] ID_VALUE = 32'h4553_4431, VERSION_VALUE = 32'h0001_0000;

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

    function [31:0] strb32;
        input [31:0] old; input [31:0] d; input [3:0] s;
        integer b;
        begin
            strb32 = old;
            for (b = 0; b < 4; b = b + 1) if (s[b]) strb32[8*b +: 8] = d[8*b +: 8];
        end
    endfunction

    // ---------------- control ----------------
    reg         run, soft_rst;
    reg  [31:0] period, seq_base;
    wire        rst = ~aresetn | soft_rst;
    assign src_rst = rst;

    // ---------------- datapath ----------------
    reg         h_valid, h_last;               // input frame held
    reg  [63:0] h_data;
    reg         valid_r, in_seg, next_sos, under_pend, late_armed, under_armed, sink_rst_q;
    reg  [31:0] k, n, frame_cnt, late_cnt, seg_cnt, under_cnt, keep_err, wait_cnt;
    reg  [6:0]  n100;                          // n mod 100 (tlast)
    wire        sink_rst = ~sink_aresetn;

    assign m_tvalid = valid_r & ~rst & ~sink_rst & ~sink_rst_q;

    wire out_hs  = m_tvalid & m_tready;
    wire in_hs   = s_axis_tvalid & s_axis_tready;
    wire due     = (period == 32'd0) | (wait_cnt == 32'd0);
    wire launch  = run & ~sink_rst & due & h_valid & (~valid_r | out_hs);
    wire [6:0] n100_now = next_sos ? 7'd0 : n100;
    assign s_axis_tready = aresetn & ~soft_rst & run & (~h_valid | launch);

    always @(posedge aclk) begin
        if (rst) begin
            h_valid <= 1'b0; h_last <= 1'b0; h_data <= 64'd0;
            valid_r <= 1'b0; m_tdata <= 128'd0; m_tuser <= 80'd0; m_tlast <= 1'b0;
            in_seg <= 1'b0; next_sos <= 1'b1; under_pend <= 1'b0; late_armed <= 1'b1; under_armed <= 1'b1;
            sink_rst_q <= 1'b0;
            k <= 32'd0; n <= 32'd0; n100 <= 7'd0; frame_cnt <= 32'd0; late_cnt <= 32'd0; seg_cnt <= 32'd0;
            under_cnt <= 32'd0; keep_err <= 32'd0; wait_cnt <= 32'd0;
        end else begin
            sink_rst_q <= sink_rst;
            if (in_hs) begin
                h_valid <= 1'b1;
                h_data  <= s_axis_tdata;
                h_last  <= s_axis_tlast;
                if (s_axis_tkeep != 8'hFF) keep_err <= keep_err + 32'd1;
            end else if (launch) begin
                h_valid <= 1'b0;
            end
            if (out_hs) begin
                frame_cnt <= frame_cnt + 32'd1;
                if (!launch) valid_r <= 1'b0;
            end
            if (wait_cnt != 32'd0 && !sink_rst) wait_cnt <= wait_cnt - 32'd1;

            // a frame is due: one count per event, not per cycle
            if (run && period != 32'd0 && due && valid_r && !out_hs) begin
                if (late_armed) late_cnt <= late_cnt + 32'd1;
                late_armed <= 1'b0;
            end else begin
                late_armed <= 1'b1;
            end
            if (run && period != 32'd0 && due && in_seg && !h_valid && !sink_rst) begin
                if (under_armed) under_cnt <= under_cnt + 32'd1;
                under_armed <= 1'b0;
                under_pend  <= 1'b1;
            end else begin
                under_armed <= 1'b1;
            end

            if (launch) begin
                valid_r  <= 1'b1;
                m_tdata  <= {64'd0, h_data};
                m_tuser  <= {next_sos ? 32'd0 : n, seq_base + k,
                             8'd0, 4'd0, under_pend & ~next_sos, 2'd0, next_sos};
                m_tlast  <= h_last || (n100_now == 7'd99);
                k        <= k + 32'd1;
                n        <= h_last ? 32'd0 : (next_sos ? 32'd1 : n + 32'd1);
                n100     <= (h_last || n100_now == 7'd99) ? 7'd0 : n100_now + 7'd1;
                in_seg   <= ~h_last;
                next_sos <= h_last;
                under_pend <= 1'b0;
                if (h_last) seg_cnt <= seg_cnt + 32'd1;
                wait_cnt <= (period == 32'd0) ? 32'd0 : period - 32'd1;
            end

            // sink in reset: it lost its stream state. The waiting frame restarts a segment, or the next one does
            if (sink_rst) begin
                if (valid_r) begin
                    m_tuser[`ESI_F_SOS] <= 1'b1;
                    m_tuser[79:48]      <= 32'd0;
                    n                   <= 32'd1;
                    n100                <= 7'd1;
                    next_sos            <= 1'b0;
                end else begin
                    next_sos <= 1'b1;
                end
                in_seg     <= in_seg & valid_r;
                under_pend <= 1'b0;
            end
        end
    end

    always @(posedge aclk) begin
        if (!aresetn) begin
            run <= 1'b0; soft_rst <= 1'b0; period <= 32'd50000; seq_base <= 32'd0;
        end else begin
            soft_rst <= 1'b0;
            if (soft_rst) run <= 1'b0;
            if (wr_en) begin
                case (wr_word)
                    10'h002: if (wr_strb[0]) begin
                        run      <= wr_data[0];
                        soft_rst <= wr_data[2];
                    end
                    10'h005: period   <= strb32(period, wr_data, wr_strb);
                    10'h008: seq_base <= strb32(seq_base, wr_data, wr_strb);
                    default: ;
                endcase
            end
        end
    end

    always @* begin
        case (rd_word)
            10'h000: rd_data = ID_VALUE;
            10'h001: rd_data = VERSION_VALUE;
            10'h002: rd_data = {31'd0, run};
            10'h003: rd_data = {27'd0, sink_rst, h_valid, m_tvalid, in_seg, run};
            10'h005: rd_data = period;
            10'h006: rd_data = frame_cnt;
            10'h007: rd_data = late_cnt;
            10'h008: rd_data = seq_base;
            10'h009: rd_data = seg_cnt;
            10'h00A: rd_data = under_cnt;
            10'h00B: rd_data = keep_err;
            default: rd_data = 32'd0;
        endcase
    end

    // AXI4-Lite protection bits and the read strobe are not used
    wire unused = &{1'b0, s_axi_awprot, s_axi_arprot, rd_en, 1'b0};

endmodule

`default_nettype wire
