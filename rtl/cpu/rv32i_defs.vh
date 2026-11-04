// rv32i_defs.vh - shared constants for the RV32I CPU modules.
//
// Include this file INSIDE a module body:  `include "rv32i_defs.vh"
// It only declares localparams, so every including module gets its own copy.
// There is intentionally no include guard: a guard would hide the
// definitions from the second module compiled in the same compilation unit.
//
// Spec: docs/cpu/rv32i_core_spec.md

// ---------------------------------------------------------------------------
// Major opcodes (insn[6:0]); insn[1:0] must be 2'b11 for any RV32I instruction
// ---------------------------------------------------------------------------
localparam [6:0] OPC_LOAD     = 7'b0000011;
localparam [6:0] OPC_MISC_MEM = 7'b0001111;   // FENCE (FENCE.I is Zifencei)
localparam [6:0] OPC_OP_IMM   = 7'b0010011;
localparam [6:0] OPC_AUIPC    = 7'b0010111;
localparam [6:0] OPC_STORE    = 7'b0100011;
localparam [6:0] OPC_OP       = 7'b0110011;
localparam [6:0] OPC_LUI      = 7'b0110111;
localparam [6:0] OPC_BRANCH   = 7'b1100011;
localparam [6:0] OPC_JALR     = 7'b1100111;
localparam [6:0] OPC_JAL      = 7'b1101111;
localparam [6:0] OPC_SYSTEM   = 7'b1110011;

// ---------------------------------------------------------------------------
// ALU operations (rv32i_alu.alu_op)
// ---------------------------------------------------------------------------
localparam [3:0] ALU_ADD  = 4'd0;
localparam [3:0] ALU_SUB  = 4'd1;
localparam [3:0] ALU_SLL  = 4'd2;
localparam [3:0] ALU_SLT  = 4'd3;
localparam [3:0] ALU_SLTU = 4'd4;
localparam [3:0] ALU_XOR  = 4'd5;
localparam [3:0] ALU_SRL  = 4'd6;
localparam [3:0] ALU_SRA  = 4'd7;
localparam [3:0] ALU_OR   = 4'd8;
localparam [3:0] ALU_AND  = 4'd9;
// 4'd10..4'd15 are unused; the ALU returns 0 for them.

// ---------------------------------------------------------------------------
// Decoder operand / write-back selects
// ---------------------------------------------------------------------------
localparam [1:0] OPA_RS1  = 2'd0;   // ALU operand A = rs1 value
localparam [1:0] OPA_PC   = 2'd1;   // ALU operand A = pc        (AUIPC, JAL)
localparam [1:0] OPA_ZERO = 2'd2;   // ALU operand A = 0         (LUI)

localparam       OPB_RS2  = 1'b0;   // ALU operand B = rs2 value
localparam       OPB_IMM  = 1'b1;   // ALU operand B = immediate

localparam [1:0] WB_ALU   = 2'd0;   // rd <= ALU result
localparam [1:0] WB_MEM   = 2'd1;   // rd <= LSU load data
localparam [1:0] WB_PC4   = 2'd2;   // rd <= pc + 4              (JAL, JALR)

// ---------------------------------------------------------------------------
// funct3 encodings used by the LSU and the branch condition
// ---------------------------------------------------------------------------
localparam [2:0] F3_LB  = 3'b000;
localparam [2:0] F3_LH  = 3'b001;
localparam [2:0] F3_LW  = 3'b010;
localparam [2:0] F3_LBU = 3'b100;
localparam [2:0] F3_LHU = 3'b101;
localparam [2:0] F3_SB  = 3'b000;
localparam [2:0] F3_SH  = 3'b001;
localparam [2:0] F3_SW  = 3'b010;

localparam [2:0] F3_BEQ  = 3'b000;
localparam [2:0] F3_BNE  = 3'b001;
localparam [2:0] F3_BLT  = 3'b100;
localparam [2:0] F3_BGE  = 3'b101;
localparam [2:0] F3_BLTU = 3'b110;
localparam [2:0] F3_BGEU = 3'b111;

// ---------------------------------------------------------------------------
// Trap cause codes (same numbers as RISC-V mcause; this core only halts, it
// does not implement the privileged trap mechanism). Used by rv32i_core.
// ---------------------------------------------------------------------------
localparam [3:0] CAUSE_INSN_MISALIGNED  = 4'd0;
localparam [3:0] CAUSE_INSN_ACCESS      = 4'd1;
localparam [3:0] CAUSE_ILLEGAL_INSN     = 4'd2;
localparam [3:0] CAUSE_BREAKPOINT       = 4'd3;
localparam [3:0] CAUSE_LOAD_MISALIGNED  = 4'd4;
localparam [3:0] CAUSE_LOAD_ACCESS      = 4'd5;
localparam [3:0] CAUSE_STORE_MISALIGNED = 4'd6;
localparam [3:0] CAUSE_STORE_ACCESS     = 4'd7;
localparam [3:0] CAUSE_ECALL_M          = 4'd11;
