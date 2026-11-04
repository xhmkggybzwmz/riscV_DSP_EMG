`timescale 1ns / 1ps
`default_nettype none

// emg_e3c_subsys - E3c recognition pipeline as one block for the SoC
// (docs/soc/e3c_integration.md):
//
//   esi_replay_axil --ESI PRE_DSP--> emg_dsp_axil --ESI POST_DSP--> emg_cnn_axil --> result FIFO
//        |  s_axi_rep                     |  s_axi_dsp                   |  s_axi_cnn
//        +--------------------------------+------------------------------+--> AXI4-Lite (SmartConnect)
//
// Control plane: three AXI4-Lite slaves (replay 0x1000_6000, DSP 0x1000_2000, CNN
// 0x1000_5000 in the SoC map). Data plane: ESI v1 valid/ready inside the block; the
// CPU never moves samples. The DSP and CNN arithmetic cores (rtl/dsp/emg_dsp.v,
// rtl/infer/emg_cnn.v) are instantiated unchanged inside the control pages.
// The tap/debug outputs are for the testbenches only; leave them open in the BD.

module emg_e3c_subsys #(
    parameter REPLAY_DEPTH_LOG2 = 11,
    parameter REPLAY_FRAMES = 0,
    parameter REPLAY_FILE = "",
    parameter DSP_COEF_FILE = "",
    parameter W1_FILE = "", parameter B1_FILE = "", parameter M1_FILE = "", parameter SH1_FILE = "",
    parameter W2_FILE = "", parameter B2_FILE = "", parameter M2_FILE = "", parameter SH2_FILE = "",
    parameter W3_FILE = "", parameter B3_FILE = "", parameter M3_FILE = "", parameter SH3_FILE = "",
    parameter FCW_FILE = "", parameter FCB_FILE = ""
) (
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF s_axi_rep:s_axi_dsp:s_axi_cnn, ASSOCIATED_RESET aresetn" *)
    input  wire         aclk,
    input  wire         aresetn,

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

    esi_replay_axil #(.DEPTH_LOG2(REPLAY_DEPTH_LOG2), .INIT_FRAMES(REPLAY_FRAMES), .REPLAY_FILE(REPLAY_FILE)) u_rep (
        .aclk(aclk), .aresetn(aresetn),
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
