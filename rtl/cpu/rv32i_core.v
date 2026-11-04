`timescale 1ns / 1ps
`default_nettype none

// rv32i_core - V1 multi-cycle RV32I core.
//
// FSM (one instruction at a time, at most one outstanding CMI request):
//   S_BOOT -> S_FETCH -> S_FETCH_WAIT -> S_DECODE -> S_EXEC -> S_FETCH
//                                                   S_EXEC -> S_MEM_REQ -> S_MEM_WAIT -> S_FETCH
//   any trap -> S_HALT (left only by rst)
//
// Timing conventions (all registers update on the rising edge, non-blocking):
//   - imem/dmem req_valid are decoded from the state register (and masked by
//     rst); all request fields come from registers, so they are stable while
//     waiting for ready.
//   - A request is accepted on the edge where valid && ready; the FSM leaves
//     S_FETCH / S_MEM_REQ on that same edge, so a request (and a store) is
//     issued exactly once.
//   - Responses are sampled on the edge that ends the cycle in which
//     rsp_valid = 1 (S_FETCH_WAIT / S_MEM_WAIT).
//   - "Retire" = normal completion: rd written (if any), pc updated, and for a
//     load/store a successful CMI response received. Retire and trap are
//     mutually exclusive. Both are reported one cycle later on the registered
//     dbg_retire_* / dbg_trap_valid_o outputs.
//   - The register file is written on the retire edge only (end of S_EXEC for
//     non-memory instructions, end of S_MEM_WAIT for loads), so each
//     instruction writes rd at most once and a trapping instruction never does.
//   - Instruction fields come from the decoder on the registered ir, which is
//     stable from S_DECODE to the end of the instruction. rs1/rs2 values and
//     the immediate are latched in S_DECODE.
//
// Spec: docs/cpu/rv32i_core_spec.md, docs/cpu/cpu_mem_interface.md

