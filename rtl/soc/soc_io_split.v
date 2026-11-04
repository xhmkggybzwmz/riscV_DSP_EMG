`timescale 1ns / 1ps
`default_nettype none

// soc_io_split - splits the router's MMIO CMI port into two CMI masters by one
// address window (E4a mailbox, docs/soc/e4a_mailbox.md):
//
//   addr in [B_BASE, B_BASE + B_SIZE)  -> port b (cmi_bram_port, mailbox BRAM)
//   any other address                  -> port a (cmi2axil, AXI4-Lite MMIO)
//
// soc_dmem_router forwards only its implemented windows, so port a sees only
// the AXI windows and port b only the mailbox window. Same scheme as the
// router: the request is forwarded combinationally to the selected port, the
// target is recorded on the accepting edge and the response is taken only from
// that target (CMI v1: one outstanding request). rst clears the record.

module soc_io_split #(
    parameter [31:0] B_BASE = 32'h2002_0000,
    parameter [31:0] B_SIZE = 32'h0000_1000
) (
    input  wire        clk,
    input  wire        rst,

    // from soc_dmem_router (CMI slave side)
    input  wire        s_req_valid,
    output wire        s_req_ready,
    input  wire [31:0] s_req_addr,
    input  wire        s_req_we,
    input  wire [3:0]  s_req_wstrb,
    input  wire [31:0] s_req_wdata,
    output wire        s_rsp_valid,
    output wire [31:0] s_rsp_rdata,
    output wire        s_rsp_err,

    // port a (CMI master side)
    output wire        a_req_valid,
    input  wire        a_req_ready,
    output wire [31:0] a_req_addr,
    output wire        a_req_we,
    output wire [3:0]  a_req_wstrb,
    output wire [31:0] a_req_wdata,
    input  wire        a_rsp_valid,
    input  wire [31:0] a_rsp_rdata,
    input  wire        a_rsp_err,

    // port b (CMI master side)
    output wire        b_req_valid,
    input  wire        b_req_ready,
    output wire [31:0] b_req_addr,
    output wire        b_req_we,
    output wire [3:0]  b_req_wstrb,
    output wire [31:0] b_req_wdata,
    input  wire        b_rsp_valid,
    input  wire [31:0] b_rsp_rdata,
    input  wire        b_rsp_err
);

    wire sel_b = ({1'b0, s_req_addr} >= {1'b0, B_BASE}) &&
                 ({1'b0, s_req_addr} <  {1'b0, B_BASE} + {1'b0, B_SIZE});

    assign a_req_valid = s_req_valid & ~sel_b;
    assign a_req_addr  = s_req_addr;
    assign a_req_we    = s_req_we;
    assign a_req_wstrb = s_req_wstrb;
    assign a_req_wdata = s_req_wdata;

    assign b_req_valid = s_req_valid & sel_b;
    assign b_req_addr  = s_req_addr;
    assign b_req_we    = s_req_we;
    assign b_req_wstrb = s_req_wstrb;
    assign b_req_wdata = s_req_wdata;

    assign s_req_ready = sel_b ? b_req_ready : a_req_ready;

    // target of the outstanding request: 0 none, 1 a, 2 b
    reg [1:0] target;
    always @(posedge clk) begin
        if (rst)
            target <= 2'd0;
        else if (s_req_valid && s_req_ready)
            target <= sel_b ? 2'd2 : 2'd1;
        else if (s_rsp_valid)
            target <= 2'd0;
    end

    assign s_rsp_valid = (target == 2'd1) ? a_rsp_valid :
                         (target == 2'd2) ? b_rsp_valid : 1'b0;
    assign s_rsp_rdata = (target == 2'd2) ? b_rsp_rdata : a_rsp_rdata;
    assign s_rsp_err   = (target == 2'd2) ? b_rsp_err   : a_rsp_err;

endmodule

`default_nettype wire
