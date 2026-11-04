`timescale 1ns / 1ps
`default_nettype none

`include "esi_defs.vh"

// esi_replay_axil - synthesizable ESI v1 replay source with an AXI4-Lite control page
// (docs/soc/e3c_integration.md section 7). Base 0x1000_6000, 4 KB. Stands in for the
// future SPI receive path: the frames come from a block RAM, not from the CPU.
//
// Frame memory: DEPTH entries of 72 bits = {flags[7:0], lane3, lane2, lane1, lane0}
// (int16 lanes; ESI lanes 4..7 are 0). Initialised from REPLAY_FILE ($readmemh, one
// 18-hex-digit word per line) and writable through WADDR/WDATA_* while RUN = 0.
// Output frame k (k = 0, 1, ... since the last start/SOFT_RESET, counting loops):
//   tdata  = {64'd0, mem[i]}        i = k mod NFRAMES
//   flags  = mem[i][71:64]          (SOS/GAP/POST_DSP etc. as stored)
//   seq    = SEQ_BASE + k           timestamp = k
//   tlast  = (k mod 100 == 99) or the last frame of a pass
// Timing: PERIOD = 0 -> flow mode, the next frame is offered as soon as the previous
// one was accepted (simulation speed; not real time). PERIOD = P > 0 -> one frame
// every P cycles (2 kHz at 100 MHz: P = 50000); if a frame is still waiting for
// TREADY when the next period starts, that period is lost (LATE_CNT + 1) and the frame
// stays valid until accepted (ESI: VALID is never withdrawn).
//
// Offset  Name       Access  Reset        Description
// 0x000   ID         RO      0x5250_4C31  "RPL1"
// 0x004   VERSION    RO      0x0001_0000
// 0x008   CTRL       RW      0x0          [0] RUN  [1] LOOP (restart at frame 0 after NFRAMES)
//                                         [2] SOFT_RESET (write 1: stop, k = 0, counters 0, a waiting
//                                         frame is withdrawn - reset the consumers too; reads 0)
// 0x00C   STATUS     RO      -            [0] ACTIVE (RUN and not DONE) [1] DONE (NFRAMES sent, no LOOP)
//                                         [2] VALID (frame waiting for TREADY)
// 0x010   NFRAMES    RW      INIT_FRAMES  frames per pass, 1..DEPTH (values outside are clamped)
// 0x014   PERIOD     RW      50000        cycles per frame, 0 = flow mode
// 0x018   FRAME_CNT  RO      0            frames delivered (handshakes) since start/SOFT_RESET
// 0x01C   LATE_CNT   RO      0            periods lost because the previous frame was not accepted
// 0x020   SEQ_BASE   RW      0            sequence number of frame k = 0
// 0x024   RPTR       RO      0            index of the next frame to send
// 0x028   WADDR      RW      0            frame memory index for writes, auto-increment
// 0x02C   WDATA_LO   RW      0            {lane1, lane0}
// 0x030   WDATA_HI   RW      0            {lane3, lane2}
// 0x034   WFLAGS     W       -            [7:0] flags; the write stores {flags, HI, LO} at WADDR.
//                                         Ignored while RUN = 1 (WREJ + 1)
// 0x038   WREJ       RO      0            rejected frame-memory writes
// 0x03C   DEPTH      RO      DEPTH        frame memory size
// Writes use all four strobes as given (absent bytes keep their value, except WFLAGS
// which needs byte 0); other offsets read 0; responses OKAY; reads have no side effects.

module esi_replay_axil #(
    parameter DEPTH_LOG2  = 11,
    parameter INIT_FRAMES = 0,
    parameter REPLAY_FILE = ""
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

    output wire         m_tvalid,
    input  wire         m_tready,
    output reg  [127:0] m_tdata,
    output reg  [79:0]  m_tuser,
    output reg          m_tlast,

    output wire         src_rst           // for stream monitors
);
    localparam DEPTH = 1 << DEPTH_LOG2;
    localparam [31:0] ID_VALUE = 32'h5250_4C31, VERSION_VALUE = 32'h0001_0000;

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

    // ---------------- frame memory (simple dual port block RAM) ----------------
    reg [71:0] mem [0:DEPTH-1];
    initial if (REPLAY_FILE != "") $readmemh(REPLAY_FILE, mem);

    reg  [DEPTH_LOG2-1:0] rptr, waddr;
    reg  [71:0]           nxt;                   // mem[rptr], registered read
    reg  [31:0]           wlo, whi, wrej;
    wire                  mem_we;
    wire [71:0]           mem_wd = {wr_data[7:0], whi, wlo};
    always @(posedge aclk) begin
        if (mem_we) mem[waddr] <= mem_wd;
        nxt <= mem[rptr];
    end

    // ---------------- control ----------------
    reg        run, loop_en, soft_rst, done, valid_r, nxt_ok;
    reg [DEPTH_LOG2:0] nframes;
    reg [31:0] period, frame_cnt, late_cnt, seq_base, k, tick_cnt;
    reg [6:0]  k100;                                    // k mod 100 (tlast)
    wire rst = ~aresetn | soft_rst;
    assign src_rst = rst;
    assign m_tvalid = valid_r & ~rst;
    assign mem_we = wr_en && wr_word == 10'h00D && wr_strb[0] && !run;

    wire [DEPTH_LOG2:0] nfr = (nframes == 0) ? 1 : (nframes > DEPTH) ? DEPTH : nframes;
    wire active = run & ~done;
    wire tick   = (period == 32'd0) ? 1'b1 : (tick_cnt == 32'd0);
    wire launch = active && tick && nxt_ok && (!valid_r || (m_tvalid && m_tready));

    always @(posedge aclk) begin
        if (rst) begin
            valid_r <= 1'b0; m_tdata <= 128'd0; m_tuser <= 80'd0; m_tlast <= 1'b0;
            rptr <= 0; k <= 32'd0; k100 <= 7'd0; done <= 1'b0; nxt_ok <= 1'b0;
            frame_cnt <= 32'd0; late_cnt <= 32'd0; tick_cnt <= 32'd0;
        end else begin
            nxt_ok <= 1'b1;                                    // nxt follows rptr one cycle later
            if (m_tvalid && m_tready) begin
                frame_cnt <= frame_cnt + 32'd1;
                if (!launch) valid_r <= 1'b0;
            end
            // period timer runs while active
            if (!active || period == 32'd0) tick_cnt <= 32'd0;
            else if (tick_cnt == period - 32'd1) tick_cnt <= 32'd0;
            else tick_cnt <= tick_cnt + 32'd1;
            if (active && period != 32'd0 && tick && valid_r && !(m_tvalid && m_tready))
                late_cnt <= late_cnt + 32'd1;
            if (launch) begin
                valid_r <= 1'b1;
                m_tdata <= {64'd0, nxt[63:0]};
                m_tuser <= {k, seq_base + k, 8'd0, nxt[71:64]};
                m_tlast <= (k100 == 7'd99) || (rptr == nfr - 1);
                k <= k + 32'd1;
                k100 <= (k100 == 7'd99) ? 7'd0 : k100 + 7'd1;
                nxt_ok <= 1'b0;
                if (rptr == nfr - 1) begin
                    rptr <= 0;
                    if (!loop_en) done <= 1'b1;
                end else begin
                    rptr <= rptr + 1'b1;
                end
            end
        end
    end

    always @(posedge aclk) begin
        if (!aresetn) begin
            run <= 1'b0; loop_en <= 1'b0; soft_rst <= 1'b0;
            nframes <= INIT_FRAMES; period <= 32'd50000; seq_base <= 32'd0;
            waddr <= 0; wlo <= 32'd0; whi <= 32'd0; wrej <= 32'd0;
        end else begin
            soft_rst <= 1'b0;
            if (soft_rst) begin
                run  <= 1'b0;
                wrej <= 32'd0;
            end
            if (wr_en) begin
                case (wr_word)
                    10'h002: if (wr_strb[0]) begin
                        run      <= wr_data[0];
                        loop_en  <= wr_data[1];
                        soft_rst <= wr_data[2];
                    end
                    10'h004: nframes  <= strb32({{(31 - DEPTH_LOG2){1'b0}}, nframes}, wr_data, wr_strb);
                    10'h005: period   <= strb32(period, wr_data, wr_strb);
                    10'h008: seq_base <= strb32(seq_base, wr_data, wr_strb);
                    10'h00A: waddr    <= strb32({{(32 - DEPTH_LOG2){1'b0}}, waddr}, wr_data, wr_strb);
                    10'h00B: wlo      <= strb32(wlo, wr_data, wr_strb);
                    10'h00C: whi      <= strb32(whi, wr_data, wr_strb);
                    10'h00D: if (wr_strb[0]) begin
                        if (run) wrej  <= wrej + 32'd1;
                        else     waddr <= waddr + 1'b1;
                    end
                    default: ;
                endcase
            end
        end
    end

    always @* begin
        case (rd_word)
            10'h000: rd_data = ID_VALUE;
            10'h001: rd_data = VERSION_VALUE;
            10'h002: rd_data = {30'd0, loop_en, run};
            10'h003: rd_data = {29'd0, m_tvalid, done, active};
            10'h004: rd_data = {{(31 - DEPTH_LOG2){1'b0}}, nframes};
            10'h005: rd_data = period;
            10'h006: rd_data = frame_cnt;
            10'h007: rd_data = late_cnt;
            10'h008: rd_data = seq_base;
            10'h009: rd_data = {{(32 - DEPTH_LOG2){1'b0}}, rptr};
            10'h00A: rd_data = {{(32 - DEPTH_LOG2){1'b0}}, waddr};
            10'h00B: rd_data = wlo;
            10'h00C: rd_data = whi;
            10'h00E: rd_data = wrej;
            10'h00F: rd_data = DEPTH;
            default: rd_data = 32'd0;
        endcase
    end

endmodule

`default_nettype wire
