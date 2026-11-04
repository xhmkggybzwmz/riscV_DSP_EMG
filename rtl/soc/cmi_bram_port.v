`timescale 1ns / 1ps
`default_nettype none

// cmi_bram_port - CMI slave -> native block-RAM port (E4a mailbox, port A of the
// shared dual-port BRAM; docs/soc/e4a_mailbox.md).
//
// The BRAM side is the AMD "BRAM_CTRL" style port that an AXI BRAM Controller
// would drive, so it connects to a Block Memory Generator in BRAM_Controller
// mode (or to dv/mbx/tb_bram_tdp.v in Icarus):
//   bram_addr   byte address of the word, 0 .. SIZE-4 (bits [1:0] = 0)
//   bram_we     byte write enables (0 for a read)
//   bram_rddata valid in the cycle after bram_en (read latency 1, no output
//               register), held until the next enable
//
// CMI behaviour (same as soc_ram_dp port B):
//   - req_ready = 1; a request accepted at edge T is answered in the cycle after
//     T (rsp_valid one cycle). Writes: bytes with wstrb[i] = 1 are written at
//     the accepting edge; the read data of a write is meaningless.
//   - Only addresses inside the window reach this port (soc_dmem_router +
//     soc_io_split); the offset is req_addr - BASE (BASE is SIZE aligned).
//   - rst: no access is enabled while rst is high, the response register is
//     cleared. The BRAM contents are not cleared (the mailbox protocol has its
//     own epoch / initialisation rules).
// Every store is in the BRAM when its response returns; the V1 core waits for
// the response before the next access, so the CPU's stores reach the BRAM in
// program order (the publish order of the mailbox protocol relies on this).

module cmi_bram_port #(
    parameter SIZE = 4096                  // bytes, power of two
) (
    input  wire        clk,
    input  wire        rst,

    input  wire        req_valid,
    output wire        req_ready,
    input  wire [31:0] req_addr,
    input  wire        req_we,
    input  wire [3:0]  req_wstrb,
    input  wire [31:0] req_wdata,
    output reg         rsp_valid,
    output wire [31:0] rsp_rdata,
    output wire        rsp_err,

    output wire        bram_clk,
    output wire        bram_rst,
    output wire        bram_en,
    output wire [3:0]  bram_we,
    output wire [31:0] bram_addr,
    output wire [31:0] bram_wrdata,
    input  wire [31:0] bram_rddata
);

    localparam AW = $clog2(SIZE);

    assign req_ready   = 1'b1;
    assign bram_clk    = clk;
    assign bram_rst    = rst;
    assign bram_en     = req_valid & ~rst;
    assign bram_we     = (req_valid & req_we & ~rst) ? req_wstrb : 4'b0000;
    assign bram_addr   = {{(32 - AW){1'b0}}, req_addr[AW-1:2], 2'b00};
    assign bram_wrdata = req_wdata;

    always @(posedge clk) begin
        if (rst) rsp_valid <= 1'b0;
        else     rsp_valid <= req_valid;
    end

    assign rsp_rdata = bram_rddata;
    assign rsp_err   = 1'b0;

endmodule

`default_nettype wire
