#!/usr/bin/env python3
"""E3c regression: RV32I SoC + DSP/CNN control pages (docs/soc/e3c_integration.md).

  python scripts/sim/run_e3c.py                 # all Icarus groups (unit, pipe, soc, trap)
  python scripts/sim/run_e3c.py --only "pipe/*" # subset (fnmatch on test names)
  python scripts/sim/run_e3c.py --xsim          # system tests in Vivado 2026.1 xsim with the
                                                # real AMD SmartConnect (block design e3c_bd)
  python scripts/sim/run_e3c.py --mutants       # negative controls: every mutant must be detected
  python scripts/sim/run_e3c.py --list

Groups
  unit/order<o>      tb_e3c_axil: AXI4-Lite pages, AW/W order o (3 = random), back-pressure
  pipe/<case>        tb_e3c_pipe: replay -> DSP -> CNN through the pages, bit-exact vs E2 golden
  soc/<prog>         tb_e3c_soc (Icarus, behavioural decoder): RV32I firmware firmware/rv32/e3c
  trap/<name>        tb_e3c_soc: generated program accessing an unmapped address -> trap, no AXI
  xsim/<prog>        tb_e3c_soc in xsim on the block design (SmartConnect 1 SI / 4 MI)
Inputs: build/e3/vec (scripts/sim/run_e3.py / gen_e3_vectors.py), build/e3c (gen_e3c.py, run here).
Results: build/e3c/runs/<test>/sim.log, build/e3c/summary.json.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BUILD = REPO / "build" / "e3c"
VEC = REPO / "build" / "e3" / "vec"
SW = REPO / "build" / "sw"
IVERILOG = Path(os.environ.get("RISCV_EMG_ICARUS_BIN", r"C:\iverilog\bin")) / "iverilog.exe"
VVP = Path(os.environ.get("RISCV_EMG_ICARUS_BIN", r"C:\iverilog\bin")) / "vvp.exe"
VIV_BIN = Path(os.environ.get("RISCV_EMG_VIVADO_ROOT", r"E:\Xilinx2026\2026.1\Vivado")) / "bin"
XPROJ = REPO / "build" / "vivado" / "e3c" / "sim"
XSIM_DIR = XPROJ / "e3c_sim.sim" / "sim_1" / "behav" / "xsim"

CPU = [f"rtl/cpu/{f}.v" for f in ("rv32i_regfile", "rv32i_alu", "rv32i_decoder", "rv32i_lsu", "rv32i_core")]
SOC = [f"rtl/soc/{f}.v" for f in ("soc_ram_dp", "soc_dmem_router", "cmi2axil", "soc_sysctrl", "rv32_soc_top")]
CORES = ["rtl/dsp/emg_dsp.v", "rtl/infer/emg_cnn.v"]
E3C = [f"rtl/e3c/{f}.v" for f in ("e3c_axil_if", "emg_dsp_axil", "emg_cnn_axil", "esi_replay_axil", "emg_e3c_subsys")]
INC = ["rtl/cpu", "rtl/soc", "rtl/esi", "dv/common", "dv/e3c", "build/e3", "build/e3c"]
TARGETS = {
    "axil": {"top": "tb_e3c_axil", "src": CORES + E3C + ["dv/common/tb_axil_master.v", "dv/common/tb_axil_monitor.v",
                                                       "dv/e3c/tb_e3c_axil.v"], "defs": []},
    "pipe": {"top": "tb_e3c_pipe", "src": CORES + E3C + ["dv/common/tb_axil_master.v", "dv/esi/esi_monitor.v",
                                                       "dv/e3c/tb_e3c_pipe.v"], "defs": []},
    "pipe_nc": {"top": "tb_e3c_pipe", "src": CORES + E3C + ["dv/common/tb_axil_master.v", "dv/esi/esi_monitor.v",
                                                          "dv/e3c/tb_e3c_pipe.v"], "defs": ["NO_COEF_INIT"]},
    "soc": {"top": "tb_e3c_soc", "src": CPU + SOC + CORES + E3C + ["dv/common/tb_axil_xbar.v",
                                                                "dv/common/tb_axil_monitor.v", "dv/e3c/tb_e3c_soc.v"],
            "defs": []},
}

# unmapped data accesses that must trap (cause 7 store / 5 load), tval = address
TRAPS = [("store_spi_tohost", 0x1000_1008, 1), ("store_irq_page", 0x1000_3008, 1),
         ("store_page4", 0x1000_4008, 1), ("store_after_replay", 0x1000_7008, 1),
         ("store_page_f", 0x1000_F008, 1), ("store_shared_buf", 0x2000_0008, 1),
         ("store_dsp_end", 0x1000_3000, 1), ("store_before_cnn", 0x1000_4FFC, 1),
         ("store_replay_end", 0x1000_7000, 1), ("store_ram_end", 0x0001_0000, 1),
         ("load_spi", 0x1000_1000, 0), ("load_shared_buf", 0x2000_0000, 0), ("load_before_cnn", 0x1000_4FFC, 0)]


def tests() -> dict:
    v = lambda c: f"+vec={(VEC / c).as_posix()}"
    t = {}
    for o in range(4):
        t[f"unit/order{o}"] = ("axil", [f"+order={o}", f"+seed={o + 11}"])
    t["pipe/s1_normal"] = ("pipe", [v("chain_s1_normal"), "+acc=1"])
    t["pipe/s2_sos_restart"] = ("pipe", [v("chain_s2_sos_restart")])
    t["pipe/s3_gap"] = ("pipe", [v("chain_s3_gap")])
    t["pipe/s4_saturation"] = ("pipe", [v("chain_s4_saturation")])
    # 20000-frame real record: the replay memory holds 2048 frames, so the first 2048 are played
    t["pipe/real_long_2048"] = ("pipe", [v("chain_real_long"), "+seed=5", "+nframes=2048"])
    t["pipe/noack_drop"] = ("pipe", [v("chain_s1_normal"), "+noack=1"])
    t["pipe/rerun_soft_reset"] = ("pipe", [v("chain_s1_normal"), "+rerun=1", "+seed=3"])
    t["pipe/loop2"] = ("pipe", [v("chain_s1_normal"), "+loops=2"])
    t["pipe/realtime_12000"] = ("pipe", [v("chain_s1_normal"), "+period=12000", "+interval_check=1"])
    t["pipe/realtime_2khz"] = ("pipe", [v("chain_s1_normal"), "+period=50000", "+interval_check=1", "+nframes=40"])
    # real 2 kHz over the whole 1400-frame record (70 M cycles)
    t["pipe/realtime_2khz_full"] = ("pipe", [v("chain_s1_normal"), "+period=50000", "+interval_check=1", "+seq_base=0"])
    t["pipe/coef_port"] = ("pipe_nc", [v("chain_s1_normal"), "+coefport=1"])
    t["pipe/axi_order0"] = ("pipe", [v("chain_s1_normal"), "+order=0", "+bmax=0", "+rmax=0"])
    gold = [v("chain_s1_normal"), "+golden=1"]
    t["soc/e3c_flow"] = ("soc", [hexarg("e3c", "e3c_flow")] + gold + ["+exp_results=22"])
    t["soc/e3c_loop"] = ("soc", [hexarg("e3c", "e3c_loop")] + gold)
    t["soc/e3c_drop"] = ("soc", [hexarg("e3c", "e3c_drop")] + gold + ["+exp_results=11"])
    t["soc/e3c_mmio"] = ("soc", [hexarg("e3c", "e3c_mmio")])
    t["soc/e3c_rt"] = ("soc", [hexarg("e3c", "e3c_rt")] + gold + ["+exp_results=11", "+max_cycles=40000000",
                                                                  "+period_check=12000"])
    t["soc/e3c_rt2k"] = ("soc", [hexarg("e3c", "e3c_rt2k")] + gold + ["+exp_results=11", "+max_cycles=90000000",
                                                                      "+period_check=50000"])
    for name, addr, st in TRAPS:
        t[f"trap/{name}"] = ("soc", [hexarg("e3c_trap", f"trap_{name}"), "+exp_trap=1",
                                     f"+exp_cause={7 if st else 5}", f"+exp_tval={addr:08x}"])
    for p in ("e3c_flow", "e3c_drop", "e3c_mmio", "e3c_loop", "e3c_rt", "e3c_rt2k"):
        t[f"xsim/{p}"] = ("xsim", t[f"soc/{p}"][1])
    for name in ("store_spi_tohost", "store_page_f", "store_shared_buf", "store_dsp_end", "load_before_cnn"):
        t[f"xsim/trap_{name}"] = ("xsim", t[f"trap/{name}"][1])
    return t


def hexarg(group: str, prog: str) -> str:
    return f"+hex={(SW / group / prog / f'{prog}.hex').as_posix()}"


# ---------------------------------------------------------------- inputs
def prepare() -> None:
    if not (VEC / "chain_s1_normal" / "input_frames.hex").is_file():
        subprocess.run([sys.executable, str(REPO / "scripts" / "sim" / "gen_e3_vectors.py")], check=True)
    subprocess.run([sys.executable, str(REPO / "scripts" / "sim" / "gen_e3c.py")], check=True, capture_output=True)
    src = BUILD / "trap_src"
    src.mkdir(parents=True, exist_ok=True)
    for f in src.glob("*.c"):
        f.unlink()
    for name, addr, st in TRAPS:
        body = (f"*(volatile uint32_t *)0x{addr:08X}u = 0x5A5A5A5Au;" if st else
                f"v = *(volatile uint32_t *)0x{addr:08X}u;")
        (src / f"trap_{name}.c").write_text(
            "/* generated by scripts/sim/run_e3c.py: unmapped access must trap before any AXI transaction */\n"
            "#include <stdint.h>\n#include \"rv32_soc.h\"\n"
            f"int main(void)\n{{\n    volatile uint32_t v = 0;\n    {body}\n    (void)v;\n    return 99;\n}}\n",
            encoding="ascii")
    for s, o in ((REPO / "firmware" / "rv32" / "e3c", SW / "e3c"), (src, SW / "e3c_trap")):
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "sw" / "build_asm_tests.py"), "--kind", "c", "-q",
                            "--src", str(s), "--out", str(o)], capture_output=True, text=True)
        if r.returncode:
            sys.exit(r.stdout + r.stderr)


def win(p: str) -> str:
    return str((REPO / p).resolve())


def compile_target(name: str, out: Path, subst: dict | None = None) -> Path:
    tg = TARGETS[name]
    srcs = []
    for s in tg["src"]:
        if subst and s in subst:
            srcs.append(str(subst[s]))
        else:
            srcs.append(win(s))
    vvp = out / f"{name}.vvp"
    out.mkdir(parents=True, exist_ok=True)
    cmd = [str(IVERILOG), "-g2012", "-o", str(vvp), "-s", tg["top"]]
    cmd += [f"-I{win(i)}" for i in INC] + [f"-D{d}" for d in tg["defs"]] + srcs
    r = subprocess.run(cmd, capture_output=True, text=True)
    errs = [l for l in (r.stdout + r.stderr).splitlines() if "error" in l.lower()]
    if r.returncode or errs:
        raise RuntimeError(f"iverilog {name}: " + "\n".join(errs[:10]))
    return vvp


RESULT_RE = re.compile(r"TEST_RESULT: (PASS|FAIL) checks=(\d+) errors=(\d+)")


def run_icarus(name: str, vvp: Path, plus: list[str], out: Path, timeout: int = 3600) -> dict:
    t0 = time.monotonic()
    out.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run([str(VVP), "-n", str(vvp), *plus], capture_output=True, text=True, timeout=timeout)
        text = r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        text = "TIMEOUT"
    (out / "sim.log").write_text(" ".join(plus) + "\n" + text, encoding="utf-8")
    return verdict(name, text, t0, out)


def verdict(name: str, text: str, t0: float, out: Path) -> dict:
    res = RESULT_RE.search(text)
    errors = [l for l in text.splitlines() if l.startswith("ERROR")]
    ok = bool(res) and res.group(1) == "PASS" and not errors
    perf = re.search(r"PERF: (.*)", text)
    return {"name": name, "ok": ok, "checks": int(res.group(2)) if res else 0, "errors": errors[:5],
            "perf": perf.group(1) if perf else "", "seconds": round(time.monotonic() - t0, 1),
            "log": str(out / "sim.log")}


# ---------------------------------------------------------------- xsim
def tool_env() -> dict:
    env = dict(os.environ)
    env["PATH"] = str(VIV_BIN) + os.pathsep + env.get("PATH", "")
    return env


def xsim_prepare(rebuild: bool) -> None:
    if rebuild or not XSIM_DIR.is_dir():
        XPROJ.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run([str(VIV_BIN / "vivado.bat"), "-mode", "batch", "-nojournal",
                            "-log", str(XPROJ.parent / "create_e3c.log"),
                            "-source", str(REPO / "fpga" / "kv260" / "tcl" / "create_e3c_project.tcl"),
                            "-tclargs", REPO.as_posix(), XPROJ.as_posix()],
                           cwd=XPROJ.parent, capture_output=True, text=True, env=tool_env())
        if r.returncode or not XSIM_DIR.is_dir():
            sys.exit("create_e3c_project failed: " + r.stdout[-2000:])
    for script in ("compile.bat", "elaborate.bat"):
        r = subprocess.run(["cmd", "/c", ".\\" + script], cwd=XSIM_DIR, capture_output=True, text=True, env=tool_env())
        log = XSIM_DIR / ("compile.log" if script == "compile.bat" else "elaborate.log")
        text = log.read_text(encoding="utf-8", errors="ignore") if log.is_file() else r.stdout
        errs = [l for l in text.splitlines() if l.startswith("ERROR")]
        if r.returncode or errs:
            sys.exit(f"xsim {script} failed:\n" + "\n".join(errs[:20]))
        print(f"xsim {script}: ok, {sum(l.startswith('WARNING') for l in text.splitlines())} warning(s)", flush=True)


def run_xsim(name: str, plus: list[str], out: Path) -> dict:
    t0 = time.monotonic()
    out.mkdir(parents=True, exist_ok=True)
    opts = XSIM_DIR / f"opts_{out.name}.txt"
    opts.write_text("".join(f"-testplusarg {p.lstrip('+')}\n" for p in plus), encoding="ascii")
    cmd = ["cmd", "/c", str(VIV_BIN / "xsim.bat"), "tb_e3c_soc_behav", "-R", "-f", opts.name,
           "-log", f"xsim_{out.name}.log"]
    try:
        r = subprocess.run(cmd, cwd=XSIM_DIR, capture_output=True, text=True, env=tool_env(), timeout=7200)
        text = r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        text = "TIMEOUT"
    (out / "sim.log").write_text(" ".join(plus) + "\n" + text, encoding="utf-8")
    opts.unlink(missing_ok=True)
    return verdict(name, text, t0, out)


# ---------------------------------------------------------------- mutants
MUTANTS = [
    ("double_write_effect", "rtl/e3c/e3c_axil_if.v", "            wr_en <= 1'b0;\n",
     "            wr_en <= wr_en & ~wr_en_q;\n", ["unit/order3"]),
    ("lost_write_same_cycle", "rtl/e3c/e3c_axil_if.v", "            if (aw_held && w_held) begin                // perform the write exactly once\n                wr_en        <= 1'b1;",
     "            if (aw_held && w_held) begin                // perform the write exactly once\n                wr_en        <= ~same_q;", ["unit/order0"]),
    ("snapshot_newest", "rtl/e3c/emg_cnn_axil.v", "    wire [RW-1:0] snap = pending ? fifo[rd_p[RES_DEPTH_LOG2-1:0]] : {RW{1'b0}};",
     "    wire [RW-1:0] snap = pending ? fifo[wr_p[RES_DEPTH_LOG2-1:0] - 1'b1] : {RW{1'b0}};", ["pipe/noack_drop"]),
    ("ack_pops_two", "rtl/e3c/emg_cnn_axil.v", "                rd_p    <= rd_p + 1'b1;", "                rd_p    <= rd_p + 2'd2;",
     ["soc/e3c_flow"]),
    ("replay_seq_offset", "rtl/e3c/esi_replay_axil.v", "                m_tuser <= {k, seq_base + k, 8'd0, nxt[71:64]};",
     "                m_tuser <= {k, seq_base + k + 32'd1, 8'd0, nxt[71:64]};", ["pipe/s1_normal"]),
    ("replay_period_plus1", "rtl/e3c/esi_replay_axil.v", "            else if (tick_cnt == period - 32'd1) tick_cnt <= 32'd0;",
     "            else if (tick_cnt == period) tick_cnt <= 32'd0;", ["pipe/realtime_2khz"]),
    ("router_dsp_window_8k", "rtl/soc/rv32_soc_top.v", "`SOC_CNN_SIZE, `SOC_DSP_SIZE, `SOC_SYSCTRL_SIZE}",
     "`SOC_CNN_SIZE, 32'h0000_2000, `SOC_SYSCTRL_SIZE}", ["trap/store_irq_page"]),
]
# helper signals some mutants need
MUT_EXTRA = {
    "double_write_effect": ("    reg [9:0] aw_word;\n", "    reg [9:0] aw_word;\n    reg wr_en_q = 1'b0;\n    always @(posedge aclk) wr_en_q <= wr_en;\n"),
    "lost_write_same_cycle": ("    reg [9:0] aw_word;\n",
                              "    reg [9:0] aw_word;\n    reg same_q = 1'b0;\n    always @(posedge aclk) if (s_axi_awvalid && s_axi_awready) same_q <= s_axi_wvalid && s_axi_wready;\n"),
}


def run_mutants(all_tests: dict) -> int:
    results = []
    for mname, f, old, new, check in MUTANTS:
        mdir = BUILD / "mut" / mname
        mdir.mkdir(parents=True, exist_ok=True)
        text = (REPO / f).read_text(encoding="utf-8")
        if text.count(old) != 1:
            sys.exit(f"mutant {mname}: pattern not found exactly once in {f}")
        text = text.replace(old, new)
        if mname in MUT_EXTRA:
            a, b = MUT_EXTRA[mname]
            if text.count(a) != 1:
                sys.exit(f"mutant {mname}: helper pattern not found")
            text = text.replace(a, b)
        mf = mdir / Path(f).name
        mf.write_text(text, encoding="utf-8")
        detected, details = True, []
        for tn in check:
            tgt, plus = all_tests[tn]
            vvp = compile_target(tgt, mdir, {f: mf})
            r = run_icarus(tn, vvp, plus, mdir / tn.replace("/", "__"), timeout=1800)
            details.append(f"{tn}: {'FAIL (detected)' if not r['ok'] else 'PASS (not detected)'}")
            detected = detected and not r["ok"]
        results.append((mname, detected, details))
        print(f"{'DETECTED' if detected else 'MISSED  '} {mname:24s} {'; '.join(details)}", flush=True)
    n = sum(d for _, d, _ in results)
    print(f"MUTANTS: {n}/{len(results)} detected")
    (BUILD / "mutants.json").write_text(json.dumps([{"mutant": m, "detected": d, "details": x} for m, d, x in results],
                                                  indent=1), encoding="utf-8")
    return 0 if n == len(results) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="*")
    ap.add_argument("--xsim", action="store_true")
    ap.add_argument("--rebuild", action="store_true", help="xsim: recreate the Vivado project")
    ap.add_argument("--mutants", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("-j", "--jobs", type=int, default=8)
    ns = ap.parse_args()
    all_t = tests()
    if ns.list:
        for k, (tg, p) in all_t.items():
            print(f"{k:32s} {tg}")
        return 0
    prepare()
    if ns.mutants:
        return run_mutants(all_t)
    sel = {k: v for k, v in all_t.items() if fnmatch.fnmatch(k, ns.only) and (v[0] == "xsim") == ns.xsim}
    runs = BUILD / "runs"
    results = []
    if ns.xsim:
        xsim_prepare(ns.rebuild)
        for k, (_, plus) in sel.items():
            r = run_xsim(k, plus, runs / k.replace("/", "__"))
            results.append(r)
            print(f"[{'PASS' if r['ok'] else 'FAIL'}] {k:32s} {r['seconds']:7.1f}s  {r['perf']}  {r['errors'][:1]}", flush=True)
    else:
        vvps = {tg: compile_target(tg, BUILD / "bin") for tg in sorted({v[0] for v in sel.values()})}
        with ThreadPoolExecutor(max_workers=ns.jobs) as ex:
            futs = {ex.submit(run_icarus, k, vvps[tg], plus, runs / k.replace("/", "__")): k
                    for k, (tg, plus) in sel.items()}
            for f in futs:
                r = f.result()
                results.append(r)
                print(f"[{'PASS' if r['ok'] else 'FAIL'}] {r['name']:32s} {r['seconds']:7.1f}s  {r['perf']}  "
                      f"{r['errors'][:1]}", flush=True)
    n_ok = sum(r["ok"] for r in results)
    print(f"SUMMARY: {n_ok}/{len(results)} passed")
    (BUILD / ("summary_xsim.json" if ns.xsim else "summary.json")).write_text(json.dumps(results, indent=1),
                                                                               encoding="utf-8")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
