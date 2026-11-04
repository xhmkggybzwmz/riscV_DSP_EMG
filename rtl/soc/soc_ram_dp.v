`timescale 1ns / 1ps
`default_nettype none

// soc_ram_dp - local RAM of the CPU subsystem: synchronous true dual-port
// block RAM with two CMI slave ports (docs/cpu/cpu_mem_interface.md).
//
//   Port A : instruction fetch, read only
//   Port B : data access, read / write with byte enables
//
// CMI behaviour (both ports):
//   - req_ready is always 1; a request accepted at edge T gets its response in
//     the cycle after T (rsp_valid for one cycle), like the CMI BRAM bridge of
//     the spec. Read data comes straight from the RAM output register.
//   - Address check: req_addr must be < MEM_BYTES (upper bits zero). Any other
//     address returns rsp_err = 1, the RAM is not enabled and nothing is
//     written: there is NO address wrap-around.
//   - Writes (port B): bytes with wstrb[i] = 1 are written at the accepting
//     edge; the read data of a write is meaningless.
//   - rst: no RAM access is enabled while rst is high and the response
//     registers are cleared, so a request accepted before reset never gets a
//     response afterwards. RAM contents are not cleared.
//   - Same address on both ports in one cycle (port B writing): port A returns
//     the old word in RTL simulation; the hardware result of such a collision
//     is not relied on. The V1 core never has an instruction fetch and a data
//     access outstanding at the same time, so collisions cannot happen.
//
// Initialisation: INIT_FILE (hex, one 32-bit word per line from address 0) is
// loaded with $readmemh in an initial block, which Vivado uses as BRAM
// initial content. Leave it empty for no initialisation.
//
// Written for Vivado block-RAM inference (synchronous read, read-first port B,
// byte-wide write enable). Confirm the result in the synthesis report.

module soc_ram_dp #(
    parameter MEM_BYTES = 65536,          // power of two, >= 8
    parameter INIT_FILE = ""
) (
    input  wire        clk,
    input  wire        rst,

    // port A: instruction fetch (CMI, read only)
    input  wire        a_req_valid,
    output wire        a_req_ready,
    input  wire [31:0] a_req_addr,
    output reg         a_rsp_valid,
    output wire [31:0] a_rsp_rdata,
    output reg         a_rsp_err,

    // port B: data (CMI)
    input  wire        b_req_valid,
    output wire        b_req_ready,
    input  wire [31:0] b_req_addr,
    input  wire        b_req_we,
    input  wire [3:0]  b_req_wstrb,
    input  wire [31:0] b_req_wdata,
    output reg         b_rsp_valid,
    output wire [31:0] b_rsp_rdata,
    output reg         b_rsp_err
);

    localparam WORDS = MEM_BYTES / 4;

    function integer clog2;               // Verilog-2001 constant function
        input integer value;
        integer v;
        begin
            v = value - 1;
            for (clog2 = 0; v > 0; clog2 = clog2 + 1)
                v = v >> 1;
        end
    endfunction

    localparam AW = clog2(WORDS);         // word-address width

    (* ram_style = "block" *)
    reg [31:0] mem [0:WORDS-1];

    initial begin
        if (INIT_FILE != "")
            $readmemh(INIT_FILE, mem);
    end

    wire a_in_range = (a_req_addr < MEM_BYTES);
    wire b_in_range = (b_req_addr < MEM_BYTES);
    wire a_en = a_req_valid & a_in_range & ~rst;
    wire b_en = b_req_valid & b_in_range & ~rst;
    wire [AW-1:0] a_word = a_req_addr[AW+1:2];
    wire [AW-1:0] b_word = b_req_addr[AW+1:2];

    reg [31:0] a_dout;
    reg [31:0] b_dout;

    // port A: read only
    always @(posedge clk) begin
        if (a_en)
            a_dout <= mem[a_word];
    end

    // port B: read-first, byte-wide write enable
    integer i;
    always @(posedge clk) begin
        if (b_en) begin
            if (b_req_we) begin
                for (i = 0; i < 4; i = i + 1)
                    if (b_req_wstrb[i])
                        mem[b_word][8*i +: 8] <= b_req_wdata[8*i +: 8];
            end
            b_dout <= mem[b_word];
        end
    end

    // CMI responses: one cycle after every accepted request
    always @(posedge clk) begin
        if (rst) begin
            a_rsp_valid <= 1'b0;
            a_rsp_err   <= 1'b0;
            b_rsp_valid <= 1'b0;
            b_rsp_err   <= 1'b0;
        end else begin
            a_rsp_valid <= a_req_valid;
            a_rsp_err   <= a_req_valid & ~a_in_range;
            b_rsp_valid <= b_req_valid;
            b_rsp_err   <= b_req_valid & ~b_in_range;
        end
    end

    assign a_req_ready = 1'b1;
    assign b_req_ready = 1'b1;
    assign a_rsp_rdata = a_dout;
    assign b_rsp_rdata = b_dout;

endmodule

`default_nettype wire
