`timescale 1ns / 1ps
`default_nettype none

// tb_rv32i_decoder - self-checking unit test for rtl/cpu/rv32i_decoder.v
//
// Three independent views of RV32I are compared:
//   1. GCC golden vectors (decoder_golden_vectors.vh): real encodings from the
//      RISC-V GNU assembler, expected fields written from instruction semantics.
//      Every output field is checked individually.
//   2. A reference decoder based on the MASK/MATCH table of the official
//      riscv-opcodes project (instruction identified by (insn & MASK) == MATCH),
//      structurally different from the case-based RTL.
//   3. Sweeps: every opcode x funct3 x funct7 combination with random register
//      fields, single-bit mutations of ECALL/EBREAK/FENCE, and random words,
//      each compared field by field (packed) against the reference.
// Mnemonic coverage of the sweeps is checked at the end.

module tb_rv32i_decoder;

    localparam TB_TIMEOUT_NS = 500000000;
    `include "tb_check.vh"
    `include "rv32i_defs.vh"

    reg  [31:0] insn;
    wire [4:0]  rd, rs1, rs2;
    wire [2:0]  funct3;
    wire [31:0] imm;
    wire [3:0]  alu_op;
    wire [1:0]  op_a_sel, wb_sel;
    wire        op_b_sel;
    wire        rd_we, rs1_used, rs2_used;
    wire        is_load, is_store, is_branch, is_jal, is_jalr;
    wire        is_fence, is_ecall, is_ebreak, illegal;

    rv32i_decoder dut (
        .insn(insn), .rd(rd), .rs1(rs1), .rs2(rs2), .funct3(funct3), .imm(imm),
        .alu_op(alu_op), .op_a_sel(op_a_sel), .op_b_sel(op_b_sel), .wb_sel(wb_sel),
        .rd_we(rd_we), .rs1_used(rs1_used), .rs2_used(rs2_used),
        .is_load(is_load), .is_store(is_store), .is_branch(is_branch),
        .is_jal(is_jal), .is_jalr(is_jalr), .is_fence(is_fence),
        .is_ecall(is_ecall), .is_ebreak(is_ebreak), .illegal(illegal)
    );

