#!/usr/bin/env python3
"""Run the RV32I SoC block design (with the AMD AXI SmartConnect) in Vivado xsim.

Steps
  1. (re)create the Vivado 2026.1 project and block design if needed:
     fpga/kv260/tcl/create_soc_project.tcl -> build/vivado/soc/soc_sim.xpr
  2. compile and elaborate the exported xsim scripts (Vivado 2026.1 tools,
     placed first on PATH; the version is checked)
  3. run tb_soc (define SOC_BD) once per program with the program's plusargs
     (passed through an -f options file: cmd splits "=" in .bat arguments)
  4. compare every run with the reference ISS (event trace + final RAM), as
     scripts/sim/run_sim.py does; SoC-only programs are self-checking only

Programs: every program of the run_sim groups core/gen/rand/c that is valid on
the SoC memory map WITH an interconnect (accesses to unmapped AXI pages are
included: the SmartConnect must answer DECERR), plus SoC reset tests.
Programs must already be built (run scripts/sim/run_sim.py first).

Usage:
  python scripts/sim/run_soc_xsim.py [patterns...] [--rebuild] [--list]
Exit status 0 if every run passes and matches the ISS.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_sim  # noqa: E402

REPO = run_sim.REPO_ROOT
VIVADO_ROOT = Path(os.environ.get("RISCV_EMG_VIVADO_ROOT", r"E:\Xilinx2026\2026.1\Vivado"))
VIV_BIN = VIVADO_ROOT / "bin"
PROJ = REPO / "build" / "vivado" / "soc"
XSIM_DIR = PROJ / "soc_sim.sim" / "sim_1" / "behav" / "xsim"
RUNS = REPO / "build" / "vivado" / "xsim_runs"


def tool_env() -> dict:
    env = dict(os.environ)
    env["PATH"] = str(VIV_BIN) + os.pathsep + env.get("PATH", "")
    return env


def check_version() -> str:
    out = subprocess.run([str(VIV_BIN / "xvlog.bat"), "--version"], capture_output=True, text=True,
                         env=tool_env(), shell=False).stdout
    first = out.strip().splitlines()[0] if out.strip() else ""
    if "2026.1" not in first:
        sys.exit(f"run_soc_xsim: expected Vivado Simulator 2026.1, got '{first}' (set RISCV_EMG_VIVADO_ROOT)")
    return first


def set_project(path: Path) -> None:
    global PROJ, XSIM_DIR, RUNS
    PROJ = path
    XSIM_DIR = PROJ / "soc_sim.sim" / "sim_1" / "behav" / "xsim"
    RUNS = PROJ.parent / f"xsim_runs_{PROJ.name}"


def build_project(force: bool, strategy: str, probe: int) -> None:
    if (PROJ / "soc_sim.xpr").is_file() and XSIM_DIR.is_dir() and not force:
        return
    PROJ.parent.mkdir(parents=True, exist_ok=True)
    print("creating Vivado project + block design ...", flush=True)
    cmd = [str(VIV_BIN / "vivado.bat"), "-mode", "batch", "-nojournal",
           "-log", str(PROJ.parent / f"create_{PROJ.name}.log"),
           "-source", str(REPO / "fpga" / "kv260" / "tcl" / "create_soc_project.tcl"),
           "-tclargs", REPO.as_posix(), PROJ.as_posix(), "-", strategy, str(probe)]
    r = subprocess.run(cmd, cwd=PROJ.parent, capture_output=True, text=True, env=tool_env())
    if r.returncode != 0 or not XSIM_DIR.is_dir():
        print(r.stdout[-3000:] + r.stderr[-2000:])
        sys.exit("run_soc_xsim: project creation failed (see build/vivado/create_soc_project.log)")


def compile_elaborate() -> None:
    for script in ("compile.bat", "elaborate.bat"):
        print(f"xsim: {script} ...", flush=True)
        # ".\script": cmd does not search the current directory when
        # NoDefaultCurrentDirectoryInExePath is set (it is, in this environment)
        r = subprocess.run(["cmd", "/c", ".\\" + script], cwd=XSIM_DIR, capture_output=True, text=True,
                           env=tool_env())
        log = XSIM_DIR / ("compile.log" if script == "compile.bat" else "elaborate.log")
        text = log.read_text(encoding="utf-8", errors="ignore") if log.is_file() else r.stdout
        errs = [l for l in text.splitlines() if l.startswith("ERROR")]
        if r.returncode != 0 or errs:
            print("\n".join(errs[:20]) or r.stdout[-3000:])
            sys.exit(f"run_soc_xsim: {script} failed")
        warns = [l for l in text.splitlines() if l.startswith("WARNING")]
        print(f"  ok, {len(warns)} warning(s)")
        for w in warns[:10]:
            print("   ", w[:160])


def probe_page_trap(args_file: Path) -> bool:
    """Program expects a data fault in 0x1000_F000-0x1000_FFFF, where the
    verification-only decode-probe variant has a real slave."""
    a = run_sim.read_args(args_file)
    cause = int(a["tb_expect_cause"], 16) if "tb_expect_cause" in a else None
    return cause in (5, 7) and 0x1000_F000 <= int(a.get("tb_expect_tval", "0"), 16) <= 0x1000_FFFF


def job_list(decode_probe: int = 0) -> dict[str, run_sim.TestSpec]:
    jobs = {}
    variant = ["+axi_decode_test"] if decode_probe else []
    for group in ("core", "gen", "rand", "c"):
        out_dir = REPO / run_sim.PROGRAM_GROUPS[group][1]
        for prog in run_sim.group_programs(group):
            args = out_dir / prog / f"{prog}.args"
            if not args.is_file() or not run_sim.soc_compatible(args):
                continue
            if decode_probe and probe_page_trap(args):
                continue
            key = f"{group}/{prog}"
            mk = lambda extra, seed=1: run_sim.TestSpec(
                top="tb_soc", sources=[], plusargs=[f"@{args.as_posix()}", *variant, *extra], seed=seed,
                program=key, args_file=args.as_posix(), compare=key not in run_sim.SOC_ONLY_PROGRAMS)
            jobs[f"xsim/{key}"] = mk([])
            if key in ("core/loop", "core/loadstore"):
                for mode in run_sim.SOC_RESET_MODES:
                    jobs[f"xsim/{key}/reset_{mode}"] = mk(["+reset_test=1", f"+reset_mode={mode}",
                                                          "+reset_after=247"])
            if key == "c/c_sysctrl":
                for s, after in ((1, 361), (2, 572)):
                    jobs[f"xsim/{key}/reset_dmem@{s}"] = mk(["+reset_test=1", "+reset_mode=dmem",
                                                            f"+reset_after={after}"], s)
    return jobs


RESULT_RE = re.compile(r"TEST_RESULT: (PASS|FAIL) checks=(\d+) errors=(\d+)")
TRACE_RE = re.compile(r"TRACE: retired=(\d+) hash=([0-9a-fA-F]+)")
REJ_RE = re.compile(r"INFO: router-rejected data accesses: (\d+)")
AXI_RE = re.compile(r"INFO: AXI cpu side: aw=(\d+) w=(\d+) b=(\d+) \(err (\d+)\) ar=(\d+) r=(\d+) \(err (\d+)\); "
                    r"sysctrl side: aw=(\d+) ar=(\d+)")


def run_job(name: str, spec: run_sim.TestSpec) -> dict:
    t0 = time.monotonic()
    out = RUNS / name.replace("/", "__").replace("@", "_s")
    out.mkdir(parents=True, exist_ok=True)
    for f in ("rtl.trace", "rtl.mem", "sim.log", "compare.log"):
        (out / f).unlink(missing_ok=True)
    plus = run_sim.expand_plusargs(spec.plusargs) + [f"+seed={spec.seed}"]
    if spec.compare:
        plus += [f"+trace_file={(out / 'rtl.trace').as_posix()}", f"+mem_dump={(out / 'rtl.mem').as_posix()}"]
    opts = XSIM_DIR / f"opts_{out.name}.txt"
    opts.write_text("".join(f"-testplusarg {p.lstrip('+')}\n" for p in plus), encoding="ascii")
    cmd = ["cmd", "/c", str(VIV_BIN / "xsim.bat"), "tb_soc_behav", "-R", "-f", opts.name,
           "-log", f"xsim_{out.name}.log"]
    r = subprocess.run(cmd, cwd=XSIM_DIR, capture_output=True, text=True, env=tool_env(), timeout=1200)
    text = r.stdout + r.stderr
    (out / "sim.log").write_text(subprocess.list2cmdline(cmd) + "\n" + opts.read_text() + text, encoding="utf-8")
    opts.unlink(missing_ok=True)
    res = RESULT_RE.search(text)
    tr = TRACE_RE.search(text)
    axi = AXI_RE.search(text)
    rej = REJ_RE.search(text)
    errors = [l for l in text.splitlines() if l.startswith("ERROR:")]
    reason = ""
    if not res:
        reason = "no TEST_RESULT line"
    elif res.group(1) != "PASS" or errors:
        reason = "testbench reported FAIL" if res.group(1) != "PASS" else f"{len(errors)} ERROR line(s)"
    compared = 0
    if not reason and spec.compare:
        if not (out / "rtl.trace").is_file():
            reason = "RTL trace file missing"
        else:
            it, im = run_sim.iss_reference(spec)
            ok, ctext, compared = run_sim.trace_compare.compare(out / "rtl.trace", it, out / "rtl.mem", im,
                                                               Path(spec.args_file).with_suffix(".dump"))
            (out / "compare.log").write_text(ctext + "\n", encoding="utf-8")
            if not ok:
                reason = "RTL/ISS mismatch (see compare.log)"
    return {"name": name, "ok": not reason, "reason": reason, "compared": compared,
            "trace": tr.groups() if tr else None, "axi": axi.groups() if axi else None,
            "rejected": int(rej.group(1)) if rej else 0,
            "seconds": time.monotonic() - t0, "log": out / "sim.log", "errors": errors[:5]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("patterns", nargs="*")
    ap.add_argument("--rebuild", action="store_true", help="recreate the Vivado project")
    ap.add_argument("--no-compile", action="store_true", help="reuse the existing elaborated snapshot")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--project", default="soc", help="project directory name under build/vivado (default soc)")
    ap.add_argument("--strategy", default="AUTOMATIC",
                    help="SmartConnect STRATEGY used when (re)creating the project (default AUTOMATIC)")
    ap.add_argument("--decode-probe", type=int, default=0,
                    help="1: verification-only variant with a second slave at 0x1000_F000 (two segments)")
    ns = ap.parse_args()
    set_project(REPO / "build" / "vivado" / ns.project)

    jobs = job_list(ns.decode_probe)
    names = [n for n in jobs if not ns.patterns or any(
        n == p or n.startswith(p + "/") or fnmatch.fnmatchcase(n, p) for p in ns.patterns)]
    if ns.list:
        print("\n".join(names)); print(f"{len(names)} job(s)")
        return 0
    print(f"xsim     : {check_version()}")
    build_project(ns.rebuild, ns.strategy, ns.decode_probe)
    print(f"project  : {PROJ.relative_to(REPO).as_posix()}")
    if not ns.no_compile:
        compile_elaborate()
    print(f"jobs     : {len(names)}\n")
    results = []
    for n in names:
        r = run_job(n, jobs[n])
        results.append(r)
        line = f"[{'PASS' if r['ok'] else 'FAIL'}] {n:52s} {r['seconds']:5.1f}s"
        if r["trace"]:
            line += f"  retired={r['trace'][0]} hash={r['trace'][1]}"
        if r["compared"]:
            line += f"  iss=match({r['compared']})"
        if r["axi"]:
            a = r["axi"]
            line += f"  axi[w{a[0]} r{a[4]} berr{a[3]} rerr{a[6]} | sysctrl w{a[7]} r{a[8]}]"
        if r.get("rejected"):
            line += f"  router-rej={r['rejected']}"
        if r["reason"]:
            line += f"  -- {r['reason']}"
            for e in r["errors"]:
                line += f"\n    | {e}"
        print(line, flush=True)
    failed = [r["name"] for r in results if not r["ok"]]
    ncmp = sum(1 for r in results if r["compared"])
    print(f"\nISS COMPARISON: {ncmp} runs identical to the ISS "
          f"({sum(r['compared'] for r in results)} events)")
    print(f"SUMMARY: {len(results) - len(failed)}/{len(results)} passed"
          + (f", FAILED: {' '.join(failed)}" if failed else ""))
    RUNS.mkdir(parents=True, exist_ok=True)
    (RUNS / "summary.txt").write_text("\n".join(
        f"{'PASS' if r['ok'] else 'FAIL'} {r['name']} compared={r['compared']} axi={r['axi']} {r['reason']}"
        for r in results) + "\n", encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
