#!/usr/bin/env python3
"""Stage 6A: the RV32I SoC on the KV260 - bitstream builds and JTAG board test.

  python scripts/fpga/kv260_6a.py build     [progs...] [--variant jtag|ps|ps_spi] [--jobs 12] [--parallel 1]
  python scripts/fpga/kv260_6a.py report    [progs...] [--variant jtag|ps|ps_spi]
  python scripts/fpga/kv260_6a.py bramcheck [progs...] [--variant jtag|ps|ps_spi]
  python scripts/fpga/kv260_6a.py board     [progs...] [--resets 10] [--freq-wait 5]     (JTAG variant)
  python scripts/fpga/kv260_6a.py psboard   [progs...] [--resets 10] [--dry-run]         (PS variant)

Variants: jtag = VIO/ILA debug cores, build/vivado/kv260_6a/; ps = PS debug path
(kv260_dbg_axil at 0xA000_0000 through M_AXI_HPM0_FPD, no JTAG), build/vivado/kv260_6a_ps/.
ps_spi = ps + STM32 EMG1 SPI monitor at 0xA013_0000 and J2 Pmod I/O,
build/vivado/kv260_spi/.
Any JTAG session hangs the KV260 Linux (docs/fpga/kv260_stage6a.md section 9), hence ps.

Programs (firmware/rv32/apps, built by scripts/sw/build_asm_tests.py --kind c):
c_boot, c_sum, c_crc16, c_sysctrl. Each one gets its own bitstream: the 64 KB
RAM is initialised through rv32_soc_top INIT_FILE ($readmemh -> BRAM INIT).

build  : fpga/kv260/tcl/build_kv260.tcl per program into build/vivado/kv260_6a/<prog>
report : resources, timing, clocks, DRC/methodology from the implementation reports
psboard: no JTAG. Copies the board script (scripts/fpga/kv260_ps_board.py), the
         .bit.bin files and rv32_6a.dtbo to ~/riscv-emg-soc/stage6a/ on the board,
         unloads the running PL app (xmutil), configures the PL through the Linux
         FPGA manager, runs the same checks as 'board' through /dev/mem, and
         finally reloads k26-starter-kits. --dry-run only prints the commands.
board  : JTAG only (volatile PL configuration; QSPI/eMMC/SD and the PS untouched).
         For every program: program the PL, read the VIO after the power-on run,
         measure pl_clk0 with the free-running counter, re-run the program N times
         through the VIO reset, capture the instruction trace with the ILA
         (segments triggered at instruction number S) and compare it with the
         reference ISS (scripts/sim/trace_compare.py rules), capture the AXI4-Lite
         TOHOST write. Results: build/vivado/kv260_6a/board/<prog>/ + board_summary.json

Expected values: ISS (reference/rv32i_iss) for c_boot/c_sum/c_crc16; for
c_sysctrl (SYSCTRL registers are not in the ISS) the self-check, the retired
count of the Vivado xsim SmartConnect run, and the Icarus SoC trace except for
loads of the cycle counter.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "sim"))
sys.path.insert(0, str(REPO / "reference" / "rv32i_iss"))
import trace_compare  # noqa: E402
import rv32i_iss  # noqa: E402

VIVADO_ROOT = Path(os.environ.get("RISCV_EMG_VIVADO_ROOT", r"E:\Xilinx2026\2026.1\Vivado"))
VIVADO = VIVADO_ROOT / "bin" / "vivado.bat"
OUT = REPO / "build" / "vivado" / "kv260_6a"
VARIANT = "jtag"
BOOTGEN = VIVADO_ROOT / "bin" / "bootgen.bat"
DTC = VIVADO_ROOT / "bin" / "dtc.exe"
BOARD = os.environ.get("RISCV_EMG_KV260", "ubuntu@192.168.137.25")
BOARD_DIR = "riscv-emg-soc/stage6a"          # relative to the board user's home
SW = REPO / "build" / "sw" / "c"
PROGRAMS = ["c_boot", "c_sum", "c_crc16", "c_sysctrl"]
E3C_PROGRAMS = ["e3c_flow", "e3c_rt", "e3c_rt2k"]  # --variant ps_e3c
E4A_PROGRAMS = ["mbx_rt2k", "mbx_flow", "mbx_rt2k_long"]  # --variant ps_e4a (firmware/rv32/mailbox)
E4C_PROGRAMS = ["mbx_mg6_rt"]          # ps_e4a hardware, E4c multi-gesture firmware: only when named
E5A_PROGRAMS = ["mbx_dma_live"]        # --variant ps_e5a (firmware/rv32/dma): PS DMA source, no data in the bitstream
# seconds a program needs on the board (100 MHz): stage 6A programs < 0.4 ms;
# e3c_flow 2 runs x 23.5 ms; e3c_rt 1400 x 120 us; e3c_rt2k 1400 x 0.5 ms (2 kHz);
# mbx_flow 11 results in 23 ms; mbx_rt2k 0.7 s; mbx_rt2k_long 8 x 0.7 s
RUN_WAIT = {"e3c_flow": 0.2, "e3c_rt": 0.4, "e3c_rt2k": 1.2, "mbx_flow": 0.2, "mbx_rt2k": 1.2, "mbx_rt2k_long": 6.5,
            "mbx_mg6_rt": 8.0}
# xsim run of an E4a program (scripts/sim/run_mbx.py --xsim) -> its cycle reference
E4A_XSIM = {"mbx_flow": "xsim__flow_reader", "mbx_rt2k": "xsim__rt2k_reader"}
ISS_PROGRAMS = {"c_boot", "c_sum", "c_crc16"}
ILA_DEPTH = 4096
SEG_SAMPLES = 3600          # planned qualified samples per trace segment (< ILA_DEPTH)


def set_variant(v: str) -> None:
    global OUT, VARIANT, SW
    VARIANT = v
    OUT = REPO / "build" / "vivado" / {"jtag": "kv260_6a", "ps": "kv260_6a_ps", "ps_spi": "kv260_spi",
                                       "ps_e3c": "kv260_e3c", "ps_e4a": "kv260_e4a", "ps_e5a": "kv260_e5a",
                                       "ps_e6a": "kv260_e6a"}[v]
    if v == "ps_e3c":                               # E3c firmware (firmware/rv32/e3c)
        SW = REPO / "build" / "sw" / "e3c"
    if v == "ps_e4a":                               # E4a mailbox firmware (firmware/rv32/mailbox)
        SW = REPO / "build" / "sw" / "mbx"
    if v in ("ps_e5a", "ps_e6a"):                   # E5a firmware (firmware/rv32/dma): the RV32I does not see the source
        SW = REPO / "build" / "sw" / "dma"


def e3c_like() -> bool:
    """variants with the E3c pipeline (model / replay BRAMs, E3c address map)"""
    return VARIANT in ("ps_e3c", "ps_e4a", "ps_e5a", "ps_e6a")


def vivado(tcl: Path, args: list[str], log: Path, timeout: int) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(VIVADO), "-mode", "batch", "-nojournal", "-log", str(log), "-source", str(tcl)]
    if args:
        cmd += ["-tclargs", *args]
    r = subprocess.run(cmd, cwd=log.parent, capture_output=True, text=True, timeout=timeout)
    return r.returncode


def fw_dir(prog: str) -> Path:
    """build directory of a program's firmware (E4c programs: firmware/rv32/mg6 -> build/sw/mg6)"""
    return REPO / "build" / "sw" / "mg6" / prog if prog in E4C_PROGRAMS else SW / prog


