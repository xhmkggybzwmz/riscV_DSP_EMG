#!/usr/bin/env python3
"""rv32i_iss - instruction-level reference model of the project's RV32I execution
environment (docs/cpu/rv32i_core_spec.md, docs/cpu/cpu_mem_interface.md,
docs/soc/memory_map.md).

Written from the RISC-V Unprivileged ISA specification (RV32I base integer
instruction set, version 2.1), NOT from the RTL:
  - instructions are identified with the MASK/MATCH values published by the
    riscv-opcodes project (an instruction is X iff (insn & MASK_X) == MATCH_X);
    anything that matches no RV32I entry is illegal;
  - immediates are assembled from the bit positions given in the spec's
    instruction-format figures;
  - semantics use unbounded Python integers with explicit 32-bit wrap and
    explicit signed/unsigned interpretation.

Execution environment (project-defined, not RISC-V privileged architecture):
  - reset pc = 0, 64 KB RAM at 0, TOHOST word at 0x1000_0008 (store ends the
    program with that value, load returns 0), every other address faults;
  - optional instruction/data error windows (same tb_* symbols as the RTL
    testbench) make accesses fault;
  - an exception halts with (cause, pc, tval) and changes no architectural state:
      0 instruction address misaligned (taken jump/branch target not 4-byte aligned), tval = target
      1 instruction access fault, tval = pc
      2 illegal instruction, tval = instruction word
      3 breakpoint (EBREAK), tval = pc
      4/6 load/store address misaligned (checked before any access), tval = address
      5/7 load/store access fault (the access was attempted), tval = address
      11 environment call (ECALL), tval = 0
  - FENCE (incl. FENCE.TSO, PAUSE, any fm/pred/succ/rs1/rd) is a no-op;
    FENCE.I, CSR*, MRET/SRET/WFI, every other SYSTEM encoding, M/A/F/D/C/V and
    custom opcodes are illegal (they match no entry of the table).
  - Registers x1..x31 start UNDEFINED. Reading an undefined register stops the
    model with an environment error: test programs must initialise registers
    (crt0 does), so the RTL can never silently depend on a reset value.

Command line:
  python reference/rv32i_iss/rv32i_iss.py --args prog.args --trace out.trace
         [--mem-dump out.mem] [--cov out.cov] [--max-steps N]
  (the .args file is the one written by scripts/sw/build_asm_tests.py:
   +hex=<image> and +tb_*=<hex> lines)

Trace format (one event per line, hex without 0x):
  A <addr> <we> <size> <value> <err>    data access attempted by the next R/T event
  R <pc> <insn> <rd_we> <rd> <wdata>    instruction retired
  T <cause> <pc> <tval>                 exception (instruction did not retire)
  E TOHOST <value> | E HALT | E LIMIT | E ENVERR <text>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

XLEN_MASK = 0xFFFF_FFFF
MEM_BYTES = 64 * 1024
TOHOST_ADDR = 0x1000_0008
RESET_PC = 0x0000_0000

# (mnemonic, MASK, MATCH, format) - values from riscv-opcodes (rv_i)
OPCODES = [
    ("lui",    0x0000007F, 0x00000037, "U"),
    ("auipc",  0x0000007F, 0x00000017, "U"),
    ("jal",    0x0000007F, 0x0000006F, "J"),
    ("jalr",   0x0000707F, 0x00000067, "I"),
    ("beq",    0x0000707F, 0x00000063, "B"),
    ("bne",    0x0000707F, 0x00001063, "B"),
    ("blt",    0x0000707F, 0x00004063, "B"),
    ("bge",    0x0000707F, 0x00005063, "B"),
    ("bltu",   0x0000707F, 0x00006063, "B"),
    ("bgeu",   0x0000707F, 0x00007063, "B"),
    ("lb",     0x0000707F, 0x00000003, "I"),
    ("lh",     0x0000707F, 0x00001003, "I"),
    ("lw",     0x0000707F, 0x00002003, "I"),
    ("lbu",    0x0000707F, 0x00004003, "I"),
    ("lhu",    0x0000707F, 0x00005003, "I"),
    ("sb",     0x0000707F, 0x00000023, "S"),
    ("sh",     0x0000707F, 0x00001023, "S"),
    ("sw",     0x0000707F, 0x00002023, "S"),
    ("addi",   0x0000707F, 0x00000013, "I"),
    ("slti",   0x0000707F, 0x00002013, "I"),
    ("sltiu",  0x0000707F, 0x00003013, "I"),
    ("xori",   0x0000707F, 0x00004013, "I"),
    ("ori",    0x0000707F, 0x00006013, "I"),
    ("andi",   0x0000707F, 0x00007013, "I"),
    ("slli",   0xFE00707F, 0x00001013, "I"),
    ("srli",   0xFE00707F, 0x00005013, "I"),
    ("srai",   0xFE00707F, 0x40005013, "I"),
    ("add",    0xFE00707F, 0x00000033, "R"),
    ("sub",    0xFE00707F, 0x40000033, "R"),
    ("sll",    0xFE00707F, 0x00001033, "R"),
    ("slt",    0xFE00707F, 0x00002033, "R"),
    ("sltu",   0xFE00707F, 0x00003033, "R"),
    ("xor",    0xFE00707F, 0x00004033, "R"),
    ("srl",    0xFE00707F, 0x00005033, "R"),
    ("sra",    0xFE00707F, 0x40005033, "R"),
    ("or",     0xFE00707F, 0x00006033, "R"),
    ("and",    0xFE00707F, 0x00007033, "R"),
    ("fence",  0x0000707F, 0x0000000F, "I"),
    ("ecall",  0xFFFFFFFF, 0x00000073, "-"),
    ("ebreak", 0xFFFFFFFF, 0x00100073, "-"),
]

LOAD_SIZE = {"lb": (1, True), "lh": (2, True), "lw": (4, True), "lbu": (1, False), "lhu": (2, False)}
STORE_SIZE = {"sb": 1, "sh": 2, "sw": 4}
# register-immediate and register-register forms -> operation
ARITH_OP = {"addi": "add", "slti": "slt", "sltiu": "sltu", "xori": "xor", "ori": "or",
            "andi": "and", "slli": "sll", "srli": "srl", "srai": "sra",
            "add": "add", "sub": "sub", "sll": "sll", "slt": "slt", "sltu": "sltu",
            "xor": "xor", "srl": "srl", "sra": "sra", "or": "or", "and": "and"}


def bits(x: int, hi: int, lo: int) -> int:
    return (x >> lo) & ((1 << (hi - lo + 1)) - 1)


def sext(value: int, nbits: int) -> int:
    """Interpret the low nbits of value as a two's-complement number."""
    value &= (1 << nbits) - 1
    return value - (1 << nbits) if value >> (nbits - 1) else value


