#!/usr/bin/env python3
"""Compare an RTL run (tb_rv32i_core trace + final RAM) with the reference ISS.

Architectural comparison, instruction by instruction (not cycle by cycle):
  - order of events; for each instruction: retire vs trap
  - retire: pc, instruction word, rd write enable; rd number and value when written
  - trap: cause, pc, tval
  - data access of the instruction (0 or 1): byte address, read/write, error;
    for writes the exact set of bytes written {address: value}
    (RTL: wstrb + lane data of the CMI request, ISS: address/size/value)
  - end condition (TOHOST value, or halt)
  - final RAM image, word by word
Only the part of the RTL trace after the last reset marker ("X") is used.

Usage:
  python scripts/sim/trace_compare.py RTL.trace ISS.trace [--rtl-mem F --iss-mem F] [--dump prog.dump]
Exit status 0 = identical, 1 = mismatch (first mismatch printed with context).
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Rec:
    kind: str                 # "R" or "T"
    fields: tuple
    access: list = field(default_factory=list)   # normalised data accesses
    line: str = ""


def norm_access_rtl(tok: list[str]) -> tuple:
    addr, we, strb, wdata, _rdata, err = (int(tok[1], 16), int(tok[2]), int(tok[3], 16),
                                          int(tok[4], 16), tok[5], int(tok[6]))
    if we:
        base = addr & ~3
        data = tuple(sorted((base + i, (wdata >> (8 * i)) & 0xFF) for i in range(4) if strb >> i & 1))
    else:
        data = ()
    return (addr, we, err, data)


def norm_access_iss(tok: list[str]) -> tuple:
    addr, we, size, value, err = int(tok[1], 16), int(tok[2]), int(tok[3]), int(tok[4], 16), int(tok[5])
    data = tuple((addr + i, (value >> (8 * i)) & 0xFF) for i in range(size)) if we else ()
    return (addr, we, err, data)


def parse(path: Path, source: str) -> tuple[list[Rec], str]:
    recs: list[Rec] = []
    pending: list = []
    end = "E <missing>"
    for raw in path.read_text(encoding="ascii").splitlines():
        tok = raw.split()
        if not tok:
            continue
        t = tok[0]
        if t == "X":                         # reset: start over
            recs, pending, end = [], [], "E <missing>"
        elif t in ("M", "A"):
            pending.append(norm_access_rtl(tok) if t == "M" else norm_access_iss(tok))
        elif t == "R":
            pc, insn, we, rd, wd = int(tok[1], 16), int(tok[2], 16), int(tok[3]), int(tok[4]), int(tok[5], 16)
            fields = (pc, insn, we, rd if we else None, wd if we else None)
            recs.append(Rec("R", fields, pending, raw)); pending = []
        elif t == "T":
            recs.append(Rec("T", (int(tok[1]), int(tok[2], 16), int(tok[3], 16)), pending, raw)); pending = []
        elif t == "E":
            end = " ".join(tok)
    if pending:
        recs.append(Rec("?", ("unattributed data access",), pending, "<accesses without retire/trap>"))
    return recs, end


def describe(r: Rec | None) -> str:
    if r is None:
        return "<none>"
    if r.kind == "R":
        pc, insn, we, rd, wd = r.fields
        s = f"retire pc={pc:08x} insn={insn:08x}" + (f" x{rd}<={wd:08x}" if we else " (no rd write)")
    elif r.kind == "T":
        s = f"trap cause={r.fields[0]} pc={r.fields[1]:08x} tval={r.fields[2]:08x}"
    else:
        s = r.fields[0]
    for a in r.access:
        s += f" | {'store' if a[1] else 'load'} @{a[0]:08x}{' ERR' if a[2] else ''}"
        if a[3]:
            s += " bytes{" + ",".join(f"{ad:x}:{v:02x}" for ad, v in a[3]) + "}"
    return s


def load_words(path: Path) -> list[int]:
    return [int(l, 16) for l in path.read_text(encoding="ascii").split() if l]


def compare(rtl_trace: Path, iss_trace: Path, rtl_mem: Path | None = None,
            iss_mem: Path | None = None, dump: Path | None = None) -> tuple[bool, str, int]:
    """Return (ok, report text, number of compared events)."""
    r_recs, r_end = parse(rtl_trace, "rtl")
    i_recs, i_end = parse(iss_trace, "iss")
    disasm = {}
    if dump and dump.is_file():
        for line in dump.read_text(encoding="utf-8").splitlines():
            mm = re.match(r"^\s*([0-9a-f]+):\s+[0-9a-f]{8}\s+(.*)$", line)
            if mm:
                disasm[int(mm.group(1), 16)] = mm.group(2).strip()
    out: list[str] = []

    def ctx(i: int) -> None:
        lo = max(0, i - 5)
        out.append("  context (index: RTL | ISS):")
        for k in range(lo, min(i + 3, max(len(r_recs), len(i_recs)))):
            rr = r_recs[k] if k < len(r_recs) else None
            ii = i_recs[k] if k < len(i_recs) else None
            mark = ">>" if k == i else "  "
            pc = ii.fields[1] if ii and ii.kind == "T" else (ii.fields[0] if ii and ii.kind == "R" else None)
            dis = f"   [{disasm.get(pc, '')}]" if pc is not None and pc in disasm else ""
            out.append(f"  {mark} #{k:<6d} RTL: {describe(rr)}")
            out.append(f"  {mark} {'':7s} ISS: {describe(ii)}{dis}")

    n = min(len(r_recs), len(i_recs))
    for i in range(n):
        a, b = r_recs[i], i_recs[i]
        if (a.kind, a.fields, a.access) != (b.kind, b.fields, b.access):
            out.append(f"MISMATCH at event #{i}:")
            out.append(f"  RTL: {describe(a)}")
            out.append(f"  ISS: {describe(b)}")
            ctx(i)
            return False, "\n".join(out), i
    if len(r_recs) != len(i_recs):
        out.append(f"MISMATCH: RTL has {len(r_recs)} events, ISS has {len(i_recs)}")
        ctx(n)
        return False, "\n".join(out), n
    if r_end != i_end:
        out.append(f"MISMATCH in end condition: RTL '{r_end}' vs ISS '{i_end}'")
        return False, "\n".join(out), n
    if rtl_mem and iss_mem:
        rw, iw = load_words(rtl_mem), load_words(iss_mem)
        if len(rw) != len(iw):
            out.append(f"MISMATCH: RAM sizes differ ({len(rw)} vs {len(iw)} words)")
            return False, "\n".join(out), n
        diffs = [k for k in range(len(rw)) if rw[k] != iw[k]]
        if diffs:
            out.append(f"MISMATCH: final RAM differs in {len(diffs)} word(s):")
            for k in diffs[:10]:
                out.append(f"  addr {4 * k:08x}: RTL {rw[k]:08x}  ISS {iw[k]:08x}")
            return False, "\n".join(out), n
        out.append(f"final RAM identical ({len(rw)} words)")
    out.insert(0, f"COMPARE: PASS events={n} end='{r_end}'")
    return True, "\n".join(out), n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rtl_trace", type=Path)
    ap.add_argument("iss_trace", type=Path)
    ap.add_argument("--rtl-mem", type=Path)
    ap.add_argument("--iss-mem", type=Path)
    ap.add_argument("--dump", type=Path, help="objdump -d output for context disassembly")
    ns = ap.parse_args()
    ok, text, _ = compare(ns.rtl_trace, ns.iss_trace, ns.rtl_mem, ns.iss_mem, ns.dump)
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
