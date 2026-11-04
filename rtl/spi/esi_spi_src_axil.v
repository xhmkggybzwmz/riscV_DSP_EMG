`timescale 1ns / 1ps
`default_nettype none

`include "esi_defs.vh"

// esi_spi_src_axil - ESI v1 source fed by the STM32 over SPI (docs/system/stm32_spi_live.md), one EMG channel.
//
//   SPI -> emg1_spi_rx -> 4-block store (128 samples each) -> sample conversion -> ESI PRE_DSP
//
// Store and forward: a packet becomes visible only after its CRC passed, so ESI never carries a sample of a
// bad packet. A packet is stored if RUN is set, the sink is out of reset, a block is free when CS falls and
// its rate equals EXPECT_RATE.
//
// Timing: there is no PERIOD. Frames leave as soon as the sink accepts them; the sample clock of the STM32
// is the only time base. A fixed 2 kHz here would drift against the STM32 oscillator and underrun or overflow
// the store sooner or later. Each packet is a burst of 128 frames every 64 ms at 2 kHz.
//
// Output frame, k = samples since SOS:
//   tdata  = lane 0: sat16((u16 - OFFSET) << SHIFT); lanes 1..7: 0. Defaults: 12-bit ADC code, mid-rail
//            2048, << 3 = +-16384 at the rails, no saturation
//   flags  = SOS on the first frame after SOFT_RESET, a reset of this page or of the sink;
//            GAP on the first frame of a block whose index does not follow the previous stored block;
//            OVR when the conversion saturated; SRC_ID = 1; POST_DSP = 0
//   seq    = EMG1 sample index (so a gap is visible in the results)    timestamp = k    tlast = last of a block
// VALID is never withdrawn, except by SOFT_RESET or while the sink is in reset.
//
// READY (PL -> STM32) goes low with 3 blocks waiting, leaving one for a packet already on its way. A packet
// that starts with no free block anyway is lost (DROP_CNT) and its successor carries GAP.
//
// Reset domains: aresetn and the page belong to the PS side, which survives a re-run of the SoC, so a PS access
// never meets a slave in reset. sink_aresetn is the reset of DSP / CNN: while it is low the store is emptied and
// nothing is accepted; the next frame carries SOS.
//
// Offset  Name          Access  Reset        Description (0x008..0x02C as in emg_spi_monitor_axil)
// 0x000   ID            RO      0x4553_5331  "ESS1"
// 0x004   VERSION/CLEAR R / W   0x0001_0000  write bit 0: clear the counters of 0x00C..0x01C and the sequence
//                                            baseline of SEQ_ERROR
// 0x008   STATUS        RO      -            [0] READY [1] CS_N [2] ACTIVE [3] HAVE_VALID [4] HAVE_EXPECTED
//                                            [5] RUN [6] SINK_RESET [7] VALID (frame waiting for TREADY)
// 0x00C   GOOD          RO      0            packets with good CRC and header, stored or not
// 0x010   CRC_ERROR     RO      0
// 0x014   SEQ_ERROR     RO      0            good packets whose index does not follow the previous good one
// 0x018   FRAME_ERROR   RO      0            bad header, CS released early, too long
// 0x01C   OVERFLOW      RO      0            SCLK edges too close for the oversampling
// 0x020   LAST_INDEX    RO      0            first sample index of the last good packet
// 0x024   EXPECTED      RO      0            index the next packet should have
// 0x028   SAMPLE_RATE   RO      0            rate field of the last good packet
// 0x02C   FORMAT        RO      0x0100_0080  U16, one channel, 128 samples
// 0x030   CTRL          RW      0x0          [0] RUN (0: nothing stored, nothing launched; a frame already
//                                            offered stays offered). [2] SOFT_RESET (write 1: stop, store
//                                            emptied, all counters 0, next frame SOS; reads 0)
// 0x034   BLOCKS        RO      0            blocks waiting, 0..4
// 0x038   DROP_CNT      RO      0            packets not stored: no free block when CS fell
// 0x03C   RATE_ERR_CNT  RO      0            good packets with a rate other than EXPECT_RATE, not stored
// 0x040   ESI_FRAMES    RO      0            frames delivered (handshakes)
// 0x044   GAP_CNT       RO      0            frames sent with GAP
// 0x048   OVR_CNT       RO      0            frames sent with OVR
// 0x04C   OFFSET        RW      2048         [15:0]
// 0x050   SHIFT         RW      3            [2:0]
// 0x054   EXPECT_RATE   RW      2000         0 = any rate
// 0x058   LEVEL_ENERGY  RO      0            sum of y^2 of lane 0 after the DSP over 128 frames, >> 6
// 0x05C   LEVEL_SEQ     RO      0            + 1 per LEVEL_ENERGY (cleared with the SoC)
// Writes honour the strobes; other offsets read 0; responses OKAY; reads have no side effects.

module esi_spi_src_axil (
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF s_axi, ASSOCIATED_RESET aresetn" *)
    input  wire         aclk,
    input  wire         aresetn,
    input  wire         sink_aresetn,

    input  wire         spi_sclk,
    input  wire         spi_mosi,
    input  wire         spi_cs_n,
    output wire         spi_ready,

    input  wire [31:0]  level_energy,     // activity of lane 0 after the DSP (emg_spi_subsys)
    input  wire [31:0]  level_seq,

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

    output wire         m_tvalid,
    input  wire         m_tready,
    output reg  [127:0] m_tdata,
    output reg  [79:0]  m_tuser,
    output reg          m_tlast,

    output wire         src_rst           // for stream monitors
);
    localparam [31:0] ID_VALUE = 32'h4553_5331, VERSION_VALUE = 32'h0001_0000, FORMAT_VALUE = 32'h0100_0080;
    localparam [2:0]  N_BLOCKS = 3'd4, READY_BELOW = 3'd3;

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
    reg         run, soft_rst, clear_stats;
    reg  [31:0] offset, shift_cfg, expect_rate;
    wire        rst      = ~aresetn | soft_rst;
    wire        sink_rst = ~sink_aresetn;
    assign src_rst = rst;

    always @(posedge aclk) begin
        if (!aresetn) begin
            run <= 1'b0; soft_rst <= 1'b0; clear_stats <= 1'b0;
            offset <= 32'd2048; shift_cfg <= 32'd3; expect_rate <= 32'd2000;
        end else begin
            soft_rst    <= 1'b0;
            clear_stats <= 1'b0;
            if (soft_rst) run <= 1'b0;
            if (wr_en) begin
                case (wr_word)
                    10'h001: if (wr_strb[0]) clear_stats <= wr_data[0];
                    10'h00C: if (wr_strb[0]) begin
                        run      <= wr_data[0];
                        soft_rst <= wr_data[2];
                    end
                    10'h013: offset      <= strb32(offset, wr_data, wr_strb) & 32'h0000_FFFF;
                    10'h014: shift_cfg   <= strb32(shift_cfg, wr_data, wr_strb) & 32'h0000_0007;
                    10'h015: expect_rate <= strb32(expect_rate, wr_data, wr_strb);
                    default: ;
                endcase
            end
        end
    end

    // ---------------- receiver ----------------
    wire        rx_start, rx_sample, rx_good, rx_crc, rx_fmt, rx_ovf, rx_active, rx_cs;
    wire [15:0] rx_data;
    wire [31:0] rx_index, rx_rate;

    emg1_spi_rx u_rx (
        .aclk(aclk), .aresetn(aresetn),
        .spi_sclk(spi_sclk), .spi_mosi(spi_mosi), .spi_cs_n(spi_cs_n),
        .frame_start(rx_start), .sample_valid(rx_sample), .sample_data(rx_data),
        .frame_good(rx_good), .crc_err(rx_crc), .fmt_err(rx_fmt), .overflow_err(rx_ovf),
        .packet_index(rx_index), .packet_rate(rx_rate), .active(rx_active), .cs_level(rx_cs));

    wire rate_ok = (expect_rate == 32'd0) || (rx_rate == expect_rate);

    // ---------------- block store ----------------
    reg  [15:0] block_store [0:511];
    reg  [15:0] mem_q;
    reg  [31:0] meta_index [0:3];
    reg  [3:0]  meta_gap;

    reg         accept, have_store_exp;
    reg  [31:0] store_exp;
    reg  [1:0]  wblk, rblk;
    reg  [6:0]  wpos, rpos;
    reg  [2:0]  blocks;

    assign spi_ready = aresetn && (blocks < READY_BELOW);

    // ---------------- output ----------------
    localparam S_IDLE = 1'b0, S_READ = 1'b1;
    reg         state, valid_r, next_sos, sink_rst_q;
    reg  [31:0] k;

    assign m_tvalid = valid_r & ~rst & ~sink_rst & ~sink_rst_q;
    wire out_hs = m_tvalid & m_tready;

    wire               mem_rd = (state == S_IDLE) && run && !sink_rst && !valid_r && (blocks != 3'd0);
    wire signed [17:0] diff   = {2'b00, mem_q} - {2'b00, offset[15:0]};
    wire signed [24:0] wide   = $signed({{7{diff[17]}}, diff}) << shift_cfg[2:0];
    wire               sat_hi = (wide > 25'sd32767);
    wire               sat_lo = (wide < -25'sd32768);
    wire [15:0]        q      = sat_hi ? 16'h7FFF : sat_lo ? 16'h8000 : wide[15:0];
    wire               q_ovr  = sat_hi | sat_lo;

    wire               load   = (state == S_READ);
    wire               f_gap  = meta_gap[rblk] && (rpos == 7'd0) && !next_sos;
    wire               pop    = load && (rpos == 7'd127);
    wire               push   = rx_good && accept && rate_ok;

    // ---------------- statistics ----------------
    reg  [31:0] good_frames, crc_errors, seq_errors, frame_errors, overflow_errors, last_index, expected_index,
                last_rate, drop_cnt, rate_err_cnt, esi_frames, gap_cnt, ovr_cnt;
    reg         have_valid, have_expected;

    always @(posedge aclk) begin
        if (rx_sample && accept) block_store[{wblk, wpos}] <= rx_data;
        if (mem_rd)              mem_q <= block_store[{rblk, rpos}];
    end

    always @(posedge aclk) begin
        if (rst || sink_rst) begin
            accept <= 1'b0; have_store_exp <= 1'b0; store_exp <= 32'd0; wblk <= 2'd0; rblk <= 2'd0;
            wpos <= 7'd0; rpos <= 7'd0; blocks <= 3'd0; meta_gap <= 4'd0;
            state <= S_IDLE; valid_r <= 1'b0; next_sos <= 1'b1; k <= 32'd0;
            m_tdata <= 128'd0; m_tuser <= 80'd0; m_tlast <= 1'b0;
        end else begin
            // store
            if (rx_start) begin
                accept <= run && (blocks < N_BLOCKS);
                wpos   <= 7'd0;
            end else if (rx_good || rx_crc || rx_fmt) begin
                accept <= 1'b0;
            end
            if (rx_sample && accept) wpos <= wpos + 7'd1;
            if (push) begin
                meta_index[wblk] <= rx_index;
                meta_gap[wblk]   <= have_store_exp && (rx_index != store_exp);
                store_exp        <= rx_index + 32'd128;
                have_store_exp   <= 1'b1;
                wblk             <= wblk + 2'd1;
            end
            blocks <= blocks + (push ? 3'd1 : 3'd0) - (pop ? 3'd1 : 3'd0);

            // output: one frame at a time, the sink needs thousands of cycles per frame
            if (out_hs) valid_r <= 1'b0;
            case (state)
                S_IDLE: if (mem_rd) state <= S_READ;
                S_READ: begin
                    state    <= S_IDLE;
                    valid_r  <= 1'b1;
                    m_tdata  <= {112'd0, q};
                    m_tuser  <= {next_sos ? 32'd0 : k, meta_index[rblk] + {25'd0, rpos},
                                 9'd0, 1'b0, 2'b01, 1'b0, q_ovr, f_gap, next_sos};
                    m_tlast  <= (rpos == 7'd127);
                    k        <= next_sos ? 32'd1 : k + 32'd1;
                    next_sos <= 1'b0;
                    rpos     <= rpos + 7'd1;
                    if (pop) rblk <= rblk + 2'd1;
                end
            endcase
        end
    end

    always @(posedge aclk) sink_rst_q <= sink_rst;

    always @(posedge aclk) begin
        if (rst || clear_stats) begin
            good_frames <= 32'd0; crc_errors <= 32'd0; seq_errors <= 32'd0; frame_errors <= 32'd0;
            overflow_errors <= 32'd0; have_expected <= 1'b0;
        end else begin
            if (rx_crc) crc_errors <= crc_errors + 32'd1;
            if (rx_fmt) frame_errors <= frame_errors + 32'd1;
            if (rx_ovf) overflow_errors <= overflow_errors + 32'd1;
            if (rx_good) begin
                good_frames <= good_frames + 32'd1;
                if (have_expected && rx_index != expected_index) seq_errors <= seq_errors + 32'd1;
                have_expected <= 1'b1;
            end
        end
        if (rst) begin
            last_index <= 32'd0; expected_index <= 32'd0; last_rate <= 32'd0; have_valid <= 1'b0;
            drop_cnt <= 32'd0; rate_err_cnt <= 32'd0; esi_frames <= 32'd0; gap_cnt <= 32'd0; ovr_cnt <= 32'd0;
        end else begin
            if (rx_good) begin
                last_index     <= rx_index;
                expected_index <= rx_index + 32'd128;
                last_rate      <= rx_rate;
                have_valid     <= 1'b1;
                if (!rate_ok) rate_err_cnt <= rate_err_cnt + 32'd1;
            end
            if (rx_start && run && !sink_rst && blocks >= N_BLOCKS) drop_cnt <= drop_cnt + 32'd1;
            if (out_hs) begin
                esi_frames <= esi_frames + 32'd1;
                if (m_tuser[`ESI_F_GAP]) gap_cnt <= gap_cnt + 32'd1;
                if (m_tuser[`ESI_F_OVR]) ovr_cnt <= ovr_cnt + 32'd1;
            end
        end
    end

    always @* begin
        case (rd_word)
            10'h000: rd_data = ID_VALUE;
            10'h001: rd_data = VERSION_VALUE;
            10'h002: rd_data = {24'd0, m_tvalid, sink_rst, run, have_expected, have_valid, rx_active, rx_cs, spi_ready};
            10'h003: rd_data = good_frames;
            10'h004: rd_data = crc_errors;
            10'h005: rd_data = seq_errors;
            10'h006: rd_data = frame_errors;
            10'h007: rd_data = overflow_errors;
            10'h008: rd_data = last_index;
            10'h009: rd_data = expected_index;
            10'h00A: rd_data = last_rate;
            10'h00B: rd_data = FORMAT_VALUE;
            10'h00C: rd_data = {31'd0, run};
            10'h00D: rd_data = {29'd0, blocks};
            10'h00E: rd_data = drop_cnt;
            10'h00F: rd_data = rate_err_cnt;
            10'h010: rd_data = esi_frames;
            10'h011: rd_data = gap_cnt;
            10'h012: rd_data = ovr_cnt;
            10'h013: rd_data = offset;
            10'h014: rd_data = shift_cfg;
            10'h015: rd_data = expect_rate;
            10'h016: rd_data = level_energy;
            10'h017: rd_data = level_seq;
            default: rd_data = 32'd0;
        endcase
    end

    // AXI4-Lite protection bits and the read strobe are not used
    wire unused = &{1'b0, s_axi_awprot, s_axi_arprot, rd_en, 1'b0};

endmodule

`default_nettype wire
