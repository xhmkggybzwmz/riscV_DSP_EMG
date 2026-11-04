`timescale 1ns / 1ps
`default_nettype none

`include "esi_defs.vh"

// emg_dsp_axil - AXI4-Lite control page around the unchanged E3a DSP core
// emg_dsp (docs/soc/e3c_integration.md section 5). Base 0x1000_2000, 4 KB.
//
// Offset  Name        Access  Reset       Description
// 0x000   ID          RO      0x4453_5031 "DSP1"
// 0x004   VERSION     RO      0x0001_0000
// 0x008   CTRL        RW      0x0         [0] ENABLE: input handshake allowed (0: s_tready = 0, the
//                                         source is held; a frame inside the core completes)
//                                         [1] SOFT_RESET (write 1: one-cycle core reset; drops the
//                                         frame in the core and a pending output frame; reads 0)
//                                         [2] CLEAR (write 1: cfg_clear, filter state is cleared before
//                                         the next frame and that frame carries SOS; reads 0)
// 0x00C   STATUS      RO      -           [0] IDLE (core not busy) [1] BUSY [2] OVF_STICKY (core)
//                                         [3] COEF_PENDING (a coefficient write waits for IDLE)
//                                         [4] OUT_VALID (output frame waiting for the CNN)
// 0x010   SEC_EN      RW      0xD         [3:0] section enables: 0 DC, 1 notch, 2 BP0, 3 BP1
//                                         (0 = bypass). Sampled by the core when it accepts a
//                                         frame, so a change never splits a frame; a change clears
//                                         the filter state and the next output frame carries SOS.
// 0x014   COEF_ADDR   RW      0           [4:0] coefficient index (section*5 + {b0,b1,b2,a1,a2}),
//                                         auto-increment after each COEF_DATA write
// 0x018   COEF_DATA   W       -           [17:0] Q2.16 coefficient. The write is held (COEF_PENDING)
//                                         until the core is IDLE and then applied in one cycle: the
//                                         core only takes coef_we in S_IDLE, so a frame always uses
//                                         one coefficient set; the next frame clears the filter state
//                                         and carries SOS (core reset contract). A COEF_DATA write
//                                         while COEF_PENDING is ignored and counted in COEF_REJ.
//                                         Reads return the last written value (not the core array).
// 0x01C   FRAMES_IN   RO      0           frames accepted by the core     \
// 0x020   FRAMES_OUT  RO      0           frames delivered downstream      |  core statistics,
// 0x024   OUT_SAT     RO      0           frames with int16 saturation     |  32-bit, wrap around,
// 0x028   INT_SAT     RO      0           internal (int27) saturations     |  cleared by reset /
// 0x02C   RESETS      RO      0           filter state resets              /  SOFT_RESET
// 0x030   SOS_CNT     RO      0           input frames with SOS            \  counted by the page
// 0x034   GAP_CNT     RO      0           input frames with GAP             | at the input
// 0x038   LAST_SEQ    RO      0           sequence number of the last output frame
// 0x03C   COEF_REJ    RO      0           rejected COEF_DATA writes      /
// other offsets: read 0, writes ignored, OKAY. WSTRB: CTRL/SEC_EN/COEF_ADDR/COEF_DATA use byte 0
// (and bytes 1..2 for COEF_DATA[17:8]); a write with the needed byte strobes clear has no effect on
// those bits.

module emg_dsp_axil #(
    parameter N_CH      = 4,
    parameter COEF_FILE = ""
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

    // ESI in (PRE_DSP) / out (POST_DSP)
    input  wire         s_tvalid,
    output wire         s_tready,
    input  wire [127:0] s_tdata,
    input  wire [79:0]  s_tuser,
    input  wire         s_tlast,
    output wire         m_tvalid,
    input  wire         m_tready,
    output wire [127:0] m_tdata,
    output wire [79:0]  m_tuser,
    output wire         m_tlast,

    output wire         core_rst          // for stream monitors (reset of this stream endpoint)
);
    localparam [31:0] ID_VALUE = 32'h4453_5031, VERSION_VALUE = 32'h0001_0000;

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

    // ---------------- registers ----------------
    reg        enable, soft_rst, clear_p, coef_pend;
    reg [3:0]  sec_en;
    reg [4:0]  coef_addr;
    reg [17:0] coef_last, coef_hold;
    reg [4:0]  coef_hold_addr;
    reg [31:0] sos_cnt, gap_cnt, last_seq, coef_rej;

    wire rst = ~aresetn | soft_rst;
    assign core_rst = rst;
    // combinational, so the write lands in the same cycle the core is seen idle: if the core
    // accepts a frame at that edge too, it already uses the new coefficient (and clears/SOS)
    wire coef_we;

    wire [31:0] st_in, st_out, st_osat, st_isat, st_res;
    wire        st_ovf, busy;
    assign coef_we = coef_pend & ~busy & ~rst;
    wire        in_valid = s_tvalid & enable;
    wire        core_ready;
    assign s_tready = core_ready & enable;

    emg_dsp #(.N_CH(N_CH), .N_SEC(4), .COEF_FILE(COEF_FILE), .SEC_EN_DEFAULT(4'b1101)) u_dsp (
        .clk(aclk), .rst(rst), .cfg_sec_en(sec_en), .cfg_clear(clear_p),
        .coef_we(coef_we), .coef_addr({3'd0, coef_hold_addr}), .coef_wdata(coef_hold),
        .s_tvalid(in_valid), .s_tready(core_ready), .s_tdata(s_tdata), .s_tuser(s_tuser), .s_tlast(s_tlast),
        .m_tvalid(m_tvalid), .m_tready(m_tready), .m_tdata(m_tdata), .m_tuser(m_tuser), .m_tlast(m_tlast),
        .stat_frames_in(st_in), .stat_frames_out(st_out), .stat_out_sat(st_osat), .stat_int_sat(st_isat),
        .stat_resets(st_res), .stat_ovf_sticky(st_ovf), .busy(busy));

    wire [15:0] in_flags = s_tuser[`ESI_FLAGS];
    wire        in_hs = in_valid & core_ready;
    wire        out_hs = m_tvalid & m_tready;

    always @(posedge aclk) begin
        if (!aresetn) begin
            enable <= 1'b0; soft_rst <= 1'b0; clear_p <= 1'b0; coef_pend <= 1'b0;
            sec_en <= 4'b1101; coef_addr <= 5'd0; coef_last <= 18'd0; coef_hold <= 18'd0;
            coef_hold_addr <= 5'd0;
            sos_cnt <= 32'd0; gap_cnt <= 32'd0; last_seq <= 32'd0; coef_rej <= 32'd0;
        end else begin
            soft_rst <= 1'b0;
            clear_p  <= 1'b0;
            if (coef_we) coef_pend <= 1'b0;             // held coefficient write applied this cycle
            if (soft_rst) begin
                sos_cnt <= 32'd0; gap_cnt <= 32'd0; last_seq <= 32'd0; coef_rej <= 32'd0;
                coef_pend <= 1'b0;
            end else begin
                if (in_hs && in_flags[`ESI_F_SOS]) sos_cnt <= sos_cnt + 32'd1;
                if (in_hs && in_flags[`ESI_F_GAP]) gap_cnt <= gap_cnt + 32'd1;
                if (out_hs) last_seq <= m_tuser[`ESI_SEQ];
            end
            if (wr_en) begin
                case (wr_word)
                    10'h002: if (wr_strb[0]) begin
                        enable   <= wr_data[0];
                        soft_rst <= wr_data[1];
                        clear_p  <= wr_data[2];
                    end
                    10'h004: if (wr_strb[0]) sec_en <= wr_data[3:0];
                    10'h005: if (wr_strb[0]) coef_addr <= wr_data[4:0];
                    10'h006: begin
                        if (coef_pend || coef_addr > 5'd19) begin
                            coef_rej <= coef_rej + 32'd1;
                        end else begin
                            coef_last      <= {wr_strb[2] ? wr_data[17:16] : coef_last[17:16],
                                               wr_strb[1] ? wr_data[15:8]  : coef_last[15:8],
                                               wr_strb[0] ? wr_data[7:0]   : coef_last[7:0]};
                            coef_hold      <= {wr_strb[2] ? wr_data[17:16] : coef_last[17:16],
                                               wr_strb[1] ? wr_data[15:8]  : coef_last[15:8],
                                               wr_strb[0] ? wr_data[7:0]   : coef_last[7:0]};
                            coef_hold_addr <= coef_addr;
                            coef_addr      <= coef_addr + 5'd1;
                            coef_pend      <= 1'b1;
                        end
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
            10'h002: rd_data = {31'd0, enable};
            10'h003: rd_data = {27'd0, m_tvalid, coef_pend, st_ovf, busy, ~busy};
            10'h004: rd_data = {28'd0, sec_en};
            10'h005: rd_data = {27'd0, coef_addr};
            10'h006: rd_data = {{14{coef_last[17]}}, coef_last};
            10'h007: rd_data = st_in;
            10'h008: rd_data = st_out;
            10'h009: rd_data = st_osat;
            10'h00A: rd_data = st_isat;
            10'h00B: rd_data = st_res;
            10'h00C: rd_data = sos_cnt;
            10'h00D: rd_data = gap_cnt;
            10'h00E: rd_data = last_seq;
            10'h00F: rd_data = coef_rej;
            default: rd_data = 32'd0;
        endcase
    end

endmodule

`default_nettype wire