module rv32i_core #(
    parameter [31:0] RESET_VECTOR = 32'h0000_0000
) (
    input  wire        clk,
    input  wire        rst,                 // synchronous, active high

    // CMI instruction port (read only)
    output wire        imem_req_valid,
    input  wire        imem_req_ready,
    output wire [31:0] imem_req_addr,
    input  wire        imem_rsp_valid,
    input  wire [31:0] imem_rsp_rdata,
    input  wire        imem_rsp_err,

    // CMI data port
    output wire        dmem_req_valid,
    input  wire        dmem_req_ready,
    output wire [31:0] dmem_req_addr,
    output wire        dmem_req_we,
    output wire [3:0]  dmem_req_wstrb,
    output wire [31:0] dmem_req_wdata,
    input  wire        dmem_rsp_valid,
    input  wire [31:0] dmem_rsp_rdata,
    input  wire        dmem_rsp_err,

    // halt / trap information (held until rst)
    output wire        halted_o,
    output reg  [3:0]  trap_cause_o,
    output reg  [31:0] trap_pc_o,
    output reg  [31:0] trap_tval_o,

    // debug: one-cycle event pulses
    output reg         dbg_trap_valid_o,    // a trap was taken (instruction did not retire)
    output reg         dbg_retire_valid_o,  // an instruction retired
    output reg  [31:0] dbg_retire_pc_o,
    output reg  [31:0] dbg_retire_insn_o,
    output reg         dbg_retire_rd_we_o,  // rd was written (never for rd = x0)
    output reg  [4:0]  dbg_retire_rd_o,
    output reg  [31:0] dbg_retire_rd_wdata_o // 0 when dbg_retire_rd_we_o = 0
);

    `include "rv32i_defs.vh"

    localparam [2:0] S_BOOT       = 3'd0;
    localparam [2:0] S_FETCH      = 3'd1;
    localparam [2:0] S_FETCH_WAIT = 3'd2;
    localparam [2:0] S_DECODE     = 3'd3;
    localparam [2:0] S_EXEC       = 3'd4;
    localparam [2:0] S_MEM_REQ    = 3'd5;
    localparam [2:0] S_MEM_WAIT   = 3'd6;
    localparam [2:0] S_HALT       = 3'd7;

    // ------------------------------------------------------------------
    // State and datapath registers
    // ------------------------------------------------------------------
    reg  [2:0]  state;          // reset
    reg  [31:0] pc;             // reset to RESET_VECTOR
    reg  [31:0] ir;             // not reset: written in S_FETCH_WAIT before use
    reg  [31:0] rs1_val;        // not reset: written in S_DECODE
    reg  [31:0] rs2_val;
    reg  [31:0] imm_r;
    reg  [31:0] mem_addr_r;     // not reset: written in S_EXEC
    reg         mem_we_r;
    reg  [3:0]  mem_wstrb_r;
    reg  [31:0] mem_wdata_r;

    // ------------------------------------------------------------------
    // Decoder (on the registered instruction)
    // ------------------------------------------------------------------
    wire [4:0]  dec_rd, dec_rs1, dec_rs2;
    wire [2:0]  dec_funct3;
    wire [31:0] dec_imm;
    wire [3:0]  dec_alu_op;
    wire [1:0]  dec_op_a_sel, dec_wb_sel;
    wire        dec_op_b_sel;
    wire        dec_rd_we, dec_rs1_used, dec_rs2_used;
    wire        dec_is_load, dec_is_store, dec_is_branch, dec_is_jal, dec_is_jalr;
    wire        dec_is_fence, dec_is_ecall, dec_is_ebreak, dec_illegal;

    rv32i_decoder u_decoder (
        .insn(ir),
        .rd(dec_rd), .rs1(dec_rs1), .rs2(dec_rs2), .funct3(dec_funct3), .imm(dec_imm),
        .alu_op(dec_alu_op), .op_a_sel(dec_op_a_sel), .op_b_sel(dec_op_b_sel),
        .wb_sel(dec_wb_sel), .rd_we(dec_rd_we),
        .rs1_used(dec_rs1_used), .rs2_used(dec_rs2_used),
        .is_load(dec_is_load), .is_store(dec_is_store), .is_branch(dec_is_branch),
        .is_jal(dec_is_jal), .is_jalr(dec_is_jalr), .is_fence(dec_is_fence),
        .is_ecall(dec_is_ecall), .is_ebreak(dec_is_ebreak), .illegal(dec_illegal)
    );

    // ------------------------------------------------------------------
    // Register file
    // ------------------------------------------------------------------
    wire        rf_we;
    wire [31:0] rf_wdata;
    wire [31:0] rf_rdata1, rf_rdata2;

    rv32i_regfile u_regfile (
        .clk(clk),
        .we(rf_we), .waddr(dec_rd), .wdata(rf_wdata),
        .raddr1(dec_rs1), .rdata1(rf_rdata1),
        .raddr2(dec_rs2), .rdata2(rf_rdata2)
    );

    // ------------------------------------------------------------------
    // ALU (operands from S_DECODE latches; stable until the next S_DECODE)
    // ------------------------------------------------------------------
    reg  [31:0] alu_a;
    wire [31:0] alu_b = (dec_op_b_sel == OPB_IMM) ? imm_r : rs2_val;
    wire [31:0] alu_result;
    wire        alu_eq, alu_lt, alu_ltu;

    always @* begin
        case (dec_op_a_sel)
            OPA_PC:   alu_a = pc;
            OPA_ZERO: alu_a = 32'd0;
            default:  alu_a = rs1_val;
        endcase
    end

    rv32i_alu u_alu (
        .alu_op(dec_alu_op), .op_a(alu_a), .op_b(alu_b),
        .result(alu_result), .eq(alu_eq), .lt(alu_lt), .ltu(alu_ltu)
    );

    // ------------------------------------------------------------------
    // LSU. The effective address is alu_result (rs1_val + imm_r), which stays
    // valid until the next S_DECODE, so it serves both S_EXEC (misalignment,
    // byte enables) and S_MEM_WAIT (load extraction).
    // ------------------------------------------------------------------
    wire        lsu_misaligned;
    wire [3:0]  lsu_wstrb;
    wire [31:0] lsu_wdata, lsu_load_data;

    rv32i_lsu u_lsu (
        .funct3(dec_funct3), .addr_lo(alu_result[1:0]), .is_store(dec_is_store),
        .store_data(rs2_val), .load_word(dmem_rsp_rdata),
        .misaligned(lsu_misaligned), .wstrb(lsu_wstrb), .wdata(lsu_wdata),
        .load_data(lsu_load_data)
    );

    // ------------------------------------------------------------------
    // Next-pc logic (used in S_EXEC)
    // ------------------------------------------------------------------
    wire [31:0] pc_plus4    = pc + 32'd4;
    wire [31:0] pc_plus_imm = pc + imm_r;          // branch and JAL target

    reg branch_taken;
    always @* begin
        case (dec_funct3)
            F3_BEQ:  branch_taken =  alu_eq;
            F3_BNE:  branch_taken = !alu_eq;
            F3_BLT:  branch_taken =  alu_lt;
            F3_BGE:  branch_taken = !alu_lt;
            F3_BLTU: branch_taken =  alu_ltu;
            F3_BGEU: branch_taken = !alu_ltu;
            default: branch_taken = 1'b0;          // unreachable: decoder rejects 010/011
        endcase
    end

    wire        is_mem      = dec_is_load | dec_is_store;
    wire [31:0] jump_target = dec_is_jalr ? {alu_result[31:1], 1'b0} : pc_plus_imm;
    wire        redirect    = dec_is_jal | dec_is_jalr | (dec_is_branch & branch_taken);
    wire [31:0] next_pc     = redirect ? jump_target : pc_plus4;
    // JAL/branch targets are pc + even offset and JALR clears bit 0, so only
    // bit 1 can be wrong. Checked only when control is actually transferred.
    wire        target_misaligned = redirect & jump_target[1];
    wire [31:0] exec_wdata  = (dec_wb_sel == WB_PC4) ? pc_plus4 : alu_result;

    // ------------------------------------------------------------------
    // Retire / trap conditions (mutually exclusive)
    // ------------------------------------------------------------------
    wire exec_retire = (state == S_EXEC) & ~is_mem & ~target_misaligned;
    wire mem_retire  = (state == S_MEM_WAIT) & dmem_rsp_valid & ~dmem_rsp_err;
    wire retire_now  = exec_retire | mem_retire;

    wire trap_fetch  = (state == S_FETCH_WAIT) & imem_rsp_valid & imem_rsp_err;
    wire trap_decode = (state == S_DECODE) & (dec_illegal | dec_is_ecall | dec_is_ebreak);
    wire trap_exec   = (state == S_EXEC) & (is_mem ? lsu_misaligned : target_misaligned);
    wire trap_mem    = (state == S_MEM_WAIT) & dmem_rsp_valid & dmem_rsp_err;
    wire trap_now    = trap_fetch | trap_decode | trap_exec | trap_mem;

    reg [3:0]  trap_cause_nx;
    reg [31:0] trap_tval_nx;
    always @* begin
        trap_cause_nx = CAUSE_ILLEGAL_INSN;
        trap_tval_nx  = 32'd0;
        if (trap_fetch) begin
            trap_cause_nx = CAUSE_INSN_ACCESS;
            trap_tval_nx  = pc;
        end else if (trap_decode) begin
            if (dec_illegal) begin
                trap_cause_nx = CAUSE_ILLEGAL_INSN;
                trap_tval_nx  = ir;
            end else if (dec_is_ecall) begin
                trap_cause_nx = CAUSE_ECALL_M;
                trap_tval_nx  = 32'd0;
            end else begin
                trap_cause_nx = CAUSE_BREAKPOINT;
                trap_tval_nx  = pc;
            end
        end else if (trap_exec) begin
            if (is_mem) begin
                trap_cause_nx = dec_is_store ? CAUSE_STORE_MISALIGNED : CAUSE_LOAD_MISALIGNED;
                trap_tval_nx  = alu_result;
            end else begin
                trap_cause_nx = CAUSE_INSN_MISALIGNED;
                trap_tval_nx  = jump_target;
            end
        end else if (trap_mem) begin
            trap_cause_nx = mem_we_r ? CAUSE_STORE_ACCESS : CAUSE_LOAD_ACCESS;
            trap_tval_nx  = mem_addr_r;
        end
    end

    // ------------------------------------------------------------------
    // Register-file write port: only on the retire edge
    //   S_EXEC     : ALU / LUI / AUIPC / JAL / JALR result
    //   S_MEM_WAIT : load data (a load to x0 still performs the access)
    // ------------------------------------------------------------------
    assign rf_we    = dec_rd_we & (exec_retire | (mem_retire & dec_is_load));
    assign rf_wdata = (state == S_MEM_WAIT) ? lsu_load_data : exec_wdata;

    // ------------------------------------------------------------------
    // CMI outputs
    // ------------------------------------------------------------------
    // rst masks the request valids directly: the state register only changes at
    // the first reset edge, so without the mask a request that is waiting for
    // ready would stay visible during the first cycle of reset (CMI rule: valid
    // is 0 while rst is high). S_BOOT keeps valid at 0 in the first cycle after
    // reset release.
    assign imem_req_valid = (state == S_FETCH) & ~rst;
    assign imem_req_addr  = pc;
    assign dmem_req_valid = (state == S_MEM_REQ) & ~rst;
    assign dmem_req_addr  = mem_addr_r;
    assign dmem_req_we    = mem_we_r;
    assign dmem_req_wstrb = mem_wstrb_r;
    assign dmem_req_wdata = mem_wdata_r;
    assign halted_o       = (state == S_HALT);

    // ------------------------------------------------------------------
    // Control registers (synchronous reset)
    // ------------------------------------------------------------------
    always @(posedge clk) begin
        if (rst) begin
            state              <= S_BOOT;
            pc                 <= RESET_VECTOR;
            trap_cause_o       <= 4'd0;
            trap_pc_o          <= 32'd0;
            trap_tval_o        <= 32'd0;
            dbg_trap_valid_o   <= 1'b0;
            dbg_retire_valid_o <= 1'b0;
        end else begin
            dbg_trap_valid_o   <= trap_now;
            dbg_retire_valid_o <= retire_now;

            if (trap_now) begin
                state        <= S_HALT;
                trap_cause_o <= trap_cause_nx;
                trap_pc_o    <= pc;
                trap_tval_o  <= trap_tval_nx;
            end else begin
                case (state)
                    S_BOOT:       state <= S_FETCH;
                    S_FETCH:      if (imem_req_ready) state <= S_FETCH_WAIT;
                    S_FETCH_WAIT: if (imem_rsp_valid) state <= S_DECODE;
                    S_DECODE:     state <= S_EXEC;
                    S_EXEC:       state <= is_mem ? S_MEM_REQ : S_FETCH;
                    S_MEM_REQ:    if (dmem_req_ready) state <= S_MEM_WAIT;
                    S_MEM_WAIT:   if (dmem_rsp_valid) state <= S_FETCH;
                    default:      state <= S_HALT;      // S_HALT: stay
                endcase
            end

            if (retire_now)
                pc <= (state == S_EXEC) ? next_pc : pc_plus4;
        end
    end

    // ------------------------------------------------------------------
    // Datapath registers (no reset; each is written before it is used)
    // ------------------------------------------------------------------
    always @(posedge clk) begin
        if (state == S_FETCH_WAIT && imem_rsp_valid && !imem_rsp_err)
            ir <= imem_rsp_rdata;

        if (state == S_DECODE) begin
            rs1_val <= rf_rdata1;
            rs2_val <= rf_rdata2;
            imm_r   <= dec_imm;
        end

        if (state == S_EXEC) begin
            mem_addr_r  <= alu_result;
            mem_we_r    <= dec_is_store;
            mem_wstrb_r <= lsu_wstrb;      // 0 for loads (CMI rule)
            mem_wdata_r <= lsu_wdata;
        end

        if (retire_now) begin
            dbg_retire_pc_o       <= pc;
            dbg_retire_insn_o     <= ir;
            dbg_retire_rd_we_o    <= rf_we;
            dbg_retire_rd_o       <= dec_rd;
            dbg_retire_rd_wdata_o <= rf_we ? rf_wdata : 32'd0;
        end
    end

endmodule

`default_nettype wire