def e4c_trace(prog: str, img: list[int]) -> tuple[list, dict]:
    """E4c traceability: the six segments in the CPU BRAM of the routed design == the generator's
    frame table (build/e4c/fw/mg6_data.json), selection / model / records recorded"""
    import hashlib
    prov = json.loads((REPO / "build" / "e4c" / "fw" / "mg6_data.json").read_text(encoding="utf-8"))
    selp = REPO / "build" / "e4c" / "mg6_selection.json"
    nm = VIVADO_ROOT.parent / "gnu" / "riscv" / "nt" / "bin" / "riscv64-unknown-elf-nm.exe"
    out = subprocess.run([str(nm), "-S", str(fw_dir(prog) / f"{prog}.elf")], capture_output=True, text=True).stdout
    sym = {f[3]: (int(f[0], 16), int(f[1], 16)) for f in (l.split() for l in out.splitlines()) if len(f) == 4}
    a, n = sym["mg6_frames"]
    blob = b"".join(w.to_bytes(4, "little") for w in img[a // 4:(a + n) // 4])
    hdr = REPO / "build" / "e4c" / "fw" / "mg6_data.h"
    checks = [
        ("E4c: mg6_frames in the CPU BRAM of the routed design == generated frame table",
         n == prov["frame_bytes"] and hashlib.sha256(blob).hexdigest() == prov["frame_words_le_sha256"],
         f"{n} bytes at 0x{a:04X}, sha256 {hashlib.sha256(blob).hexdigest()[:16]}"),
        ("E4c: mg6_data.h == the header the provenance describes",
         sha256(hdr) == prov["header_sha256"], prov["header_sha256"][:16]),
        ("E4c: selection file == the one the generator used",
         selp.is_file() and sha256(selp) == prov["selection_sha256"], prov["selection_sha256"][:16]),
        ("E4c: model of the selection == E2 reference fold model file",
         prov["model"]["int8_npz_sha256"] == sha256(REPO / "analysis" / "ml" / "e2" / "e2_20261022_023953" / "models"
                                                    / "s1_test_s3_seed0_int8.npz"), prov["model"]["int8_npz_sha256"][:16]),
    ]
    sec = {"segments": [{k: s[k] for k in ("label", "gesture", "session", "participant", "trial", "start", "length",
                                           "source", "sha256")} for s in prov["segments"]],
           "esi_lsb_mV": prov["esi_lsb_mV"], "esi_rule": prov["esi_rule"], "dsp_mask": prov["dsp_mask"],
           "packing": prov["packing"], "frame_table": {"address": f"0x{a:04X}", "bytes": n,
                                                       "sha256": prov["frame_words_le_sha256"]},
           "mg6_data.h_sha256": prov["header_sha256"], "selection_sha256": prov["selection_sha256"],
           "model_int8_npz_sha256": prov["model"]["int8_npz_sha256"], "generator": prov["generator"],
           "data_license": "GRABMyo 1.1.0, CC BY 4.0 (data/public/grabmyo/raw/LICENSE.txt); samples not in git"}
    return checks, sec


# ---------------------------------------------------------------- build
def build_one(prog: str, jobs: int) -> tuple[str, int, float]:
    hexf = fw_dir(prog) / f"{prog}.hex"
    if not hexf.is_file():
        sys.exit(f"{hexf} missing: python scripts/sw/build_asm_tests.py --kind c --src firmware/rv32/apps --out build/sw/c")
    t0 = time.time()
    rc = vivado(REPO / "fpga" / "kv260" / "tcl" / "build_kv260.tcl",
                [REPO.as_posix(), (OUT / prog).as_posix(), hexf.as_posix(), "bit", str(jobs), VARIANT],
                OUT / f"build_{prog}.log", 3 * 3600)
    if rc == 0 and VARIANT in ("ps", "ps_spi", "ps_e3c", "ps_e4a", "ps_e5a", "ps_e6a"):
        rc = make_bit_bin(OUT / prog / "out")
    return prog, rc, time.time() - t0


def make_bit_bin(out: Path) -> int:
    """kv260_soc.bit -> kv260_soc.bit.bin (bootgen -process_bitstream bin), the format
    the Linux FPGA manager loads"""
    (out / "kv260_soc.bif").write_text("all:\n{\n  [destination_device = pl] kv260_soc.bit\n}\n", encoding="ascii")
    r = subprocess.run([str(BOOTGEN), "-image", "kv260_soc.bif", "-arch", "zynqmp", "-process_bitstream", "bin",
                        "-w", "on"], cwd=out, capture_output=True, text=True)
    ok = r.returncode == 0 and (out / "kv260_soc.bit.bin").is_file()
    if not ok:
        print(r.stdout[-2000:] + r.stderr[-2000:])
    return 0 if ok else 1


def cmd_build(ns) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    fails = 0
    with ThreadPoolExecutor(max_workers=ns.parallel) as ex:
        for prog, rc, dt in ex.map(lambda p: build_one(p, ns.jobs), ns.progs):
            ok = rc == 0 and (OUT / prog / "out" / "kv260_soc.bit").is_file()
            fails += not ok
            print(f"{'OK  ' if ok else 'FAIL'} {prog:10s} {dt / 60:5.1f} min  log {OUT.relative_to(REPO).as_posix()}/build_{prog}.log",
                  flush=True)
    return 1 if fails else 0


# ---------------------------------------------------------------- report
def read_summary(prog: str) -> dict:
    d: dict = {"clocks": []}
    p = OUT / prog / "out" / "summary.txt"
    if not p.is_file():
        return d
    for line in p.read_text(encoding="utf-8").splitlines():
        if " = " in line:
            k, v = line.split(" = ", 1)
            if k == "clock":
                d["clocks"].append(v)
            else:
                d[k] = v
    return d


def util_row(text: str, name: str) -> str | None:
    m = re.search(r"^\|\s*" + re.escape(name) + r"\s*\|\s*([\d.]+)\s*\|", text, re.M)
    return m.group(1) if m else None


def hier_rows(text: str) -> dict:
    """instance -> (total LUTs, FFs, RAMB36, RAMB18, DSP) from utilization_hier.rpt"""
    rows = {}
    header = None
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and cells[0] == "Instance":
            header = cells
            continue
        if header and len(cells) == len(header):
            rows[cells[0]] = dict(zip(header, cells))
    return rows


def check_timing_counts(text: str) -> dict:
    out = {}
    for key, pat in [("no_clock", r"There (?:are|is) (\d+) register/latch pins? with no clock"),
                     ("unconstrained_internal", r"There (?:are|is) (\d+) pins? that (?:are|is) not constrained for maximum delay\."),
                     ("no_input_delay", r"There (?:are|is) (\d+) input ports? with no input delay"),
                     ("no_output_delay", r"There (?:are|is) (\d+) ports? with no output delay"),
                     ("multiple_clock", r"There (?:are|is) (\d+) register/latch pins? with multiple clocks"),
                     ("loops", r"There (?:are|is) (\d+) combinational loops?")]:
        m = re.search(pat, text)
        out[key] = int(m.group(1)) if m else None
    return out


def prog_report(prog: str) -> dict:
    r = OUT / prog / "out" / "reports"
    s = read_summary(prog)
    rep = {"program": prog, "summary": s}
    if not (r / "utilization.rpt").is_file():
        return rep
    u = (r / "utilization.rpt").read_text(encoding="utf-8", errors="replace")
    rep["util"] = {k: util_row(u, k) for k in ("CLB LUTs", "LUT as Logic", "LUT as Memory", "CLB Registers",
                                               "Block RAM Tile", "RAMB36/FIFO*", "RAMB18", "DSPs", "BUFGCE")}
    h = hier_rows((r / "utilization_hier.rpt").read_text(encoding="utf-8", errors="replace"))
    rep["hier"] = {k: v for k, v in h.items() if k.strip() in
                   ("kv260_bd_wrapper", "kv260_bd_i", "cpu_subsys", "axi_smc", "ila", "vio", "sysctrl",
                    "obs", "rst_pl0", "zynq_ultra_ps_e_0", "dbg_hub")}
    rep["check_timing"] = check_timing_counts((r / "check_timing.rpt").read_text(encoding="utf-8", errors="replace"))
    ts = (r / "timing_summary.rpt").read_text(encoding="utf-8", errors="replace")
    m = re.search(r"WNS\(ns\)\s+TNS\(ns\).*?\n[-\s]+\n\s*([-\d.]+)\s+([-\d.]+)\s+(\d+)\s+(\d+)\s+([-\d.]+)\s+([-\d.]+)\s+(\d+)\s+(\d+)\s+([-\d.]+)\s+([-\d.]+)",
                  ts, re.S)
    if m:
        rep["timing"] = dict(zip(["WNS", "TNS", "TNS_fail", "TNS_total", "WHS", "THS", "THS_fail", "THS_total",
                                  "WPWS", "TPWS"], m.groups()))
    rep["timing_met"] = "All user specified timing constraints are met" in ts
    drc = (r / "drc.rpt").read_text(encoding="utf-8", errors="replace")
    rep["drc_rule_counts"] = re.findall(r"^\|\s*(\S+)\s*\|\s*(Error|Critical Warning|Warning|Advisory)\s*\|[^|]*\|\s*(\d+)\s*\|",
                                         drc, re.M)
    meth = (r / "methodology.rpt").read_text(encoding="utf-8", errors="replace")
    rep["methodology"] = re.findall(r"^\|\s*(\S+)\s*\|\s*(Critical Warning|Warning|Advisory)\s*\|([^|]*)\|\s*(\d+)\s*\|",
                                    meth, re.M)
    log = OUT / f"build_{prog}.log"
    if log.is_file():
        lt = log.read_text(encoding="utf-8", errors="replace")
        rep["log_errors"] = sorted(set(re.findall(r"^ERROR: .*$", lt, re.M)))
        rep["log_crit"] = sorted(set(re.findall(r"^CRITICAL WARNING: .*$", lt, re.M)))
    return rep


def cmd_report(ns) -> int:
    reps = [prog_report(p) for p in ns.progs]
    (OUT / "build_summary.json").write_text(json.dumps(reps, indent=1), encoding="utf-8")
    for rep in reps:
        s = rep["summary"]
        print(f"== {rep['program']}: wns={s.get('wns')} tns={s.get('tns')} whs={s.get('whs')} "
              f"drc_err={s.get('drc_errors')} timing_met={rep.get('timing_met')}")
        for c in s.get("clocks", []):
            print("   clock", c)
        print("   util", rep.get("util"))
        print("   check_timing", rep.get("check_timing"))
        for k, v in rep.get("hier", {}).items():
            print(f"   hier {k:22s} LUT={v.get('Total LUTs')} FF={v.get('FFs')} RAMB36={v.get('RAMB36')} "
                  f"RAMB18={v.get('RAMB18')} DSP={v.get('DSP Blocks', v.get('DSP48 Blocks'))}")
        for d in rep.get("drc_rule_counts", []):
            print("   drc", d)
        for d in rep.get("methodology", []):
            print("   methodology", d)
        for e in rep.get("log_errors", []) + rep.get("log_crit", []):
            print("   log", e[:200])
    return 0


# ---------------------------------------------------------------- BRAM contents
def bram_image(dump: Path, mem_bytes: int = 65536) -> list[int]:
    """64 KB RAM image (words) rebuilt from the INIT strings of kv260_bram_dump.tcl.
    Supports data widths 1/2/4/8 per cell (READ_WIDTH 1/2/4/9, parity bits unused)."""
    words = [0] * (mem_bytes // 4)
    cell = None
    for line in dump.read_text(encoding="utf-8").splitlines():
        tok = line.split()
        if tok[0] == "CELL":
            p = dict(t.split("=", 1) for t in tok[2:] if "=" in t)
            w = int(p["READ_WIDTH_A"])
            dw = {1: 1, 2: 2, 4: 4, 9: 8}[w]
            cell = (int(p["ram_addr_begin"]), int(p["ram_addr_end"]), int(p["ram_slice_begin"]),
                    int(p["ram_slice_end"]), dw)
            if cell[3] - cell[2] + 1 != dw:
                raise ValueError(f"unexpected slice width in {line}")
        elif tok[0] == "INIT":
            a0, a1, s0, s1, dw = cell
            bits = int(tok[2].split("'h")[1], 16)            # 256 bits, bit 0 = LSB
            per = 256 // dw
            for k in range(per):
                addr = a0 + int(tok[1], 16) * per + k
                if addr > a1:
                    break
                v = (bits >> (k * dw)) & ((1 << dw) - 1)
                words[addr] |= v << s0
    return words


def bram_arrays(dump: Path) -> dict:
    """{RTL memory name: {address: value}} from a kv260_bram_dump.tcl dump; data widths
    8/16/32 per cell (READ_WIDTH 9/18/36), slice bits = data bits first, then parity"""
    cells, cur = [], None
    for line in dump.read_text(encoding="utf-8").splitlines():
        tok = line.split()
        if tok[0] == "CELL":
            pr = dict(x.split("=", 1) for x in tok[2:] if "=" in x)
            cur = {"p": pr, "init": {}, "initp": {}}
            cells.append(cur)
        elif tok[0] in ("INIT", "INITP"):
            cur["init" if tok[0] == "INIT" else "initp"][int(tok[1], 16)] = int(tok[2].split("'h")[1], 16)
    arrays: dict = {}
    for c in cells:
        pr = c["p"]
        w = int(pr["READ_WIDTH_A"])
        dw, pw = {9: (8, 1), 18: (16, 2), 36: (32, 4)}[w]
        a0, a1, s0, s1 = (int(pr[k]) for k in ("ram_addr_begin", "ram_addr_end", "ram_slice_begin", "ram_slice_end"))
        name = pr.get("RTL_RAM_NAME", "?").split("/")[-1]
        d = sum(v << (256 * i) for i, v in c["init"].items())
        pa = sum(v << (256 * i) for i, v in c["initp"].items())
        arr = arrays.setdefault(name, {})
        for a in range(a0, a1 + 1):
            k = a - a0
            word = ((d >> (dw * k)) & ((1 << dw) - 1)) | (((pa >> (pw * k)) & ((1 << pw) - 1)) << dw)
            arr[a] = arr.get(a, 0) | ((word & ((1 << (s1 - s0 + 1)) - 1)) << s0)
    return arrays


def e3c_bram_check(prog: str) -> list[tuple[str, bool, str]]:
    """routed BRAM init of the E3c pipeline: CNN conv weights vs the E2 model files,
    replay frames vs build/e3c/replay_*.hex"""
    dcp = OUT / prog / "kv260_soc.runs" / "impl_1" / "kv260_bd_wrapper_routed.dcp"
    dump = OUT / prog / "out" / "bram_dump_e3c.txt"
    vivado(REPO / "fpga" / "kv260" / "tcl" / "kv260_bram_dump.tcl", [dcp.as_posix(), dump.as_posix(), "*/e3c/*"],
           OUT / f"bramdump_e3c_{prog}.log", 1800)
    arr = bram_arrays(dump)
    model = REPO / "build" / "e3" / "vec" / "model"
    res = []
    files = [("w1_reg", model / "conv1_w.hex"), ("w2_reg", model / "conv2_w.hex"), ("w3_reg", model / "conv3_w.hex")]
    if VARIANT in ("ps_e5a", "ps_e6a"):             # no frame memory at all: nothing of the data set in the bitstream
        res.append(("no replay frame memory (mem_reg) in the routed design", "mem_reg" not in arr,
                    f"E3c BRAM arrays {sorted(arr)}"))
    else:
        files.append(("mem_reg", REPO / "build" / "e3c" / "replay_chain_s1_normal.hex"))
    for name, f in files:
        exp = [int(l, 16) for l in f.read_text(encoding="ascii").split()]
        got = arr.get(name, {})
        bad = [i for i, e in enumerate(exp) if got.get(i, 0) != e]
        res.append((f"{name} == {f.name}", not bad, f"{len(exp)} entries" + (f", {len(bad)} differ" if bad else "")))
    return res


def cmd_bramcheck(ns) -> int:
    fails = 0
    for prog in ns.progs:
        dcp = OUT / prog / "kv260_soc.runs" / "impl_1" / "kv260_bd_wrapper_routed.dcp"
        dump = OUT / prog / "out" / "bram_dump.txt"
        if not dcp.is_file():
            print(f"FAIL {prog}: no routed checkpoint"); fails += 1; continue
        vivado(REPO / "fpga" / "kv260" / "tcl" / "kv260_bram_dump.tcl", [dcp.as_posix(), dump.as_posix()],
               OUT / f"bramdump_{prog}.log", 1800)
        img = bram_image(dump)
        exp = [int(l, 16) for l in (fw_dir(prog) / f"{prog}.hex").read_text(encoding="ascii").split()]
        exp += [0] * (len(img) - len(exp))
        bad = [i for i, (a, b) in enumerate(zip(img, exp)) if a != b]
        ok = not bad
        fails += not ok
        print(f"{'OK  ' if ok else 'FAIL'} {prog}: routed BRAM INIT == {prog}.hex for all {len(img)} words"
              + ("" if ok else f"; {len(bad)} differ, first at word {bad[0]}: {img[bad[0]]:08x} vs {exp[bad[0]]:08x}"))
        if e3c_like():
            for what, good, detail in e3c_bram_check(prog):
                fails += not good
                print(f"{'OK  ' if good else 'FAIL'} {prog}: routed E3c BRAM {what} ({detail})")
    return 1 if fails else 0


# ---------------------------------------------------------------- expected values
def iss_reference(prog: str):
    """(records, end, retired) of the ISS run, or None for SoC-only programs."""
    if prog not in ISS_PROGRAMS:
        return None
    iss, lines = rv32i_iss.run(SW / prog / f"{prog}.args")
    p = OUT / "board" / prog / "iss.trace"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
    recs, end = trace_compare.parse(p, "iss")
    return recs, end, iss.retired


def xsim_perf(prog: str) -> dict | None:
    if prog in E4A_XSIM:                            # E4a firmware: tb_mbx_soc in xsim (run_mbx.py --xsim)
        log = REPO / "build" / "mbx" / "runs" / E4A_XSIM[prog] / "sim.log"
        if not log.is_file():
            return None
        m = re.search(r"PERF: cycles=(\d+)", log.read_text(encoding="utf-8", errors="replace"))
        return {"cycles": int(m.group(1)), "retired": None} if m else None
    if prog.startswith("e3c_"):                     # E3c firmware: tb_e3c_soc in xsim (run_e3c.py --xsim)
        log = REPO / "build" / "e3c" / "runs" / f"xsim__{prog}" / "sim.log"
        if not log.is_file():
            return None
        m = re.search(r"PERF: cycles=(\d+)", log.read_text(encoding="utf-8", errors="replace"))
        return {"cycles": int(m.group(1)), "retired": None} if m else None
    log = REPO / "build" / "vivado" / "xsim_runs_soc" / f"xsim__c__{prog}" / "sim.log"
    if not log.is_file():
        return None
    m = re.search(r"PERF: cycles=(\d+) retired=(\d+)", log.read_text(encoding="utf-8", errors="replace"))
    return {"cycles": int(m.group(1)), "retired": int(m.group(2))} if m else None


def plan_segments(recs) -> list[int]:
    """start instruction numbers so that each segment needs < SEG_SAMPLES qualified samples"""
    starts, start, used, idx = [0], 0, 0, 0
    for i, r in enumerate(recs):
        cost = 1 + sum(2 + (12 if a[0] >= 0x1000_0000 else 0) for a in r.access)
        if used + cost > SEG_SAMPLES:
            start = idx = i - 2           # small overlap
            starts.append(start)
            used = 0
        used += cost
    return starts


# ---------------------------------------------------------------- board test
def tcl_str(p: Path) -> str:
    return "{" + p.as_posix() + "}"


def gen_board_tcl(progs: list[str], plans: dict, resets: int, freq_wait: float) -> str:
    t = [f"source {tcl_str(REPO / 'fpga' / 'kv260' / 'tcl' / 'kv260_hw_lib.tcl')}",
         "hw_open"]
    for prog in progs:
        out = OUT / prog / "out"
        bdir = OUT / "board" / prog
        t += [f"kv prog {prog}",
              f"hw_program {tcl_str(out / 'kv260_soc.bit')} {tcl_str(out / 'kv260_soc.ltx')}",
              "ila_list",
              "kv time [clock microseconds]", "vio_dump poweron",
              f"after {int(freq_wait * 1000)}",
              "kv time [clock microseconds]", "vio_dump freq"]
        for i in range(1, resets + 1):
            t += ["cpu_reset_pulse", "after 100", f"vio_dump run{i}"]
        for k, s in enumerate(plans[prog]):
            t.append(f"ila_trace {tcl_str(bdir / f'trace_seg{k}.csv')} {s}")
        t += [f"ila_tohost {tcl_str(bdir / 'tohost.csv')}", "vio_dump final"]
    t += ["hw_close", "kv done"]
    return "\n".join(t) + "\n"


def parse_kv(log_text: str) -> list[list[str]]:
    return [l[4:].split() for l in log_text.splitlines() if l.startswith("KV: ")]


def read_ila_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    header = [h.strip() for h in rows[0]]
    data = [r for r in rows[1:] if r and not r[0].startswith("Radix")]
    return header, data


def col(header: list[str], suffix: str) -> int:
    # Vivado appends _<n> to a probe name when the net also feeds another debug core
    m = [i for i, h in enumerate(header)
         if re.sub(r"(_\d+)?(\[\d+:\d+\])?$", "", h).endswith(suffix)]
    if len(m) != 1:
        raise KeyError(f"ILA column *{suffix}: {[header[i] for i in m]}")
    return m[0]


# bit fields of the packed ILA buses (fpga/kv260/rtl/kv260_cpu_dbg.v)
CTRL_BITS = {"retire_valid": (0, 1), "rd_we": (1, 1), "trap_valid": (2, 1), "req_valid": (3, 1),
             "req_ready": (4, 1), "req_we": (5, 1), "rsp_valid": (6, 1), "rsp_err": (7, 1),
             "halted": (8, 1), "tohost_wr": (9, 1), "aresetn": (10, 1), "pl_resetn0": (11, 1),
             "trace_event": (12, 1), "axi_hs": (13, 1), "imem_valid": (14, 1), "imem_ready": (15, 1),
             "req_wstrb": (16, 4), "trap_cause": (20, 4), "rd": (24, 5),
             "awvalid": (32, 1), "awready": (33, 1), "wvalid": (34, 1), "wready": (35, 1),
             "bvalid": (36, 1), "bready": (37, 1), "arvalid": (38, 1), "arready": (39, 1),
             "rvalid": (40, 1), "rready": (41, 1), "bresp": (42, 2), "rresp": (44, 2), "wstrb": (46, 4)}
WORDS = {"ila_cpu": ["retire_pc", "retire_insn", "rd_wdata", "req_addr", "req_wdata"],
         "ila_trap": ["trap_pc", "trap_tval", "tohost_value"],
         "ila_axi": ["awaddr", "wdata", "araddr", "rdata"]}


def hexval(s: str) -> int:
    # hardware captures are 0/1 only; X/Z (simulated CSV from tb_kv260_dbg) read as 0
    return int(re.sub(r"[xXzZ]", "0", s), 16)


def ila_samples(path: Path) -> list[dict]:
    """decoded samples of an ILA CSV (one dict per stored sample, in order)"""
    header, rows = read_ila_csv(path)
    c = {k: col(header, k) for k in ("ila_ctrl", "obs_retired", "ila_cpu", "ila_trap", "ila_axi")}
    out = []
    for r in rows:
        ctrl = hexval(r[c["ila_ctrl"]])
        s = {k: (ctrl >> lo) & ((1 << w) - 1) for k, (lo, w) in CTRL_BITS.items()}
        s["retired"] = hexval(r[c["obs_retired"]])
        for bus, names in WORDS.items():
            v = hexval(r[c[bus]])
            for i, n in enumerate(names):
                s[n] = (v >> (32 * i)) & 0xFFFF_FFFF
        out.append(s)
    return out


def ila_to_trace(path: Path) -> tuple[str, int, bool]:
    """RTL-format trace text (M/R/T/E lines, as dv/soc/tb_soc.v writes) from a
    trace capture; returns (text, number of samples, reached the program end)."""
    return samples_to_trace(ila_samples(path))


def ps_samples(records: list[list[int]], regs: dict) -> list[dict]:
    """samples (same keys as ila_samples) from kv260_dbg_axil trace records
    (8 words each, fpga/kv260/rtl/kv260_dbg_axil.v); TOHOST value = data of the
    store to 0x1000_0008, trap pc/tval from the TRAP_PC/TRAP_TVAL registers
    (a trap halts the CPU, so there is at most one per run)"""
    out, tohost = [], 0
    for w in records:
        ctrl = w[0] | ((w[7] >> 16) & 0x3FFF) << 32
        s = {k: (ctrl >> lo) & ((1 << n) - 1) for k, (lo, n) in CTRL_BITS.items()}
        s.update(retired=w[1], retire_pc=w[2], retire_insn=w[3], rd_wdata=w[4], req_addr=w[5],
                 req_wdata=w[6], cycle=w[7] & 0xFFFF, trap_pc=regs.get("TRAP_PC", 0),
                 trap_tval=regs.get("TRAP_TVAL", 0))
        if s["req_valid"] and s["req_ready"] and s["req_we"] and s["req_addr"] & ~3 == 0x1000_0008:
            tohost = s["req_wdata"]
        s["tohost_value"] = tohost
        out.append(s)
    return out


def samples_to_trace(rows: list[dict]) -> tuple[str, int, bool]:
    out, req, tohost, end = [], None, None, False
    for s in rows:
        if s["req_valid"] and s["req_ready"]:
            req = (s["req_addr"], s["req_we"], s["req_wstrb"], s["req_wdata"])
        if s["rsp_valid"] and req is not None:
            out.append(f"M {req[0]:08x} {req[1]} {req[2]:x} {req[3]:08x} 00000000 {s['rsp_err']}")
            req = None
        if s["tohost_wr"] and tohost is None:
            tohost = s["tohost_value"]
        if s["trap_valid"]:
            out.append(f"T {s['trap_cause']} {s['trap_pc']:08x} {s['trap_tval']:08x}")
            out.append("E HALT")
            end = True
            break
        if s["retire_valid"]:
            out.append(f"R {s['retire_pc']:08x} {s['retire_insn']:08x} {s['rd_we']} {s['rd']:02d} {s['rd_wdata']:08x}")
            if tohost is not None:            # the TOHOST store has retired: program end
                out.append(f"E TOHOST {tohost:08x}")
                end = True
                break
    return "\n".join(out) + "\n", len(rows), end


def first_retired(path, rows: list[dict] | None = None) -> int:
    for s in (rows if rows is not None else ila_samples(path)):
        if s["retire_valid"]:
            return s["retired"]
    return -1


def compare_segment(hw_text: str, start: int, iss_recs, iss_end: str) -> tuple[bool, str, int]:
    with tempfile.NamedTemporaryFile("w", suffix=".trace", delete=False, encoding="ascii") as fh:
        fh.write(hw_text)
        tmp = Path(fh.name)
    try:
        recs, end = trace_compare.parse(tmp, "rtl")
    finally:
        tmp.unlink()
    recs = [r for r in recs if r.kind != "?"]          # trailing access without retire (buffer end)
    for k, r in enumerate(recs):
        i = start + k
        if i >= len(iss_recs):
            return False, f"segment {start}: HW record #{i} beyond ISS end", k
        b = iss_recs[i]
        same = (r.kind, r.fields) == (b.kind, b.fields) and (k == 0 or r.access == b.access)
        if not same:
            return False, (f"segment {start}: mismatch at instruction #{i}\n  HW : {trace_compare.describe(r)}\n"
                           f"  ISS: {trace_compare.describe(b)}"), k
    if end != "E <missing>" and end != iss_end:
        return False, f"segment {start}: end '{end}' vs ISS '{iss_end}'", len(recs)
    return True, f"segment {start}: {len(recs)} records identical" + (f", end {end}" if end != "E <missing>" else ""), len(recs)


def vio_values(kvs: list[list[str]], prog: str) -> dict:
    """{tag: {probe suffix: int}} for one program block of the KV output"""
    res: dict = {}
    cur = None
    times = []
    for k in kvs:
        if k[0] == "prog":
            cur = k[1]
        elif cur == prog and k[0] == "vio":
            tag, name, val = k[1], k[2], k[3]
            short = name.split("/")[-1]
            res.setdefault(tag, {})[short] = int(val, 16) if not val.startswith("out=") else val
        elif cur == prog and k[0] == "time":
            times.append(int(k[1]))
    res["_times_us"] = times
    return res


def vget(d: dict, suffix: str):
    m = [v for k, v in d.items() if k.endswith(suffix)]
    return m[0] if len(m) == 1 else None


def analyze_program(prog: str, vv: dict, segs_in: list, tohost_rows: list | None, nresets: int,
                    bdir: Path, freq: tuple | None, label: str) -> dict:
    """Checks shared by the JTAG (VIO/ILA) and PS (kv260_dbg_axil) board tests.
    vv: {tag: {name: int}} per run (poweron, run1..N); segs_in: [(k, start, samples)];
    tohost_rows: samples around the TOHOST write with key "t" (cycle); freq: (cycles, s)."""
    res = {"checks": []}

    def check(name, ok, detail=""):
        res["checks"].append({"name": name, "ok": bool(ok), "detail": detail})

    ref = iss_reference(prog)
    xs = xsim_perf(prog)
    exp_retired = ref[2] if ref else (xs["retired"] if xs else None)
    runs = sorted([t for t in vv if t.startswith("run")], key=lambda s: int(s[3:]))
    for tag in ["poweron"] + runs:
        d = vv.get(tag)
        if not d:
            check(f"{tag}: result read", False, "no data")
            continue
        ft, tw, hl = vget(d, "first_tohost"), vget(d, "tohost_writes"), vget(d, "halted")
        check(f"{tag}: TOHOST first value == 1 (PASS)", ft == 1, f"first_tohost={ft}")
        check(f"{tag}: exactly one TOHOST write", tw == 1, f"tohost_writes={tw}")
        check(f"{tag}: no trap (halted == 0)", hl == 0,
              f"halted={hl} cause={vget(d, 'trap_cause')} pc={vget(d, 'trap_pc')} tval={vget(d, 'trap_tval')}")
        check(f"{tag}: reset released (peripheral_aresetn == 1, pl_resetn0 == 1)",
              vget(d, "peripheral_aresetn") == 1 and vget(d, "pl_resetn0") == 1)
        if exp_retired is not None:
            tr = vget(d, "tohost_retired")
            check(f"{tag}: instructions before TOHOST == reference - 1", tr == exp_retired - 1,
                  f"tohost_retired={tr} reference={exp_retired}")
    if "poweron" in vv:
        check("power-on run: reset_releases == 1", vget(vv["poweron"], "reset_releases") == 1,
              f"reset_releases={vget(vv['poweron'], 'reset_releases')}")
    cyc = [vget(vv[t], "run_cycles") for t in ["poweron"] + runs if t in vv]
    check("run_cycles identical in every run", len(set(cyc)) == 1, f"run_cycles={sorted(set(cyc))}")
    rr = [vget(vv[f"run{i}"], "reset_releases") for i in range(1, nresets + 1) if f"run{i}" in vv]
    check(f"each {label} reset = one reset release", rr == list(range(2, 2 + len(rr))) and len(rr) == nresets,
          f"reset_releases={rr}")
    res["run_cycles"] = cyc[0] if cyc else None
    res["xsim"] = xs
    if xs and cyc:
        res["run_cycles_minus_xsim"] = cyc[0] - xs["cycles"]
    if freq:
        dc, dt = freq
        res["pl_clk0_mhz_measured"] = dc / dt / 1e6
        check("pl_clk0 measured 100 MHz +-1 %", abs(dc / dt / 1e6 - 100.0) < 1.0,
              f"{dc} cycles in {dt:.3f} s = {dc / dt / 1e6:.4f} MHz")
    # instruction trace
    segs = []
    for k, s, rows in segs_in:
        if rows is None:
            check(f"trace segment {k} captured", False)
            continue
        hw_text, nsamp, reached_end = samples_to_trace(rows)
        (bdir / prog / f"trace_seg{k}.trace").write_text(hw_text, encoding="ascii")
        fr = first_retired(None, rows)
        check(f"trace segment {k}: starts at instruction #{s}", fr == s, f"first retired index {fr}")
        if ref:
            ok, msg, n = compare_segment(hw_text, s, ref[0], ref[1])
            check(f"trace segment {k}: trace == ISS", ok, msg)
            segs.append({"start": s, "records": n, "samples": nsamp, "end": reached_end})
        else:
            segs.append({"start": s, "samples": nsamp, "end": reached_end,
                         "records": hw_text.count("\nR ") + hw_text.startswith("R ")})
    res["segments"] = segs
    if ref:
        covered = set()
        for sg in segs:
            covered.update(range(sg["start"], sg["start"] + sg["records"]))
        check("trace segments cover every instruction up to TOHOST",
              covered >= set(range(len(ref[0]))) and any(sg["end"] for sg in segs),
              f"{len(covered & set(range(len(ref[0]))))}/{len(ref[0])} records")
    elif segs:
        icarus = REPO / "build" / "sim" / f"soc__c__{prog}" / "rtl.trace"
        hw = (bdir / prog / "trace_seg0.trace")
        if icarus.is_file():
            # Icarus tb_soc has no SmartConnect: same instructions, different
            # timing, so only values derived from the SYSCTRL cycle counter differ
            a, a_end = trace_compare.parse(hw, "rtl")
            b, b_end = trace_compare.parse(icarus, "rtl")
            seq = [i for i, (x, y) in enumerate(zip(a, b)) if (x.kind, x.fields[:3], [acc[:3] for acc in x.access])
                   != (y.kind, y.fields[:3], [acc[:3] for acc in y.access])]
            check(f"{prog}: HW instruction sequence == Icarus SoC trace (pc, insn, rd_we, data addresses)",
                  not seq and len(a) == len(b) and a_end == b_end,
                  f"HW {len(a)} records {a_end}, Icarus {len(b)} {b_end}, first difference {seq[:1]}")
            vdiff = [f"#{i} pc={x.fields[0]:08x} HW={x.fields[4]} Icarus={y.fields[4]}"
                     for i, (x, y) in enumerate(zip(a, b)) if x.fields != y.fields]
            res["sysctrl_value_differences"] = vdiff
            check(f"{prog}: rd-value differences only after CYCLE_LO reads (listed in board_summary.json)",
                  len(vdiff) <= 4, f"{len(vdiff)} differences: {vdiff}")
    # TOHOST AXI4-Lite transaction (CPU side of the SmartConnect)
    if tohost_rows:
        rows = tohost_rows
        hs = {ch: [s for s in rows if s[f"{ch}valid"] and s[f"{ch}ready"]] for ch in ("aw", "w", "b", "ar", "r")}
        trig = next((s["t"] for s in rows if s["tohost_wr"]), None)
        aw = [(s["t"], s["awaddr"]) for s in hs["aw"]]
        wd = [(s["t"], s["wdata"], s["wstrb"]) for s in hs["w"]]
        br = [(s["t"], s["bresp"]) for s in hs["b"]]
        # the TOHOST transaction: the CPU-side AW/W handshakes nearest to the
        # SYSCTRL write (the SmartConnect returns AWREADY/WREADY one cycle
        # after SYSCTRL has performed the write), then the first B after AW
        near = lambda lst: sorted(lst, key=lambda e: abs(e[0] - trig))[:1] if trig is not None else []
        last_aw, last_w = near(aw), near(wd)
        first_b = [b for b in br if last_aw and b[0] > last_aw[0][0]][:1]
        rel = (lambda x: x - trig) if trig is not None else (lambda x: x)
        res["tohost_axi"] = {
            "trigger_cycle": trig,
            "aw": [(rel(i), hex(a)) for i, a in aw], "w": [(rel(i), hex(d), s) for i, d, s in wd],
            "b": [(rel(i), b) for i, b in br], "ar_count": len(hs["ar"]), "r_count": len(hs["r"])}
        check("AXI4-Lite TOHOST write AW 0x10000008 / W 0x00000001 strb f / B OKAY",
              bool(last_aw and last_w and first_b) and last_aw[-1][1] == 0x1000_0008
              and last_w[-1][1] == 1 and last_w[-1][2] == 0xF and first_b[0][1] == 0,
              json.dumps(res["tohost_axi"]))
    res["failed"] = sum(not c["ok"] for c in res["checks"])
    return res


def print_result(prog: str, res: dict, verbose: bool) -> None:
    nf = res["failed"]
    print(f"== {prog}: {len(res['checks']) - nf}/{len(res['checks'])} checks passed"
          f"  run_cycles={res.get('run_cycles')} xsim={res.get('xsim')}  f={res.get('pl_clk0_mhz_measured')}")
    for c in res["checks"]:
        if not c["ok"] or verbose:
            print(f"   {'ok  ' if c['ok'] else 'FAIL'} {c['name']}  {c['detail']}")


def cmd_board(ns) -> int:
    bdir = OUT / "board"
    bdir.mkdir(parents=True, exist_ok=True)
    plans = {}
    for prog in ns.progs:
        if not (OUT / prog / "out" / "kv260_soc.bit").is_file():
            sys.exit(f"no bitstream for {prog}: run 'build' first")
        (bdir / prog).mkdir(parents=True, exist_ok=True)
        ref = iss_reference(prog)
        plans[prog] = plan_segments(ref[0]) if ref else [0]
    tcl = bdir / "board_test.tcl"
    log = bdir / "board_test.log"
    if ns.analyze_only:                       # re-check the last session's captures
        rc = 0
    else:
        tcl.write_text(gen_board_tcl(ns.progs, plans, ns.resets, ns.freq_wait), encoding="utf-8")
        print(f"running Hardware Manager session ({len(ns.progs)} programs) ...", flush=True)
        rc = vivado(tcl, [], log, 3600)
    text = log.read_text(encoding="utf-8", errors="replace")
    kvs = parse_kv(text)
    if rc != 0 or ["done"] not in kvs:
        errs = re.findall(r"^ERROR: .*$", text, re.M)
        print(f"Hardware Manager session failed (rc={rc}): {errs[:5]}")
    summary, fails = {}, 0
    for prog in ns.progs:
        vv = vio_values(kvs, prog)
        freq = None
        if "freq" in vv and "poweron" in vv and len(vv["_times_us"]) >= 2:
            freq = ((vget(vv["freq"], "free_cycles") - vget(vv["poweron"], "free_cycles")) & 0xFFFF_FFFF,
                    (vv["_times_us"][1] - vv["_times_us"][0]) * 1e-6)
        segs = []
        for k, s in enumerate(plans[prog]):
            p = bdir / prog / f"trace_seg{k}.csv"
            segs.append((k, s, ila_samples(p) if p.is_file() else None))
        p = bdir / prog / "tohost.csv"
        trows = None
        if p.is_file():
            trows = ila_samples(p)
            for i, s in enumerate(trows):
                s["t"] = i
        runs = {t: v for t, v in vv.items() if t == "poweron" or t.startswith("run")}
        res = analyze_program(prog, runs, segs, trows, ns.resets, bdir, freq, "VIO")
        fails += res["failed"]
        summary[prog] = res
        print_result(prog, res, ns.verbose)
    (OUT / "board_summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return 1 if fails or rc != 0 else 0


# ---------------------------------------------------------------- PS variant board test
def ps_run_values(regs: dict) -> dict:
    """kv260_dbg_axil registers -> the names analyze_program expects (as the VIO probes)"""
    st = regs.get("STATUS", 0)
    return {"first_tohost": regs.get("FIRST_TOHOST"), "tohost_writes": regs.get("TOHOST_WRITES"),
            "halted": (st >> 1) & 1, "trap_cause": regs.get("TRAP_CAUSE"), "trap_pc": regs.get("TRAP_PC"),
            "trap_tval": regs.get("TRAP_TVAL"), "peripheral_aresetn": (st >> 2) & 1, "pl_resetn0": (st >> 3) & 1,
            "tohost_retired": regs.get("TOHOST_RETIRED"), "reset_releases": regs.get("RESET_RELEASES"),
            "run_cycles": regs.get("RUN_CYCLES"), "free_cycles": regs.get("FREE_CYCLES")}


def parse_ps_result(text: str) -> dict:
    out = {"runs": {}, "segs": [], "freq": None, "scratch": None, "state": ""}
    cur = None
    for line in text.splitlines():
        tok = line.split()
        if not tok:
            continue
        if tok[0] == "RUN":
            out["runs"].setdefault(tok[1], {})[tok[2]] = int(tok[3], 16)
        elif tok[0] == "FREQ":
            out["freq"] = (int(tok[1]), float(tok[2]))
        elif tok[0] == "SCRATCH":
            out["scratch"] = int(tok[1], 16)
        elif tok[0] == "STATE":
            out["state"] = line[6:]
        elif tok[0] == "SEG":
            cur = {"start": int(tok[1]), "count": int(tok[2]), "regs": {}, "recs": []}
            out["segs"].append(cur)
        elif tok[0] == "SEGREG":
            cur["regs"][tok[2]] = int(tok[3], 16)
        elif tok[0] == "REC":
            cur["recs"].append([int(x, 16) for x in tok[1:9]])
    return out


def ssh(cmd: str, timeout: int = 600) -> tuple[int, str]:
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", BOARD, cmd],
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def ps_rows(sg: dict | None) -> list | None:
    """samples of one PS trace segment, with AXI address/data taken from the CMI
    request of the IO access and the 16-bit cycle stamp unwrapped into "t" """
    if not sg:
        return None
    rows = ps_samples(sg["recs"], sg["regs"])
    base, prev = 0, None
    for s in rows:
        s["awaddr"], s["wdata"], s["wstrb"] = s["req_addr"], s["req_wdata"], s["req_wstrb"]
        if prev is not None and s["cycle"] < prev:
            base += 1 << 16
        prev = s["cycle"]
        s["t"] = base + s["cycle"]
    return rows


def board_state() -> dict:
    """read-only snapshot of the board items a load could leave changed"""
    rc, o = ssh(f"sudo python3 {BOARD_DIR}/kv260_ps_board.py check")
    st = {}
    for line in o.splitlines():
        tok = line.split(None, 2)
        if len(tok) >= 2 and tok[0] == "STATE":
            st[tok[1]] = tok[2] if len(tok) > 2 else ""
    return st


def compare_state(before: dict, after: dict) -> list[str]:
    keys = ("fpga_state", "fpga_flags", "overlays", "fw_path", "dfx_apps", "AFI_FS", "PL0_REF_CTRL")
    return [f"{k}: before '{before.get(k)}' now '{after.get(k)}'" for k in keys if before.get(k) != after.get(k)]


def cmd_psboard(ns) -> int:
    bdir = OUT / "board"
    bdir.mkdir(parents=True, exist_ok=True)
    dtbo = OUT / "rv32_6a.dtbo"
    plans = {}
    for prog in ns.progs:
        if not (OUT / prog / "out" / "kv260_soc.bit.bin").is_file() and not ns.dry_run:
            sys.exit(f"no kv260_soc.bit.bin for {prog}: run 'build --variant {VARIANT}' first")
        (bdir / prog).mkdir(parents=True, exist_ok=True)
        ref = iss_reference(prog)
        plans[prog] = plan_segments(ref[0]) if ref else [0]
    script = f"sudo python3 {BOARD_DIR}/kv260_ps_board.py"
    steps = [("copy", f"mkdir -p {BOARD_DIR}/fw (no sudo); scp kv260_ps_board.py, rv32_6a.dtbo, "
                      + ", ".join(f"{p}.bit.bin" for p in ns.progs) + f" -> ~/{BOARD_DIR}/"),
             ("ssh", f"{script} check"),
             ("ssh", "sudo xmutil unloadapp")]
    for prog in ns.progs:
        steps += [("ssh", f"{script} load {prog}.bit.bin"),
                  ("ssh", f"{script} test --resets {ns.resets} --freq-wait {ns.freq_wait} "
                          f"--run-wait {RUN_WAIT.get(prog, 0.02)} "
                          f"--starts {','.join(str(s) for s in plans[prog])} --out {BOARD_DIR}/result_{prog}.txt"),
                  ("ssh", f"{script} unload")]
    if not ns.no_restore:
        steps.append(("ssh", "sudo xmutil loadapp k26-starter-kits"))
    steps.append(("fetch", f"scp ~/{BOARD_DIR}/result_*.txt -> {bdir.relative_to(REPO).as_posix()}/"))
    if ns.dry_run:
        for kind, c in steps:
            print(f"[{kind}] {c}")
        return 0
    if not ns.analyze_only:
        subprocess.run([str(DTC), "-@", "-I", "dts", "-O", "dtb", "-o", str(dtbo),
                        str(REPO / "fpga" / "kv260" / "dts" / "rv32_6a.dts")], check=True, capture_output=True)
        lf = bdir / "kv260_ps_board.py"              # copy with LF line ends
        lf.write_bytes((REPO / "scripts" / "fpga" / "kv260_ps_board.py").read_bytes().replace(b"\r\n", b"\n"))
        log = []
        before = None
        for kind, c in steps:
            rc = 0
            if kind == "copy":
                rc, o = ssh(f"mkdir -p {BOARD_DIR}/fw")
                files = [(lf, f"{BOARD_DIR}/kv260_ps_board.py"),
                         (dtbo, f"{BOARD_DIR}/fw/rv32_6a.dtbo")]
                files += [(OUT / p / "out" / "kv260_soc.bit.bin", f"{BOARD_DIR}/fw/{p}.bit.bin") for p in ns.progs]
                for src, dst in files:
                    r = subprocess.run(["scp", "-o", "BatchMode=yes", str(src), f"{BOARD}:{dst}"],
                                       capture_output=True, text=True)
                    if r.returncode:
                        print(f"scp failed: {src}: {r.stderr.strip()}")
                        return 1
                o = f"copied {len(files)} files"
            elif kind == "fetch":
                for p in ns.progs:
                    subprocess.run(["scp", "-o", "BatchMode=yes", f"{BOARD}:{BOARD_DIR}/result_{p}.txt",
                                    str(bdir / p / "result.txt")], capture_output=True, text=True)
                o = "fetched"
            else:
                rc, o = ssh(c)
            if kind == "ssh" and c.endswith(" check") and before is None:
                before = {}
                for line in o.splitlines():
                    tok = line.split(None, 2)
                    if len(tok) >= 2 and tok[0] == "STATE":
                        before[tok[1]] = tok[2] if len(tok) > 2 else ""
            log.append(f"$ {c}\n{o}\n")
            print(f"[{kind}] {c}\n    {o[-600:]}", flush=True)
            if kind == "ssh" and rc != 0 and "unloadapp" not in c:
                print("board step failed; removing the overlay and restoring the app")
                ssh(f"{script} unload")
                if not ns.no_restore:
                    ssh("sudo xmutil loadapp k26-starter-kits")
                diff = compare_state(before or {}, board_state())
                for d in diff:
                    print(f"UNRESTORED {d}")
                log += [f"UNRESTORED {d}" for d in diff]
                (bdir / "psboard.log").write_text("\n".join(log), encoding="utf-8")
                return 1
        diff = compare_state(before or {}, board_state())
        for d in diff:
            print(f"UNRESTORED {d}")
        log += [f"UNRESTORED {d}" for d in diff] or ["STATE restored: all compared items as before"]
        (bdir / "psboard.log").write_text("\n".join(log), encoding="utf-8")
    summary, fails = {}, 0
    for prog in ns.progs:
        f = bdir / prog / "result.txt"
        if not f.is_file():
            print(f"FAIL {prog}: no result file")
            fails += 1
            continue
        r = parse_ps_result(f.read_text(encoding="utf-8"))
        vv = {t: ps_run_values(v) for t, v in r["runs"].items()}
        by_start = {sg["start"]: sg for sg in r["segs"]}
        segs, trows = [], None
        for k, s in enumerate(plans[prog]):
            rows = ps_rows(by_start.get(s))
            if rows and any(x["tohost_wr"] for x in rows):
                trows = rows
            segs.append((k, s, rows))
        res = analyze_program(prog, vv, segs, trows, ns.resets, bdir, r["freq"], "CTRL.cpu_reset")
        res["checks"].append({"name": "PS write/read of the debug SCRATCH register",
                              "ok": r["scratch"] == 0x5A5A_A5A5,
                              "detail": f"{r['scratch']:08x}" if r["scratch"] is not None else "missing"})
        res["failed"] = sum(not c["ok"] for c in res["checks"])
        res["board_state"] = r["state"]
        fails += res["failed"]
        summary[prog] = res
        print_result(prog, res, ns.verbose)
    (OUT / "board_summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return 1 if fails else 0


def sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


# untracked files that cannot reach a bitstream or a board package (documentation only); any other
# untracked file and every tracked change mark the build dirty
DOC_ONLY_UNTRACKED = ("docs/",)


def git_identity(repo: Path = REPO) -> dict:
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True).stdout
    import hashlib
    status = g("status", "--porcelain", "--untracked-files=all").splitlines()
    docs = [l[3:] for l in status if l.startswith("?? ") and l[3:].startswith(DOC_ONLY_UNTRACKED)]
    changes = [l for l in status if not (l.startswith("?? ") and l[3:] in docs)]
    diff = subprocess.run(["git", "-C", str(repo), "diff", "HEAD"], capture_output=True).stdout
    return {"commit": g("rev-parse", "HEAD").strip(), "branch": g("rev-parse", "--abbrev-ref", "HEAD").strip(),
            "dirty": bool(changes), "changes": changes, "untracked_docs": docs,
            "worktree_diff_sha256": hashlib.sha256(diff).hexdigest() if changes else None}


def addr_consistency() -> list[tuple[str, bool, str]]:
    """one register map: soc_addr_map.vh vs firmware header, simulation decoder,
    BD address segments of every built E3c bitstream, xsim BD"""
    vh = (REPO / "rtl" / "soc" / "soc_addr_map.vh").read_text(encoding="utf-8")
    m = {n: int(v.replace("_", ""), 16) for n, v in re.findall(r"`define SOC_(\w+) 32'h([0-9A-Fa-f_]+)", vh)}
    win = {k: (m[f"{k}_BASE"], m[f"{k}_SIZE"]) for k in ("SYSCTRL", "DSP", "CNN", "REPLAY")}
    res = []
    hdr = (REPO / "firmware" / "rv32" / "e3c" / "rv32_e3c.h").read_text(encoding="utf-8")
    fw = {"DSP": "DSP_BASE", "CNN": "CNN_BASE", "REPLAY": "RPL_BASE"}
    for k, macro in fw.items():
        mm = re.search(rf"#define {macro}\s+0x([0-9A-Fa-f]+)u", hdr)
        ok = bool(mm) and int(mm.group(1), 16) == win[k][0]
        res.append((f"firmware {macro} == SOC_{k}_BASE", ok, f"0x{win[k][0]:08X}"))
    soc_h = (REPO / "firmware" / "rv32" / "bsp" / "rv32_soc.h").read_text(encoding="utf-8")
    mm = re.search(r"#define SYSCTRL_BASE\s+0x([0-9A-Fa-f]+)u", soc_h)
    res.append(("firmware SYSCTRL_BASE == SOC_SYSCTRL_BASE", bool(mm) and int(mm.group(1), 16) == win["SYSCTRL"][0], ""))
    tb = (REPO / "dv" / "e3c" / "tb_e3c_soc.v").read_text(encoding="utf-8")
    mb = re.search(r"\.BASE\(\{32'h([0-9A-F_]+), 32'h([0-9A-F_]+), 32'h([0-9A-F_]+), 32'h([0-9A-F_]+)\}\)", tb)
    tbb = [int(x.replace("_", ""), 16) for x in mb.groups()][::-1] if mb else []
    res.append(("Icarus decoder windows == soc_addr_map.vh", tbb == [win[k][0] for k in ("SYSCTRL", "DSP", "CNN", "REPLAY")],
                ", ".join(f"0x{b:08X}" for b in tbb)))
    sims = [REPO / "build" / "vivado" / "e3c" / "sim" / "reports" / "bd_summary.txt"]
    # (E5a: no replay window on the CPU side, e5a_consistency)
    for f in sims + sorted((REPO / "build" / "vivado" / "kv260_e3c").glob("*/out/bd_summary.txt")) + \
            sorted((REPO / "build" / "vivado" / "kv260_e4a").glob("*/out/bd_summary.txt")):
        if not f.is_file():
            continue
        segs = {(int(o, 16), int(r, 16)) for o, r in re.findall(r"^addr \S+ offset=0x([0-9A-Fa-f]+) range=0x([0-9A-Fa-f]+)",
                                                             f.read_text(encoding="utf-8"), re.M)}
        ok = segs == set(win.values())
        res.append((f"BD segments {f.relative_to(REPO).as_posix()} == router windows", ok,
                    ", ".join(f"0x{o:08X}+0x{r:X}" for o, r in sorted(segs))))
    return res


def mbx_consistency(prog: str) -> list[tuple[str, bool, str]]:
    """E4a mailbox: one address / layout source (soc_addr_map.vh, protocol.py) in the firmware
    header, the PS reader config, the xsim BD and this bitstream's BD; BRAM configuration,
    CDC report, hardware handoff"""
    vh = (REPO / "rtl" / "soc" / "soc_addr_map.vh").read_text(encoding="utf-8")
    m = {n: int(v.replace("_", ""), 16) for n, v in re.findall(r"`define (\w+) 32'h([0-9A-Fa-f_]+)", vh)}
    res = []
    hdr = (REPO / "firmware" / "rv32" / "mailbox" / "mbx_layout.h").read_text(encoding="utf-8")
    mm = re.search(r"#define MBX_BASE\s+0x([0-9A-Fa-f]+)u", hdr)
    res.append(("firmware MBX_BASE == SOC_MBX_BASE", bool(mm) and int(mm.group(1), 16) == m["SOC_MBX_BASE"],
                f"0x{m['SOC_MBX_BASE']:08X}"))
    cfg = json.loads((REPO / "host" / "ps_mailbox" / "configs" / "kv260_mailbox.json").read_text(encoding="utf-8"))
    res.append(("PS reader config ps_base == KV260_MBX_PS_BASE", int(cfg["ps_base"], 16) == m["KV260_MBX_PS_BASE"],
                cfg["ps_base"]))
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "gen" / "gen_mailbox.py"), "--check"],
                       capture_output=True, text=True)
    res.append(("generated mailbox constants up to date (gen_mailbox.py --check)", r.returncode == 0,
                r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""))
    want = (m["KV260_MBX_PS_BASE"], m["KV260_MBX_PS_SIZE"])
    # the first file is the stand-alone xsim BD of the mailbox: ps_e6a has none, its own BD is what is checked
    for f, pat in (((REPO / "build" / "vivado" / "mbx" / "sim" / "reports" / "bd_summary.txt", r"^addr ps (\S+)"),) * (VARIANT != "ps_e6a")
                   + ((OUT / prog / "out" / "bd_summary.txt", r"^ps_addr (\S+)"),)):
        text = f.read_text(encoding="utf-8") if f.is_file() else ""
        segs = {(int(o, 16), int(rr, 16)) for n, o, rr in re.findall(pat + r" offset=0x([0-9A-Fa-f]+) range=0x([0-9A-Fa-f]+)",
                                                                       text, re.M) if "mbx" in n}
        res.append((f"PS mailbox segment {f.relative_to(REPO).as_posix()} == KV260_MBX_PS_*", segs == {want},
                    ", ".join(f"0x{o:08X}+0x{s:X}" for o, s in sorted(segs))))
    bd = (OUT / prog / "out" / "bd_summary.txt").read_text(encoding="utf-8")
    if VARIANT == "ps_e6a":
        src = {(int(o, 16), int(rr, 16)) for n, o, rr in re.findall(r"^ps_addr (\S+) offset=0x([0-9A-Fa-f]+) range=0x([0-9A-Fa-f]+)", bd, re.M)
               if "e3c" in n}
        res.append(("SPI source page == host/spi_live BASE, SIZE (0xA013_0000, 4 KB), the only e3c PS segment", src == {(0xA0130000, 0x1000)},
                    ", ".join(f"0x{o:08X}+0x{r:X}" for o, r in sorted(src))))
    dbg = re.findall(r"^ps_addr (\S+) offset=0x([0-9A-Fa-f]+) range=0x([0-9A-Fa-f]+)", bd, re.M)
    res.append(("debug window unchanged 0xA000_0000 + 256 KB",
                any("dbg" in n and int(o, 16) == 0xA0000000 and int(rr, 16) == 0x40000 for n, o, rr in dbg),
                str([(n, o, rr) for n, o, rr in dbg])))
    cfgm = dict(re.findall(r"^mbx_mem CONFIG\.(\S+) = (.*)$", bd, re.M))
    res.append(("mailbox BRAM: true dual port, BRAM_Controller, READ_FIRST both ports, 32 x 1024",
                cfgm.get("Memory_Type") == "True_Dual_Port_RAM" and cfgm.get("use_bram_block") == "BRAM_Controller"
                and cfgm.get("Operating_Mode_A") == "READ_FIRST" and cfgm.get("Operating_Mode_B") == "READ_FIRST"
                and cfgm.get("Write_Width_A") == "32" and cfgm.get("Write_Depth_A") == "1024", str(cfgm)))
    cdc = OUT / prog / "out" / "reports" / "cdc.rpt"
    text = cdc.read_text(encoding="utf-8", errors="replace") if cdc.is_file() else ""
    unsafe = re.findall(r"Unsafe\s*\|\s*(\d+)", text)
    res.append(("CDC report: no unsafe crossings", cdc.is_file() and all(int(u) == 0 for u in unsafe),
                f"unsafe counts {unsafe}" if cdc.is_file() else "no cdc.rpt"))
    hwh = OUT / prog / "out" / "kv260_soc.hwh"
    res.append(("hardware handoff kv260_soc.hwh exported", hwh.is_file(), sha256(hwh) if hwh.is_file() else "missing"))
    return res


def e5a_consistency(prog: str, img: list[int]) -> tuple[list, dict]:
    """E5a: the data path of the block design against soc_addr_map.vh and host/ps_dma, and the evidence that the data
    set is not in the bitstream"""
    sys.path.insert(0, str(REPO / "host"))
    from ps_dma import registers as R               # noqa: PLC0415
    from ps_mailbox.overlay_device import hwh_ranges  # noqa: PLC0415
    vh = (REPO / "rtl" / "soc" / "soc_addr_map.vh").read_text(encoding="utf-8")
    m = {n: int(v.replace("_", ""), 16) for n, v in re.findall(r"`define (\w+) 32'h([0-9A-Fa-f_]+)", vh)}
    o = OUT / prog / "out"
    bd = (o / "bd_summary.txt").read_text(encoding="utf-8")
    res = []
    cpu = {(int(a, 16), int(b, 16)) for a, b in re.findall(r"^addr \S+ offset=0x([0-9A-Fa-f]+) range=0x([0-9A-Fa-f]+)", bd, re.M)}
    res.append(("CPU segments == SYSCTRL, DSP, CNN (no replay page)",
                cpu == {(m[f"SOC_{k}_BASE"], m[f"SOC_{k}_SIZE"]) for k in ("SYSCTRL", "DSP", "CNN")},
                ", ".join(f"0x{a:08X}+0x{b:X}" for a, b in sorted(cpu))))
    res.append(("source of the pipeline: emg_e5_subsys, no esi_replay / emg_e3c_subsys",
                bool(re.search(r"^cell e3c .*emg_e5_subsys", bd, re.M)) and "emg_e3c_subsys" not in bd, ""))
    rng = {r["modtype"]: r for r in hwh_ranges(o / "kv260_soc.hwh")}
    dma, src = rng.get("axi_dma"), rng.get("emg_e5_subsys")
    res.append(("HWH: AXI DMA at KV260_DMA_PS_*, == host/ps_dma/registers.py",
                bool(dma) and (dma["base"], dma["size"]) == (m["KV260_DMA_PS_BASE"], m["KV260_DMA_PS_SIZE"]) == (R.DMA_BASE, R.DMA_SIZE),
                str(dma and (hex(dma["base"]), hex(dma["size"])))))
    res.append(("HWH: source page at KV260_ESRC_PS_*, == host/ps_dma/registers.py",
                bool(src) and (src["base"], src["size"]) == (m["KV260_ESRC_PS_BASE"], m["KV260_ESRC_PS_SIZE"]) == (R.SRC_BASE, R.SRC_SIZE),
                str(src and (hex(src["base"]), hex(src["size"])))))
    dmac = dict(re.findall(r"^axi_dma CONFIG\.(\S+) = (.*)$", bd, re.M))
    res.append(("AXI DMA: simple mode, MM2S only, 128-bit memory side, 64-bit stream, no DRE, 26-bit length",
                dmac.get("c_include_sg") == "0" and dmac.get("c_include_mm2s") == "1" and dmac.get("c_include_s2mm") == "0"
                and dmac.get("c_m_axi_mm2s_data_width") == "128" and dmac.get("c_m_axis_mm2s_tdata_width") == "64"
                and dmac.get("c_include_mm2s_dre") == "0" and dmac.get("c_sg_length_width") == "26", str(dmac)))
    bdv = dict(re.findall(r"^ps (\S+) = (.*)$", bd, re.M))
    res.append(("S_AXI_HP0_FPD enabled, 128 bit", bdv.get("PSU__USE__S_AXI_GP2") == "1" and bdv.get("PSU__SAXIGP2__DATA_WIDTH") == "128",
                f"{bdv.get('PSU__USE__S_AXI_GP2')} / {bdv.get('PSU__SAXIGP2__DATA_WIDTH')}"))
    dsegs = re.findall(r"^dma_addr (\S+) offset=0x([0-9A-Fa-f]+) range=0x([0-9A-Fa-f]+)", bd, re.M)
    res.append(("DMA master reaches DDR segments only (HP0_DDR_LOW, HP0_DDR_HIGH)",
                len(dsegs) == 2 and all("DDR" in n for n, _, _ in dsegs), str(dsegs)))
    # the encoding of the PS-side width and what a design with this PS configuration programs at boot
    psu = list((OUT / prog).glob("kv260_soc.gen/sources_1/bd/kv260_bd/ip/*zynq_ultra_ps_e_0_0/psu_init.c"))
    text = psu[0].read_text(encoding="utf-8", errors="replace") if psu else ""
    mm = re.search(r"PSU_AFIFM2_AFIFM_RDCTRL_FABRIC_WIDTH\s+(0x[0-9a-fA-F]+|\d+)", text)
    res.append(("psu_init: AFIFM2 RDCTRL FABRIC_WIDTH of this design == host/ps_dma/device.py table (128 bit)",
                bool(mm) and int(mm.group(1), 0) == 0, mm.group(1) if mm else "not found"))
    # nothing of the data set in the CPU image: far below one 600-frame segment, no frame table symbol
    words = sum(1 for w in img if w)
    res.append(("firmware image holds no frames: < 4 KB of non-zero words (one 600-frame segment = 4.8 KB)", words * 4 < 4096,
                f"{words * 4} bytes"))
    nm = VIVADO_ROOT.parent / "gnu" / "riscv" / "nt" / "bin" / "riscv64-unknown-elf-nm.exe"
    syms = subprocess.run([str(nm), str(fw_dir(prog) / f"{prog}.elf")], capture_output=True, text=True).stdout
    res.append(("firmware has no frame table symbol, no replay access helpers linked",
                bool(syms) and "mg6_frames" not in syms, f"{len(syms.splitlines())} symbols"))
    fwdir = REPO / "firmware" / "rv32" / "dma"
    sec = {"dma_ps_base": f"0x{R.DMA_BASE:08X}", "source_ps_base": f"0x{R.SRC_BASE:08X}", "axi_dma": dmac,
           "hp0_data_width": bdv.get("PSU__SAXIGP2__DATA_WIDTH"), "hp0_afi_rdctrl_fabric_width": mm.group(1) if mm else None,
           "data_in_bitstream": False, "data_loaded_by": "host/ps_dma (PS, at run time)",
           "dma_common.h_sha256": sha256(fwdir / "dma_common.h"),
           "registers.py_sha256": sha256(REPO / "host" / "ps_dma" / "registers.py"),
           "dataset_config_sha256": sha256(REPO / "host" / "ps_dma" / "configs" / "e2_ref_fold.json")}
    return res, sec


def cmd_manifest(ns) -> int:
    fails = 0
    gid = git_identity()
    model = REPO / "build" / "e3" / "vec" / "model"
    for prog in ns.progs:
        o = OUT / prog / "out"
        if not (o / "kv260_soc.bit").is_file():
            print(f"FAIL {prog}: no bitstream"); fails += 1; continue
        summ = read_summary(prog)
        bd = (o / "bd_summary.txt").read_text(encoding="utf-8")
        bdv = dict(re.findall(r"^ps (\S+) = (.*)$", bd, re.M))
        rep_ = prog_report(prog)
        hexf = fw_dir(prog) / f"{prog}.hex"
        init_file = Path(summ.get("init_file", ""))
        checks = []
        checks.append(("bitstream INIT_FILE is this program's image", init_file.resolve() == hexf.resolve(),
                       str(init_file)))
        checks.append(("part / board", "xck26-sfvc784-2LV-c" in bd and "kv260_som:part0:1.4" in bd, bd.splitlines()[0]))
        checks.append(("HPM0 data width 32", bdv.get("PSU__MAXIGP0__DATA_WIDTH") == "32", bdv.get("PSU__MAXIGP0__DATA_WIDTH")))
        checks.append(("PL0 100 MHz from IOPLL", bdv.get("PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ") == "100"
                       and bdv.get("PSU__CRL_APB__PL0_REF_CTRL__SRCSEL") == "IOPLL",
                       f"actual {bdv.get('PSU__CRL_APB__PL0_REF_CTRL__ACT_FREQMHZ')} MHz"))
        rst = re.findall(r"^(rst_pl0|rst_ps) (C_\w+) = (\d)", bd, re.M)
        checks.append(("resets: rst_pl0 ext low/aux high (dbg cpu_reset), rst_ps ext low",
                       ("rst_pl0", "C_EXT_RESET_HIGH", "0") in rst and ("rst_pl0", "C_AUX_RESET_HIGH", "1") in rst
                       and ("rst_ps", "C_EXT_RESET_HIGH", "0") in rst, str(rst)))
        timing_ok = rep_.get("timing_met") and float(summ.get("wns", "-1")) > 0 and float(summ.get("tns", "-1")) == 0.0
        checks.append(("timing met (WNS > 0, TNS = 0), 0 unconstrained", bool(timing_ok) and
                       rep_.get("check_timing", {}).get("unconstrained_internal") == 0,
                       f"WNS {summ.get('wns')} TNS {summ.get('tns')}"))
        checks.append(("DRC errors 0", summ.get("drc_errors") == "0", summ.get("drc_errors")))
        # BRAM contents of this routed design
        dcp = OUT / prog / "kv260_soc.runs" / "impl_1" / "kv260_bd_wrapper_routed.dcp"
        dump = o / "bram_dump.txt"
        vivado(REPO / "fpga" / "kv260" / "tcl" / "kv260_bram_dump.tcl", [dcp.as_posix(), dump.as_posix()],
               OUT / f"bramdump_{prog}.log", 1800)
        img = bram_image(dump)
        exp = [int(l, 16) for l in hexf.read_text(encoding="ascii").split()]
        exp += [0] * (len(img) - len(exp))
        checks.append((f"CPU BRAM INIT == {hexf.name} (current firmware)", img == exp,
                       f"{sum(a != b for a, b in zip(img, exp))} words differ"))
        e4c_sec = None
        if prog in E4C_PROGRAMS:
            e4c_c, e4c_sec = e4c_trace(prog, img)
            checks += e4c_c
        if e3c_like():
            checks += [(f"E3c BRAM {w}", g, d) for w, g, d in e3c_bram_check(prog)]
            if VARIANT not in ("ps_e5a", "ps_e6a"):
                checks += [(f"address map: {w}", g, d) for w, g, d in addr_consistency()]
        if VARIANT in ("ps_e4a", "ps_e5a", "ps_e6a"):
            checks += [(f"mailbox: {w}", g, d) for w, g, d in mbx_consistency(prog)]
        e5a_sec = None
        if VARIANT == "ps_e5a":
            c5, e5a_sec = e5a_consistency(prog, img)
            checks += [(f"E5a: {w}", g, d) for w, g, d in c5]
        man = {
            "bitstream": {"name": f"{prog}.bit.bin", "bit": str(o / "kv260_soc.bit"),
                          "bit_sha256": sha256(o / "kv260_soc.bit"),
                          "bit_bin_sha256": sha256(o / "kv260_soc.bit.bin") if (o / "kv260_soc.bit.bin").is_file() else None},
            "variant": VARIANT, "vivado": summ.get("vivado"), "git": gid,
            "firmware": {"name": prog, "hex": str(hexf), "hex_sha256": sha256(hexf),
                         "elf_sha256": sha256(hexf.with_suffix(".elf"))},
            "model": {f.name: sha256(f) for f in sorted(model.glob("*.hex"))} if e3c_like() else None,
            "replay": ({"file": "build/e3c/replay_chain_s1_normal.hex",
                        "sha256": sha256(REPO / "build" / "e3c" / "replay_chain_s1_normal.hex"),
                        "frames": 1400} if e3c_like() and VARIANT not in ("ps_e5a", "ps_e6a") else None),
            "mailbox": ({"protocol": "mailbox v1 (docs/soc/e4a_mailbox.md)",
                         "cpu_base": "0x20020000", "ps_base": json.loads((REPO / "host" / "ps_mailbox" / "configs" /
                                                                          "kv260_mailbox.json").read_text())["ps_base"],
                         "protocol.py_sha256": sha256(REPO / "host" / "ps_mailbox" / "protocol.py"),
                         "kv260_mailbox.json_sha256": sha256(REPO / "host" / "ps_mailbox" / "configs" / "kv260_mailbox.json"),
                         "mbx_layout.h_sha256": sha256(REPO / "firmware" / "rv32" / "mailbox" / "mbx_layout.h"),
                         "hwh_sha256": sha256(o / "kv260_soc.hwh") if (o / "kv260_soc.hwh").is_file() else None}
                        if VARIANT in ("ps_e4a", "ps_e5a", "ps_e6a") else None),
            "e4c": e4c_sec,
            "e5a": e5a_sec,
            "register_map": {"VERSION registers": "0x00010000 (DSP1, CNN1, RPL1; SYSCTRL 0x00010000)",
                             "soc_addr_map.vh_sha256": sha256(REPO / "rtl" / "soc" / "soc_addr_map.vh"),
                             "rv32_e3c.h_sha256": sha256(REPO / "firmware" / "rv32" / "e3c" / "rv32_e3c.h"),
                             "spec": "docs/soc/e3c_integration.md"},
            "clock": f"pl_clk0 {bdv.get('PSU__CRL_APB__PL0_REF_CTRL__ACT_FREQMHZ')} MHz "
                     f"(IOPLL / {bdv.get('PSU__CRL_APB__PL0_REF_CTRL__DIVISOR0')} / {bdv.get('PSU__CRL_APB__PL0_REF_CTRL__DIVISOR1')})",
            "hpm0": f"M_AXI_HPM0_FPD {bdv.get('PSU__MAXIGP0__DATA_WIDTH')} bit, debug window 0xA000_0000 (256 KB)",
            "timing": {"wns": summ.get("wns"), "tns": summ.get("tns"), "whs": summ.get("whs")},
            "resources": rep_.get("util"),
            "board_test": {"program": prog, "run_wait_s": RUN_WAIT.get(prog, 0.02),
                           "expect": ("live: no TOHOST, no trap, every CNN result published; frames from host/ps_dma or the STM32"
                                      if VARIANT in ("ps_e5a", "ps_e6a") else
                                      "TOHOST = 1 once, no trap, run_cycles identical in every run"),
                           "xsim_cycles": (xsim_perf(prog) or {}).get("cycles")},
            "checks": [{"name": n, "ok": bool(g), "detail": d} for n, g, d in checks],
        }
        (o / "manifest.json").write_text(json.dumps(man, indent=1), encoding="utf-8")
        nf = sum(not c["ok"] for c in man["checks"])
        fails += nf
        print(f"== {prog}: manifest {o.relative_to(REPO).as_posix()}/manifest.json, {len(checks) - nf}/{len(checks)} checks")
        print(f"   bit.bin sha256 {man['bitstream']['bit_bin_sha256']}")
        print(f"   firmware {hexf.name} sha256 {man['firmware']['hex_sha256']}")
        for c in man["checks"]:
            print(f"   {'ok  ' if c['ok'] else 'FAIL'} {c['name']}  {c['detail']}")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("progs", nargs="*", default=PROGRAMS)
    b.add_argument("--jobs", type=int, default=12)
    b.add_argument("--parallel", type=int, default=1)
    r = sub.add_parser("report")
    r.add_argument("progs", nargs="*", default=PROGRAMS)
    c = sub.add_parser("bramcheck")
    c.add_argument("progs", nargs="*", default=PROGRAMS)
    mf = sub.add_parser("manifest")
    mf.add_argument("progs", nargs="*", default=PROGRAMS)
    for x in (b, r, c, mf):
        x.add_argument("--variant", choices=["jtag", "ps", "ps_spi", "ps_e3c", "ps_e4a", "ps_e5a", "ps_e6a"], default="jtag")
    t = sub.add_parser("board")
    t.add_argument("progs", nargs="*", default=PROGRAMS)
    t.add_argument("--resets", type=int, default=10)
    t.add_argument("--freq-wait", type=float, default=5.0)
    t.add_argument("-v", "--verbose", action="store_true")
    t.add_argument("--analyze-only", action="store_true", help="no JTAG session: re-check the last captures")
    q = sub.add_parser("psboard")
    q.add_argument("progs", nargs="*", default=PROGRAMS)
    q.add_argument("--resets", type=int, default=10)
    q.add_argument("--freq-wait", type=float, default=5.0)
    q.add_argument("-v", "--verbose", action="store_true")
    q.add_argument("--dry-run", action="store_true", help="only print the board commands")
    q.add_argument("--analyze-only", action="store_true", help="no board session: re-check the last results")
    q.add_argument("--no-restore", action="store_true", help="do not reload k26-starter-kits at the end")
    q.add_argument("--variant", choices=["ps", "ps_spi", "ps_e3c", "ps_e4a"], default="ps")
    ns = ap.parse_args()
    if getattr(ns, "variant", "") == "ps_e3c":
        if ns.progs == PROGRAMS:
            ns.progs = E3C_PROGRAMS
        known = E3C_PROGRAMS
    elif getattr(ns, "variant", "") == "ps_e4a":
        if ns.progs == PROGRAMS:
            ns.progs = E4A_PROGRAMS
        known = E4A_PROGRAMS + E4C_PROGRAMS
    elif getattr(ns, "variant", "") in ("ps_e5a", "ps_e6a"):
        if ns.progs == PROGRAMS:
            ns.progs = E5A_PROGRAMS
        known = E5A_PROGRAMS
    else:
        known = PROGRAMS
    for p in ns.progs:
        if p not in known:
            sys.exit(f"unknown program {p}; known: {known}")
    set_variant(getattr(ns, "variant", "jtag") if ns.cmd != "psboard" else
                (ns.variant if ns.variant in ("ps_spi", "ps_e3c", "ps_e4a") else "ps"))
    return {"build": cmd_build, "report": cmd_report, "bramcheck": cmd_bramcheck,
            "board": cmd_board, "psboard": cmd_psboard, "manifest": cmd_manifest}[ns.cmd](ns)


if __name__ == "__main__":
    sys.exit(main())