`ifdef DUMP_WAVES
    initial begin $dumpfile("wave.vcd"); $dumpvars(0, tb_rv32i_decoder); end
`endif

    // =====================================================================
    // Expected values
    // =====================================================================
    reg [3:0]  e_alu;
    reg [1:0]  e_opa, e_wb;
    reg        e_opb;
    reg        e_rd_we, e_rs1u, e_rs2u;
    reg        e_load, e_store, e_branch, e_jal, e_jalr;
    reg        e_fence, e_ecall, e_ebreak, e_illegal;
    reg [31:0] e_imm;

    task exp_defaults;   // also the required values for an illegal instruction
        begin
            e_alu = ALU_ADD; e_opa = OPA_RS1; e_opb = OPB_RS2; e_wb = WB_ALU;
            e_rd_we = 0; e_rs1u = 0; e_rs2u = 0;
            e_load = 0; e_store = 0; e_branch = 0; e_jal = 0; e_jalr = 0;
            e_fence = 0; e_ecall = 0; e_ebreak = 0; e_illegal = 0;
        end
    endtask

    wire [20:0] dut_ctrl = {alu_op, op_a_sel, op_b_sel, wb_sel, rd_we, rs1_used, rs2_used,
                            is_load, is_store, is_branch, is_jal, is_jalr,
                            is_fence, is_ecall, is_ebreak, illegal};

    function [20:0] exp_ctrl;
        input dummy;
        begin
            exp_ctrl = {e_alu, e_opa, e_opb, e_wb, e_rd_we, e_rs1u, e_rs2u,
                        e_load, e_store, e_branch, e_jal, e_jalr,
                        e_fence, e_ecall, e_ebreak, e_illegal};
        end
    endfunction

    reg [8*64-1:0] msg;

    // Itemised comparison of every output (used for the golden vectors).
    task check_all_fields;
        input check_imm;
        begin
            #1;
            $sformat(msg, "insn=%h rd",       insn); tb_check(msg, rd,     insn[11:7]);
            $sformat(msg, "insn=%h rs1",      insn); tb_check(msg, rs1,    insn[19:15]);
            $sformat(msg, "insn=%h rs2",      insn); tb_check(msg, rs2,    insn[24:20]);
            $sformat(msg, "insn=%h funct3",   insn); tb_check(msg, funct3, insn[14:12]);
            if (check_imm) begin
                $sformat(msg, "insn=%h imm",  insn); tb_check(msg, imm,    e_imm);
            end
            $sformat(msg, "insn=%h alu_op",   insn); tb_check(msg, alu_op,   e_alu);
            $sformat(msg, "insn=%h op_a_sel", insn); tb_check(msg, op_a_sel, e_opa);
            $sformat(msg, "insn=%h op_b_sel", insn); tb_check(msg, op_b_sel, e_opb);
            $sformat(msg, "insn=%h wb_sel",   insn); tb_check(msg, wb_sel,   e_wb);
            $sformat(msg, "insn=%h rd_we",    insn); tb_check(msg, rd_we,    e_rd_we);
            $sformat(msg, "insn=%h rs1_used", insn); tb_check(msg, rs1_used, e_rs1u);
            $sformat(msg, "insn=%h rs2_used", insn); tb_check(msg, rs2_used, e_rs2u);
            $sformat(msg, "insn=%h is_load",  insn); tb_check(msg, is_load,  e_load);
            $sformat(msg, "insn=%h is_store", insn); tb_check(msg, is_store, e_store);
            $sformat(msg, "insn=%h is_branch",insn); tb_check(msg, is_branch,e_branch);
            $sformat(msg, "insn=%h is_jal",   insn); tb_check(msg, is_jal,   e_jal);
            $sformat(msg, "insn=%h is_jalr",  insn); tb_check(msg, is_jalr,  e_jalr);
            $sformat(msg, "insn=%h is_fence", insn); tb_check(msg, is_fence, e_fence);
            $sformat(msg, "insn=%h is_ecall", insn); tb_check(msg, is_ecall, e_ecall);
            $sformat(msg, "insn=%h is_ebreak",insn); tb_check(msg, is_ebreak,e_ebreak);
            $sformat(msg, "insn=%h illegal",  insn); tb_check(msg, illegal,  e_illegal);
        end
    endtask

    // =====================================================================
    // Golden-vector check tasks (one per instruction class)
    // =====================================================================
    task chk_lui;
        input [31:0] w; input [4:0] d; input [31:0] i;
        begin
            insn = w; exp_defaults; e_imm = i;
            e_opa = OPA_ZERO; e_opb = OPB_IMM; e_rd_we = (d != 0);
            tb_check("golden lui rd", w[11:7], d);
            check_all_fields(1'b1);
        end
    endtask

    task chk_auipc;
        input [31:0] w; input [4:0] d; input [31:0] i;
        begin
            insn = w; exp_defaults; e_imm = i;
            e_opa = OPA_PC; e_opb = OPB_IMM; e_rd_we = (d != 0);
            tb_check("golden auipc rd", w[11:7], d);
            check_all_fields(1'b1);
        end
    endtask

    task chk_jal;
        input [31:0] w; input [4:0] d; input [31:0] i;
        begin
            insn = w; exp_defaults; e_imm = i;
            e_jal = 1; e_opa = OPA_PC; e_opb = OPB_IMM; e_wb = WB_PC4; e_rd_we = (d != 0);
            tb_check("golden jal rd", w[11:7], d);
            check_all_fields(1'b1);
        end
    endtask

    task chk_jalr;
        input [31:0] w; input [4:0] d; input [4:0] s1; input [31:0] i;
        begin
            insn = w; exp_defaults; e_imm = i;
            e_jalr = 1; e_opb = OPB_IMM; e_wb = WB_PC4; e_rd_we = (d != 0); e_rs1u = 1;
            tb_check("golden jalr rd", w[11:7], d);
            tb_check("golden jalr rs1", w[19:15], s1);
            check_all_fields(1'b1);
        end
    endtask

    task chk_branch;
        input [31:0] w; input [2:0] f3; input [4:0] s1; input [4:0] s2; input [31:0] i;
        begin
            insn = w; exp_defaults; e_imm = i;
            e_branch = 1; e_rs1u = 1; e_rs2u = 1;
            tb_check("golden branch funct3", w[14:12], f3);
            tb_check("golden branch rs1", w[19:15], s1);
            tb_check("golden branch rs2", w[24:20], s2);
            check_all_fields(1'b1);
        end
    endtask

    task chk_load;
        input [31:0] w; input [2:0] f3; input [4:0] d; input [4:0] s1; input [31:0] i;
        begin
            insn = w; exp_defaults; e_imm = i;
            e_load = 1; e_opb = OPB_IMM; e_wb = WB_MEM; e_rd_we = (d != 0); e_rs1u = 1;
            tb_check("golden load funct3", w[14:12], f3);
            tb_check("golden load rd", w[11:7], d);
            tb_check("golden load rs1", w[19:15], s1);
            check_all_fields(1'b1);
        end
    endtask

    task chk_store;
        input [31:0] w; input [2:0] f3; input [4:0] s1; input [4:0] s2; input [31:0] i;
        begin
            insn = w; exp_defaults; e_imm = i;
            e_store = 1; e_opb = OPB_IMM; e_rs1u = 1; e_rs2u = 1;
            tb_check("golden store funct3", w[14:12], f3);
            tb_check("golden store rs1", w[19:15], s1);
            tb_check("golden store rs2", w[24:20], s2);
            check_all_fields(1'b1);
        end
    endtask

    task chk_opimm;
        input [31:0] w; input [3:0] op; input [4:0] d; input [4:0] s1; input [31:0] i;
        begin
            insn = w; exp_defaults; e_imm = i;
            e_alu = op; e_opb = OPB_IMM; e_rd_we = (d != 0); e_rs1u = 1;
            tb_check("golden op-imm rd", w[11:7], d);
            tb_check("golden op-imm rs1", w[19:15], s1);
            check_all_fields(1'b1);
        end
    endtask

    task chk_op;
        input [31:0] w; input [3:0] op; input [4:0] d; input [4:0] s1; input [4:0] s2;
        begin
            insn = w; exp_defaults; e_imm = 32'd0;
            e_alu = op; e_rd_we = (d != 0); e_rs1u = 1; e_rs2u = 1;
            tb_check("golden op rd", w[11:7], d);
            tb_check("golden op rs1", w[19:15], s1);
            tb_check("golden op rs2", w[24:20], s2);
            check_all_fields(1'b1);
        end
    endtask

    task chk_fence;
        input [31:0] w;
        begin
            insn = w; exp_defaults; e_fence = 1;
            check_all_fields(1'b0);
        end
    endtask

    task chk_ecall;
        input [31:0] w;
        begin
            insn = w; exp_defaults; e_ecall = 1;
            check_all_fields(1'b0);
        end
    endtask

    task chk_ebreak;
        input [31:0] w;
        begin
            insn = w; exp_defaults; e_ebreak = 1;
            check_all_fields(1'b0);
        end
    endtask

    task chk_illegal;
        input [31:0] w;
        begin
            insn = w; exp_defaults; e_illegal = 1;
            check_all_fields(1'b0);
        end
    endtask

    // =====================================================================
    // Reference decoder: MASK/MATCH table (values from riscv-opcodes, RV32I)
    // =====================================================================
    localparam N_ID = 40;
    localparam ID_ILLEGAL = 0,
               ID_LUI = 1, ID_AUIPC = 2, ID_JAL = 3, ID_JALR = 4,
               ID_BEQ = 5, ID_BNE = 6, ID_BLT = 7, ID_BGE = 8, ID_BLTU = 9, ID_BGEU = 10,
               ID_LB = 11, ID_LH = 12, ID_LW = 13, ID_LBU = 14, ID_LHU = 15,
               ID_SB = 16, ID_SH = 17, ID_SW = 18,
               ID_ADDI = 19, ID_SLTI = 20, ID_SLTIU = 21, ID_XORI = 22, ID_ORI = 23, ID_ANDI = 24,
               ID_SLLI = 25, ID_SRLI = 26, ID_SRAI = 27,
               ID_ADD = 28, ID_SUB = 29, ID_SLL = 30, ID_SLT = 31, ID_SLTU = 32,
               ID_XOR = 33, ID_SRL = 34, ID_SRA = 35, ID_OR = 36, ID_AND = 37,
               ID_FENCE = 38, ID_ECALL = 39, ID_EBREAK = 40;

    reg [31:0] mask_t  [1:N_ID];
    reg [31:0] match_t [1:N_ID];
    integer    hits    [0:N_ID];

    task def_insn;
        input integer id; input [31:0] m; input [31:0] v;
        begin mask_t[id] = m; match_t[id] = v; end
    endtask

    task init_table;
        begin
            def_insn(ID_LUI,    32'h0000007f, 32'h00000037);
            def_insn(ID_AUIPC,  32'h0000007f, 32'h00000017);
            def_insn(ID_JAL,    32'h0000007f, 32'h0000006f);
            def_insn(ID_JALR,   32'h0000707f, 32'h00000067);
            def_insn(ID_BEQ,    32'h0000707f, 32'h00000063);
            def_insn(ID_BNE,    32'h0000707f, 32'h00001063);
            def_insn(ID_BLT,    32'h0000707f, 32'h00004063);
            def_insn(ID_BGE,    32'h0000707f, 32'h00005063);
            def_insn(ID_BLTU,   32'h0000707f, 32'h00006063);
            def_insn(ID_BGEU,   32'h0000707f, 32'h00007063);
            def_insn(ID_LB,     32'h0000707f, 32'h00000003);
            def_insn(ID_LH,     32'h0000707f, 32'h00001003);
            def_insn(ID_LW,     32'h0000707f, 32'h00002003);
            def_insn(ID_LBU,    32'h0000707f, 32'h00004003);
            def_insn(ID_LHU,    32'h0000707f, 32'h00005003);
            def_insn(ID_SB,     32'h0000707f, 32'h00000023);
            def_insn(ID_SH,     32'h0000707f, 32'h00001023);
            def_insn(ID_SW,     32'h0000707f, 32'h00002023);
            def_insn(ID_ADDI,   32'h0000707f, 32'h00000013);
            def_insn(ID_SLTI,   32'h0000707f, 32'h00002013);
            def_insn(ID_SLTIU,  32'h0000707f, 32'h00003013);
            def_insn(ID_XORI,   32'h0000707f, 32'h00004013);
            def_insn(ID_ORI,    32'h0000707f, 32'h00006013);
            def_insn(ID_ANDI,   32'h0000707f, 32'h00007013);
            def_insn(ID_SLLI,   32'hfe00707f, 32'h00001013);
            def_insn(ID_SRLI,   32'hfe00707f, 32'h00005013);
            def_insn(ID_SRAI,   32'hfe00707f, 32'h40005013);
            def_insn(ID_ADD,    32'hfe00707f, 32'h00000033);
            def_insn(ID_SUB,    32'hfe00707f, 32'h40000033);
            def_insn(ID_SLL,    32'hfe00707f, 32'h00001033);
            def_insn(ID_SLT,    32'hfe00707f, 32'h00002033);
            def_insn(ID_SLTU,   32'hfe00707f, 32'h00003033);
            def_insn(ID_XOR,    32'hfe00707f, 32'h00004033);
            def_insn(ID_SRL,    32'hfe00707f, 32'h00005033);
            def_insn(ID_SRA,    32'hfe00707f, 32'h40005033);
            def_insn(ID_OR,     32'hfe00707f, 32'h00006033);
            def_insn(ID_AND,    32'hfe00707f, 32'h00007033);
            def_insn(ID_FENCE,  32'h0000707f, 32'h0000000f);
            def_insn(ID_ECALL,  32'hffffffff, 32'h00000073);
            def_insn(ID_EBREAK, 32'hffffffff, 32'h00100073);
        end
    endtask

    integer ref_matches;
    function integer ref_id;
        input [31:0] w;
        integer t;
        begin
            ref_id = ID_ILLEGAL;
            ref_matches = 0;
            for (t = 1; t <= N_ID; t = t + 1)
                if ((w & mask_t[t]) == match_t[t]) begin
                    ref_id = t;
                    ref_matches = ref_matches + 1;
                end
        end
    endfunction

    // Arithmetic shift right by n, kept in its own signed variable on purpose.
    function [31:0] asr;
        input [31:0] x;
        input integer n;
        reg signed [31:0] t;
        begin
            t = x;
            asr = t >>> n;
        end
    endfunction

    // Immediate by major opcode, written with shifts/masks (not bit concatenation).
    function [31:0] ref_imm;
        input [31:0] w;
        begin
            case (w[6:0])
                7'b0000011, 7'b0010011, 7'b1100111, 7'b0001111, 7'b1110011:      // I
                    ref_imm = asr(w, 20);
                7'b0100011:                                                         // S
                    ref_imm = (asr(w, 20) & 32'hFFFF_FFE0) | ((w >> 7) & 32'h1F);
                7'b1100011:                                                         // B
                    ref_imm = (asr(w, 19) & 32'hFFFF_F000)
                            | (((w >> 7)  & 32'h1)  << 11)
                            | (((w >> 25) & 32'h3F) << 5)
                            | (((w >> 8)  & 32'hF)  << 1);
                7'b0110111, 7'b0010111:                                             // U
                    ref_imm = w & 32'hFFFF_F000;
                7'b1101111:                                                         // J
                    ref_imm = (asr(w, 11) & 32'hFFF0_0000)
                            | (w & 32'h000F_F000)
                            | (((w >> 20) & 32'h1)   << 11)
                            | (((w >> 21) & 32'h3FF) << 1);
                default:
                    ref_imm = 32'd0;
            endcase
        end
    endfunction

    // Fill e_* from the reference decoder.
    task ref_expect;
        input [31:0] w;
        integer id;
        reg wr;
        begin
            exp_defaults;
            e_imm = ref_imm(w);
            wr = 0;
            id = ref_id(w);
            if (ref_matches > 1)
                tb_fail("reference table: more than one MASK/MATCH entry matched");
            case (id)
                ID_LUI:   begin e_opa = OPA_ZERO; e_opb = OPB_IMM; wr = 1; end
                ID_AUIPC: begin e_opa = OPA_PC;   e_opb = OPB_IMM; wr = 1; end
                ID_JAL:   begin e_jal = 1;  e_opa = OPA_PC; e_opb = OPB_IMM; e_wb = WB_PC4; wr = 1; end
                ID_JALR:  begin e_jalr = 1; e_opb = OPB_IMM; e_wb = WB_PC4; wr = 1; e_rs1u = 1; end
                ID_BEQ, ID_BNE, ID_BLT, ID_BGE, ID_BLTU, ID_BGEU:
                          begin e_branch = 1; e_rs1u = 1; e_rs2u = 1; end
                ID_LB, ID_LH, ID_LW, ID_LBU, ID_LHU:
                          begin e_load = 1; e_opb = OPB_IMM; e_wb = WB_MEM; wr = 1; e_rs1u = 1; end
                ID_SB, ID_SH, ID_SW:
                          begin e_store = 1; e_opb = OPB_IMM; e_rs1u = 1; e_rs2u = 1; end
                ID_ADDI, ID_SLTI, ID_SLTIU, ID_XORI, ID_ORI, ID_ANDI, ID_SLLI, ID_SRLI, ID_SRAI: begin
                    e_opb = OPB_IMM; wr = 1; e_rs1u = 1;
                    case (id)
                        ID_ADDI:  e_alu = ALU_ADD;
                        ID_SLTI:  e_alu = ALU_SLT;
                        ID_SLTIU: e_alu = ALU_SLTU;
                        ID_XORI:  e_alu = ALU_XOR;
                        ID_ORI:   e_alu = ALU_OR;
                        ID_ANDI:  e_alu = ALU_AND;
                        ID_SLLI:  e_alu = ALU_SLL;
                        ID_SRLI:  e_alu = ALU_SRL;
                        default:  e_alu = ALU_SRA;
                    endcase
                end
                ID_ADD, ID_SUB, ID_SLL, ID_SLT, ID_SLTU, ID_XOR, ID_SRL, ID_SRA, ID_OR, ID_AND: begin
                    wr = 1; e_rs1u = 1; e_rs2u = 1;
                    case (id)
                        ID_ADD:  e_alu = ALU_ADD;
                        ID_SUB:  e_alu = ALU_SUB;
                        ID_SLL:  e_alu = ALU_SLL;
                        ID_SLT:  e_alu = ALU_SLT;
                        ID_SLTU: e_alu = ALU_SLTU;
                        ID_XOR:  e_alu = ALU_XOR;
                        ID_SRL:  e_alu = ALU_SRL;
                        ID_SRA:  e_alu = ALU_SRA;
                        ID_OR:   e_alu = ALU_OR;
                        default: e_alu = ALU_AND;
                    endcase
                end
                ID_FENCE:  e_fence  = 1;
                ID_ECALL:  e_ecall  = 1;
                ID_EBREAK: e_ebreak = 1;
                default:   e_illegal = 1;
            endcase
            e_rd_we = wr && (w[11:7] != 5'd0);
            hits[id] = hits[id] + 1;
        end
    endtask

    // Compare DUT and reference for one word (4 checks: fields, imm, control).
    task check_vs_ref;
        input [31:0] w;
        begin
            insn = w;
            ref_expect(w);
            #1;
            $sformat(msg, "ref insn=%h fields", w);
            tb_check(msg, {rd, rs1, rs2, funct3}, {w[11:7], w[19:15], w[24:20], w[14:12]});
            $sformat(msg, "ref insn=%h imm", w);
            tb_check(msg, imm, e_imm);
            $sformat(msg, "ref insn=%h ctrl{alu,opa,opb,wb,we,u1,u2,ld,st,br,jal,jalr,fen,ec,eb,ill}", w);
            tb_check(msg, dut_ctrl, exp_ctrl(1'b0));
        end
    endtask

    function [31:0] enc_r;    // generic R-shaped packing helper
        input [6:0] f7; input [4:0] r2; input [4:0] r1; input [2:0] f3;
        input [4:0] d;  input [6:0] opc;
        begin enc_r = {f7, r2, r1, f3, d, opc}; end
    endfunction

    integer i, j, k, n, missing;
    reg [31:0] w;

    initial begin
        tb_init;
        init_table;
        for (i = 0; i <= N_ID; i = i + 1) hits[i] = 0;
        insn = 32'h0000_0013;

        // ---------------------------------------------------------------
        // 1. GCC golden vectors (itemised field checks)
        // ---------------------------------------------------------------
        `include "decoder_golden_vectors.vh"

        // ---------------------------------------------------------------
        // 2. Directed illegal / reserved encodings
        // ---------------------------------------------------------------
        chk_illegal(32'h0000_0000);                            // all zeros
        chk_illegal(32'hFFFF_FFFF);                            // all ones
        chk_illegal(32'h0000_4501);                            // c.li a0,0 (16-bit, low bits 01)
        chk_illegal(32'h0000_8082);                            // c.ret     (16-bit, low bits 10)
        chk_illegal(32'h0000_0001);                            // low bits 01
        chk_illegal(32'h0000_0010);                            // OP-IMM opcode with low bits 00
        chk_illegal(32'h0000_000B);                            // custom-0
        chk_illegal(32'h0000_002B);                            // custom-1
        chk_illegal(32'h0000_005B);                            // custom-2
        chk_illegal(32'h0000_007B);                            // custom-3
        chk_illegal(32'h0000_0007);                            // LOAD-FP (F extension)
        chk_illegal(32'h0000_0027);                            // STORE-FP
        chk_illegal(32'h0000_0053);                            // OP-FP
        chk_illegal(32'h0000_001B);                            // OP-IMM-32 (RV64 only)
        chk_illegal(32'h0000_003B);                            // OP-32 (RV64 only)
        chk_illegal(32'h0000_3003);                            // LD (funct3 011)
        chk_illegal(32'h0000_6003);                            // LWU (funct3 110, RV64)
        chk_illegal(32'h0000_7003);                            // LOAD funct3 111
        chk_illegal(32'h0000_3023);                            // SD (funct3 011)
        chk_illegal(32'h0000_4023);                            // STORE funct3 100
        chk_illegal(32'h0000_2063);                            // BRANCH funct3 010
        chk_illegal(32'h0000_3063);                            // BRANCH funct3 011
        chk_illegal(32'h0000_1067);                            // JALR funct3 001
        chk_illegal(32'h0000_2067);                            // JALR funct3 010
        chk_illegal(enc_r(7'b0000001, 5'd0, 5'd1, 3'b001, 5'd1, 7'b0010011)); // SLLI shamt[5]=1
        chk_illegal(enc_r(7'b0000001, 5'd0, 5'd1, 3'b101, 5'd1, 7'b0010011)); // SRLI shamt[5]=1
        chk_illegal(enc_r(7'b0100001, 5'd0, 5'd1, 3'b101, 5'd1, 7'b0010011)); // SRAI shamt[5]=1
        chk_illegal(enc_r(7'b0100000, 5'd0, 5'd1, 3'b001, 5'd1, 7'b0010011)); // SLLI with imm[10]=1
        chk_illegal(enc_r(7'b0100000, 5'd2, 5'd1, 3'b001, 5'd3, 7'b0110011)); // OP funct7=0100000, SLL
        chk_illegal(enc_r(7'b0100000, 5'd2, 5'd1, 3'b111, 5'd3, 7'b0110011)); // OP funct7=0100000, AND
        chk_illegal(enc_r(7'b1000000, 5'd2, 5'd1, 3'b000, 5'd3, 7'b0110011)); // OP funct7=1000000
        chk_illegal(32'h0000_00F3);                            // ECALL with rd=1
        chk_illegal(32'h0000_8073);                            // ECALL with rs1=1
        chk_illegal(32'h0020_0073);                            // imm=2 (URET-like, reserved)
        chk_illegal(32'h7B20_0073);                            // DRET
        chk_illegal(32'h0000_200F);                            // MISC-MEM funct3 010
        chk_illegal(32'h0000_700F);                            // MISC-MEM funct3 111

        // ---------------------------------------------------------------
        // 3. Exhaustive opcode x funct3 x funct7 sweep, random register fields
        // ---------------------------------------------------------------
        for (i = 0; i < 128; i = i + 1)
            for (j = 0; j < 8; j = j + 1)
                for (k = 0; k < 128; k = k + 1) begin
                    w = $random(tb_seed);
                    check_vs_ref(enc_r(k[6:0], w[4:0], w[9:5], j[2:0], w[14:10], i[6:0]));
                end

        // ---------------------------------------------------------------
        // 4. Single-bit mutations of ECALL, EBREAK and FENCE
        // ---------------------------------------------------------------
        for (i = 0; i < 32; i = i + 1) begin
            check_vs_ref(32'h0000_0073 ^ (32'd1 << i));
            check_vs_ref(32'h0010_0073 ^ (32'd1 << i));
            check_vs_ref(32'h0FF0_000F ^ (32'd1 << i));
        end

        // ---------------------------------------------------------------
        // 5. FENCE with random fm/pred/succ/rs1/rd (all legal)
        // ---------------------------------------------------------------
        for (n = 0; n < 2000; n = n + 1) begin
            w = $random(tb_seed);
            check_vs_ref({w[31:15], 3'b000, w[11:7], 7'b0001111});
        end

        // ---------------------------------------------------------------
        // 6. Random words: fully random, and random with a valid major opcode
        // ---------------------------------------------------------------
        for (n = 0; n < 100000; n = n + 1)
            check_vs_ref($random(tb_seed));
        for (n = 0; n < 100000; n = n + 1) begin
            w = $random(tb_seed);
            case (n % 11)
                0:  w[6:0] = 7'b0000011;  1: w[6:0] = 7'b0001111;  2:  w[6:0] = 7'b0010011;
                3:  w[6:0] = 7'b0010111;  4: w[6:0] = 7'b0100011;  5:  w[6:0] = 7'b0110011;
                6:  w[6:0] = 7'b0110111;  7: w[6:0] = 7'b1100011;  8:  w[6:0] = 7'b1100111;
                9:  w[6:0] = 7'b1101111;  default: w[6:0] = 7'b1110011;
            endcase
            check_vs_ref(w);
        end

        // ---------------------------------------------------------------
        // 7. Coverage: every RV32I mnemonic and the illegal class were hit
        // ---------------------------------------------------------------
        missing = 0;
        for (i = 0; i <= N_ID; i = i + 1)
            if (hits[i] == 0) begin
                $sformat(msg, "coverage: reference id %0d never exercised", i);
                tb_fail(msg);
                missing = missing + 1;
            end
        tb_check("coverage: mnemonics not exercised", missing, 0);
        $display("INFO: sweep hits: illegal=%0d ecall=%0d ebreak=%0d fence=%0d slli=%0d srai=%0d sub=%0d",
                 hits[ID_ILLEGAL], hits[ID_ECALL], hits[ID_EBREAK], hits[ID_FENCE],
                 hits[ID_SLLI], hits[ID_SRAI], hits[ID_SUB]);

        tb_finish;
    end

endmodule

`default_nettype wire