def signed(v: int) -> int:
    return sext(v, 32)


def identify(insn: int) -> tuple[str, str] | None:
    hits = [(n, f) for n, m, v, f in OPCODES if insn & m == v]
    if len(hits) > 1:
        raise AssertionError(f"opcode table overlap for {insn:08x}: {hits}")
    return hits[0] if hits else None


def immediate(insn: int, fmt: str) -> int:
    """Sign-extended immediate as a Python int (spec instruction-format figures)."""
    if fmt == "I":
        return sext(bits(insn, 31, 20), 12)
    if fmt == "S":
        return sext((bits(insn, 31, 25) << 5) | bits(insn, 11, 7), 12)
    if fmt == "B":
        return sext((bits(insn, 31, 31) << 12) | (bits(insn, 7, 7) << 11) |
                    (bits(insn, 30, 25) << 5) | (bits(insn, 11, 8) << 1), 13)
    if fmt == "U":
        return sext(bits(insn, 31, 12) << 12, 32)
    if fmt == "J":
        return sext((bits(insn, 31, 31) << 20) | (bits(insn, 19, 12) << 12) |
                    (bits(insn, 20, 20) << 11) | (bits(insn, 30, 21) << 1), 21)
    return 0


class EnvError(Exception):
    """The test environment broke a rule (e.g. read of an undefined register)."""


