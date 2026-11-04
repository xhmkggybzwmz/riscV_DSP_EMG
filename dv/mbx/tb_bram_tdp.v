`timescale 1ns / 1ps
`default_nettype none

// tb_bram_tdp - behavioural true dual-port block RAM for the Icarus mailbox tests
// (stands in for the Block Memory Generator in BRAM_Controller mode; the xsim tests
// use the real IP). Both ports: byte address (word = addr[AW-1:2]), byte write
// enables, read latency 1 without output register, READ_FIRST (a port that reads
// the word the other port writes in the same cycle gets the old value; the KV260
// design runs both ports on one clock).
//
// Collision statistics (same word, same cycle):
//   n_rw_coll  one port writes, the other reads   (allowed; protocol double-reads)
//   n_ww_coll  both ports write                   (protocol violation: every mailbox
//                                                  word has exactly one writer)
// +bram_collision_corrupt=1: on a read/write collision the reading port returns
// 32'hDEAD_BEEF instead of the old word (worst case "invalid data" behaviour), to
// show that the double-read rules of the protocol make collisions harmless.

module tb_bram_tdp #(
    parameter SIZE = 4096
) (
    input  wire        clka,
    input  wire        ena,
    input  wire [3:0]  wea,
    input  wire [31:0] addra,
    input  wire [31:0] dina,
    output reg  [31:0] douta,
    input  wire        clkb,
    input  wire        enb,
    input  wire [3:0]  web,
    input  wire [31:0] addrb,
    input  wire [31:0] dinb,
    output reg  [31:0] doutb
);
    localparam WORDS = SIZE / 4;
    localparam AW = $clog2(SIZE);
    reg [31:0] mem [0:WORDS-1];
    integer i, n_rw_coll = 0, n_ww_coll = 0, corrupt = 0;
    initial begin
        for (i = 0; i < WORDS; i = i + 1) mem[i] = 32'h0;
        douta = 32'h0; doutb = 32'h0;
        if (!$value$plusargs("bram_collision_corrupt=%d", corrupt)) corrupt = 0;
    end

    wire [AW-3:0] ia = addra[AW-1:2], ib = addrb[AW-1:2];
    wire coll = ena && enb && ia == ib;
    wire a_wr = |wea, b_wr = |web;

    always @(posedge clka) if (ena) begin
        douta <= (coll && b_wr && !a_wr && corrupt) ? 32'hDEAD_BEEF : mem[ia];
        if (wea[0]) mem[ia][7:0]   <= dina[7:0];
        if (wea[1]) mem[ia][15:8]  <= dina[15:8];
        if (wea[2]) mem[ia][23:16] <= dina[23:16];
        if (wea[3]) mem[ia][31:24] <= dina[31:24];
    end
    always @(posedge clkb) if (enb) begin
        doutb <= (coll && a_wr && !b_wr && corrupt) ? 32'hDEAD_BEEF : mem[ib];
        if (web[0]) mem[ib][7:0]   <= dinb[7:0];
        if (web[1]) mem[ib][15:8]  <= dinb[15:8];
        if (web[2]) mem[ib][23:16] <= dinb[23:16];
        if (web[3]) mem[ib][31:24] <= dinb[31:24];
    end
    always @(posedge clka) if (coll) begin
        if (a_wr && b_wr) begin
            n_ww_coll = n_ww_coll + 1;
            $display("ERROR: mailbox BRAM: both ports write word %0d in one cycle (t=%0t)", ia, $time);
        end else if (a_wr || b_wr)
            n_rw_coll = n_rw_coll + 1;
    end
endmodule

`default_nettype wire
