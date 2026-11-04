`timescale 1ns / 1ps
`default_nettype none

// emg_spi_subsys - the E3c recognition pipeline fed by the STM32 (docs/system/stm32_spi_live.md):
// emg_e5_subsys with esi_spi_src_axil instead of esi_dma_src_axil, plus the lane 0 activity counter.
//
//   SPI (J2 Pmod) --> esi_spi_src_axil --ESI PRE_DSP--> emg_dsp_axil --ESI POST_DSP--> emg_cnn_axil
//                          |  s_axi_rep (PS)                 |  s_axi_dsp (CPU)            |  s_axi_cnn (CPU)
//
// Control plane as in E5a: the source page is in the PS reset domain (src_aresetn), DSP and CNN pages belong to
// the RV32I (aresetn). The tap/debug outputs are for the testbenches only; leave them open in the BD.

module emg_spi_subsys #(
    parameter DSP_COEF_FILE = "",
    parameter W1_FILE = "", parameter B1_FILE = "", parameter M1_FILE = "", parameter SH1_FILE = "",
    parameter W2_FILE = "", parameter B2_FILE = "", parameter M2_FILE = "", parameter SH2_FILE = "",
    parameter W3_FILE = "", parameter B3_FILE = "", parameter M3_FILE = "", parameter SH3_FILE = "",
    parameter FCW_FILE = "", parameter FCB_FILE = ""
) (
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF s_axi_rep:s_axi_dsp:s_axi_cnn, ASSOCIATED_RESET aresetn:src_aresetn" *)
    input  wire         aclk,
    input  wire         aresetn,
    input  wire         src_aresetn,

    input  wire         spi_sclk,
    input  wire         spi_mosi,
    input  wire         spi_cs_n,
    output wire         spi_ready,

    input  wire [11:0]  s_axi_rep_awaddr,  input wire [2:0] s_axi_rep_awprot, input wire s_axi_rep_awvalid,
    output wire         s_axi_rep_awready,
    input  wire [31:0]  s_axi_rep_wdata,   input wire [3:0] s_axi_rep_wstrb,  input wire s_axi_rep_wvalid,
    output wire         s_axi_rep_wready,
    output wire [1:0]   s_axi_rep_bresp,   output wire s_axi_rep_bvalid,      input wire s_axi_rep_bready,
    input  wire [11:0]  s_axi_rep_araddr,  input wire [2:0] s_axi_rep_arprot, input wire s_axi_rep_arvalid,
    output wire         s_axi_rep_arready,
    output wire [31:0]  s_axi_rep_rdata,   output wire [1:0] s_axi_rep_rresp, output wire s_axi_rep_rvalid,
    input  wire         s_axi_rep_rready,

    input  wire [11:0]  s_axi_dsp_awaddr,  input wire [2:0] s_axi_dsp_awprot, input wire s_axi_dsp_awvalid,
    output wire         s_axi_dsp_awready,
    input  wire [31:0]  s_axi_dsp_wdata,   input wire [3:0] s_axi_dsp_wstrb,  input wire s_axi_dsp_wvalid,
    output wire         s_axi_dsp_wready,
    output wire [1:0]   s_axi_dsp_bresp,   output wire s_axi_dsp_bvalid,      input wire s_axi_dsp_bready,
    input  wire [11:0]  s_axi_dsp_araddr,  input wire [2:0] s_axi_dsp_arprot, input wire s_axi_dsp_arvalid,
    output wire         s_axi_dsp_arready,
    output wire [31:0]  s_axi_dsp_rdata,   output wire [1:0] s_axi_dsp_rresp, output wire s_axi_dsp_rvalid,
    input  wire         s_axi_dsp_rready,

    input  wire [11:0]  s_axi_cnn_awaddr,  input wire [2:0] s_axi_cnn_awprot, input wire s_axi_cnn_awvalid,
    output wire         s_axi_cnn_awready,
    input  wire [31:0]  s_axi_cnn_wdata,   input wire [3:0] s_axi_cnn_wstrb,  input wire s_axi_cnn_wvalid,
    output wire         s_axi_cnn_wready,
    output wire [1:0]   s_axi_cnn_bresp,   output wire s_axi_cnn_bvalid,      input wire s_axi_cnn_bready,
    input  wire [11:0]  s_axi_cnn_araddr,  input wire [2:0] s_axi_cnn_arprot, input wire s_axi_cnn_arvalid,
    output wire         s_axi_cnn_arready,
    output wire [31:0]  s_axi_cnn_rdata,   output wire [1:0] s_axi_cnn_rresp, output wire s_axi_cnn_rvalid,
    input  wire         s_axi_cnn_rready,

    // testbench taps (ESI links and CNN debug port)
    output wire         tap_rep_valid, output wire tap_rep_ready, output wire [127:0] tap_rep_data,
    output wire [79:0]  tap_rep_user,  output wire tap_rep_last,  output wire tap_rep_rst,
    output wire         tap_dsp_valid, output wire tap_dsp_ready, output wire [127:0] tap_dsp_data,
    output wire [79:0]  tap_dsp_user,  output wire tap_dsp_last,  output wire tap_dsp_rst,
    output wire         tap_cnn_rst,   output wire tap_res_event,
    output wire         dbg_valid, output wire [2:0] dbg_layer, output wire [5:0] dbg_ch,
    output wire [23:0]  dbg_step, output wire [7:0] dbg_seg, output wire [31:0] dbg_acc, output wire [7:0] dbg_act
);
    wire         r_valid, r_ready, r_last, d_valid, d_ready, d_last;
    wire [127:0] r_data, d_data;
    wire [79:0]  r_user, d_user;

    // ---------------- activity of lane 0 after the DSP, for proportional control on the PS ----------------
    // sum of y^2 over 128 frames (64 ms at 2 kHz), >> 6: RMS = sqrt(ENERGY / 2), no result of the CNN involved
    wire               lv_hs = d_valid & d_ready;
    wire signed [15:0] lv_y  = d_data[15:0];
    wire signed [31:0] lv_sq = lv_y * lv_y;
    reg  [37:0]        lv_acc;
    reg  [6:0]         lv_n;
    reg  [31:0]        lv_energy, lv_seq;
    wire [37:0]        lv_sum = lv_acc + {6'd0, lv_sq};
    always @(posedge aclk) begin
        if (!aresetn) begin
            lv_acc <= 38'd0; lv_n <= 7'd0; lv_energy <= 32'd0; lv_seq <= 32'd0;
        end else if (lv_hs) begin
            if (d_user[0] || d_user[1]) begin         // SOS / GAP: the filters restarted
                lv_acc <= {6'd0, lv_sq}; lv_n <= 7'd1;
            end else if (lv_n == 7'd127) begin
                lv_energy <= lv_sum[37:6]; lv_seq <= lv_seq + 32'd1;
                lv_acc <= 38'd0; lv_n <= 7'd0;
            end else begin
                lv_acc <= lv_sum; lv_n <= lv_n + 7'd1;
            end
        end
    end

    esi_spi_src_axil u_rep (
        .aclk(aclk), .aresetn(src_aresetn), .sink_aresetn(aresetn),
        .spi_sclk(spi_sclk), .spi_mosi(spi_mosi), .spi_cs_n(spi_cs_n), .spi_ready(spi_ready),
        .level_energy(lv_energy), .level_seq(lv_seq),
        .s_axi_awaddr(s_axi_rep_awaddr), .s_axi_awprot(s_axi_rep_awprot), .s_axi_awvalid(s_axi_rep_awvalid),
        .s_axi_awready(s_axi_rep_awready), .s_axi_wdata(s_axi_rep_wdata), .s_axi_wstrb(s_axi_rep_wstrb),
        .s_axi_wvalid(s_axi_rep_wvalid), .s_axi_wready(s_axi_rep_wready), .s_axi_bresp(s_axi_rep_bresp),
        .s_axi_bvalid(s_axi_rep_bvalid), .s_axi_bready(s_axi_rep_bready), .s_axi_araddr(s_axi_rep_araddr),
        .s_axi_arprot(s_axi_rep_arprot), .s_axi_arvalid(s_axi_rep_arvalid), .s_axi_arready(s_axi_rep_arready),
        .s_axi_rdata(s_axi_rep_rdata), .s_axi_rresp(s_axi_rep_rresp), .s_axi_rvalid(s_axi_rep_rvalid),
        .s_axi_rready(s_axi_rep_rready),
        .m_tvalid(r_valid), .m_tready(r_ready), .m_tdata(r_data), .m_tuser(r_user), .m_tlast(r_last),
        .src_rst(tap_rep_rst));

    emg_dsp_axil #(.N_CH(4), .COEF_FILE(DSP_COEF_FILE)) u_dsp (
        .aclk(aclk), .aresetn(aresetn),
        .s_axi_awaddr(s_axi_dsp_awaddr), .s_axi_awprot(s_axi_dsp_awprot), .s_axi_awvalid(s_axi_dsp_awvalid),
        .s_axi_awready(s_axi_dsp_awready), .s_axi_wdata(s_axi_dsp_wdata), .s_axi_wstrb(s_axi_dsp_wstrb),
        .s_axi_wvalid(s_axi_dsp_wvalid), .s_axi_wready(s_axi_dsp_wready), .s_axi_bresp(s_axi_dsp_bresp),
        .s_axi_bvalid(s_axi_dsp_bvalid), .s_axi_bready(s_axi_dsp_bready), .s_axi_araddr(s_axi_dsp_araddr),
        .s_axi_arprot(s_axi_dsp_arprot), .s_axi_arvalid(s_axi_dsp_arvalid), .s_axi_arready(s_axi_dsp_arready),
        .s_axi_rdata(s_axi_dsp_rdata), .s_axi_rresp(s_axi_dsp_rresp), .s_axi_rvalid(s_axi_dsp_rvalid),
        .s_axi_rready(s_axi_dsp_rready),
        .s_tvalid(r_valid), .s_tready(r_ready), .s_tdata(r_data), .s_tuser(r_user), .s_tlast(r_last),
        .m_tvalid(d_valid), .m_tready(d_ready), .m_tdata(d_data), .m_tuser(d_user), .m_tlast(d_last),
        .core_rst(tap_dsp_rst));

    emg_cnn_axil #(.N_CH(4),
        .W1_FILE(W1_FILE), .B1_FILE(B1_FILE), .M1_FILE(M1_FILE), .SH1_FILE(SH1_FILE),
        .W2_FILE(W2_FILE), .B2_FILE(B2_FILE), .M2_FILE(M2_FILE), .SH2_FILE(SH2_FILE),
        .W3_FILE(W3_FILE), .B3_FILE(B3_FILE), .M3_FILE(M3_FILE), .SH3_FILE(SH3_FILE),
        .FCW_FILE(FCW_FILE), .FCB_FILE(FCB_FILE)) u_cnn (
        .aclk(aclk), .aresetn(aresetn),
        .s_axi_awaddr(s_axi_cnn_awaddr), .s_axi_awprot(s_axi_cnn_awprot), .s_axi_awvalid(s_axi_cnn_awvalid),
        .s_axi_awready(s_axi_cnn_awready), .s_axi_wdata(s_axi_cnn_wdata), .s_axi_wstrb(s_axi_cnn_wstrb),
        .s_axi_wvalid(s_axi_cnn_wvalid), .s_axi_wready(s_axi_cnn_wready), .s_axi_bresp(s_axi_cnn_bresp),
        .s_axi_bvalid(s_axi_cnn_bvalid), .s_axi_bready(s_axi_cnn_bready), .s_axi_araddr(s_axi_cnn_araddr),
        .s_axi_arprot(s_axi_cnn_arprot), .s_axi_arvalid(s_axi_cnn_arvalid), .s_axi_arready(s_axi_cnn_arready),
        .s_axi_rdata(s_axi_cnn_rdata), .s_axi_rresp(s_axi_cnn_rresp), .s_axi_rvalid(s_axi_cnn_rvalid),
        .s_axi_rready(s_axi_cnn_rready),
        .s_tvalid(d_valid), .s_tready(d_ready), .s_tdata(d_data), .s_tuser(d_user), .s_tlast(d_last),
        .core_rst(tap_cnn_rst),
        .dbg_valid(dbg_valid), .dbg_layer(dbg_layer), .dbg_ch(dbg_ch), .dbg_step(dbg_step), .dbg_seg(dbg_seg),
        .dbg_acc(dbg_acc), .dbg_act(dbg_act), .res_event(tap_res_event));

    assign tap_rep_valid = r_valid; assign tap_rep_ready = r_ready; assign tap_rep_data = r_data;
    assign tap_rep_user  = r_user;  assign tap_rep_last  = r_last;
    assign tap_dsp_valid = d_valid; assign tap_dsp_ready = d_ready; assign tap_dsp_data = d_data;
    assign tap_dsp_user  = d_user;  assign tap_dsp_last  = d_last;

endmodule

`default_nettype wire
