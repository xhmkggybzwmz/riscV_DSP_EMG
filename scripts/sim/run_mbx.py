#!/usr/bin/env python3
"""E4a mailbox regression: RV32I -> shared BRAM -> PS reader (docs/soc/e4a_mailbox.md).

  python scripts/sim/run_mbx.py                 # all Icarus tests (unit, soc, trap)
  python scripts/sim/run_mbx.py --only "soc/*"  # subset (fnmatch on test names)
  python scripts/sim/run_mbx.py --xsim          # xsim on the block design mbx_bd: real AMD
                                                # SmartConnect, Block Memory Generator and AXI
                                                # BRAM Controller
  python scripts/sim/run_mbx.py --list

Groups
  unit/<name>   tb_mbx_unit: soc_io_split + cmi_bram_port + BRAM + PS controller, directed
  soc/<name>    tb_mbx_soc (Icarus, behavioural BRAM / controller): firmware/rv32/mailbox
                with the PS consumer (dv/mbx/mbx_ps_reader.vh) and the producer monitor
  trap/<name>   tb_mbx_soc: accesses next to / misaligned in the mailbox window must trap
  xsim/<name>   tb_mbx_soc in xsim on the block design (define MBX_BD)
Inputs: as scripts/sim/run_e3c.py (build/e3/vec, build/e3c); firmware -> build/sw/mbx.
Results: build/mbx/runs/<test>/sim.log, build/mbx/summary.json.
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

REPO = e3c.REPO
BUILD = REPO / "build" / "mbx"
SW = REPO / "build" / "sw"
XPROJ = REPO / "build" / "vivado" / "mbx" / "sim"
E4C_VEC = REPO / "build" / "e4c" / "vec"            # scripts/sim/gen_e4c.py
E4C_FW = REPO / "build" / "e4c" / "fw"              # generated mg6_data.h (git-ignored, real samples)
MG6_MEM: dict[str, tuple[int, int]] = {}            # program -> (__bss_start, __bss_end), from prepare()
XSIM_DIR = XPROJ / "mbx_sim.sim" / "sim_1" / "behav" / "xsim"

SOC = e3c.SOC + ["rtl/soc/soc_io_split.v", "rtl/soc/cmi_bram_port.v"]
MODELS = ["dv/mbx/tb_bram_tdp.v", "dv/mbx/tb_axil_bram_ctrl.v", "dv/common/tb_axil_master.v"]
e3c.INC.append("dv/mbx")
e3c.TARGETS["mbx_unit"] = {"top": "tb_mbx_unit", "defs": [],
                           "src": ["rtl/soc/soc_io_split.v", "rtl/soc/cmi_bram_port.v", "dv/common/tb_cmi_slave.v"]
                           + MODELS + ["dv/mbx/tb_mbx_unit.v"]}
e3c.TARGETS["mbx_soc"] = {"top": "tb_mbx_soc", "defs": [],
                          "src": e3c.CPU + SOC + e3c.CORES + e3c.E3C + MODELS +
                          ["dv/common/tb_axil_xbar.v", "dv/common/tb_axil_monitor.v", "dv/mbx/tb_mbx_soc.v"]}

# accesses that must trap: (name, address, kind) kind: st / ld unmapped, mst / mld misaligned
TRAPS = [("store_before_mbx", 0x2001_FFFC, "st"), ("load_after_mbx", 0x2002_1000, "ld"),
         ("store_shared_buf", 0x2000_0008, "st"), ("load_page_after", 0x2003_0000, "ld"),
         ("load_misaligned", 0x2002_0002, "mld"), ("store_misaligned", 0x2002_0006, "mst")]
CAUSE = {"st": 7, "ld": 5, "mld": 4, "mst": 6}


def hexarg(group: str, prog: str) -> str:
    return f"+hex={(SW / group / prog / f'{prog}.hex').as_posix()}"


def tests() -> dict:
    vec = f"+vec={(e3c.VEC / 'chain_s1_normal').as_posix()}"
    gold = [vec, "+golden=1"]
    rg = [vec, "+rd_golden=1"]
    rd = ["+rd_mode=1"]
    m = lambda p: hexarg("mbx", p)
    t = {"unit/mbx_unit": ("mbx_unit", []), "unit/mbx_unit_seed7": ("mbx_unit", ["+seed=7"])}
    t["soc/mmio"] = ("mbx_soc", [m("mbx_mmio"), "+mon_off=1"])
    t["soc/flow_reader"] = ("mbx_soc", [m("mbx_flow")] + gold + rd + [
        "+exp_results=11", "+exp_rx=11", "+exp_gaps=0", "+exp_published=11", "+exp_drop=0"])
    t["soc/flow_noreader"] = ("mbx_soc", [m("mbx_flow")] + gold + ["+exp_results=11", "+exp_published=11",
                                                                    "+exp_drop=0"])
    # 22 results, 16 slots, nobody reading: 16 published, 6 dropped, the CNN never stalled
    t["soc/loop2_noreader"] = ("mbx_soc", [m("mbx_loop2"), "+exp_published=16", "+exp_drop=6"])
    t["soc/loop2_fast"] = ("mbx_soc", [m("mbx_loop2")] + rg + rd + [
        "+rd_gap=0", "+exp_rx=22", "+exp_gaps=0", "+exp_published=22", "+exp_drop=0"])
    # consumer slower than the producer: drops in the middle, reported by F_DROP_BEFORE gaps
    t["soc/loop2_slow"] = ("mbx_soc", [m("mbx_loop2")] + rg + rd + [
        "+rd_rec_gap=1200000", "+drain_max=25000000", "+exp_min_drop=1"])
    # consumer attaches after the producer finished: the first 16 kept, 6 dropped after them
    t["soc/late_reader"] = ("mbx_soc", [m("mbx_loop2")] + rg + rd + [
        "+rd_start=5200000", "+exp_rx=16", "+exp_gaps=0", "+exp_published=16", "+exp_drop=6"])
    t["soc/live_reader"] = ("mbx_soc", [m("mbx_loop2")] + rg + rd + ["+rd_policy=1", "+rd_start=2500000"])
    t["soc/restart_reader"] = ("mbx_soc", [m("mbx_loop2")] + rg + rd + [
        "+rd_restart_after=5", "+exp_rx=22", "+exp_gaps=0"])
    t["soc/wrap"] = ("mbx_soc", [m("mbx_wrap")] + rg + rd + ["+rd_gap=0", "+exp_rx=22", "+exp_published=22"])
    t["soc/wrap_noreader"] = ("mbx_soc", [m("mbx_wrap"), "+exp_published=16", "+exp_drop=6"])
    t["soc/collide_corrupt"] = ("mbx_soc", [m("mbx_loop2")] + rg + rd + [
        "+rd_gap=0", "+rd_hammer=8", "+bram_collision_corrupt=1", "+exp_rx=22", "+exp_min_coll=10"])
    t["soc/cpu_reset_midwrite"] = ("mbx_soc", [m("mbx_loop2")] + rg + rd + [
        "+rd_gap=0", "+cpu_reset_on_rec=5", "+cpu_reset_delay=8", "+exp_epochs=2", "+exp_resync=1",
        "+max_cycles=9000000"])
    t["soc/rt2k_reader"] = ("mbx_soc", [m("mbx_rt2k")] + gold + rd + [
        "+seq_base=0", "+period_check=50000", "+rd_gap=2000", "+exp_results=11", "+exp_rx=11",
        "+exp_gaps=0", "+max_cycles=90000000"])
    # E4c: six real GRABMyo segments (scripts/sim/gen_e4c.py), replay frame memory rewritten by the
    # RV32I per segment; golden = the exact stream the replay delivers (build/e4c/vec/chain_mg6_p<P>)
    # every frame the replay emits vs the E2 input stream (timestamp restarts each 600-frame run),
    # replay memory rewritten only while idle with all results acknowledged, no store into code /
    # read-only data, stack above .bss (bounds from the ELF, prepare())
    def mg6(prog, p):
        a = [hexarg("mg6", prog), f"+vec={(E4C_VEC / f'chain_mg6_p{p}').as_posix()}",
             f"+rd_expect={(E4C_VEC / f'chain_mg6_p{p}' / 'rd_expect.hex').as_posix()}",
             "+rpl_check=1", "+rpl_run_len=600", "+rpl_swap_check=1"]
        if prog in MG6_MEM:
            a += [f"+mem_guard={MG6_MEM[prog][0]:x}", f"+stack_floor={MG6_MEM[prog][1]:x}"]
        return a
    t["soc/mg6_reader"] = ("mbx_soc", mg6("mbx_mg6", 2) + ["+golden=1"] + rd + [
        "+seq_base=0", "+exp_results=36", "+exp_rx=36", "+exp_gaps=0", "+exp_published=36", "+exp_drop=0",
        "+max_cycles=20000000"])
    t["soc/mg6_noreader"] = ("mbx_soc", mg6("mbx_mg6", 2) + ["+golden=1", "+seq_base=0", "+exp_results=36",
                                                             "+exp_published=16", "+exp_drop=20",
                                                             "+max_cycles=20000000"])
    # the full E4c-B program: six segments x four runs x three decisions = 72 results (flow mode;
    # mbx_mg6_rt differs only in the 2 kHz pacing and its timing checks)
    t["soc/mg6_p4_reader"] = ("mbx_soc", mg6("mbx_mg6_p4", 4) + ["+golden=1"] + rd + [
        "+seq_base=0", "+exp_results=72", "+exp_rx=72", "+exp_gaps=0", "+exp_published=72", "+exp_drop=0",
        "+max_cycles=40000000"])
    for name, addr, kind in TRAPS:
        t[f"trap/{name}"] = ("mbx_soc", [hexarg("mbx_trap", f"trap_{name}"), "+exp_trap=1",
                                         f"+exp_cause={CAUSE[kind]}", f"+exp_tval={addr:08x}"])
    for k in ("mmio", "flow_reader", "loop2_noreader", "loop2_fast", "loop2_slow", "restart_reader", "wrap",
              "cpu_reset_midwrite", "rt2k_reader"):
        t[f"xsim/{k}"] = ("xsim", t[f"soc/{k}"][1])
    # real Block Memory Generator collision behaviour (no forced corruption)
    t["xsim/collide"] = ("xsim", [m("mbx_loop2")] + rg + rd + ["+rd_gap=0", "+rd_hammer=8", "+exp_rx=22"])
    for name in ("store_before_mbx", "load_after_mbx", "load_misaligned"):
        t[f"xsim/trap_{name}"] = ("xsim", t[f"trap/{name}"][1])
    t["xsim/mg6_reader"] = ("xsim", t["soc/mg6_reader"][1])
    # real 2 kHz frame rate, one run per segment (18 results, about 180 M cycles)
    t["xsim/mg6_rt1_reader"] = ("xsim", mg6("mbx_mg6_rt1", 1) + ["+golden=1"] + rd + [
        "+seq_base=0", "+rd_gap=2000", "+exp_results=18", "+exp_rx=18", "+exp_gaps=0", "+exp_published=18",
        "+exp_drop=0", "+max_cycles=200000000"])
    return t


def prepare() -> None:
    e3c.prepare()
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "gen" / "gen_mailbox.py"), "--check"],
                       capture_output=True, text=True)
    if r.returncode:
        sys.exit("mailbox constants out of date (scripts/gen/gen_mailbox.py):\n" + r.stdout)
    src = BUILD / "trap_src"
    src.mkdir(parents=True, exist_ok=True)
    for f in src.glob("*.c"):
        f.unlink()
    for name, addr, kind in TRAPS:
        # misaligned: lw / sw in assembly (with a constant misaligned address the compiler
        # would legally split the access into byte / halfword accesses)
        if kind == "st":
            body = f"*(volatile uint32_t *)0x{addr:08X}u = 0x5A5A5A5Au;"
        elif kind == "ld":
            body = f"v = *(volatile uint32_t *)0x{addr:08X}u;"
        elif kind == "mst":
            body = f'__asm__ volatile ("sw %0, 0(%1)" : : "r"(0x5A5A5A5Au), "r"(0x{addr:08X}u) : "memory");'
        else:
            body = f'{{ uint32_t t; __asm__ volatile ("lw %0, 0(%1)" : "=r"(t) : "r"(0x{addr:08X}u) : "memory"); v = t; }}'
        (src / f"trap_{name}.c").write_text(
            "/* generated by scripts/sim/run_mbx.py: must trap before any bus access */\n"
            "#include <stdint.h>\n#include \"rv32_soc.h\"\n"
            f"int main(void)\n{{\n    volatile uint32_t v = 0;\n    {body}\n    (void)v;\n    return 99;\n}}\n",
            encoding="ascii")
    for s, o in ((REPO / "firmware" / "rv32" / "mailbox", SW / "mbx"), (src, SW / "mbx_trap")):
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "sw" / "build_asm_tests.py"), "--kind", "c", "-q",
                            "--src", str(s), "--out", str(o)], capture_output=True, text=True)
        if r.returncode:
            sys.exit(r.stdout + r.stderr)
    # E4c programs need the generated data header (scripts/sim/gen_e4c.py -> build/e4c/fw)
    if not (E4C_FW / "mg6_data.h").is_file():
        print("note: build/e4c/fw/mg6_data.h missing (python scripts/sim/gen_e4c.py): mg6 programs not built")
        return
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "sw" / "build_asm_tests.py"), "--kind", "c", "-q",
                        "--src", str(REPO / "firmware" / "rv32" / "mg6"), "--out", str(SW / "mg6"),
                        "--inc", str(E4C_FW)], capture_output=True, text=True)
    if r.returncode:
        sys.exit(r.stdout + r.stderr)
    nm = e3c.VIV_BIN.parent.parent / "gnu" / "riscv" / "nt" / "bin" / "riscv64-unknown-elf-nm.exe"
    for elf in (SW / "mg6").glob("*/*.elf"):
        syms = {f[2]: int(f[0], 16) for f in (l.split() for l in subprocess.run(
            [str(nm), str(elf)], capture_output=True, text=True).stdout.splitlines()) if len(f) == 3}
        MG6_MEM[elf.stem] = (syms["__bss_start"], syms["__bss_end"])


# ---------------------------------------------------------------- xsim
def xsim_prepare(rebuild: bool) -> None:
    if rebuild or not XSIM_DIR.is_dir():
        XPROJ.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run([str(e3c.VIV_BIN / "vivado.bat"), "-mode", "batch", "-nojournal",
                            "-log", str(XPROJ.parent / "create_mbx.log"),
                            "-source", str(REPO / "fpga" / "kv260" / "tcl" / "create_mbx_project.tcl"),
                            "-tclargs", REPO.as_posix(), XPROJ.as_posix()],
                           cwd=XPROJ.parent, capture_output=True, text=True, env=e3c.tool_env())
        if r.returncode or not XSIM_DIR.is_dir():
            sys.exit("create_mbx_project failed: " + r.stdout[-3000:])
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
    cmd = ["cmd", "/c", str(e3c.VIV_BIN / "xsim.bat"), "tb_mbx_soc_behav", "-R", "-f", opts.name,
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
    ap.add_argument("-j", "--jobs", type=int, default=8)
    ns = ap.parse_args()
    all_t = tests()
    if ns.list:
        for k, (tg, _) in all_t.items():
            print(f"{k:32s} {tg}")
        return 0
    prepare()
    all_t = tests()                       # again: the mg6 memory bounds come from the built ELFs
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
            futs = {ex.submit(e3c.run_icarus, k, vvps[tg], plus, runs / k.replace("/", "__"),
                              7200 if "rt2k" in k else 3600): k for k, (tg, plus) in sel.items()}
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
