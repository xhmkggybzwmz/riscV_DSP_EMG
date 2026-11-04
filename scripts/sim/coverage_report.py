#!/usr/bin/env python3
"""Functional coverage report for the RV32I core regression (Python, no tools).

Reads build/sim/summary.json written by scripts/sim/run_sim.py, re-executes
every program whose RTL run PASSED and was found identical to the reference ISS
(the same image the RTL ran), and bins what was executed. It also reads the
MEMSTAT lines of the RTL logs (bus wait/latency) and the names of the passing
reset tests.

Three levels are reported for each bin:
  executed  - the condition occurred in at least one program run
  checked   - ... in a run with an automatic PASS/FAIL decision (every program
              test: TB checks + TOHOST/expected trap)
  ISS-match - ... in a run whose complete trace and final RAM matched the ISS
For program runs "checked" and "ISS-match" coincide by construction (a run is
only counted if it passed); bins hit only by the unit tests (which use their
own reference models, not the ISS) are listed separately.

Usage:  python scripts/sim/coverage_report.py   -> build/sim/coverage.md, coverage.json
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SIM = REPO / "build" / "sim"
sys.path.insert(0, str(REPO / "reference" / "rv32i_iss"))
import rv32i_iss  # noqa: E402

MNEMONICS = [n for n, *_ in rv32i_iss.OPCODES]
R_OPS = ["add", "sub", "sll", "slt", "sltu", "xor", "srl", "sra", "or", "and"]
ALU_OPS = ["add", "sub", "sll", "slt", "sltu", "xor", "srl", "sra", "or", "and",
           "addi", "slti", "sltiu", "xori", "ori", "andi", "slli", "srli", "srai"]
BRANCH = ["beq", "bne", "blt", "bge", "bltu", "bgeu"]
LOADS = {"lb": 1, "lbu": 1, "lh": 2, "lhu": 2, "lw": 4}
STORES = {"sb": 1, "sh": 2, "sw": 4}
CAUSES = {0: "insn misaligned", 1: "insn access fault", 2: "illegal insn", 3: "breakpoint",
          4: "load misaligned", 5: "load access fault", 6: "store misaligned",
          7: "store access fault", 11: "ecall"}
VALUE_CLASSES = ["zero", "one", "minus1", "int_min", "int_max", "small+", "small-", "large+", "large-"]


def vclass(v: int) -> str:
    v &= 0xFFFFFFFF
    if v == 0: return "zero"
    if v == 1: return "one"
    if v == 0xFFFFFFFF: return "minus1"
    if v == 0x80000000: return "int_min"
    if v == 0x7FFFFFFF: return "int_max"
    s = v - (1 << 32) if v >> 31 else v
    if 0 < s < 4096: return "small+"
    if -4096 < s < 0: return "small-"
    return "large+" if s > 0 else "large-"


def main() -> int:
    summ = SIM / "summary.json"
    if not summ.is_file():
        print("coverage_report: run scripts/sim/run_sim.py first", file=sys.stderr)
        return 2
    results = json.loads(summ.read_text(encoding="utf-8"))
    progs = {}
    for r in results:
        if r.get("program") and r["status"] == "PASS" and r.get("compared"):
            progs.setdefault(r["program"], r["args_file"])

    insn = Counter()
    opnd = defaultdict(set)              # op -> {(which, class)}
    sign_split = Counter()               # signed/unsigned disagreement cases
    shifts = defaultdict(set)
    br = Counter()
    jumps = Counter()
    mem = Counter()
    imm_edges = Counter()
    regs = {"rd": set(), "rs1": set(), "rs2": set()}
    causes = Counter()
    for prog, args in sorted(progs.items()):
        cov: list[dict] = []
        rv32i_iss.run(Path(args), cov=cov)
        for c in cov:
            n = c.get("name")
            if "trap" in c:
                causes[c["trap"]] += 1
                if n in ("ecall", "ebreak"):          # executed; ends in a trap by definition
                    insn[n] += 1
            if not n:
                continue
            if "trap" in c:
                continue
            insn[n] += 1
            fmt = c.get("fmt")
            regs["rd"].add(c["rd"]) if fmt not in ("S", "B") and n not in ("fence",) else None
            if fmt in ("R", "I", "S", "B") and n not in ("fence", "ecall", "ebreak"):
                regs["rs1"].add(c["rs1"])
            if fmt in ("R", "S", "B"):
                regs["rs2"].add(c["rs2"])
            if n in ALU_OPS:
                opnd[n].add(("a", vclass(c["a"])))
                opnd[n].add(("b", vclass(c["b"])))
                if n in ("slt", "sltu", "slti", "sltiu") and (c["a"] ^ c["b"]) >> 31:
                    sign_split[n] += 1
                if n in ("sll", "srl", "sra", "slli", "srli", "srai"):
                    sh = c["b"] & 31
                    shifts[n].add("0" if sh == 0 else "31" if sh == 31 else "1-30")
                    if n in ("sll", "srl", "sra") and c["b"] >> 5:
                        shifts[n].add("rs2[31:5]!=0")
            if n in BRANCH:
                br[(n, "taken" if c["taken"] else "not")] += 1
                if c["taken"]:
                    br[("any", "taken back" if c["back"] else "taken fwd")] += 1
                if (c["a"] ^ c["b"]) >> 31 and n in ("blt", "bge", "bltu", "bgeu"):
                    sign_split[n] += 1
            if n in ("jal", "jalr"):
                jumps[(n, "back" if c.get("back") else "fwd")] += 1
                jumps[(n, "rd=x0" if c["rd"] == 0 else "rd!=x0")] += 1
            if n in LOADS or n in STORES:
                mem[(n, c["addr"] & 3)] += 1
                if n in LOADS and c["rd"] == 0:
                    mem[(n, "rd=x0")] += 1
                mem[(n, "imm<0" if c["imm"] < 0 else "imm>=0")] += 1
            if fmt in ("I", "S") and n not in ("slli", "srli", "srai", "fence"):
                if c["imm"] in (-2048, 2047, 0, -1):
                    imm_edges[(fmt, c["imm"])] += 1
            if fmt == "U":
                u = (c["imm"] >> 12) & 0xFFFFF
                if u in (0, 0xFFFFF, 0x80000):
                    imm_edges[("U", u)] += 1

    # bus timing from RTL logs, reset scenarios from test names
    lat = {"imem": Counter(), "dmem": Counter()}
    stalls = Counter()
    resets = Counter()
    for r in results:
        if r["status"] != "PASS" or not r.get("program"):
            continue
        log = SIM / r["name"].replace("/", "__").replace("@", "_s") / "sim.log"
        if log.is_file():
            mm = re.search(r"MEMSTAT: imem_stall=(\d+) dmem_stall=(\d+) imem_lat=([\d,]+) dmem_lat=([\d,]+)",
                           log.read_text(encoding="utf-8"))
            if mm:
                stalls["imem"] += int(mm.group(1)); stalls["dmem"] += int(mm.group(2))
                for port, grp in (("imem", 3), ("dmem", 4)):
                    for k, v in enumerate(mm.group(grp).split(","), start=1):
                        lat[port][k] += int(v)
        m2 = re.search(r"/reset_(\w+)@", r["name"])
        if m2:
            resets[m2.group(1)] += 1

    # ---------------- report ----------------
    out = ["# RV32I functional coverage", "",
           f"Source: {len(progs)} programs whose RTL runs passed and matched the ISS "
           f"(from build/sim/summary.json). Counts are per executed instruction.", ""]
    missing = []

    def table(title, header, rows):
        out.extend([f"## {title}", "", "| " + " | ".join(header) + " |",
                    "|" + "---|" * len(header)])
        out.extend("| " + " | ".join(str(x) for x in row) + " |" for row in rows)
        out.append("")

    rows = []
    for n in MNEMONICS:
        lvl = ("ISS-match" + (" (always traps)" if n in ("ecall", "ebreak") else "")) if insn[n] else "**not executed**"
        rows.append((n, insn[n], lvl))
        if insn[n] == 0:
            missing.append(f"instruction {n}")
    table("Instructions (executed in ISS-matched program runs)", ["mnemonic", "count", "level"], rows)

    rows = []
    for n in ALU_OPS:
        seen = opnd[n]
        # reachable classes of b: register operands all 9; 12-bit immediates only
        # zero/one/minus1/small+/small-; shift amounts only zero/one/small+
        b_pool = (VALUE_CLASSES if n in R_OPS else
                  ["zero", "one", "small+"] if n in ("slli", "srli", "srai") else
                  ["zero", "one", "minus1", "small+", "small-"])
        a_miss = [c for c in VALUE_CLASSES if ("a", c) not in seen]
        b_miss = [c for c in b_pool if ("b", c) not in seen]
        rows.append((n, f"{9 - len(a_miss)}/9", f"{len(b_pool) - len(b_miss)}/{len(b_pool)}",
                     ", ".join(a_miss) or "-", ", ".join(b_miss) or "-"))
        missing += [f"{n} operand a = {c}" for c in a_miss] + [f"{n} operand b = {c}" for c in b_miss]
    table("ALU operand value classes (rs1 = a, rs2/imm = b; b counted against the reachable classes)",
          ["op", "a classes", "b classes", "a not seen", "b not seen"], rows)

    table("Signed vs unsigned disagreement (operands with different sign bits)",
          ["op", "count"], [(n, sign_split[n]) for n in
                            ("slt", "sltu", "slti", "sltiu", "blt", "bge", "bltu", "bgeu")])
    for n in ("slt", "sltu", "blt", "bge", "bltu", "bgeu"):
        if sign_split[n] == 0:
            missing.append(f"sign-differing operands for {n}")

    rows = [(n, ", ".join(sorted(shifts[n])) or "-") for n in ("sll", "srl", "sra", "slli", "srli", "srai")]
    table("Shift amounts", ["op", "seen"], rows)
    for n in ("sll", "srl", "sra", "slli", "srli", "srai"):
        for b in ("0", "31", "1-30"):
            if b not in shifts[n]:
                missing.append(f"{n} shamt {b}")

    rows = [(n, br[(n, "taken")], br[(n, "not")]) for n in BRANCH]
    rows.append(("(any) taken forward / backward", br[("any", "taken fwd")], br[("any", "taken back")]))
    table("Branches", ["branch", "taken", "not taken"], rows)
    for n in BRANCH:
        for t in ("taken", "not"):
            if br[(n, t)] == 0:
                missing.append(f"{n} {t}")

    rows = [(f"{j} {k}", jumps[(j, k)]) for j in ("jal", "jalr") for k in ("fwd", "back", "rd=x0", "rd!=x0")]
    table("Jumps", ["case", "count"], rows)

    rows = []
    for n, size in {**LOADS, **STORES}.items():
        offs = [o for o in range(4) if o % size == 0]
        rows.append((n, " ".join(f"+{o}:{mem[(n, o)]}" for o in offs),
                     mem[(n, "imm<0")], mem[(n, "imm>=0")], mem[(n, "rd=x0")] if n in LOADS else "-"))
        for o in offs:
            if mem[(n, o)] == 0:
                missing.append(f"{n} at addr[1:0]={o}")
    table("Loads / stores (aligned byte offsets; misaligned ones are in the trap table)",
          ["op", "addr[1:0]: count", "imm<0", "imm>=0", "rd=x0"], rows)

    rows = [(f"{k[0]}-type {k[1] if k[0] != 'U' else hex(k[1])}", v) for k, v in sorted(imm_edges.items(), key=str)]
    table("Immediate boundary values", ["immediate", "count"], rows)

    table("Registers used", ["field", "registers seen"],
          [(f, f"{len(v)}/32" + ("" if len(v) == 32 else f" (missing {sorted(set(range(32)) - v)})"))
           for f, v in regs.items()])

    rows = [(c, CAUSES[c], causes[c]) for c in CAUSES]
    table("Exceptions (trap events that matched the ISS)", ["cause", "name", "count"], rows)
    for c in CAUSES:
        if causes[c] == 0:
            missing.append(f"trap cause {c}")

    rows = [(p, stalls[p], " ".join(f"{k}:{lat[p][k]}" for k in range(1, 16) if lat[p][k]))
            for p in ("imem", "dmem")]
    table("Memory timing seen by the core (all passing program runs)",
          ["port", "cycles waiting for ready", "response latency (cycles: count)"], rows)

    table("Reset scenarios (passing tests)", ["mode", "tests"], sorted(resets.items()))

    out += ["## Not covered", ""] + ([f"- {m}" for m in missing] or ["- (none of the listed bins)"])
    out += ["", "## Covered by unit tests only (own reference models, not the ISS)", "",
            "- ALU: all 10 operations x 16x16 edge values, every shift amount 0..31 (tb_rv32i_alu)",
            "- Decoder: every opcode x funct3 x funct7 combination (tb_rv32i_decoder)",
            "- LSU: every funct3 x addr[1:0] x data pattern, including undefined funct3 (tb_rv32i_lsu)",
            "- Regfile: every address pair, read-during-write (tb_rv32i_regfile)", ""]
    SIM.mkdir(parents=True, exist_ok=True)
    (SIM / "coverage.md").write_text("\n".join(out), encoding="utf-8", newline="\n")
    (SIM / "coverage.json").write_text(json.dumps({
        "programs": len(progs), "instructions": dict(insn), "causes": {str(k): v for k, v in causes.items()},
        "branches": {f"{k[0]}:{k[1]}": v for k, v in br.items()}, "missing": missing,
        "resets": dict(resets), "stalls": dict(stalls)}, indent=1), encoding="utf-8")
    print(f"coverage_report: {len(progs)} programs, {sum(insn.values())} instructions, "
          f"{len(missing)} uncovered bin(s) -> build/sim/coverage.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