class Iss:
    def __init__(self, image: bytes, i_err=None, d_err=None, mem_bytes: int = MEM_BYTES):
        if len(image) > mem_bytes:
            raise ValueError("program image larger than RAM")
        self.mem = bytearray(mem_bytes)
        self.mem[:len(image)] = image
        self.mem_bytes = mem_bytes
        self.i_err = i_err              # (lo, hi) inclusive or None
        self.d_err = d_err
        self.pc = RESET_PC
        self.x: list[int | None] = [0] + [None] * 31
        self.halted = False
        self.tohost: int | None = None
        self.trap: tuple[int, int, int] | None = None
        self.retired = 0

    # ---------------- registers ----------------
    def rd_reg(self, r: int) -> int:
        v = self.x[r]
        if v is None:
            raise EnvError(f"read of undefined register x{r} at pc {self.pc:08x}")
        return v

    def wr_reg(self, r: int, v: int) -> tuple[int, int, int]:
        if r == 0:
            return (0, 0, 0)                    # x0 is hard-wired: no write
        self.x[r] = v & XLEN_MASK
        return (1, r, v & XLEN_MASK)

    # ---------------- memory ----------------
    def _in(self, a: int, win) -> bool:
        return win is not None and win[0] <= a <= win[1]

    def fetch_ok(self, a: int) -> bool:
        return a + 3 < self.mem_bytes and not self._in(a, self.i_err)

    def data_region(self, a: int, size: int) -> str:
        if a & ~3 == TOHOST_ADDR:
            return "tohost"
        if a + size - 1 < self.mem_bytes and not self._in(a, self.d_err):
            return "ram"
        return "fault"

    def load_bytes(self, a: int, n: int) -> int:
        return int.from_bytes(self.mem[a:a + n], "little")

    # ---------------- one instruction ----------------
    def step(self) -> list[tuple]:
        """Execute one instruction; return its trace events."""
        pc = self.pc
        if not self.fetch_ok(pc):
            return self._trap(1, pc, pc, [])
        insn = self.load_bytes(pc, 4)
        hit = identify(insn)
        if hit is None:
            return self._trap(2, pc, insn, [])
        name, fmt = hit
        imm = immediate(insn, fmt)
        rd, rs1, rs2 = bits(insn, 11, 7), bits(insn, 19, 15), bits(insn, 24, 20)
        events: list[tuple] = []
        info = {"name": name, "rd": rd, "rs1": rs1, "rs2": rs2, "imm": imm, "fmt": fmt}
        self.info = info
        next_pc = (pc + 4) & XLEN_MASK
        wr = (0, 0, 0)

        if name == "lui":
            wr = self.wr_reg(rd, imm)
        elif name == "auipc":
            wr = self.wr_reg(rd, pc + imm)
        elif name in ("jal", "jalr"):
            if name == "jal":
                target = (pc + imm) & XLEN_MASK
            else:
                target = (self.rd_reg(rs1) + imm) & XLEN_MASK & ~1
            info["back"] = target <= pc
            if target % 4:
                return self._trap(0, pc, target, events)
            wr = self.wr_reg(rd, pc + 4)
            next_pc = target
        elif fmt == "B":
            a, b = self.rd_reg(rs1), self.rd_reg(rs2)
            taken = {
                "beq": a == b, "bne": a != b,
                "blt": signed(a) < signed(b), "bge": signed(a) >= signed(b),
                "bltu": a < b, "bgeu": a >= b,
            }[name]
            info.update(a=a, b=b, taken=taken, back=imm <= 0)
            if taken:
                target = (pc + imm) & XLEN_MASK
                if target % 4:
                    return self._trap(0, pc, target, events)
                next_pc = target
        elif name in LOAD_SIZE:
            size, is_signed = LOAD_SIZE[name]
            addr = (self.rd_reg(rs1) + imm) & XLEN_MASK
            info.update(addr=addr, size=size)
            if addr % size:
                return self._trap(4, pc, addr, events)
            region = self.data_region(addr, size)
            if region == "fault":
                events.append(("A", addr, 0, size, 0, 1))
                return self._trap(5, pc, addr, events)
            raw = 0 if region == "tohost" else self.load_bytes(addr, size)
            events.append(("A", addr, 0, size, raw, 0))
            value = sext(raw, 8 * size) if is_signed else raw
            wr = self.wr_reg(rd, value)
        elif name in STORE_SIZE:
            size = STORE_SIZE[name]
            addr = (self.rd_reg(rs1) + imm) & XLEN_MASK
            data = self.rd_reg(rs2) & ((1 << (8 * size)) - 1)
            info.update(addr=addr, size=size)
            if addr % size:
                return self._trap(6, pc, addr, events)
            region = self.data_region(addr, size)
            if region == "fault":
                events.append(("A", addr, 1, size, data, 1))
                return self._trap(7, pc, addr, events)
            events.append(("A", addr, 1, size, data, 0))
            if region == "tohost":
                if size != 4:
                    raise EnvError(f"sub-word store to TOHOST at pc {pc:08x}")
                self.tohost = data
            else:
                self.mem[addr:addr + size] = data.to_bytes(size, "little")
        elif name == "fence":
            pass
        elif name == "ecall":
            return self._trap(11, pc, 0, events)
        elif name == "ebreak":
            return self._trap(3, pc, pc, events)
        else:                                       # OP-IMM and OP
            a = self.rd_reg(rs1)
            if fmt == "R":
                b = self.rd_reg(rs2)
            elif name in ("slli", "srli", "srai"):
                b = bits(insn, 24, 20)              # shamt field
            else:
                b = imm & XLEN_MASK                 # sign-extended immediate, as 32 bits
            op = ARITH_OP[name]
            sh = b & 0x1F
            result = {
                "add":  a + b,
                "sub":  a - b,
                "sll":  a << sh,
                "slt":  1 if signed(a) < signed(b) else 0,
                "sltu": 1 if a < b else 0,
                "xor":  a ^ b,
                "srl":  a >> sh,
                "sra":  signed(a) >> sh,             # Python >> on negatives is arithmetic
                "or":   a | b,
                "and":  a & b,
            }[op]
            wr = self.wr_reg(rd, result)
            info["a"], info["b"] = a, b

        events.append(("R", pc, insn, wr[0], wr[1] if wr[0] else 0, wr[2] if wr[0] else 0))
        self.retired += 1
        self.pc = next_pc
        return events

    def _trap(self, cause: int, pc: int, tval: int, events: list) -> list:
        self.info = dict(getattr(self, "info", {}) if cause not in (1, 2) else {}, trap=cause)
        self.halted = True
        self.trap = (cause, pc, tval & XLEN_MASK)
        events.append(("T", cause, pc, tval & XLEN_MASK))
        return events


