#!/usr/bin/env python3
"""Generate GCC-assembled golden vectors for tb_rv32i_decoder.

Each entry below pairs an assembly instruction with the decoder result expected
from the *meaning* of that instruction (written by hand, not derived from its
encoding). The script assembles all entries with the RISC-V GNU toolchain, reads
the real encodings back from objdump and writes a Verilog include file of task
calls, so the testbench checks the decoder against encodings produced by an
independent assembler.

Output: dv/cpu/unit/decoder_golden_vectors.vh (committed; re-run only when the
entry list changes).

Toolchain lookup: $RISCV_EMG_RISCV_GCC_BIN (directory containing
riscv64-unknown-elf-gcc / objdump), default: the toolchain bundled with
Vivado 2026.1 (E:/Xilinx2026/2026.1/gnu/riscv/nt/bin).

Usage:  python scripts/gen/gen_decoder_vectors.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_FILE = REPO_ROOT / "dv" / "cpu" / "unit" / "decoder_golden_vectors.vh"
DEFAULT_GCC_BIN = os.environ.get("RISCV_EMG_GCC_BIN") or str(
    Path(os.environ.get("RISCV_EMG_VIVADO_ROOT", r"E:\Xilinx2026\2026.1\Vivado")).parent / "gnu" / "riscv" / "nt" / "bin")
PREFIX = "riscv64-unknown-elf-"
# Extensions are enabled only so that the assembler can produce encodings that
# the RV32I decoder must reject (CSR, FENCE.I, M, A, PAUSE). No 'C': no 16-bit forms.
MARCH = "rv32ima_zicsr_zifencei_zihintpause"
TEXT_BASE = 0x100000      # leaves room for negative JAL/branch offsets

# (assembly, testbench call with {insn} placeholder)
# Immediates in the calls are the values the decoder's imm output must carry.
ENTRIES: list[tuple[str, str]] = [
    # ---- LUI / AUIPC (imm = U-immediate << 12) ----
    ("lui x1, 0x0",             "chk_lui({insn}, 1, 32'h00000000)"),
    ("lui x31, 0xfffff",        "chk_lui({insn}, 31, 32'hFFFFF000)"),
    ("lui x5, 0x80000",         "chk_lui({insn}, 5, 32'h80000000)"),
    ("lui x0, 0x12345",         "chk_lui({insn}, 0, 32'h12345000)"),
    ("auipc x2, 0x0",           "chk_auipc({insn}, 2, 32'h00000000)"),
    ("auipc x3, 0xfffff",       "chk_auipc({insn}, 3, 32'hFFFFF000)"),
    ("auipc x0, 0x1",           "chk_auipc({insn}, 0, 32'h00001000)"),
    # ---- JAL (imm = byte offset) ----
    ("jal x1, .+0",             "chk_jal({insn}, 1, 0)"),
    ("jal x0, .+2048",          "chk_jal({insn}, 0, 2048)"),
    ("jal x1, .+0xffffe",       "chk_jal({insn}, 1, 1048574)"),
    ("jal x5, .-0x100000",      "chk_jal({insn}, 5, -1048576)"),
    ("jal x31, .-2",            "chk_jal({insn}, 31, -2)"),
    ("jal x7, .+0x7fe",         "chk_jal({insn}, 7, 2046)"),
    ("jal x8, .+0x800",         "chk_jal({insn}, 8, 2048)"),
    # ---- JALR ----
    ("jalr x1, 0(x2)",          "chk_jalr({insn}, 1, 2, 0)"),
    ("jalr x0, -2048(x31)",     "chk_jalr({insn}, 0, 31, -2048)"),
    ("jalr x5, 2047(x6)",       "chk_jalr({insn}, 5, 6, 2047)"),
    ("jalr x1, -1(x1)",         "chk_jalr({insn}, 1, 1, -1)"),
    # ---- Branches (imm = byte offset) ----
    ("beq x1, x2, .+4094",      "chk_branch({insn}, 3'b000, 1, 2, 4094)"),
    ("bne x3, x4, .-4096",      "chk_branch({insn}, 3'b001, 3, 4, -4096)"),
    ("blt x5, x6, .+2",         "chk_branch({insn}, 3'b100, 5, 6, 2)"),
    ("bge x7, x8, .-2",         "chk_branch({insn}, 3'b101, 7, 8, -2)"),
    ("bltu x30, x31, .+2048",   "chk_branch({insn}, 3'b110, 30, 31, 2048)"),
    ("bgeu x0, x1, .-2048",     "chk_branch({insn}, 3'b111, 0, 1, -2048)"),
    ("beq x0, x0, .+0",         "chk_branch({insn}, 3'b000, 0, 0, 0)"),
    ("bne x31, x0, .+0x7fe",    "chk_branch({insn}, 3'b001, 31, 0, 2046)"),
    # ---- Loads ----
    ("lb x1, -2048(x2)",        "chk_load({insn}, 3'b000, 1, 2, -2048)"),
    ("lh x3, 2047(x4)",         "chk_load({insn}, 3'b001, 3, 4, 2047)"),
    ("lw x5, 0(x6)",            "chk_load({insn}, 3'b010, 5, 6, 0)"),
    ("lbu x7, -1(x8)",          "chk_load({insn}, 3'b100, 7, 8, -1)"),
    ("lhu x31, 1(x30)",         "chk_load({insn}, 3'b101, 31, 30, 1)"),
    ("lw x0, 4(x1)",            "chk_load({insn}, 3'b010, 0, 1, 4)"),
    # ---- Stores ----
    ("sb x1, -2048(x2)",        "chk_store({insn}, 3'b000, 2, 1, -2048)"),
    ("sh x3, 2047(x4)",         "chk_store({insn}, 3'b001, 4, 3, 2047)"),
    ("sw x31, -1(x30)",         "chk_store({insn}, 3'b010, 30, 31, -1)"),
    ("sw x0, 0(x0)",            "chk_store({insn}, 3'b010, 0, 0, 0)"),
    ("sh x9, 32(x10)",          "chk_store({insn}, 3'b001, 10, 9, 32)"),
    # ---- OP-IMM ----
    ("addi x1, x2, -2048",      "chk_opimm({insn}, ALU_ADD, 1, 2, -2048)"),
    ("addi x0, x0, 0",          "chk_opimm({insn}, ALU_ADD, 0, 0, 0)"),
    ("slti x3, x4, 2047",       "chk_opimm({insn}, ALU_SLT, 3, 4, 2047)"),
    ("sltiu x5, x6, -1",        "chk_opimm({insn}, ALU_SLTU, 5, 6, -1)"),
    ("xori x7, x8, -1",         "chk_opimm({insn}, ALU_XOR, 7, 8, -1)"),
    ("ori x9, x10, 0x555",      "chk_opimm({insn}, ALU_OR, 9, 10, 1365)"),
    ("andi x11, x12, -2048",    "chk_opimm({insn}, ALU_AND, 11, 12, -2048)"),
    ("slli x13, x14, 31",       "chk_opimm({insn}, ALU_SLL, 13, 14, 31)"),
    ("slli x15, x16, 0",        "chk_opimm({insn}, ALU_SLL, 15, 16, 0)"),
    ("srli x17, x18, 31",       "chk_opimm({insn}, ALU_SRL, 17, 18, 31)"),
    ("srli x19, x20, 1",        "chk_opimm({insn}, ALU_SRL, 19, 20, 1)"),
    ("srai x21, x22, 31",       "chk_opimm({insn}, ALU_SRA, 21, 22, 32'h0000041F)"),
    ("srai x23, x24, 0",        "chk_opimm({insn}, ALU_SRA, 23, 24, 32'h00000400)"),
    # ---- OP ----
    ("add x1, x2, x3",          "chk_op({insn}, ALU_ADD, 1, 2, 3)"),
    ("sub x4, x5, x6",          "chk_op({insn}, ALU_SUB, 4, 5, 6)"),
    ("sll x7, x8, x9",          "chk_op({insn}, ALU_SLL, 7, 8, 9)"),
    ("slt x10, x11, x12",       "chk_op({insn}, ALU_SLT, 10, 11, 12)"),
    ("sltu x13, x14, x15",      "chk_op({insn}, ALU_SLTU, 13, 14, 15)"),
    ("xor x16, x17, x18",       "chk_op({insn}, ALU_XOR, 16, 17, 18)"),
    ("srl x19, x20, x21",       "chk_op({insn}, ALU_SRL, 19, 20, 21)"),
    ("sra x22, x23, x24",       "chk_op({insn}, ALU_SRA, 22, 23, 24)"),
    ("or x25, x26, x27",        "chk_op({insn}, ALU_OR, 25, 26, 27)"),
    ("and x28, x29, x30",       "chk_op({insn}, ALU_AND, 28, 29, 30)"),
    ("add x0, x31, x31",        "chk_op({insn}, ALU_ADD, 0, 31, 31)"),
    ("sub x31, x0, x1",         "chk_op({insn}, ALU_SUB, 31, 0, 1)"),
    # ---- FENCE family (legal, executed as NOP) ----
    ("fence",                   "chk_fence({insn})"),
    ("fence r, w",              "chk_fence({insn})"),
    ("fence iorw, iorw",        "chk_fence({insn})"),
    ("fence.tso",               "chk_fence({insn})"),
    ("pause",                   "chk_fence({insn})"),
    # ---- ECALL / EBREAK ----
    ("ecall",                   "chk_ecall({insn})"),
    ("ebreak",                  "chk_ebreak({insn})"),
    # ---- Not implemented in this core: must be illegal ----
    ("fence.i",                 "chk_illegal({insn})"),
    ("csrrw x1, mstatus, x2",   "chk_illegal({insn})"),
    ("csrrs x0, mcycle, x0",    "chk_illegal({insn})"),
    ("csrrwi x3, mscratch, 5",  "chk_illegal({insn})"),
    ("csrrci x0, mie, 31",      "chk_illegal({insn})"),
    ("mret",                    "chk_illegal({insn})"),
    ("sret",                    "chk_illegal({insn})"),
    ("wfi",                     "chk_illegal({insn})"),
    ("unimp",                   "chk_illegal({insn})"),
    ("mul x1, x2, x3",          "chk_illegal({insn})"),
    ("mulh x4, x5, x6",         "chk_illegal({insn})"),
    ("div x7, x8, x9",          "chk_illegal({insn})"),
    ("remu x10, x11, x12",      "chk_illegal({insn})"),
    ("lr.w x1, (x2)",           "chk_illegal({insn})"),
    ("amoadd.w x3, x4, (x5)",   "chk_illegal({insn})"),
]

OBJDUMP_RE = re.compile(r"^\s*([0-9a-f]+):\s+([0-9a-f]{8})\s+(.*)$")


def tool(name: str) -> str:
    bin_dir = Path(os.environ.get("RISCV_EMG_RISCV_GCC_BIN", DEFAULT_GCC_BIN))
    exe = bin_dir / (PREFIX + name + (".exe" if os.name == "nt" else ""))
    if not exe.is_file():
        sys.exit(f"gen_decoder_vectors: {exe} not found (set RISCV_EMG_RISCV_GCC_BIN)")
    return str(exe)


def main() -> int:
    gcc, objdump = tool("gcc"), tool("objdump")
    version = subprocess.run([gcc, "--version"], capture_output=True, text=True,
                             check=True).stdout.splitlines()[0]
    asm = [".option norvc", ".option norelax", ".text", ".globl _start", "_start:"]
    asm += [f"    {a}" for a, _ in ENTRIES]

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "vectors.S"
        elf = Path(tmp) / "vectors.elf"
        src.write_text("\n".join(asm) + "\n", encoding="ascii")
        subprocess.run([gcc, f"-march={MARCH}", "-mabi=ilp32", "-nostdlib", "-nostartfiles",
                        f"-Wl,-Ttext=0x{TEXT_BASE:x}", "-o", str(elf), str(src)], check=True)
        dump = subprocess.run([objdump, "-d", "-M", "no-aliases,numeric", str(elf)],
                              capture_output=True, text=True, check=True).stdout

    words = []
    for line in dump.splitlines():
        m = OBJDUMP_RE.match(line)
        if m:
            words.append((int(m.group(1), 16), m.group(2), m.group(3).strip()))
    if len(words) != len(ENTRIES):
        sys.exit(f"gen_decoder_vectors: expected {len(ENTRIES)} words, objdump gave {len(words)}")
    for i, (addr, _, _) in enumerate(words):
        if addr != TEXT_BASE + 4 * i:
            sys.exit(f"gen_decoder_vectors: unexpected address 0x{addr:x} for entry {i}")

    lines = [
        "// decoder_golden_vectors.vh - GENERATED by scripts/gen/gen_decoder_vectors.py",
        "// Do not edit by hand. Included inside tb_rv32i_decoder.",
        f"// Assembler : {version}",
        f"// -march={MARCH} -mabi=ilp32, .option norvc/norelax, text base 0x{TEXT_BASE:x}",
        "// Expected decoder fields are written from the instruction semantics.",
        "",
    ]
    for (asm_text, call), (_, word, disasm) in zip(ENTRIES, words):
        stmt = call.format(insn=f"32'h{word.upper()}") + ";"
        lines.append(f"{stmt:60s} // {asm_text:24s} | objdump: {disasm}")
    # newline="\n": keep LF on Windows too (see .gitattributes)
    OUT_FILE.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
    print(f"wrote {OUT_FILE.relative_to(REPO_ROOT).as_posix()} ({len(ENTRIES)} vectors)")
    print(f"assembler: {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
