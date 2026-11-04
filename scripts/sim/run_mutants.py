#!/usr/bin/env python3
"""Mutation check for the CPU testbenches (unit tests and core program tests).

Each mutant is a copy of one RTL file with a single deliberate bug (typical
RV32I / Verilog mistakes). The matching unit test is compiled against the
mutated copy and must report FAIL; a mutant that still PASSes means the
testbench has a blind spot. Core mutants are run against every program
(zero-wait and slow memory) plus one reset test; one failing run kills them. Mutated copies live in build/mutants/ (ignored by
Git); the original RTL is never modified.

Usage:  python scripts/sim/run_mutants.py [--seed N]
Exit status: 0 if every mutant is killed, 1 otherwise.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_sim  # noqa: E402  (same directory)

REPO_ROOT = run_sim.REPO_ROOT
MUT_DIR = REPO_ROOT / "build" / "mutants"

# (id, test, rtl file, original text, mutated text, description)
MUTANTS = [
    ("R1", "regfile", "rv32i_regfile.v",
     "assign rdata1 = (raddr1 == 5'd0) ? 32'd0 : regs[raddr1];",
     "assign rdata1 = regs[raddr1];",
     "port 1 loses the x0 = 0 guard"),
    ("R2", "regfile", "rv32i_regfile.v",
     "assign rdata2 = (raddr2 == 5'd0) ? 32'd0 : regs[raddr2];",
     "assign rdata2 = (raddr2 == 5'd0) ? 32'd0 : regs[raddr1];",
     "port 2 reads port 1's address"),
    ("R3", "regfile", "rv32i_regfile.v",
     "assign rdata1 = (raddr1 == 5'd0) ? 32'd0 : regs[raddr1];",
     "assign rdata1 = (raddr1 == 5'd0) ? 32'd0 : (we && waddr == raddr1) ? wdata : regs[raddr1];",
     "unspecified write-through on port 1"),
    ("R4", "regfile", "rv32i_regfile.v",
     "if (we && (waddr != 5'd0))",
     "if (waddr != 5'd0)",
     "write enable ignored"),
    ("A1", "alu", "rv32i_alu.v",
     "wire signed [31:0] sra_res = $signed(op_a) >>> shamt;",
     "wire signed [31:0] sra_res = op_a >>> shamt;",
     "SRA on an unsigned operand (logical shift)"),
    ("A2", "alu", "rv32i_alu.v",
     "ALU_SRA:  result = sra_res;",
     "ALU_SRA:  result = alu_op[0] ? $signed(op_a) >>> shamt : 32'd0;",
     "SRA inside a mixed signed/unsigned expression"),
    ("A3", "alu", "rv32i_alu.v",
     "assign lt  = ($signed(op_a) < $signed(op_b));",
     "assign lt  = (op_a < op_b);",
     "signed compare done unsigned"),
    ("A4", "alu", "rv32i_alu.v",
     "wire [4:0]         shamt   = op_b[4:0];",
     "wire [5:0]         shamt   = op_b[5:0];",
     "shift amount uses 6 bits"),
    ("A5", "alu", "rv32i_alu.v",
     "ALU_SLTU: result = {31'd0, ltu};",
     "ALU_SLTU: result = {31'd0, lt};",
     "SLTU uses the signed flag"),
    ("A6", "alu", "rv32i_alu.v",
     "wire [31:0]        sub_res = op_a - op_b;",
     "wire [31:0]        sub_res = op_b - op_a;",
     "SUB operands swapped"),
    ("D1", "decoder", "rv32i_decoder.v",
     "dec_illegal = (funct7 != 7'b0000000) && (funct7 != 7'b0100000);",
     "dec_illegal = 1'b0;",
     "SRLI/SRAI reserved imm[11:5] accepted"),
    ("D2", "decoder", "rv32i_decoder.v",
     "// SLLI\n                    dec_illegal = (funct7 != 7'b0000000);",
     "// SLLI\n                    dec_illegal = (funct7[6:1] != 6'b000000);",
     "SLLI with shamt[5]=1 accepted"),
    ("D3", "decoder", "rv32i_decoder.v",
     "dec_illegal = (funct3 != 3'b000);\n            end\n\n            OPC_BRANCH",
     "dec_illegal = 1'b0;\n            end\n\n            OPC_BRANCH",
     "JALR funct3 not checked"),
    ("D4", "decoder", "rv32i_decoder.v",
     "is_ecall    = (insn == 32'h0000_0073);",
     "is_ecall    = (insn[31:15] == 17'd0) && (insn[14:12] == 3'b000) && (insn[6:0] == 7'b1110011);",
     "ECALL accepted with rd != 0"),
    ("D5", "decoder", "rv32i_decoder.v",
     "{{19{insn[31]}}, insn[31], insn[7], insn[30:25]",
     "{{19{insn[31]}}, insn[7], insn[31], insn[30:25]",
     "B-immediate bits 11/12 swapped"),
    ("D6", "decoder", "rv32i_decoder.v",
     "insn[19:12], insn[20], insn[30:21]",
     "insn[20], insn[19:12], insn[30:21]",
     "J-immediate field order wrong"),
    ("D7", "decoder", "rv32i_decoder.v",
     "rd_we   = writes_rd && (insn[11:7] != 5'd0);",
     "rd_we   = writes_rd;",
     "rd_we set for rd = x0"),
    ("D8", "decoder", "rv32i_decoder.v",
     "if (dec_illegal) begin",
     "if (1'b0) begin",
     "illegal instructions keep their control signals"),
    ("D9", "decoder", "rv32i_decoder.v",
     "dec_illegal = (funct3 != 3'b000);                 // FENCE.I and reserved",
     "dec_illegal = (funct3[2:1] != 2'b00);",
     "FENCE.I accepted"),
    ("D10", "decoder", "rv32i_decoder.v",
     "alu_op    = (funct3 == 3'b000 && funct7[5]) ? ALU_SUB : arith_op;",
     "alu_op    = arith_op;",
     "SUB decoded as ADD"),
    ("D11", "decoder", "rv32i_decoder.v",
     "dec_illegal = (funct3 == 3'b010) || (funct3 == 3'b011);",
     "dec_illegal = (funct3 == 3'b010);",
     "BRANCH funct3=011 accepted"),
    ("D12", "decoder", "rv32i_decoder.v",
     "wire [31:0] imm_s = {{20{insn[31]}}, insn[31:25], insn[11:7]};",
     "wire [31:0] imm_s = {20'd0, insn[31:25], insn[11:7]};",
     "S-immediate not sign-extended"),
    ("L1", "lsu", "rv32i_lsu.v",
     "3'b000:  load_data = {{24{ld_byte[7]}}, ld_byte};",
     "3'b000:  load_data = {24'd0, ld_byte};",
     "LB zero-extends"),
    ("L2", "lsu", "rv32i_lsu.v",
     "addr_lo[1] ? load_word[31:16] : load_word[15:0]",
     "addr_lo[0] ? load_word[31:16] : load_word[15:0]",
     "half-word selected by the wrong address bit"),
    ("L3", "lsu", "rv32i_lsu.v",
     "if (!is_store || misaligned)",
     "if (!is_store)",
     "misaligned store keeps its byte enables"),
    ("L4", "lsu", "rv32i_lsu.v",
     "wdata = {2{store_data[15:0]}};",
     "wdata = {16'd0, store_data[15:0]};",
     "SH data not placed in the upper lane"),
    ("L5", "lsu", "rv32i_lsu.v",
     "2'b10:   misaligned = (addr_lo != 2'b00);",
     "2'b10:   misaligned = addr_lo[0];",
     "word misalignment only checks bit 0"),
    ("L6", "lsu", "rv32i_lsu.v",
     "wstrb = 4'b0001 << addr_lo;",
     "wstrb = 4'b0001;",
     "SB strobe ignores the address"),
    # ---- core: run against every program (zero-wait + slow memory) and a reset test ----
    ("C1", "core", "rv32i_core.v",
     "dec_is_jalr ? {alu_result[31:1], 1'b0} : pc_plus_imm",
     "dec_is_jalr ? alu_result : pc_plus_imm",
     "JALR target bit 0 not cleared"),
    ("C2", "core", "rv32i_core.v",
     "wire        target_misaligned = redirect & jump_target[1];",
     "wire        target_misaligned = (redirect | dec_is_branch) & jump_target[1];",
     "misaligned-target trap on a NOT-taken branch"),
    ("C3", "core", "rv32i_core.v",
     "wire        is_mem      = dec_is_load | dec_is_store;",
     "wire        is_mem      = (dec_is_load & dec_rd_we) | dec_is_store;",
     "load to x0 skips the memory access"),
    ("C4", "core", "rv32i_core.v",
     "wire exec_retire = (state == S_EXEC) & ~is_mem & ~target_misaligned;",
     "wire exec_retire = (state == S_EXEC) & ~is_mem;",
     "jump with misaligned target still writes rd / retires"),
    ("C5", "core", "rv32i_core.v",
     "wire mem_retire  = (state == S_MEM_WAIT) & dmem_rsp_valid & ~dmem_rsp_err;",
     "wire mem_retire  = (state == S_MEM_WAIT) & dmem_rsp_valid;",
     "load writes rd despite a bus error"),
    ("C6", "core", "rv32i_core.v",
     "assign dmem_req_valid = (state == S_MEM_REQ) & ~rst;",
     "assign dmem_req_valid = ((state == S_MEM_REQ) | ((state == S_MEM_WAIT) & ~dmem_rsp_valid)) & ~rst;",
     "request re-issued while waiting (duplicate store)"),
    ("C7", "core", "rv32i_core.v",
     "S_MEM_REQ:    if (dmem_req_ready) state <= S_MEM_WAIT;",
     "S_MEM_REQ:    state <= S_MEM_WAIT;",
     "request withdrawn before ready (no handshake wait)"),
    ("C8", "core", "rv32i_core.v",
     "trap_tval_nx  = ir;",
     "trap_tval_nx  = pc;",
     "illegal-instruction tval is the pc"),
    ("C9", "core", "rv32i_core.v",
     "trap_cause_nx = CAUSE_ECALL_M;",
     "trap_cause_nx = CAUSE_BREAKPOINT;",
     "ECALL reported as breakpoint"),
    ("C10", "core", "rv32i_core.v",
     "F3_BGE:  branch_taken = !alu_lt;",
     "F3_BGE:  branch_taken = !alu_ltu;",
     "BGE uses the unsigned compare"),
    ("C11", "core", "rv32i_core.v",
     "            state              <= S_BOOT;",
     "            state              <= S_FETCH;",
     "fetch request asserted during reset"),
    ("C12", "core", "rv32i_core.v",
     "S_FETCH_WAIT: if (imem_rsp_valid) state <= S_DECODE;",
     "S_FETCH_WAIT: state <= S_DECODE;",
     "decode before the instruction arrives"),
    ("C13", "core", "rv32i_core.v",
     "assign rf_wdata = (state == S_MEM_WAIT) ? lsu_load_data : exec_wdata;",
     "assign rf_wdata = (state == S_MEM_WAIT) ? dmem_rsp_rdata : exec_wdata;",
     "load result not extracted/extended by the LSU"),
    # (pc <= next_pc for loads/stores would be an equivalent mutant: next_pc is
    #  pc+4 whenever no branch/jump is decoded, so it is not listed)
    ("C14", "core", "rv32i_core.v",
     "pc <= (state == S_EXEC) ? next_pc : pc_plus4;",
     "pc <= (state == S_EXEC) ? next_pc : pc_plus_imm;",
     "pc after a load/store is pc+imm instead of pc+4"),
    ("C15", "core", "rv32i_core.v",
     "mem_wstrb_r <= lsu_wstrb;      // 0 for loads (CMI rule)",
     "mem_wstrb_r <= 4'b1111;",
     "store writes all four bytes (and loads carry wstrb)"),
    ("C16", "core", "rv32i_core.v",
     "assign imem_req_valid = (state == S_FETCH) & ~rst;",
     "assign imem_req_valid = (state == S_FETCH);",
     "fetch request stays visible in the first reset cycle"),
    # ---- base-module bugs run on the whole core: in random programs (no
    #      self-checks) only the RTL/ISS comparison can detect them ----
    ("P1", "core", "rv32i_alu.v",
     "wire signed [31:0] sra_res = $signed(op_a) >>> shamt;",
     "wire signed [31:0] sra_res = op_a >>> shamt;",
     "core: SRA becomes a logical shift"),
    ("P2", "core", "rv32i_alu.v",
     "assign lt  = ($signed(op_a) < $signed(op_b));",
     "assign lt  = (op_a < op_b);",
     "core: signed compare done unsigned"),
    ("P3", "core", "rv32i_lsu.v",
     "3'b000:  load_data = {{24{ld_byte[7]}}, ld_byte};",
     "3'b000:  load_data = {24'd0, ld_byte};",
     "core: LB zero-extends"),
    ("P4", "core", "rv32i_decoder.v",
     "wire [31:0] imm_s = {{20{insn[31]}}, insn[31:25], insn[11:7]};",
     "wire [31:0] imm_s = {20'd0, insn[31:25], insn[11:7]};",
     "core: S-immediate not sign-extended"),
]

# core-mutant test selection (all programs)
CORE_SELECTION = ["core/*/zero", "core/*/slow@1", "core/loop/reset_*@1", "rand/*/zero"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--only", help="comma-separated mutant ids to run (default: all)")
    args = ap.parse_args()

    iverilog, vvp = run_sim.find_tool("iverilog"), run_sim.find_tool("vvp")
    cpu_sources, cpu_incdirs = run_sim.parse_filelist("rtl/cpu/cpu.f")
    shutil.rmtree(MUT_DIR, ignore_errors=True)

    if any(m[1] == "core" for m in MUTANTS):
        b = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "sw" / "build_asm_tests.py")],
                           capture_output=True, text=True)
        if b.returncode != 0:
            print(b.stdout + b.stderr)
            return 1
    core_reg = {**run_sim.program_tests("core", [args.seed]),
                **run_sim.program_tests("rand", [args.seed])}
    core_names = run_sim.select(list(core_reg), CORE_SELECTION)

    survivors = []
    selected = [m for m in MUTANTS if not args.only or m[0] in args.only.split(",")]
    for mid, test, rtl, orig, new, desc in selected:
        src_path = REPO_ROOT / "rtl" / "cpu" / rtl
        text = src_path.read_text(encoding="utf-8")
        count = text.count(orig)
        if count != 1:
            print(f"[ERROR] {mid}: pattern found {count} times in {rtl}; mutant table is stale")
            survivors.append(mid)
            continue
        out = MUT_DIR / mid / rtl
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text.replace(orig, new), encoding="utf-8", newline="\n")
        mutated_rel = out.relative_to(REPO_ROOT).as_posix()
        sources = [mutated_rel if s.endswith("/" + rtl) else s for s in cpu_sources]

        if test == "core":
            runs = []
            for n in core_names:
                base = core_reg[n]
                spec = run_sim.TestSpec(top=base.top, sources=sources + base.sources,
                                        incdirs=cpu_incdirs, plusargs=base.plusargs,
                                        seed=base.seed, program=base.program,
                                        args_file=base.args_file)
                runs.append(run_sim.run_test(f"mutant_{mid}/{n}", spec, iverilog, vvp,
                                             base.seed, wave=False, allow_warnings=True,
                                             timeout=120, extra_plusargs=[]))
        else:
            top, tb_srcs, inc = run_sim.UNIT_TESTS[test]
            spec = run_sim.TestSpec(top=top, sources=sources + tb_srcs, incdirs=cpu_incdirs + inc)
            runs = [run_sim.run_test(f"mutant_{mid}", spec, iverilog, vvp, args.seed,
                                     wave=False, allow_warnings=True, timeout=600,
                                     extra_plusargs=[])]

        invalid = [r for r in runs if r.reason.startswith(("compile failed", "missing source"))]
        killers = [r for r in runs if r.status == "FAIL" and r not in invalid]
        iss_only = [r for r in killers if r.name.startswith(f"mutant_{mid}/rand/")
                    and r.reason.startswith("RTL/ISS mismatch")]
        if invalid:
            verdict = f"INVALID ({invalid[0].reason})"
            survivors.append(mid)
        elif killers:
            verdict = f"killed  ({len(killers)}/{len(runs)} runs fail"
            verdict += f", {len(iss_only)} random by ISS)" if test == "core" else ")"
        else:
            verdict = "SURVIVED"
            survivors.append(mid)
        print(f"[{mid:4s}] {test:8s} {verdict:44s} {desc}", flush=True)

    total = len(selected)
    print(f"\nMUTANTS: {total - len(survivors)}/{total} killed"
          + (f", NOT killed: {' '.join(survivors)}" if survivors else ""))
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
