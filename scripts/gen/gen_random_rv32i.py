#!/usr/bin/env python3
"""Constrained-random RV32I program generator (assembly source).

Programs only use RV32I instructions this core implements and are built by
scripts/sw/build_asm_tests.py like the directed tests. They are checked by
comparing the RTL run against the reference ISS (instruction trace + final
RAM); a normal program ends through TOHOST, a "trap" program ends with one
deliberately faulting instruction whose (cause, pc, tval) is also declared.

Constraints that keep programs well defined:
  - every register x1..x31 is initialised with a random value first
    (crt0 has already set them to 0); x0 is used as source and destination;
  - loads/stores: a fresh base register is loaded with an address chosen so
    that base + imm lands inside a private data area and is aligned for the
    access width; imm covers -2048..2047 including the extremes;
  - forward branches/jumps skip a short random block; backward branches only
    close counted loops whose counter register is not written in the body;
    backward JAL calls an out-of-line block that returns with JALR (offset
    0, +1 (bit 0 cleared) or negative), the link register is protected;
  - no CSR, no FENCE.I, no misaligned access in the body.

Reproducible: program rand_<seed>.S / rand_trap_<seed>.S depends only on the
seed. Regenerate one program with --seed S --count 1.

Usage:
  python scripts/gen/gen_random_rv32i.py [--seed 1] [--count 40] [--length 300] [--trap-count 20]
Output: build/gen/random/*.S (ignored by Git)
"""

from __future__ import annotations

import argparse
import random
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "build" / "gen" / "random"
DATA_BYTES = 1024
ALL_REGS = list(range(1, 32))
R_OPS = ["add", "sub", "sll", "slt", "sltu", "xor", "srl", "sra", "or", "and"]
I_OPS = ["addi", "slti", "sltiu", "xori", "ori", "andi"]
SH_OPS = ["slli", "srli", "srai"]
BRANCHES = ["beq", "bne", "blt", "bge", "bltu", "bgeu"]
LOADS = [("lb", 1), ("lbu", 1), ("lh", 2), ("lhu", 2), ("lw", 4)]
STORES = [("sb", 1), ("sh", 2), ("sw", 4)]
IMM_EDGES = [-2048, -2047, -1, 0, 1, 2, 2046, 2047]
WORD_EDGES = [0x00000000, 0x00000001, 0xFFFFFFFF, 0x7FFFFFFF, 0x80000000, 0x80000001,
              0x0000FFFF, 0xFFFF0000, 0x000000FF, 0xAAAAAAAA, 0x55555555]