# ---------------------------------------------------------------------------
def read_args(path: Path) -> dict:
    args = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("+") and "=" in line:
            k, v = line[1:].split("=", 1)
            args[k] = v
    return args


def load_image(hex_path: Path) -> bytes:
    words = [int(l, 16) for l in hex_path.read_text(encoding="ascii").split() if l]
    return b"".join(w.to_bytes(4, "little") for w in words)


def fmt_event(e: tuple) -> str:
    if e[0] == "A":
        return f"A {e[1]:08x} {e[2]} {e[3]} {e[4]:08x} {e[5]}"
    if e[0] == "R":
        return f"R {e[1]:08x} {e[2]:08x} {e[3]} {e[4]:02d} {e[5]:08x}"
    if e[0] == "T":
        return f"T {e[1]} {e[2]:08x} {e[3]:08x}"
    return " ".join(str(x) for x in e)


def run(args_file: Path, max_steps: int = 2_000_000, cov=None):
    a = read_args(args_file)
    hexfile = Path(a["hex"])
    win = lambda lo, hi: (int(a[lo], 16), int(a[hi], 16)) if lo in a else None
    iss = Iss(load_image(hexfile), i_err=win("tb_i_err_lo", "tb_i_err_hi"),
              d_err=win("tb_d_err_lo", "tb_d_err_hi"))
    lines: list[str] = []
    end = "E LIMIT"
    try:
        for _ in range(max_steps):
            iss.info = {}
            ev = iss.step()
            lines += [fmt_event(e) for e in ev]
            if cov is not None:
                cov.append(dict(iss.info))
            if iss.halted:
                end = "E HALT"
                break
            if iss.tohost is not None:
                end = f"E TOHOST {iss.tohost:08x}"
                break
    except EnvError as exc:
        end = f"E ENVERR {exc}"
    lines.append(end)
    return iss, lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--args", required=True, type=Path, help=".args file of the program")
    ap.add_argument("--trace", type=Path, help="write the event trace here")
    ap.add_argument("--mem-dump", type=Path, help="write final RAM, one hex word per line")
    ap.add_argument("--max-steps", type=int, default=2_000_000)
    ns = ap.parse_args()
    iss, lines = run(ns.args, ns.max_steps)
    text = "\n".join(lines) + "\n"
    if ns.trace:
        ns.trace.parent.mkdir(parents=True, exist_ok=True)
        ns.trace.write_text(text, encoding="ascii", newline="\n")
    else:
        sys.stdout.write(text)
    if ns.mem_dump:
        words = [int.from_bytes(iss.mem[i:i + 4], "little") for i in range(0, iss.mem_bytes, 4)]
        ns.mem_dump.write_text("".join(f"{w:08x}\n" for w in words), encoding="ascii", newline="\n")
    print(f"rv32i_iss: retired={iss.retired} end={lines[-1]}", file=sys.stderr)
    return 0 if not lines[-1].startswith(("E ENVERR", "E LIMIT")) else 1


if __name__ == "__main__":
    sys.exit(main())
