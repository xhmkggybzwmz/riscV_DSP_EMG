`timescale 1ns / 1ps
`default_nettype none

// rv32i_decoder - RV32I instruction decoder (purely combinational).
//
// Outputs depend only on the instruction word; the module has no knowledge of
// any core state, so it can be shared by the V1 multi-cycle core and a future
// pipelined core.
//
// Field outputs (always the raw instruction fields, legal or not):
//   rd, rs1, rs2, funct3
// Immediate (selected by the major opcode only, independent of legality):
//   I-type: LOAD, OP-IMM, JALR, MISC-MEM, SYSTEM    S-type: STORE
//   B-type: BRANCH    U-type: LUI, AUIPC    J-type: JAL    others (OP, unknown): 0
//   For SLLI/SRLI/SRAI the immediate is the raw I-immediate; the ALU uses bits [4:0].
// Control outputs: see the table in docs/cpu/rv32i_core_spec.md.
//   rd_we is 1 only when the instruction writes rd AND rd != x0.
//   When illegal=1 every control output is forced to its default:
//   alu_op=ALU_ADD, op_a_sel=OPA_RS1, op_b_sel=OPB_RS2, wb_sel=WB_ALU and all
//   enables/flags 0, so an illegal instruction can have no side effect.
//
// Legal instructions (everything else sets illegal=1):
//   LUI, AUIPC, JAL, JALR (funct3=000), BEQ/BNE/BLT/BGE/BLTU/BGEU,
//   LB/LH/LW/LBU/LHU, SB/SH/SW, ADDI/SLTI/SLTIU/XORI/ORI/ANDI,
//   SLLI/SRLI (imm[11:5]=0000000), SRAI (imm[11:5]=0100000),
//   ADD..AND (funct7=0000000), SUB/SRA (funct7=0100000),
//   FENCE (funct3=000; fm/pred/succ/rs1/rd ignored, so FENCE.TSO and PAUSE are legal),
//   ECALL (exactly 0x00000073), EBREAK (exactly 0x00100073).
//   Not implemented and therefore illegal: FENCE.I (Zifencei), all CSR
//   instructions, MRET/WFI/other SYSTEM encodings, M/A/F/D/C/V and custom opcodes.
//
// Spec: docs/cpu/rv32i_core_spec.md

