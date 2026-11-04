`timescale 1ns / 1ps
`default_nettype none

// soc_dmem_router - routes the core's CMI data port by exact address window
// (docs/soc/memory_map.md, docs/soc/soc_bus_architecture.md section 3).
//
//   0 <= addr < RAM_BYTES            -> local RAM (port B of soc_ram_dp)
//   addr in one of the IO windows    -> MMIO: CMI side of cmi2axil -> AXI
//                                       (production: SYSCTRL 0x1000_0000, 4 KB)
//   any other address                -> error, answered by the router itself
//                                       one cycle after acceptance
//
// Only implemented windows are forwarded. An unmapped address (e.g.
// 0x0001_0000, 0x1000_1000, 0x2000_0008) never produces a request on the RAM
// or the AXI side, so it cannot alias onto a real register (the SmartConnect
// with a single address segment does not return DECERR, see
// docs/soc/soc_bus_architecture.md section 10). The windows are parameters:
// IO_WINDOWS windows, window i = [IO_BASE[32i+:32], IO_BASE + IO_SIZE).
// They must match the AXI address map of the block design.
//
// The router is not an interconnect: one CMI master, at most one outstanding
// request (CMI v1). The request is forwarded combinationally to the selected
// target (valid/fields; ready comes back from that target). On the accepting
// edge the target is recorded, and the response is taken only from that
// target, so every request gets exactly one response from the right place and
// a store is forwarded to exactly one target. rst clears the record and the
// error response, so no response for a pre-reset request is passed on.

module soc_dmem_router #(
    parameter [31:0]               RAM_BYTES  = 32'h0001_0000,
    parameter                      IO_WINDOWS = 1,
    parameter [32*IO_WINDOWS-1:0]  IO_BASE    = 32'h1000_0000,
    parameter [32*IO_WINDOWS-1:0]  IO_SIZE    = 32'h0000_1000
) (
    input  wire        clk,
    input  wire        rst,

    // from the core (CMI slave side)
    input  wire        s_req_valid,
    output wire        s_req_ready,
    input  wire [31:0] s_req_addr,
    input  wire        s_req_we,
    input  wire [3:0]  s_req_wstrb,
    input  wire [31:0] s_req_wdata,
    output wire        s_rsp_valid,
    output wire [31:0] s_rsp_rdata,
    output wire        s_rsp_err,

    // to the local RAM (CMI master side)
    output wire        ram_req_valid,
    input  wire        ram_req_ready,
    output wire [31:0] ram_req_addr,
    output wire        ram_req_we,
    output wire [3:0]  ram_req_wstrb,
    output wire [31:0] ram_req_wdata,
    input  wire        ram_rsp_valid,
    input  wire [31:0] ram_rsp_rdata,
    input  wire        ram_rsp_err,

    // to the MMIO path / cmi2axil (CMI master side)
    output wire        io_req_valid,
    input  wire        io_req_ready,
    output wire [31:0] io_req_addr,
    output wire        io_req_we,
    output wire [3:0]  io_req_wstrb,
    output wire [31:0] io_req_wdata,
    input  wire        io_rsp_valid,
    input  wire [31:0] io_rsp_rdata,
    input  wire        io_rsp_err
);

    localparam [1:0] T_NONE = 2'd0, T_RAM = 2'd1, T_IO = 2'd2, T_ERR = 2'd3;

    // ---- address decode (constant windows; 33-bit compare avoids overflow) ----
    wire sel_ram = (s_req_addr < RAM_BYTES);

    wire [IO_WINDOWS-1:0] io_hit;
    genvar w;
    generate
        for (w = 0; w < IO_WINDOWS; w = w + 1) begin : g_io_win
            wire [32:0] lo = {1'b0, IO_BASE[32*w +: 32]};
            wire [32:0] hi = {1'b0, IO_BASE[32*w +: 32]} + {1'b0, IO_SIZE[32*w +: 32]};
            assign io_hit[w] = ({1'b0, s_req_addr} >= lo) && ({1'b0, s_req_addr} < hi);
        end
    endgenerate

    wire sel_io  = !sel_ram && (|io_hit);
    wire sel_err = !sel_ram && !sel_io;

    // synthesis translate_off
    integer cw;
    initial begin
        for (cw = 0; cw < IO_WINDOWS; cw = cw + 1) begin
            if (IO_SIZE[32*cw +: 32] == 32'd0 ||
                {1'b0, IO_BASE[32*cw +: 32]} + {1'b0, IO_SIZE[32*cw +: 32]} > 33'h1_0000_0000 ||
                IO_BASE[32*cw +: 32] < RAM_BYTES) begin
                $display("ERROR: soc_dmem_router: bad IO window %0d base=%h size=%h",
                         cw, IO_BASE[32*cw +: 32], IO_SIZE[32*cw +: 32]);
                $finish;
            end
        end
    end
    // synthesis translate_on

    // request forwarding (combinational; the core drives stable registered fields)
    assign ram_req_valid = s_req_valid & sel_ram;
    assign ram_req_addr  = s_req_addr;
    assign ram_req_we    = s_req_we;
    assign ram_req_wstrb = s_req_wstrb;
    assign ram_req_wdata = s_req_wdata;

    assign io_req_valid  = s_req_valid & sel_io;
    assign io_req_addr   = s_req_addr;
    assign io_req_we     = s_req_we;
    assign io_req_wstrb  = s_req_wstrb;
    assign io_req_wdata  = s_req_wdata;

    assign s_req_ready = sel_ram ? ram_req_ready :
                         sel_io  ? io_req_ready  : 1'b1;      // error target is always ready

    wire hs = s_req_valid & s_req_ready;

    // target of the outstanding request; error responder
    reg [1:0] target;
    reg       err_rsp;

    always @(posedge clk) begin
        if (rst) begin
            target  <= T_NONE;
            err_rsp <= 1'b0;
        end else begin
            err_rsp <= hs & sel_err;
            if (hs)
                target <= sel_ram ? T_RAM : sel_io ? T_IO : T_ERR;
            else if (s_rsp_valid)
                target <= T_NONE;
        end
    end

    // response from the recorded target only
    assign s_rsp_valid = (target == T_RAM) ? ram_rsp_valid :
                         (target == T_IO)  ? io_rsp_valid  :
                         (target == T_ERR) ? err_rsp       : 1'b0;
    assign s_rsp_rdata = (target == T_IO) ? io_rsp_rdata : ram_rsp_rdata;
    assign s_rsp_err   = (target == T_RAM) ? ram_rsp_err :
                         (target == T_IO)  ? io_rsp_err  : 1'b1;

endmodule

`default_nettype wire
