`timescale 1ns / 1ps
`default_nettype none

// rv32i_alu - RV32I integer ALU (purely combinational).
//
// result : selected by alu_op (ALU_* in rv32i_defs.vh); unused codes give 0.
//          Arithmetic wraps modulo 2^32. Shifts use op_b[4:0] only.
// eq/lt/ltu : comparison flags of op_a vs op_b, always valid regardless of
//          alu_op; used for the branch condition (lt signed, ltu unsigned).
//
// The arithmetic right shift has its own signed net so that signedness is not
// lost inside the result multiplexer (a $signed() operand mixed with unsigned
// operands in one expression is evaluated as unsigned).
//
// Spec: docs/cpu/rv32i_core_spec.md

module rv32i_alu (
    input  wire [3:0]  alu_op,
    input  wire [31:0] op_a,
    input  wire [31:0] op_b,
    output reg  [31:0] result,
    output wire        eq,
    output wire        lt,
    output wire        ltu
);

    `include "rv32i_defs.vh"

    wire [4:0]         shamt   = op_b[4:0];
    wire [31:0]        add_res = op_a + op_b;
    wire [31:0]        sub_res = op_a - op_b;
    wire [31:0]        sll_res = op_a << shamt;
    wire [31:0]        srl_res = op_a >> shamt;
    wire signed [31:0] sra_res = $signed(op_a) >>> shamt;

    assign eq  = (op_a == op_b);
    assign lt  = ($signed(op_a) < $signed(op_b));
    assign ltu = (op_a < op_b);

    always @* begin
        case (alu_op)
            ALU_ADD:  result = add_res;
            ALU_SUB:  result = sub_res;
            ALU_SLL:  result = sll_res;
            ALU_SLT:  result = {31'd0, lt};
            ALU_SLTU: result = {31'd0, ltu};
            ALU_XOR:  result = op_a ^ op_b;
            ALU_SRL:  result = srl_res;
            ALU_SRA:  result = sra_res;
            ALU_OR:   result = op_a | op_b;
            ALU_AND:  result = op_a & op_b;
            default:  result = 32'd0;
        endcase
    end

endmodule

`default_nettype wire