module rv32i_decoder (
    input  wire [31:0] insn,

    output wire [4:0]  rd,
    output wire [4:0]  rs1,
    output wire [4:0]  rs2,
    output wire [2:0]  funct3,
    output reg  [31:0] imm,

    output reg  [3:0]  alu_op,     // ALU_*
    output reg  [1:0]  op_a_sel,   // OPA_*
    output reg         op_b_sel,   // OPB_*
    output reg  [1:0]  wb_sel,     // WB_*
    output reg         rd_we,      // writes rd (never 1 for rd = x0)
    output reg         rs1_used,   // reads rs1
    output reg         rs2_used,   // reads rs2

    output reg         is_load,
    output reg         is_store,
    output reg         is_branch,  // condition from funct3 (F3_B*)
    output reg         is_jal,
    output reg         is_jalr,
    output reg         is_fence,   // executes as a NOP
    output reg         is_ecall,
    output reg         is_ebreak,
    output reg         illegal
);

    `include "rv32i_defs.vh"

    wire [6:0] opcode = insn[6:0];
    wire [6:0] funct7 = insn[31:25];

    assign rd     = insn[11:7];
    assign rs1    = insn[19:15];
    assign rs2    = insn[24:20];
    assign funct3 = insn[14:12];

    // ------------------------------------------------------------------
    // Immediates
    // ------------------------------------------------------------------
    wire [31:0] imm_i = {{20{insn[31]}}, insn[31:20]};
    wire [31:0] imm_s = {{20{insn[31]}}, insn[31:25], insn[11:7]};
    wire [31:0] imm_b = {{19{insn[31]}}, insn[31], insn[7], insn[30:25], insn[11:8], 1'b0};
    wire [31:0] imm_u = {insn[31:12], 12'd0};
    wire [31:0] imm_j = {{11{insn[31]}}, insn[31], insn[19:12], insn[20], insn[30:21], 1'b0};

    always @* begin
        case (opcode)
            OPC_LOAD, OPC_OP_IMM, OPC_JALR, OPC_MISC_MEM, OPC_SYSTEM: imm = imm_i;
            OPC_STORE:            imm = imm_s;
            OPC_BRANCH:           imm = imm_b;
            OPC_LUI, OPC_AUIPC:   imm = imm_u;
            OPC_JAL:              imm = imm_j;
            default:              imm = 32'd0;
        endcase
    end

    // ------------------------------------------------------------------
    // ALU operation for OP / OP-IMM (funct3 plus the funct7[5] "alt" bit)
    // ------------------------------------------------------------------
    reg [3:0] arith_op;
    always @* begin
        case (funct3)
            3'b000:  arith_op = ALU_ADD;            // ADD/ADDI (SUB handled below)
            3'b001:  arith_op = ALU_SLL;
            3'b010:  arith_op = ALU_SLT;
            3'b011:  arith_op = ALU_SLTU;
            3'b100:  arith_op = ALU_XOR;
            3'b101:  arith_op = funct7[5] ? ALU_SRA : ALU_SRL;
            3'b110:  arith_op = ALU_OR;
            default: arith_op = ALU_AND;
        endcase
    end

    // ------------------------------------------------------------------
    // Main decode
    // ------------------------------------------------------------------
    reg dec_illegal;
    reg writes_rd;

    always @* begin
        // defaults (also the forced values for an illegal instruction)
        alu_op    = ALU_ADD;
        op_a_sel  = OPA_RS1;
        op_b_sel  = OPB_RS2;
        wb_sel    = WB_ALU;
        writes_rd = 1'b0;
        rs1_used  = 1'b0;
        rs2_used  = 1'b0;
        is_load   = 1'b0;
        is_store  = 1'b0;
        is_branch = 1'b0;
        is_jal    = 1'b0;
        is_jalr   = 1'b0;
        is_fence  = 1'b0;
        is_ecall  = 1'b0;
        is_ebreak = 1'b0;
        dec_illegal = 1'b0;

        case (opcode)
            OPC_LUI: begin
                op_a_sel  = OPA_ZERO;
                op_b_sel  = OPB_IMM;
                writes_rd = 1'b1;
            end

            OPC_AUIPC: begin
                op_a_sel  = OPA_PC;
                op_b_sel  = OPB_IMM;
                writes_rd = 1'b1;
            end

            OPC_JAL: begin
                is_jal    = 1'b1;
                op_a_sel  = OPA_PC;
                op_b_sel  = OPB_IMM;
                wb_sel    = WB_PC4;
                writes_rd = 1'b1;
            end

            OPC_JALR: begin
                is_jalr   = 1'b1;
                op_b_sel  = OPB_IMM;
                wb_sel    = WB_PC4;
                writes_rd = 1'b1;
                rs1_used  = 1'b1;
                dec_illegal = (funct3 != 3'b000);
            end

            OPC_BRANCH: begin
                is_branch = 1'b1;
                rs1_used  = 1'b1;
                rs2_used  = 1'b1;
                dec_illegal = (funct3 == 3'b010) || (funct3 == 3'b011);
            end

            OPC_LOAD: begin
                is_load   = 1'b1;
                op_b_sel  = OPB_IMM;
                wb_sel    = WB_MEM;
                writes_rd = 1'b1;
                rs1_used  = 1'b1;
                dec_illegal = (funct3 == 3'b011) || (funct3 == 3'b110) || (funct3 == 3'b111);
            end

            OPC_STORE: begin
                is_store  = 1'b1;
                op_b_sel  = OPB_IMM;
                rs1_used  = 1'b1;
                rs2_used  = 1'b1;
                dec_illegal = (funct3[2] == 1'b1) || (funct3[1:0] == 2'b11);
            end

            OPC_OP_IMM: begin
                alu_op    = arith_op;
                op_b_sel  = OPB_IMM;
                writes_rd = 1'b1;
                rs1_used  = 1'b1;
                if (funct3 == 3'b001)                          // SLLI
                    dec_illegal = (funct7 != 7'b0000000);
                else if (funct3 == 3'b101)                     // SRLI / SRAI
                    dec_illegal = (funct7 != 7'b0000000) && (funct7 != 7'b0100000);
            end

            OPC_OP: begin
                alu_op    = (funct3 == 3'b000 && funct7[5]) ? ALU_SUB : arith_op;
                writes_rd = 1'b1;
                rs1_used  = 1'b1;
                rs2_used  = 1'b1;
                if (funct7 == 7'b0100000)
                    dec_illegal = (funct3 != 3'b000) && (funct3 != 3'b101);   // only SUB, SRA
                else
                    dec_illegal = (funct7 != 7'b0000000);
            end

            OPC_MISC_MEM: begin
                is_fence    = 1'b1;
                dec_illegal = (funct3 != 3'b000);                 // FENCE.I and reserved
            end

            OPC_SYSTEM: begin
                is_ecall    = (insn == 32'h0000_0073);
                is_ebreak   = (insn == 32'h0010_0073);
                dec_illegal = !(is_ecall || is_ebreak);
            end

            default: dec_illegal = 1'b1;                          // includes insn[1:0] != 2'b11
        endcase

        if (dec_illegal) begin
            alu_op    = ALU_ADD;
            op_a_sel  = OPA_RS1;
            op_b_sel  = OPB_RS2;
            wb_sel    = WB_ALU;
            writes_rd = 1'b0;
            rs1_used  = 1'b0;
            rs2_used  = 1'b0;
            is_load   = 1'b0;
            is_store  = 1'b0;
            is_branch = 1'b0;
            is_jal    = 1'b0;
            is_jalr   = 1'b0;
            is_fence  = 1'b0;
            is_ecall  = 1'b0;
            is_ebreak = 1'b0;
        end

        illegal = dec_illegal;
        rd_we   = writes_rd && (insn[11:7] != 5'd0);
    end

endmodule

`default_nettype wire