class Gen:
    def __init__(self, seed: int, length: int):
        self.rng = random.Random(seed)
        self.length = length
        self.lines: list[str] = []
        self.label_n = 0
        self.protected: set[int] = set()      # registers that must not be written now
        self.subs: list[str] = []             # out-of-line subroutine bodies

    # ---------------- helpers ----------------
    def label(self, stem: str) -> str:
        self.label_n += 1
        return f".L{stem}{self.label_n}"

    def emit(self, s: str) -> None:
        self.lines.append("    " + s)

    def reg(self, allow_x0: bool = True) -> int:
        pool = ([0] if allow_x0 else []) + ALL_REGS
        return self.rng.choice(pool)

    def dst(self) -> int:
        """Destination register: any register (x0 allowed) that is not protected."""
        while True:
            r = self.reg(allow_x0=self.rng.random() < 0.08)
            if r not in self.protected:
                return r

    def imm12(self) -> int:
        return self.rng.choice(IMM_EDGES) if self.rng.random() < 0.4 else self.rng.randint(-2048, 2047)

    def word(self) -> int:
        return self.rng.choice(WORD_EDGES) if self.rng.random() < 0.4 else self.rng.getrandbits(32)

    # ---------------- instruction kinds (no control flow) ----------------
    def alu(self) -> None:
        r = self.rng.random()
        if r < 0.40:
            op = self.rng.choice(R_OPS)
            # sometimes the same register on both sides (x op x)
            a = self.reg(); b = a if self.rng.random() < 0.1 else self.reg()
            self.emit(f"{op:6s} x{self.dst()}, x{a}, x{b}")
        elif r < 0.75:
            op = self.rng.choice(I_OPS)
            self.emit(f"{op:6s} x{self.dst()}, x{self.reg()}, {self.imm12()}")
        elif r < 0.90:
            op = self.rng.choice(SH_OPS)
            sh = self.rng.choice([0, 1, 15, 16, 30, 31, self.rng.randint(0, 31)])
            self.emit(f"{op:6s} x{self.dst()}, x{self.reg()}, {sh}")
        else:
            op = self.rng.choice(["lui", "auipc"])
            imm = self.rng.choice([0, 1, 0x7FFFF, 0x80000, 0xFFFFF, self.rng.getrandbits(20)])
            self.emit(f"{op:6s} x{self.dst()}, {imm:#x}")

    def mem(self) -> None:
        store = self.rng.random() < 0.45
        op, size = self.rng.choice(STORES if store else LOADS)
        imm = self.imm12()
        imm -= imm % size if imm % size else 0                 # aligned offset
        off = self.rng.randrange(0, DATA_BYTES, size)            # final address offset
        base = self.dst()
        while base == 0:
            base = self.dst()
        self.emit(f"LA_ABS x{base}, rand_data + {off - imm}")
        if store:
            self.emit(f"{op:6s} x{self.reg()}, {imm}(x{base})")
        else:
            self.emit(f"{op:6s} x{self.dst()}, {imm}(x{base})")

    def straight(self) -> None:
        r = self.rng.random()
        if r < 0.07:                               # re-seed a register with a boundary value
            self.emit(f"li     x{self.dst()}, {self.rng.choice(WORD_EDGES):#x}")
        elif r < 0.62:
            self.alu()
        elif r < 0.97:
            self.mem()
        else:
            self.emit(self.rng.choice(["fence", "fence rw, rw", "fence iorw, iorw"]))

    def block(self, n: int) -> None:
        for _ in range(n):
            self.straight()

    # ---------------- control flow ----------------
    def fwd_branch(self) -> None:
        op = self.rng.choice(BRANCHES)
        a = self.reg(); b = a if self.rng.random() < 0.15 else self.reg()
        lab = self.label("f")
        self.emit(f"{op:6s} x{a}, x{b}, {lab}")
        self.block(self.rng.randint(1, 5))
        self.lines.append(f"{lab}:")

    def fwd_jal(self) -> None:
        lab = self.label("j")
        self.emit(f"jal    x{self.dst()}, {lab}")
        self.block(self.rng.randint(1, 4))
        self.lines.append(f"{lab}:")

    def loop(self) -> None:
        cnt = self.rng.choice([r for r in ALL_REGS if r not in self.protected])
        n = self.rng.randint(1, 4)
        top = self.label("l")
        self.emit(f"li     x{cnt}, {n}")
        self.lines.append(f"{top}:")
        self.protected.add(cnt)
        self.block(self.rng.randint(1, 6))
        if self.rng.random() < 0.3:
            self.fwd_branch()
        self.protected.discard(cnt)
        self.emit(f"addi   x{cnt}, x{cnt}, -1")
        close = self.rng.choice([f"bne    x{cnt}, x0, {top}", f"blt    x0, x{cnt}, {top}",
                                 f"bltu   x0, x{cnt}, {top}", f"bge    x{cnt}, x0, {top}"])
        if close.startswith("bge"):                  # bge cnt,0 also loops at 0: use cnt-1 form
            close = f"bne    x{cnt}, x0, {top}"
        self.emit(close)

    def call(self) -> None:
        """Backward JAL to an out-of-line block that returns through JALR."""
        link = self.rng.choice([r for r in ALL_REGS if r not in self.protected])
        sub = self.label("s")
        body: list[str] = [f"{sub}:"]
        saved, self.lines = self.lines, []
        self.protected.add(link)
        self.block(self.rng.randint(1, 4))
        style = self.rng.choice(["plain", "odd", "neg"])
        ret_rd = self.dst()
        if style == "plain":
            self.emit(f"jalr   x{ret_rd}, 0(x{link})")
        elif style == "odd":
            self.emit(f"jalr   x{ret_rd}, 1(x{link})")      # bit 0 of the target is cleared
        else:
            tmp = self.rng.choice([r for r in ALL_REGS if r not in self.protected])
            self.emit(f"addi   x{tmp}, x{link}, 12")
            self.emit(f"jalr   x{ret_rd}, -12(x{tmp})")
        self.protected.discard(link)
        body += self.lines
        self.lines = saved
        self.subs.append("\n".join(body))
        self.emit(f"jal    x{link}, {sub}")

    # ---------------- program ----------------
    def body(self) -> None:
        for r in ALL_REGS:
            self.emit(f"li     x{r}, {self.word():#x}")
        while len(self.lines) < self.length:
            r = self.rng.random()
            if r < 0.70:
                self.straight()
            elif r < 0.82:
                self.fwd_branch()
            elif r < 0.88:
                self.fwd_jal()
            elif r < 0.95:
                self.loop()
            else:
                self.call()

    def trap_tail(self) -> list[str]:
        """One faulting instruction with a known (cause, pc, tval)."""
        kind = self.rng.choice(["illegal", "ecall", "ebreak", "lmis", "smis", "lfault", "sfault",
                                "jmis", "bmis", "ifault"])
        t = ["    .globl tb_expect_pc"]
        exp: list[tuple[str, str]] = []
        if kind == "illegal":
            w = self.rng.choice([0x00000000, 0x0000100F, 0x30200073, 0x02B50533, 0x0000000B, 0x00004501])
            t += ["tb_expect_pc:", f"    .word {w:#010x}"]
            exp = [("cause", "2"), ("tval", f"{w:#x}")]
        elif kind in ("ecall", "ebreak"):
            t += ["tb_expect_pc:", f"    {kind}"]
            exp = [("cause", "11" if kind == "ecall" else "3"), ("tval", "0" if kind == "ecall" else "tb_expect_pc")]
        elif kind in ("lmis", "smis"):
            op, size = self.rng.choice(LOADS[2:] if kind == "lmis" else STORES[1:])
            off = self.rng.choice([o for o in range(1, 4) if o % size])
            t = [f"    LA_ABS x9, rand_data + {off}", "    .globl tb_expect_pc", "tb_expect_pc:",
                 f"    {op} x{'10' if kind == 'lmis' else '11'}, 0(x9)"]
            exp = [("cause", "4" if kind == "lmis" else "6"), ("tval", f"rand_data + {off}")]
        elif kind in ("lfault", "sfault"):
            a = self.rng.choice([0x00010000, 0x10000000, 0x20000000, 0xFFFFFFF0])
            op = self.rng.choice(["lw", "lbu", "lh"] if kind == "lfault" else ["sw", "sb", "sh"])
            t = [f"    li x9, {a:#x}", "    .globl tb_expect_pc", "tb_expect_pc:", f"    {op} x10, 0(x9)"]
            exp = [("cause", "5" if kind == "lfault" else "7"), ("tval", f"{a:#x}")]
        elif kind == "jmis":
            t += ["tb_expect_pc:", "    jal x1, trap_target + 2"]
            exp = [("cause", "0"), ("tval", "trap_target + 2")]
        elif kind == "bmis":
            t += ["tb_expect_pc:", "    beq x0, x0, trap_target + 2"]
            exp = [("cause", "0"), ("tval", "trap_target + 2")]
        else:
            t = ["    li x9, 0x20000000", "    jalr x1, 0(x9)"]
            exp = [("cause", "1"), ("pc", "0x20000000"), ("tval", "0x20000000")]
        t += ["    jal x0, tb_fail", "trap_target:", "    nop", "    nop", "    jal x0, tb_fail"]
        t += [f"    TB_EXPECT tb_expect_{k}, {v}" for k, v in exp]
        return t


