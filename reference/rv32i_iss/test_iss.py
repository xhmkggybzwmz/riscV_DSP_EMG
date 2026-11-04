#!/usr/bin/env python3
"""Self-test of the RV32I reference model (rv32i_iss.py), independent of the RTL.

1. Decode against the GNU assembler: every vector in
   dv/cpu/unit/decoder_golden_vectors.vh was assembled by riscv64-unknown-elf-gcc
   (scripts/gen/gen_decoder_vectors.py). The ISS must identify the same
   mnemonic as the assembly source, the same register fields and immediate,
   and must reject every encoding the assembler produced for an extension
   this core does not implement.
2. Semantics: known-answer vectors computed by hand from the RV32I
   specification text (one instruction executed on a prepared machine state).
3. Exceptions: cause/pc/tval and "no architectural change" for each class.

Usage:  python reference/rv32i_iss/test_iss.py      (exit 0 = all pass)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
import rv32i_iss as m  # noqa: E402

FAILS: list[str] = []
CHECKS = 0


def check(what: str, got, exp) -> None:
    global CHECKS
    CHECKS += 1
    if got != exp:
        FAILS.append(f"{what}: got {got!r} expected {exp!r}")


# ---------------------------------------------------------------------------
# encoder (spec format figures) - itself verified against GCC below
# ---------------------------------------------------------------------------
def enc_r(f7, rs2, rs1, f3, rd, opc):
    return (f7 << 25) | (rs2 << 20) | (rs1 << 15) | (f3 << 12) | (rd << 7) | opc


def enc_i(imm, rs1, f3, rd, opc):
    return ((imm & 0xFFF) << 20) | (rs1 << 15) | (f3 << 12) | (rd << 7) | opc


def enc_s(imm, rs2, rs1, f3, opc=0x23):
    imm &= 0xFFF
    return ((imm >> 5) << 25) | (rs2 << 20) | (rs1 << 15) | (f3 << 12) | ((imm & 0x1F) << 7) | opc


def enc_b(off, rs2, rs1, f3):
    o = off & 0x1FFF
    return (((o >> 12) & 1) << 31) | (((o >> 5) & 0x3F) << 25) | (rs2 << 20) | (rs1 << 15) | \
        (f3 << 12) | (((o >> 1) & 0xF) << 8) | (((o >> 11) & 1) << 7) | 0x63


def enc_u(imm20, rd, opc):
    return ((imm20 & 0xFFFFF) << 12) | (rd << 7) | opc


def enc_j(off, rd):
    o = off & 0x1FFFFF
    return (((o >> 20) & 1) << 31) | (((o >> 1) & 0x3FF) << 21) | (((o >> 11) & 1) << 20) | \
        (((o >> 12) & 0xFF) << 12) | (rd << 7) | 0x6F


# ---------------------------------------------------------------------------
# 1. decode vs GCC golden vectors
# ---------------------------------------------------------------------------
CALL_RE = re.compile(r"^(chk_\w+)\(32'h([0-9A-Fa-f]{8})(.*?)\);\s*//\s*(.*?)\s*\|")


def parse_num(tok: str) -> int:
    tok = tok.strip()
    if tok.startswith("32'h"):
        return int(tok[4:], 16)
    if tok.startswith("3'b"):
        return int(tok[3:], 2)
    return int(tok, 0) if tok.lstrip("-").isdigit() else tok


def test_decode_vs_gcc() -> None:
    vec = REPO / "dv" / "cpu" / "unit" / "decoder_golden_vectors.vh"
    n = 0
    for line in vec.read_text(encoding="ascii").splitlines():
        mt = CALL_RE.match(line)
        if not mt:
            continue
        n += 1
        task, word, rest, asm = mt.group(1), int(mt.group(2), 16), mt.group(3), mt.group(4)
        args = [parse_num(t) for t in rest.split(",")[1:]]
        mnem = asm.split()[0]
        hit = m.identify(word)
        if task == "chk_illegal":
            check(f"GCC {asm!r} must be illegal", hit, None)
            continue
        expected_name = {"fence.tso": "fence", "pause": "fence"}.get(mnem, mnem)
        check(f"GCC {asm!r} mnemonic", hit[0] if hit else None, expected_name)
        if hit is None:
            continue
        imm = m.immediate(word, hit[1]) & 0xFFFFFFFF
        rd, rs1, rs2 = m.bits(word, 11, 7), m.bits(word, 19, 15), m.bits(word, 24, 20)
        if task in ("chk_lui", "chk_auipc", "chk_jal"):
            check(f"GCC {asm!r} rd", rd, args[0]); check(f"GCC {asm!r} imm", imm, args[1] & 0xFFFFFFFF)
        elif task == "chk_jalr":
            check(f"GCC {asm!r} rd/rs1", (rd, rs1), (args[0], args[1]))
            check(f"GCC {asm!r} imm", imm, args[2] & 0xFFFFFFFF)
        elif task == "chk_branch":
            check(f"GCC {asm!r} rs1/rs2", (rs1, rs2), (args[1], args[2]))
            check(f"GCC {asm!r} imm", imm, args[3] & 0xFFFFFFFF)
        elif task == "chk_load":
            check(f"GCC {asm!r} rd/rs1", (rd, rs1), (args[1], args[2]))
            check(f"GCC {asm!r} imm", imm, args[3] & 0xFFFFFFFF)
        elif task == "chk_store":
            check(f"GCC {asm!r} rs1/rs2", (rs1, rs2), (args[1], args[2]))
            check(f"GCC {asm!r} imm", imm, args[3] & 0xFFFFFFFF)
        elif task == "chk_opimm":
            check(f"GCC {asm!r} rd/rs1", (rd, rs1), (args[1], args[2]))
            check(f"GCC {asm!r} imm", imm, args[3] & 0xFFFFFFFF)
        elif task == "chk_op":
            check(f"GCC {asm!r} rd/rs1/rs2", (rd, rs1, rs2), (args[1], args[2], args[3]))
    check("number of GCC vectors parsed", n, 84)

    # the encoder used by the semantic tests reproduces GCC encodings
    check("enc addi x1,x2,-2048", enc_i(-2048, 2, 0, 1, 0x13), 0x80010093)
    check("enc sw x31,-1(x30)", enc_s(-1, 31, 30, 2), 0xFFFF2FA3)
    check("enc beq x1,x2,+4094", enc_b(4094, 2, 1, 0), 0x7E208FE3)
    check("enc bne x3,x4,-4096", enc_b(-4096, 4, 3, 1), 0x80419063)
    check("enc jal x5,-1048576", enc_j(-1048576, 5), 0x800002EF)
    check("enc jal x1,+1048574", enc_j(1048574, 1), 0x7FFFF0EF)
    check("enc lui x31,0xfffff", enc_u(0xFFFFF, 31, 0x37), 0xFFFFFFB7)
    check("enc sra x22,x23,x24", enc_r(0x20, 24, 23, 5, 22, 0x33), 0x418BDB33)


# ---------------------------------------------------------------------------
# 2./3. execute single instructions on a prepared state
# ---------------------------------------------------------------------------
def machine(words, regs=None, pc=0x100, data=None, d_err=None, i_err=None):
    img = bytearray(m.MEM_BYTES)
    for i, w in enumerate(words):
        img[pc + 4 * i: pc + 4 * i + 4] = w.to_bytes(4, "little")
    for addr, b in (data or {}).items():
        img[addr] = b
    iss = m.Iss(bytes(img), d_err=d_err, i_err=i_err)
    iss.pc = pc
    iss.x = [0] + [0x0BAD0000 + r for r in range(1, 32)]      # defined, recognisable
    for r, v in (regs or {}).items():
        iss.x[r] = v & 0xFFFFFFFF
    return iss


def run1(words, regs=None, **kw):
    iss = machine(words, regs, **kw)
    ev = iss.step()
    return iss, ev


def alu_kat(name, word, a, b, expected):
    iss, ev = run1([word], {1: a, 2: b})
    check(f"{name} a={a:#x} b={b:#x}", iss.x[3], expected & 0xFFFFFFFF)


def test_semantics() -> None:
    R = lambda f7, f3: enc_r(f7, 2, 1, f3, 3, 0x33)            # op x3, x1, x2
    I = lambda imm, f3: enc_i(imm, 1, f3, 3, 0x13)              # opi x3, x1, imm
    # --- register-register (values from the spec definitions) ---
    alu_kat("add wraps", R(0, 0), 0x7FFFFFFF, 1, 0x80000000)
    alu_kat("add -1+-1", R(0, 0), 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFE)
    alu_kat("sub 0-1", R(0x20, 0), 0, 1, 0xFFFFFFFF)
    alu_kat("sub min-1", R(0x20, 0), 0x80000000, 1, 0x7FFFFFFF)
    alu_kat("sll by 31", R(0, 1), 1, 31, 0x80000000)
    alu_kat("sll uses rs2[4:0] (33->1)", R(0, 1), 1, 33, 2)
    alu_kat("slt -1<1", R(0, 2), 0xFFFFFFFF, 1, 1)
    alu_kat("slt 1<-1", R(0, 2), 1, 0xFFFFFFFF, 0)
    alu_kat("slt min<max", R(0, 2), 0x80000000, 0x7FFFFFFF, 1)
    alu_kat("sltu 0xffffffff<1", R(0, 3), 0xFFFFFFFF, 1, 0)
    alu_kat("sltu 1<0xffffffff", R(0, 3), 1, 0xFFFFFFFF, 1)
    alu_kat("sltu equal", R(0, 3), 7, 7, 0)
    alu_kat("xor", R(0, 4), 0xFF00FF00, 0x0F0F0F0F, 0xF00FF00F)
    alu_kat("srl by 31", R(0, 5), 0x80000000, 31, 1)
    alu_kat("srl by 4", R(0, 5), 0x80000000, 4, 0x08000000)
    alu_kat("sra by 31", R(0x20, 5), 0x80000000, 31, 0xFFFFFFFF)
    alu_kat("sra by 4", R(0x20, 5), 0x80000000, 4, 0xF8000000)
    alu_kat("sra positive", R(0x20, 5), 0x7FFFFFFF, 4, 0x07FFFFFF)
    alu_kat("sra uses rs2[4:0] (36->4)", R(0x20, 5), 0x80000000, 36, 0xF8000000)
    alu_kat("or", R(0, 6), 0xFF00FF00, 0x0F0F0F0F, 0xFF0FFF0F)
    alu_kat("and", R(0, 7), 0xFF00FF00, 0x0F0F0F0F, 0x0F000F00)
    # --- register-immediate ---
    alu_kat("addi -2048", I(-2048, 0), 0, 0, 0xFFFFF800)
    alu_kat("addi wraps", I(1, 0), 0x7FFFFFFF, 0, 0x80000000)
    alu_kat("slti -1<0", I(0, 2), 0xFFFFFFFF, 0, 1)
    alu_kat("slti 0<-1", I(-1, 2), 0, 0, 0)
    alu_kat("sltiu 5<-1 (0xffffffff)", I(-1, 3), 5, 0, 1)
    alu_kat("sltiu 0<1 (seqz)", I(1, 3), 0, 0, 1)
    alu_kat("xori -1 (not)", I(-1, 4), 0x0F0F0F0F, 0, 0xF0F0F0F0)
    alu_kat("ori", I(0x0F0, 6), 0x0F0F0F0F, 0, 0x0F0F0FFF)
    alu_kat("andi -16", I(-16, 7), 0x0F0F0F0F, 0, 0x0F0F0F00)
    alu_kat("slli 31", enc_i(31, 1, 1, 3, 0x13), 1, 0, 0x80000000)
    alu_kat("srli 31", enc_i(31, 1, 5, 3, 0x13), 0x80000000, 0, 1)
    alu_kat("srai 31", enc_i(0x400 | 31, 1, 5, 3, 0x13), 0x80000000, 0, 0xFFFFFFFF)
    alu_kat("srai 0", enc_i(0x400, 1, 5, 3, 0x13), 0x80000000, 0, 0x80000000)
    # --- LUI / AUIPC ---
    iss, _ = run1([enc_u(0xFFFFF, 3, 0x37)]); check("lui 0xfffff", iss.x[3], 0xFFFFF000)
    iss, _ = run1([enc_u(0x80000, 3, 0x37)]); check("lui 0x80000", iss.x[3], 0x80000000)
    iss, _ = run1([enc_u(0xFFFFF, 3, 0x17)]); check("auipc -0x1000 at 0x100", iss.x[3], 0xFFFFF100)
    iss, _ = run1([enc_u(1, 3, 0x17)]); check("auipc +0x1000 at 0x100", iss.x[3], 0x1100)
    # --- x0 ---
    iss, ev = run1([enc_i(5, 0, 0, 0, 0x13)]); check("addi x0 keeps 0", iss.x[0], 0)
    check("addi x0 reports no write", ev[-1][3], 0)
    # --- JAL / JALR ---
    iss, ev = run1([enc_j(8, 1)]); check("jal link", iss.x[1], 0x104); check("jal target", iss.pc, 0x108)
    iss, _ = run1([enc_j(-0x100, 1)]); check("jal backward target", iss.pc, 0x000)
    iss, _ = run1([enc_i(0, 2, 0, 1, 0x67)], {2: 0x201}); check("jalr clears bit 0", iss.pc, 0x200)
    iss, _ = run1([enc_i(-8, 2, 0, 1, 0x67)], {2: 0x208}); check("jalr negative offset", iss.pc, 0x200)
    iss, _ = run1([enc_i(0, 1, 0, 1, 0x67)], {1: 0x300})
    check("jalr rd==rs1 uses old rs1", (iss.pc, iss.x[1]), (0x300, 0x104))
    iss, ev = run1([enc_j(6, 1)], {1: 0x55})
    check("jal misaligned target traps", iss.trap, (0, 0x100, 0x106)); check("jal misaligned: rd kept", iss.x[1], 0x55)
    iss, _ = run1([enc_i(2, 2, 0, 1, 0x67)], {2: 0x200, 1: 0x66})
    check("jalr target bit1 traps", iss.trap, (0, 0x100, 0x202)); check("jalr misaligned: rd kept", iss.x[1], 0x66)
    # --- branches ---
    B = lambda f3, a, b, off=16: run1([enc_b(off, 2, 1, f3)], {1: a, 2: b})[0].pc
    for f3, a, b, taken, nm in [(0, 5, 5, 1, "beq"), (0, 5, 6, 0, "beq"), (1, 5, 6, 1, "bne"),
                                (1, 5, 5, 0, "bne"), (4, 0xFFFFFFFF, 1, 1, "blt"), (4, 1, 0xFFFFFFFF, 0, "blt"),
                                (5, 1, 0xFFFFFFFF, 1, "bge"), (5, 3, 3, 1, "bge"), (5, 0xFFFFFFFF, 1, 0, "bge"),
                                (6, 1, 0xFFFFFFFF, 1, "bltu"), (6, 0xFFFFFFFF, 1, 0, "bltu"),
                                (7, 0xFFFFFFFF, 1, 1, "bgeu"), (7, 1, 1, 1, "bgeu"), (7, 1, 2, 0, "bgeu")]:
        check(f"{nm} {a:#x},{b:#x}", B(f3, a, b), 0x110 if taken else 0x104)
    iss, _ = run1([enc_b(6, 2, 1, 0)], {1: 1, 2: 2})
    check("not-taken branch to misaligned target: no trap", (iss.trap, iss.pc), (None, 0x104))
    iss, _ = run1([enc_b(6, 2, 1, 0)], {1: 2, 2: 2})
    check("taken branch to misaligned target traps", iss.trap, (0, 0x100, 0x106))
    # --- loads / stores ---
    data = {0x400: 0x01, 0x401: 0x7F, 0x402: 0xFF, 0x403: 0x80}
    L = lambda f3, off: run1([enc_i(off, 1, f3, 3, 0x03)], {1: 0x400}, data=data)[0].x[3]
    check("lb +0", L(0, 0), 0x00000001); check("lb +2", L(0, 2), 0xFFFFFFFF)
    check("lb +3", L(0, 3), 0xFFFFFF80); check("lbu +3", L(4, 3), 0x00000080)
    check("lh +0", L(1, 0), 0x00007F01); check("lh +2", L(1, 2), 0xFFFF80FF)
    check("lhu +2", L(5, 2), 0x000080FF); check("lw", L(2, 0), 0x80FF7F01)
    iss, ev = run1([enc_i(0, 1, 2, 0, 0x03)], {1: 0x400}, data=data)
    check("lw x0: access performed", [e[:3] for e in ev if e[0] == "A"], [("A", 0x400, 0)])
    check("lw x0: no write", (iss.x[0], ev[-1][3]), (0, 0))
    S = lambda f3, off, v: bytes(run1([enc_s(off, 2, 1, f3)], {1: 0x400, 2: v}, data=data)[0].mem[0x400:0x404])
    check("sb +1", S(0, 1, 0xFFFFFFAA), bytes([0x01, 0xAA, 0xFF, 0x80]))
    check("sh +2", S(1, 2, 0x7777CCDD), bytes([0x01, 0x7F, 0xDD, 0xCC]))
    check("sw", S(2, 0, 0x11223344), bytes([0x44, 0x33, 0x22, 0x11]))
    # --- misaligned / faults: no state change ---
    iss, ev = run1([enc_i(2, 1, 2, 3, 0x03)], {1: 0x400, 3: 0x77}, data=data)
    check("lw misaligned", iss.trap, (4, 0x100, 0x402)); check("lw misaligned: rd kept", iss.x[3], 0x77)
    check("lw misaligned: no access", [e for e in ev if e[0] == "A"], [])
    iss, ev = run1([enc_i(1, 1, 5, 3, 0x03)], {1: 0x400}, data=data)
    check("lhu misaligned", iss.trap, (4, 0x100, 0x401))
    iss, ev = run1([enc_s(3, 2, 1, 1)], {1: 0x400, 2: 0x5555}, data=data)
    check("sh misaligned", iss.trap, (6, 0x100, 0x403))
    check("sh misaligned: memory unchanged", bytes(iss.mem[0x400:0x404]), bytes([0x01, 0x7F, 0xFF, 0x80]))
    iss, ev = run1([enc_i(0, 1, 2, 3, 0x03)], {1: 0x30000000, 3: 0x77})
    check("lw unmapped", iss.trap, (5, 0x100, 0x30000000)); check("lw unmapped: rd kept", iss.x[3], 0x77)
    check("lw unmapped: access attempted", [e[:3] + (e[5],) for e in ev if e[0] == "A"], [("A", 0x30000000, 0, 1)])
    iss, ev = run1([enc_i(0, 1, 2, 0, 0x03)], {1: 0x500}, d_err=(0x500, 0x5FF))
    check("lw x0 in error window faults", iss.trap, (5, 0x100, 0x500))
    iss, ev = run1([enc_s(0, 2, 1, 2)], {1: 0x500, 2: 1}, d_err=(0x500, 0x5FF))
    check("sw in error window faults", iss.trap, (7, 0x100, 0x500))
    check("sw in error window: RAM unchanged (model)", bytes(iss.mem[0x500:0x504]), bytes(4))
    iss, ev = run1([enc_s(0, 2, 1, 2)], {1: 0x10000008, 2: 0x1})
    check("sw TOHOST", (iss.tohost, iss.trap), (1, None))
    # --- illegal / system ---
    for w, nm in [(0x00000000, "all zero"), (0xFFFFFFFF, "all one"), (0x0000100F, "fence.i"),
                  (0x30200073, "mret"), (0x10500073, "wfi"), (0x300110F3, "csrrw"),
                  (0x023100B3, "mul"), (0x0000000B, "custom-0"), (0x00004501, "compressed"),
                  (enc_r(1, 0, 1, 1, 1, 0x13), "slli shamt[5]"), (enc_r(0x21, 0, 1, 5, 1, 0x13), "srai shamt[5]"),
                  (enc_r(0x20, 2, 1, 1, 3, 0x33), "OP funct7=0100000 sll"), (0x000000F3, "ecall rd=1"),
                  (enc_i(0, 1, 3, 3, 0x03), "load funct3=011"), (enc_s(0, 2, 1, 3), "store funct3=011"),
                  (enc_b(8, 2, 1, 2), "branch funct3=010"), (enc_i(0, 1, 1, 1, 0x67), "jalr funct3=001")]:
        iss, ev = run1([w], {3: 0x99})
        check(f"illegal {nm}", iss.trap, (2, 0x100, w))
    iss, _ = run1([0x00000073]); check("ecall", iss.trap, (11, 0x100, 0))
    iss, _ = run1([0x00100073]); check("ebreak", iss.trap, (3, 0x100, 0x100))
    for w, nm in [(0x0FF0000F, "fence"), (0x8330000F, "fence.tso"), (0x0100000F, "pause"), (0x0FF5850F, "fence rd/rs1")]:
        iss, ev = run1([w], {10: 0x1234})
        check(f"{nm} is a no-op", (iss.trap, iss.pc, iss.x[10], ev[-1][3]), (None, 0x104, 0x1234, 0))
    iss, ev = run1([0], pc=0x100, i_err=(0x100, 0x1FF)); check("fetch in error window", iss.trap, (1, 0x100, 0x100))
    # --- undefined register read is an environment error ---
    iss = machine([enc_r(0, 2, 1, 0, 3, 0x33)])
    iss.x[1] = None
    try:
        iss.step()
        check("undefined register read detected", False, True)
    except m.EnvError:
        check("undefined register read detected", True, True)


def main() -> int:
    test_decode_vs_gcc()
    test_semantics()
    for f in FAILS:
        print("FAIL:", f)
    print(f"ISS_SELFTEST: {'PASS' if not FAILS else 'FAIL'} checks={CHECKS} errors={len(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
