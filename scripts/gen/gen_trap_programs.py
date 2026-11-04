#!/usr/bin/env python3
"""Generate one-exception test programs for the RV32I core (simulation only).

Each generated program sets sentinels, executes exactly one faulting
instruction and declares the expected (cause, pc, tval) with TB_EXPECT, so
the core testbench checks the trap and run_sim.py also compares the whole run
with the reference ISS. Categories:

  ill_*   encodings the core must reject as illegal (cause 2, tval = word):
          unimplemented extensions (Zicsr, Zifencei, M, A, F/D, C, V-style
          major opcodes, custom-0..3, RV64-only opcodes), privileged
          instructions (MRET/SRET/WFI/...), reserved funct3/funct7/shamt
          encodings and SYSTEM variants of ECALL/EBREAK with non-zero fields.
          The destination register named by the word keeps its sentinel.
  mis_*   misaligned loads/stores of every width and offset (cause 4/6)
  jmis_*  taken JAL/JALR/branch to a target with bit 1 set (cause 0)
  ld_/st_/if_unmapped_*   accesses outside RAM/TOHOST (cause 5/7/1)
  st_alias_tohost_*       SW of 1 (= PASS) to an unmapped address whose page offset is
                          TOHOST's (0x008): must fault, and must not end the test as PASS

Output: build/gen/trap/<name>.S (ignored by Git). Build with
  python scripts/sw/build_asm_tests.py --src build/gen/trap --out build/sw/gen_trap

Usage:  python scripts/gen/gen_trap_programs.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "build" / "gen" / "trap"
SENT = 0x5EB71E11


def r_type(f7, rs2, rs1, f3, rd, opc):
    return (f7 << 25) | (rs2 << 20) | (rs1 << 15) | (f3 << 12) | (rd << 7) | opc


def illegal_words() -> list[tuple[str, int]]:
    w: list[tuple[str, int]] = [
        ("zero", 0x00000000), ("ones", 0xFFFFFFFF),
        ("c_nop", 0x00000001), ("c_li", 0x00004501), ("c_ret", 0x00008082), ("c_lw", 0x0000411C),
        ("fence_i", 0x0000100F),
        ("mret", 0x30200073), ("sret", 0x10200073), ("uret", 0x00200073), ("wfi", 0x10500073),
        ("dret", 0x7B200073), ("sfence_vma", 0x12000073),
        ("ecall_rd1", 0x000000F3), ("ecall_rs1", 0x00008073), ("ebreak_rd1", 0x001000F3),
        ("system_f3_4", 0x00004073),
        ("flw", 0x0000A007), ("fsw", 0x0020A027), ("fld", 0x0000B007), ("fadd_s", 0x00000053),
        ("fmadd_s", 0x00000043), ("fmsub_s", 0x00000047), ("fnmsub_s", 0x0000004B), ("fnmadd_s", 0x0000004F),
        ("custom0", 0x0000000B), ("custom1", 0x0000002B), ("custom2", 0x0000005B), ("custom3", 0x0000007B),
        ("op_imm_32", 0x0000009B), ("op_32", 0x000000BB), ("ld", 0x0000B183), ("lwu", 0x0000E183),
        ("sd", 0x0030B023), ("opc_48bit", 0x0000001F), ("opc_64bit", 0x0000003F), ("opc_7f", 0x0000007F),
        ("amo_lr_w", 0x100120AF), ("amo_sc_w", 0x1820A1AF), ("amoswap_w", 0x0820A1AF), ("amoadd_w", 0x0020A1AF),
    ]
    for f3, nm in [(1, "csrrw"), (2, "csrrs"), (3, "csrrc"), (5, "csrrwi"), (6, "csrrsi"), (7, "csrrci")]:
        w.append((nm, (0x300 << 20) | (11 << 15) | (f3 << 12) | (10 << 7) | 0x73))
    for f3, nm in enumerate(["mul", "mulh", "mulhsu", "mulhu", "div", "divu", "rem", "remu"]):
        w.append((nm, r_type(1, 12, 11, f3, 10, 0x33)))
    for f3 in (3, 6, 7):
        w.append((f"load_f3_{f3}", (f3 << 12) | (11 << 15) | (10 << 7) | 0x03))
    for f3 in (3, 4, 5, 6, 7):
        w.append((f"store_f3_{f3}", (12 << 20) | (11 << 15) | (f3 << 12) | 0x23))
    for f3 in (2, 3):
        w.append((f"branch_f3_{f3}", (12 << 20) | (11 << 15) | (f3 << 12) | (4 << 8) | 0x63))
    for f3 in range(1, 8):
        w.append((f"jalr_f3_{f3}", (11 << 15) | (f3 << 12) | (10 << 7) | 0x67))
    for f3 in range(1, 8):
        w.append((f"miscmem_f3_{f3}", (f3 << 12) | 0x0F))
    w += [("slli_shamt5", r_type(0x01, 0, 11, 1, 10, 0x13)), ("srli_shamt5", r_type(0x01, 0, 11, 5, 10, 0x13)),
          ("srai_shamt5", r_type(0x21, 0, 11, 5, 10, 0x13)), ("slli_f7_20", r_type(0x20, 0, 11, 1, 10, 0x13)),
          ("srli_f7_40", r_type(0x40, 0, 11, 5, 10, 0x13))]
    for f3 in (1, 2, 3, 4, 6, 7):
        w.append((f"op_f7_20_f3_{f3}", r_type(0x20, 12, 11, f3, 10, 0x33)))
    for f7 in (0x02, 0x10, 0x40, 0x7F):
        w.append((f"op_f7_{f7:02x}", r_type(f7, 12, 11, 0, 10, 0x33)))
    return w


HEADER = '#include "test_macros.h"\n\n'


def prog_illegal(name: str, word: int) -> str:
    rd = (word >> 7) & 0x1F
    sentinel = f"    li    x{rd}, {SENT:#x}\n" if rd not in (0, 3, 31) else ""
    check = (f"    TB_EXPECT tb_check_reg0, {rd}\n    TB_EXPECT tb_check_val0, {SENT:#x}\n"
             if sentinel else "")
    return (f"/* {name}: {word:08x} must be an illegal instruction (cause 2). Generated. */\n{HEADER}"
            f"    .text\n    .globl test_main\ntest_main:\n{sentinel}"
            f"    .globl tb_expect_pc\ntb_expect_pc:\n    .word {word:#010x}\n    jal   x0, tb_fail\n\n"
            f"    TB_EXPECT tb_expect_cause, 2\n    TB_EXPECT tb_expect_tval, {word:#x}\n{check}")


DATA = "    .data\n    .align 4\nbuf:\n    .word 0x11223344, 0x55667788, 0x99AABBCC, 0xDDEEFF00\n\n"


def prog_misaligned(op: str, size: int, off: int) -> str:
    is_store = op.startswith("s")
    cause = 6 if is_store else 4
    if is_store:
        body = f"    li    a0, 0x5A5A5A5A\n    .globl tb_expect_pc\ntb_expect_pc:\n    {op}    a0, {off}(s1)\n"
        check = "    TB_EXPECT tb_check_mem_addr, buf\n    TB_EXPECT tb_check_mem_val, 0x11223344\n"
    else:
        body = f"    li    a0, {SENT:#x}\n    .globl tb_expect_pc\ntb_expect_pc:\n    {op}   a0, {off}(s1)\n"
        check = f"    TB_EXPECT tb_check_reg0, 10\n    TB_EXPECT tb_check_val0, {SENT:#x}\n"
    return (f"/* {op} at offset {off}: misaligned {'store' if is_store else 'load'} (cause {cause}). Generated. */\n"
            f"{HEADER}{DATA}    .text\n    .globl test_main\ntest_main:\n    LA_ABS s1, buf\n{body}"
            f"    jal   x0, tb_fail\n\n    TB_EXPECT tb_expect_cause, {cause}\n"
            f"    TB_EXPECT tb_expect_tval, buf + {off}\n{check}")


def prog_jump_misaligned(kind: str) -> str:
    setup, insn = "", ""
    if kind == "jal":
        insn = "jal   ra, target + 2"
    elif kind == "jalr":
        setup = "    LA_ABS a3, target\n"
        insn = "jalr  ra, 2(a3)"
    elif kind == "jalr_neg":
        setup = "    LA_ABS a3, target + 8\n"
        insn = "jalr  ra, -6(a3)"
    else:                                   # taken branch of each kind
        setup = "    li    a0, -1\n    li    a1, 1\n"
        operands = {"beq": "a0, a0", "bne": "a0, a1", "blt": "a0, a1", "bge": "a1, a0",
                    "bltu": "a1, a0", "bgeu": "a0, a1"}[kind]
        insn = f"{kind}  {operands}, target + 2"
    return (f"/* taken {kind} to a target with bit 1 set: cause 0, link not written. Generated. */\n"
            f"{HEADER}    .text\n    .globl test_main\ntest_main:\n{setup}    li    ra, {SENT:#x}\n"
            f"    .globl tb_expect_pc\ntb_expect_pc:\n    {insn}\n    jal   x0, tb_fail\n"
            f"target:\n    nop\n    nop\n    jal   x0, tb_fail\n\n"
            f"    TB_EXPECT tb_expect_cause, 0\n    TB_EXPECT tb_expect_tval, target + 2\n"
            f"    TB_EXPECT tb_check_reg0, 1\n    TB_EXPECT tb_check_val0, {SENT:#x}\n")


def prog_unmapped_data(op: str, addr: int) -> str:
    is_store = op.startswith("s")
    cause = 7 if is_store else 5
    rd = "x0" if op.endswith("_x0") else "a0"
    mnem = op.replace("_x0", "")
    line = f"{mnem}    a0, 0(s2)" if is_store else f"{mnem}   {rd}, 0(s2)"
    check = ("" if is_store or rd == "x0" else
             f"    TB_EXPECT tb_check_reg0, 10\n    TB_EXPECT tb_check_val0, {SENT:#x}\n")
    return (f"/* {op} at unmapped address {addr:#010x}: cause {cause}. Generated. */\n{HEADER}"
            f"    .text\n    .globl test_main\ntest_main:\n    li    a0, {SENT:#x}\n    li    s2, {addr:#x}\n"
            f"    .globl tb_expect_pc\ntb_expect_pc:\n    {line}\n    jal   x0, tb_fail\n\n"
            f"    TB_EXPECT tb_expect_cause, {cause}\n    TB_EXPECT tb_expect_tval, {addr:#x}\n{check}")


def prog_alias_tohost(addr: int) -> str:
    return (f"/* sw of 1 to unmapped {addr:#010x} (page offset of TOHOST): cause 7, no TOHOST "
            f"write. Generated. */\n{HEADER}"
            f"    .text\n    .globl test_main\ntest_main:\n    li    a0, 1\n    li    s2, {addr:#x}\n"
            f"    .globl tb_expect_pc\ntb_expect_pc:\n    sw    a0, 0(s2)\n    jal   x0, tb_fail\n\n"
            f"    TB_EXPECT tb_expect_cause, 7\n    TB_EXPECT tb_expect_tval, {addr:#x}\n")


def prog_unmapped_fetch(addr: int) -> str:
    return (f"/* jump to unmapped/non-executable {addr:#010x}: fetch fault, cause 1. Generated. */\n{HEADER}"
            f"    .text\n    .globl test_main\ntest_main:\n    li    a3, {addr:#x}\njump_site:\n"
            f"    jalr  ra, 0(a3)\n    jal   x0, tb_fail\n\n"
            f"    TB_EXPECT tb_expect_cause, 1\n    TB_EXPECT tb_expect_pc, {addr:#x}\n"
            f"    TB_EXPECT tb_expect_tval, {addr:#x}\n"
            f"    TB_EXPECT tb_check_reg0, 1\n    TB_EXPECT tb_check_val0, jump_site + 4\n")


def main() -> int:
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    progs: dict[str, str] = {}
    for nm, word in illegal_words():
        progs[f"ill_{nm}"] = prog_illegal(nm, word)
    for op, size in [("lh", 2), ("lhu", 2), ("lw", 4), ("sh", 2), ("sw", 4)]:
        for off in range(1, 4):
            if off % size:
                progs[f"mis_{op}_{off}"] = prog_misaligned(op, size, off)
    for kind in ("jal", "jalr", "jalr_neg", "beq", "bne", "blt", "bge", "bltu", "bgeu"):
        progs[f"jmis_{kind}"] = prog_jump_misaligned(kind)
    unmapped = [0x00010000, 0x0FFFFFFC, 0x10000000, 0x10000004, 0x1000000C, 0x10001000,
                0x20000000, 0x30000000, 0xFFFFFFFC,
                # phase 5: pages that are not implemented in the SoC (SPI, DSP, IRQ, reserved,
                # shared buffers) and an address with the MSB set
                0x10002000, 0x10003000, 0x1000F000, 0x20010000, 0x80000000]
    for a in unmapped:
        for op in ("lw", "lhu", "lb", "lw_x0", "sw", "sh", "sb"):
            progs[f"{'st' if op[0] == 's' else 'ld'}_unmapped_{op}_{a:08x}"] = prog_unmapped_data(op, a)
    for a in (0x10001008, 0x1000F008, 0x20000008):
        progs[f"st_alias_tohost_{a:08x}"] = prog_alias_tohost(a)
    for a in (0x00010000, 0x10000000, 0x10000008, 0x20000000, 0xFFFFFFFC):
        progs[f"if_unmapped_{a:08x}"] = prog_unmapped_fetch(a)
    for name, text in progs.items():
        (OUT / f"{name}.S").write_text(text, encoding="ascii", newline="\n")
    print(f"gen_trap_programs: wrote {len(progs)} programs to {OUT.relative_to(REPO).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
