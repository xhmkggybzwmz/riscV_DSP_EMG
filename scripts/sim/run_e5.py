#!/usr/bin/env python3
"""E5a regression: PS memory -> AXI DMA -> esi_dma_src_axil -> DSP -> CNN -> RV32I -> mailbox -> PS reader
(docs/system/e5a_ps_dma.md).

  python scripts/sim/run_e5.py                 # all Icarus tests (unit, soc, trap)
  python scripts/sim/run_e5.py --only "soc/*"  # subset (fnmatch on test names)
  python scripts/sim/run_e5.py --xsim          # xsim on the block design e5_bd: real AMD AXI DMA,
                                               # SmartConnects, Block Memory Generator, BRAM Controller
  python scripts/sim/run_e5.py --mutants       # negative controls: every mutant of the source must be detected
  python scripts/sim/run_e5.py --list

Groups
  unit/<name>   tb_esi_dma_src: the source alone, random stalls
  soc/<name>    tb_e5_soc (Icarus, behavioural DMA and BRAM models): firmware/rv32/dma, PS feeder and reader
  trap/<name>   tb_e5_soc: the old replay page has no route from the CPU in this SoC
  xsim/<name>   tb_e5_soc in xsim on the block design (define E5_BD)
Inputs: build/e4c/vec (gen_e4c.py), build/e5/vec (gen_e5.py, run here); firmware -> build/sw/dma.
Results: build/e5/runs/<test>/sim.log, build/e5/summary.json.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_e3c as e3c  # noqa: E402  (Icarus helpers, E3c inputs)
import run_mbx as mbx  # noqa: E402

REPO = e3c.REPO
BUILD = REPO / "build" / "e5"
SW = REPO / "build" / "sw"
XPROJ = REPO / "build" / "vivado" / "e5" / "sim"
XSIM_DIR = XPROJ / "e5_sim.sim" / "sim_1" / "behav" / "xsim"
E4C_VEC = REPO / "build" / "e4c" / "vec"
DDR = BUILD / "vec" / "ddr_mg6.hex"

E5 = [f"rtl/e3c/{f}.v" for f in ("e3c_axil_if", "emg_dsp_axil", "emg_cnn_axil", "esi_dma_src_axil", "emg_e5_subsys")]
e3c.INC.append("dv/e5")
e3c.TARGETS["e5_src"] = {"top": "tb_esi_dma_src", "defs": [],
                         "src": ["rtl/e3c/e3c_axil_if.v", "rtl/e3c/esi_dma_src_axil.v", "dv/common/tb_axil_master.v",
                                 "dv/esi/esi_monitor.v", "dv/e5/tb_esi_dma_src.v"]}
e3c.TARGETS["e5_soc"] = {"top": "tb_e5_soc", "defs": [],
                         "src": e3c.CPU + mbx.SOC + e3c.CORES + E5 + mbx.MODELS +
                         ["dv/common/tb_axil_xbar.v", "dv/common/tb_axil_monitor.v", "dv/e5/tb_axi_dma_model.v",
                          "dv/e5/tb_e5_soc.v"]}
TRAPS = [("store_replay_page", 0x1000_6008, "st"), ("load_replay_page", 0x1000_6000, "ld"),
         ("store_after_mbx", 0x2002_1000, "st")]
CAUSE = {"st": 7, "ld": 5}
MEM: dict[str, tuple[int, int]] = {}       # program -> (__bss_start, __bss_end)


def hexarg(group: str, prog: str) -> str:
    return f"+hex={(SW / group / prog / f'{prog}.hex').as_posix()}"


def tests() -> dict:
    d = lambda p: hexarg("dma", p)
    rd = ["+rd_mode=1"]

    # P runs per segment: E2 vectors of the exact stream, every source frame vs that stream (timestamp
    # restarts each 600-frame segment), no store into code, stack above .bss
    def chain(prog, p, period=0, segs=6):
        a = [d(prog), f"+ddr={DDR.as_posix()}", "+feed=1", f"+dma_segs={segs}", "+dma_len=600", f"+dma_passes={p}",
             f"+src_period={period}", f"+vec={(E4C_VEC / f'chain_mg6_p{p}').as_posix()}",
             f"+rd_expect={(E4C_VEC / f'chain_mg6_p{p}' / 'rd_expect.hex').as_posix()}",
             "+rpl_check=1", "+rpl_run_len=600", "+seq_base=0", f"+exp_frames={600 * segs * p}", f"+exp_segs={segs * p}",
             "+exp_late=0", "+exp_under=0"]
        if prog in MEM:
            a += [f"+mem_guard={MEM[prog][0]:x}", f"+stack_floor={MEM[prog][1]:x}"]
        return a

    t = {f"unit/src_seed{s}": ("e5_src", [f"+seed={s}"]) for s in (1, 2, 3, 7, 11)}
    t["soc/p1_flow"] = ("e5_soc", chain("mbx_dma_r18", 1) + ["+golden=1"] + rd + [
        "+ddr_base=30000000", "+exp_results=18", "+exp_rx=18", "+exp_gaps=0", "+exp_published=18", "+exp_drop=0",
        "+max_cycles=20000000"])
    # the same 72 results as the E4c-B board program (mg6_p4), bit for bit against the E2 reference
    t["soc/p4_flow"] = ("e5_soc", chain("mbx_dma_r72", 4) + ["+golden=1"] + rd + [
        "+exp_results=72", "+exp_rx=72", "+exp_gaps=0", "+exp_published=72", "+exp_drop=0", "+max_cycles=40000000"])
    # nobody reading: 16 kept, the rest dropped, the stream and the CNN never stall
    t["soc/p2_noreader"] = ("e5_soc", chain("mbx_dma_r36", 2) + ["+golden=1", "+exp_results=36", "+exp_published=16",
                                                                 "+exp_drop=20", "+max_cycles=20000000"])
    # paced just above the CNN latency (PERIOD 12000, 8 kHz), two segments: exact spacing on every frame
    t["soc/paced"] = ("e5_soc", chain("mbx_dma_live", 1, 12000, 2) + ["+rd_golden=1"] + rd + [
        "+feed_start=300000", "+rd_gap=2000", "+src_interval=12000", "+live_results=6", "+exp_rx=6", "+exp_gaps=0", "+max_cycles=20000000"])
    # the board image: no TOHOST, keeps running after the data ended
    t["soc/live"] = ("e5_soc", chain("mbx_dma_live", 1) + ["+rd_golden=1"] + rd + [
        "+live_results=18", "+exp_rx=18", "+exp_gaps=0", "+max_cycles=20000000"])
    # the DMA starves the source for 200 periods inside every transfer: counted, flagged, no sample lost,
    # results unchanged (the flag differs: no per-frame compare)
    # (PERIOD 200 is shorter than one frame in the CNN: late frames are normal here, not counted)
    a = [x for x in chain("mbx_dma_r18", 1, 200) if not x.startswith(("+rpl_check", "+exp_under", "+exp_late"))]
    t["soc/starved"] = ("e5_soc", a + ["+rd_golden=1"] + rd + [
        "+dma_stall_at=300", "+dma_stall_cycles=40000", "+rd_gap=2000", "+exp_results=18", "+exp_rx=18",
        "+exp_gaps=0", "+exp_drop=0", "+exp_under=6", "+max_cycles=40000000"])
    # random gaps in the input and the first transfer late: the data arrive whenever they arrive
    t["soc/late_start"] = ("e5_soc", chain("mbx_dma_r18", 1) + ["+golden=1"] + rd + [
        "+feed_start=300000", "+dma_stall_pct=30", "+exp_results=18", "+exp_rx=18", "+exp_gaps=0", "+max_cycles=30000000"])
    # SoC re-run in the middle of a record write: the PS side (DMA, source) keeps going, the sink restarts with
    # SOS. No golden compare: the stream restarts at an arbitrary frame
    t["soc/cpu_reset_midrun"] = ("e5_soc", [d("mbx_dma_r18"), f"+ddr={DDR.as_posix()}", "+feed=1", "+dma_segs=6",
                                            "+dma_len=600", "+dma_passes=2", "+src_period=0"] + rd + [
        "+rd_gap=0", "+cpu_reset_on_rec=5", "+cpu_reset_delay=8", "+exp_epochs=2", "+exp_resync=1",
        "+max_cycles=40000000"])
    for name, addr, kind in TRAPS:
        t[f"trap/{name}"] = ("e5_soc", [hexarg("dma_trap", f"trap_{name}"), "+exp_trap=1",
                                        f"+exp_cause={CAUSE[kind]}", f"+exp_tval={addr:08x}"])
    for k in ("p1_flow", "p4_flow", "live", "paced", "cpu_reset_midrun"):
        t[f"xsim/{k}"] = ("xsim", t[f"soc/{k}"][1])
    # real 2 kHz frame rate through the real DMA, slow memory: 18 results, about 180 M cycles
    t["xsim/p1_rt2k"] = ("xsim", chain("mbx_dma_r18", 1, 50000) + ["+rd_golden=1"] + rd + [
        "+feed_start=300000", "+ddr_lat=40", "+ddr_stall_pct=20", "+rd_gap=2000", "+src_interval=50000", "+exp_results=18", "+exp_rx=18",
        "+exp_gaps=0", "+exp_published=18", "+exp_drop=0", "+max_cycles=200000000"])
    for name, addr, kind in TRAPS[:2]:
        t[f"xsim/trap_{name}"] = ("xsim", t[f"trap/{name}"][1])
    return t


def prepare() -> None:
    e3c.prepare()                         # E3 vectors, model files, e3c_paths.vh
    if (E4C_VEC / "chain_mg6_p1").is_dir():
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "sim" / "gen_e5.py")], capture_output=True, text=True)
        if r.returncode:
            sys.exit("gen_e5.py: " + r.stdout + r.stderr)
    else:
        print("note: build/e4c missing (ml/scripts/e4c_multigesture_eval.py, scripts/sim/gen_e4c.py): soc/ and xsim/ data tests cannot run")
    src = BUILD / "trap_src"
    src.mkdir(parents=True, exist_ok=True)
    for f in src.glob("*.c"):
        f.unlink()
    for name, addr, kind in TRAPS:
        body = (f"*(volatile uint32_t *)0x{addr:08X}u = 0x5A5A5A5Au;" if kind == "st" else
                f"v = *(volatile uint32_t *)0x{addr:08X}u;")
        (src / f"trap_{name}.c").write_text(
            "/* generated by scripts/sim/run_e5.py: must trap before any bus access */\n"
            "#include <stdint.h>\n#include \"rv32_soc.h\"\n"
            f"int main(void)\n{{\n    volatile uint32_t v = 0;\n    {body}\n    (void)v;\n    return 99;\n}}\n",
            encoding="ascii")
    for s, o in ((REPO / "firmware" / "rv32" / "dma", SW / "dma"), (src, SW / "dma_trap")):
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "sw" / "build_asm_tests.py"), "--kind", "c", "-q",
                            "--src", str(s), "--out", str(o)], capture_output=True, text=True)
        if r.returncode:
            sys.exit(r.stdout + r.stderr)
    nm = e3c.VIV_BIN.parent.parent / "gnu" / "riscv" / "nt" / "bin" / "riscv64-unknown-elf-nm.exe"
    for elf in (SW / "dma").glob("*/*.elf"):
        syms = {f[2]: int(f[0], 16) for f in (l.split() for l in subprocess.run(
            [str(nm), str(elf)], capture_output=True, text=True).stdout.splitlines()) if len(f) == 3}
        MEM[elf.stem] = (syms["__bss_start"], syms["__bss_end"])


# ---------------------------------------------------------------- mutants
SRC = "rtl/e3c/esi_dma_src_axil.v"
MUTANTS = [
    ("no_sos_after_tlast", "                next_sos <= h_last;\n", "                next_sos <= 1'b0;\n"),
    ("timestamp_not_restarted", "m_tuser  <= {next_sos ? 32'd0 : n, seq_base + k,", "m_tuser  <= {n, seq_base + k,"),
    ("seq_ignores_base", "seq_base + k,\n", "k,\n"),
    ("period_off_by_one", "wait_cnt <= (period == 32'd0) ? 32'd0 : period - 32'd1;", "wait_cnt <= (period == 32'd0) ? 32'd0 : period;"),
    ("tlast_missing", "m_tlast  <= h_last || (n100_now == 7'd99);", "m_tlast  <= (n100_now == 7'd99);"),
    ("tlast_no_hundred", "m_tlast  <= h_last || (n100_now == 7'd99);", "m_tlast  <= h_last;"),
    ("lanes_swapped", "m_tdata  <= {64'd0, h_data};", "m_tdata  <= {64'd0, h_data[31:0], h_data[63:32]};"),
    ("input_lost_when_stalled", "assign s_axis_tready = aresetn & ~soft_rst & run & (~h_valid | launch);",
     "assign s_axis_tready = aresetn & ~soft_rst & run;"),
    ("valid_dropped_when_late", "if (!launch) valid_r <= 1'b0;", "if (!launch) valid_r <= 1'b0;\n            end else if (run && due && valid_r) begin\n                valid_r <= 1'b0;"),
    ("underrun_not_counted", "if (under_armed) under_cnt <= under_cnt + 32'd1;", "if (1'b0) under_cnt <= under_cnt + 32'd1;"),
    ("underrun_every_cycle", "if (under_armed) under_cnt <= under_cnt + 32'd1;", "under_cnt <= under_cnt + 32'd1;"),
    ("underrun_flag_missing", "8'd0, 4'd0, under_pend & ~next_sos, 2'd0, next_sos};", "8'd0, 4'd0, 1'b0, 2'd0, next_sos};"),
    ("underrun_between_segments", "due && in_seg && !h_valid", "due && !h_valid"),
    ("late_not_counted", "if (late_armed) late_cnt <= late_cnt + 32'd1;", "if (1'b0) late_cnt <= late_cnt + 32'd1;"),
    ("sink_reset_waiting_no_sos", "m_tuser[`ESI_F_SOS] <= 1'b1;", "m_tuser[`ESI_F_SOS] <= m_tuser[`ESI_F_SOS];"),
    ("sink_reset_idle_no_sos", "                    next_sos <= 1'b1;\n                end\n                in_seg", "                end\n                in_seg"),
    ("sink_reset_offered", "assign m_tvalid = valid_r & ~rst & ~sink_rst & ~sink_rst_q;", "assign m_tvalid = valid_r & ~rst;"),
    ("run_ignored", "wire launch  = run & ~sink_rst & due", "wire launch  = ~sink_rst & due"),
    ("soft_reset_keeps_counters", "k <= 32'd0; n <= 32'd0; n100 <= 7'd0; frame_cnt <= 32'd0;", "k <= 32'd0; n <= 32'd0; n100 <= 7'd0; frame_cnt <= aresetn ? frame_cnt : 32'd0;"),
    ("keep_not_checked", "if (s_axis_tkeep != 8'hFF) keep_err <= keep_err + 32'd1;", ""),
]


def run_mutants() -> int:
    results = []
    for name, old, new in MUTANTS:
        text = (REPO / SRC).read_text(encoding="utf-8")
        if text.count(old) != 1:
            print(f"[BAD ] {name}: pattern found {text.count(old)} times")
            results.append(False)
            continue
        d = BUILD / "mutants" / name
        d.mkdir(parents=True, exist_ok=True)
        f = d / "esi_dma_src_axil.v"
        f.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
        try:
            vvp = e3c.compile_target("e5_src", d, {SRC: f})
            r = e3c.run_icarus(f"mutant/{name}", vvp, ["+seed=1"], d / "run", 300)
            detected = not r["ok"]
            why = (r["errors"] or ["no ERROR line"])[0][:90]
        except RuntimeError as e:
            detected, why = False, f"does not compile: {str(e)[:120]}"
        results.append(detected)
        print(f"[{'DETECTED' if detected else 'MISSED  '}] {name:28s} {why}", flush=True)
    print(f"MUTANTS: {sum(results)}/{len(results)} detected")
    return 0 if all(results) else 1


def xsim_prepare(rebuild: bool) -> None:
    if rebuild or not XSIM_DIR.is_dir():
        XPROJ.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run([str(e3c.VIV_BIN / "vivado.bat"), "-mode", "batch", "-nojournal",
                            "-log", str(XPROJ.parent / "create_e5.log"),
                            "-source", str(REPO / "fpga" / "kv260" / "tcl" / "create_e5_project.tcl"),
                            "-tclargs", REPO.as_posix(), XPROJ.as_posix()],
                           cwd=XPROJ.parent, capture_output=True, text=True, env=e3c.tool_env())
        if r.returncode or not XSIM_DIR.is_dir():
            sys.exit("create_e5_project failed: " + r.stdout[-3000:])
    for script in ("compile.bat", "elaborate.bat"):
        r = subprocess.run(["cmd", "/c", ".\\" + script], cwd=XSIM_DIR, capture_output=True, text=True,
                           env=e3c.tool_env())
        log = XSIM_DIR / ("compile.log" if script == "compile.bat" else "elaborate.log")
        text = log.read_text(encoding="utf-8", errors="ignore") if log.is_file() else r.stdout
        errs = [l for l in text.splitlines() if l.startswith("ERROR")]
        if r.returncode or errs:
            sys.exit(f"xsim {script} failed:\n" + "\n".join(errs[:20]) + r.stdout[-1500:])
        print(f"xsim {script}: ok, {sum(l.startswith('WARNING') for l in text.splitlines())} warning(s)", flush=True)


def run_xsim(name: str, plus: list[str], out: Path) -> dict:
    t0 = time.monotonic()
    out.mkdir(parents=True, exist_ok=True)
    opts = XSIM_DIR / f"opts_{out.name}.txt"
    opts.write_text("".join(f"-testplusarg {p.lstrip('+')}\n" for p in plus), encoding="ascii")
    cmd = ["cmd", "/c", str(e3c.VIV_BIN / "xsim.bat"), "tb_e5_soc_behav", "-R", "-f", opts.name,
           "-log", f"xsim_{out.name}.log"]
    try:
        r = subprocess.run(cmd, cwd=XSIM_DIR, capture_output=True, text=True, env=e3c.tool_env(), timeout=7200)
        text = r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        text = "TIMEOUT"
    (out / "sim.log").write_text(" ".join(plus) + "\n" + text, encoding="utf-8")
    opts.unlink(missing_ok=True)
    return e3c.verdict(name, text, t0, out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="*")
    ap.add_argument("--xsim", action="store_true")
    ap.add_argument("--rebuild", action="store_true", help="xsim: recreate the Vivado project")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--mutants", action="store_true")
    ap.add_argument("-j", "--jobs", type=int, default=8)
    ns = ap.parse_args()
    if ns.mutants:
        return run_mutants()
    if ns.list:
        for k, (tg, _) in tests().items():
            print(f"{k:32s} {tg}")
        return 0
    # the source unit test needs no firmware, vectors or data (a fresh clone can run it)
    if not all(k.startswith("unit/") for k in tests() if fnmatch.fnmatch(k, ns.only)):
        prepare()
    all_t = tests()
    sel = {k: v for k, v in all_t.items() if fnmatch.fnmatch(k, ns.only) and (v[0] == "xsim") == ns.xsim}
    runs = BUILD / "runs"
    results = []
    if ns.xsim:
        xsim_prepare(ns.rebuild)
        for k, (_, plus) in sel.items():
            r = run_xsim(k, plus, runs / k.replace("/", "__"))
            results.append(r)
            print(f"[{'PASS' if r['ok'] else 'FAIL'}] {k:32s} {r['seconds']:7.1f}s  {r['perf']}  {r['errors'][:1]}",
                  flush=True)
    else:
        vvps = {tg: e3c.compile_target(tg, BUILD / "bin") for tg in sorted({v[0] for v in sel.values()})}
        with ThreadPoolExecutor(max_workers=ns.jobs) as ex:
            futs = {ex.submit(e3c.run_icarus, k, vvps[tg], plus, runs / k.replace("/", "__"), 7200): k
                    for k, (tg, plus) in sel.items()}
            for f in futs:
                r = f.result()
                results.append(r)
                print(f"[{'PASS' if r['ok'] else 'FAIL'}] {r['name']:32s} {r['seconds']:7.1f}s  {r['perf']}  "
                      f"{r['errors'][:1]}", flush=True)
    n_ok = sum(r["ok"] for r in results)
    print(f"SUMMARY: {n_ok}/{len(results)} passed")
    BUILD.mkdir(parents=True, exist_ok=True)
    (BUILD / ("summary_xsim.json" if ns.xsim else "summary.json")).write_text(json.dumps(results, indent=1),
                                                                               encoding="utf-8")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