def program(seed: int, length: int, trap: bool) -> str:
    g = Gen(seed, length)
    g.body()
    out = ["/* Generated by scripts/gen/gen_random_rv32i.py "
           f"(seed {seed}, length {length}, {'trap' if trap else 'pass'}). Do not edit. */",
           '#include "test_macros.h"', "",
           "    .data", "    .align 4", "rand_data:"]
    drng = random.Random(seed ^ 0xDA7A)
    out += [f"    .word {drng.getrandbits(32):#010x}" for _ in range(DATA_BYTES // 4)]
    out += ["", "    .text", "    .globl test_main", "test_main:"]
    out += g.lines
    if trap:
        out += g.trap_tail()
    else:
        out.append("    PASS")
    out += ["", "    /* out-of-line blocks reached by backward JAL */"] + g.subs
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=1, help="first seed")
    ap.add_argument("--count", type=int, default=40, help="number of normal programs")
    ap.add_argument("--trap-count", type=int, default=20, help="number of trap-ending programs")
    ap.add_argument("--length", type=int, default=300, help="approximate body length (lines)")
    ap.add_argument("--keep", action="store_true", help="do not delete existing programs")
    ns = ap.parse_args()
    if not ns.keep:
        shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)
    for s in range(ns.seed, ns.seed + ns.count):
        (OUT / f"rand_{s}.S").write_text(program(s, ns.length, False), encoding="ascii", newline="\n")
    for s in range(ns.seed, ns.seed + ns.trap_count):
        (OUT / f"rand_trap_{s}.S").write_text(program(s + 100000, ns.length // 2, True),
                                              encoding="ascii", newline="\n")
    print(f"gen_random_rv32i: {ns.count} normal + {ns.trap_count} trap programs "
          f"(seeds {ns.seed}..{ns.seed + max(ns.count, ns.trap_count) - 1}) in {OUT.relative_to(REPO).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
