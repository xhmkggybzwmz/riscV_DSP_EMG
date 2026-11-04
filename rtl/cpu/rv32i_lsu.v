`timescale 1ns / 1ps
`default_nettype none

// rv32i_lsu - load/store data path for the CMI data port (purely combinational).
//
// Implements the byte-lane rules of docs/cpu/cpu_mem_interface.md section 3.
// The access size comes from funct3[1:0] (00 byte, 01 half, 10 word); the
// load extension from funct3[2] (1 = zero-extend). funct3 legality is the
// decoder's job; for funct3[1:0] = 11 every output is 0.
//
//   misaligned : half access with addr[0]=1, or word access with addr[1:0]!=0.
//                The core must trap before issuing the request.
//   wstrb      : CMI byte write enables. 0 when is_store=0 or misaligned, so a
//                load or a misaligned store can never write memory.
//   wdata      : store data replicated to every lane (SB x4, SH x2, SW as is).
//   load_data  : byte/half selected from the full read word by addr[1:0] and
//                sign/zero-extended (LB/LH/LW/LBU/LHU). funct3 011/110/111 give
//                0. For a misaligned access the value is undefined (the core
//                traps before issuing the request and never writes rd).

module rv32i_lsu (
    input  wire [2:0]  funct3,       // instruction funct3 (load or store encoding)
    input  wire [1:0]  addr_lo,      // effective address bits [1:0]
    input  wire        is_store,     // 1 = store, 0 = load
    input  wire [31:0] store_data,   // rs2 value
    input  wire [31:0] load_word,    // CMI rsp_rdata (whole aligned word)

    output reg         misaligned,
    output reg  [3:0]  wstrb,
    output reg  [31:0] wdata,
    output reg  [31:0] load_data
);

    wire [1:0] size     = funct3[1:0];
    wire       unsigned_ld = funct3[2];

    // byte / half selected from the read word
    reg  [7:0]  ld_byte;
    wire [15:0] ld_half = addr_lo[1] ? load_word[31:16] : load_word[15:0];

    always @* begin
        case (addr_lo)
            2'd0:    ld_byte = load_word[7:0];
            2'd1:    ld_byte = load_word[15:8];
            2'd2:    ld_byte = load_word[23:16];
            default: ld_byte = load_word[31:24];
        endcase
    end

    always @* begin
        case (size)
            2'b00:   misaligned = 1'b0;
            2'b01:   misaligned = addr_lo[0];
            2'b10:   misaligned = (addr_lo != 2'b00);
            default: misaligned = 1'b0;
        endcase
    end

    always @* begin
        wstrb = 4'b0000;
        wdata = 32'd0;
        case (size)
            2'b00: begin
                wdata = {4{store_data[7:0]}};
                wstrb = 4'b0001 << addr_lo;
            end
            2'b01: begin
                wdata = {2{store_data[15:0]}};
                wstrb = addr_lo[1] ? 4'b1100 : 4'b0011;
            end
            2'b10: begin
                wdata = store_data;
                wstrb = 4'b1111;
            end
            default: ;
        endcase
        if (!is_store || misaligned)
            wstrb = 4'b0000;
    end

    always @* begin
        case (funct3)
            3'b000:  load_data = {{24{ld_byte[7]}}, ld_byte};    // LB
            3'b001:  load_data = {{16{ld_half[15]}}, ld_half};   // LH
            3'b010:  load_data = load_word;                      // LW
            3'b100:  load_data = {24'd0, ld_byte};               // LBU
            3'b101:  load_data = {16'd0, ld_half};               // LHU
            default: load_data = 32'd0;
        endcase
    end

endmodule

`default_nettype wire
