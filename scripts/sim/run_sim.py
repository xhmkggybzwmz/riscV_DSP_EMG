#!/usr/bin/env python3
"""Run the self-checking Verilog testbenches with Icarus Verilog, and compare
every program run with the reference ISS (reference/rv32i_iss).

Test names
    iss/selftest                  reference-model self-test (python, no RTL)
    unit/<module>@<seed>          CPU module unit tests (regfile, alu, decoder, lsu)
    core/<prog>/zero              directed program (tests/cpu/asm), zero-wait memory
    core/<prog>/rand@<seed>       random ready (50 %), latency 1..4
    core/<prog>/slow@<seed>       random ready (25 %), latency 1..8
    core/<prog>/reset_<mode>@<s>  rst asserted at a bus condition, program runs again
                                  (modes: dmem dmem_wait dmem_rsp imem_wait imem_pending imem_rsp)
    gen/<prog>/{zero,slow@s}      generated one-exception programs (scripts/gen/gen_trap_programs.py)
    rand/<prog>/{zero,slow@s}     constrained-random programs (scripts/gen/gen_random_rv32i.py)
    c/<prog>/zero                 bare-metal C programs (firmware/rv32/apps) on the core bench
    soc/<group>/<prog>            the same programs on the SoC subsystem (tb_soc, Icarus):
                                  real BRAM + router + cmi2axil + SYSCTRL, m_axi connected
                                  DIRECTLY to SYSCTRL (no interconnect: this does not test the
                                  AMD SmartConnect; that is done in Vivado xsim, see
                                  scripts/sim/run_soc_xsim.py). Programs whose expected fault
                                  address lies in the AXI region, or that need error windows,
                                  are not run here.
    soc/core/loop/reset_<mode>@s  SoC reset tests (imem_rsp, dmem_rsp)

Examples (from any directory):
    python scripts/sim/run_sim.py                    # everything, seeds 1,2,3
    python scripts/sim/run_sim.py unit               # all unit tests
    python scripts/sim/run_sim.py core/alu "core/*/zero"
    python scripts/sim/run_sim.py rand --rand-seed 500 --rand-count 100
    python scripts/sim/run_sim.py --seeds 7          # one seed
    python scripts/sim/run_sim.py --list

A selection pattern matches a test if it equals the name, is a prefix followed
by "/" or "@", or matches as a shell wildcard.

A test passes only if
    - iverilog compiles it with no errors (and no warnings unless --allow-warnings),
    - vvp exits with status 0 within the timeout,
    - the log has exactly one "TEST_RESULT: PASS" line and no "ERROR:" line,
    - (program tests) the RTL event trace (retire/trap events, data accesses with
      written bytes, end condition) and the final RAM image are identical to the
      reference ISS run of the same image (scripts/sim/trace_compare.py), and the
      TRACE hash equals that of every other passing run of the same program.
Programs are (re)generated and built first unless --no-build is given.
Exit status: 0 if every selected test passes, 1 otherwise, 2 for usage/tool errors.

Tool lookup: $RISCV_EMG_IVERILOG_BIN (directory with iverilog/vvp) if set, else PATH.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_DIR = REPO_ROOT / "build" / "sim"
ISS_DIR = REPO_ROOT / "build" / "iss"
sys.path.insert(0, str(REPO_ROOT / "reference" / "rv32i_iss"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rv32i_iss          # noqa: E402  reference model
import trace_compare      # noqa: E402

COMMON_INCDIRS = ["dv/common"]
# -g2001: RTL and testbenches are Verilog-2001. iverilog's -g2001 is not a full
# syntax checker; scripts/lint/check_verilog2001.py covers what it lets through.
IVERILOG_FLAGS = ["-g2001", "-Wall"]


@dataclass
class TestSpec:
    top: str
    sources: list[str]
    filelists: list[str] = field(default_factory=list)
    incdirs: list[str] = field(default_factory=list)
    plusargs: list[str] = field(default_factory=list)
    seed: int = 1
    program: str = ""          # program tests: "<group>/<program>"
    args_file: str = ""        # program tests: .args file (image + tb_* symbols)
    kind: str = "vvp"          # "vvp" or "python"
    compare: bool = True       # program tests: compare with the ISS


# ---------------------------------------------------------------------------
# Test registry
# ---------------------------------------------------------------------------
UNIT_TESTS = {
    "regfile": ("tb_rv32i_regfile", ["dv/cpu/unit/tb_rv32i_regfile.v"], []),
    "alu":     ("tb_rv32i_alu",     ["dv/cpu/unit/tb_rv32i_alu.v"], []),
    "decoder": ("tb_rv32i_decoder", ["dv/cpu/unit/tb_rv32i_decoder.v"], ["dv/cpu/unit"]),
    "lsu":     ("tb_rv32i_lsu",     ["dv/cpu/unit/tb_rv32i_lsu.v"], []),
}

# SoC (stage 5) unit tests: compiled with rtl/cpu/cpu.f + rtl/soc/soc.f
SOC_UNIT_TESTS = {
    "soc_ram":     ("tb_soc_ram_dp", ["dv/common/tb_cmi_monitor.v", "dv/soc/tb_soc_ram_dp.v"], []),
    "soc_router":  ("tb_soc_dmem_router", ["dv/common/tb_cmi_monitor.v", "dv/common/tb_cmi_slave.v",
                                           "dv/soc/tb_soc_dmem_router.v"], []),
    "cmi2axil":    ("tb_cmi2axil", ["dv/common/tb_cmi_monitor.v", "dv/common/tb_axil_slave_bfm.v",
                                    "dv/common/tb_axil_monitor.v", "dv/soc/tb_cmi2axil.v"], []),
    "soc_sysctrl": ("tb_soc_sysctrl", ["dv/common/tb_axil_monitor.v", "dv/soc/tb_soc_sysctrl.v"], []),
}

SOC_TB = ("tb_soc", ["dv/common/tb_cmi_monitor.v", "dv/common/tb_axil_monitor.v", "dv/soc/tb_soc.v"])
SOC_RESET_MODES = ["imem_rsp", "dmem_rsp"]          # reachable with the 1-cycle BRAM
# programs that use SYSCTRL registers other than TOHOST: SoC only, no ISS comparison
# (the ISS does not model SYSCTRL); they check themselves and report through TOHOST
SOC_ONLY_PROGRAMS = {"c/c_sysctrl"}

CORE_TB = ("tb_rv32i_core",
           ["dv/common/tb_mem_model.v", "dv/common/tb_cmi_monitor.v",
            "dv/cpu/core/tb_rv32i_core.v"])

# name -> (plusargs, uses seed)
CORE_CONFIGS = {
    "zero": (["+ready_pct=100", "+lat_min=1", "+lat_max=1"], False),
    "rand": (["+ready_pct=50", "+lat_min=1", "+lat_max=4"], True),
    "slow": (["+ready_pct=25", "+lat_min=1", "+lat_max=8"], True),
}
RESET_PROGRAMS = ["loop", "loadstore"]          # restartable programs
RESET_MODES = ["dmem", "dmem_wait", "dmem_rsp", "imem_wait", "imem_pending", "imem_rsp"]

# group -> (source dir, build dir, generator script or None)
PROGRAM_GROUPS = {
    "core": ("tests/cpu/asm", "build/sw/cpu", None),
    "gen":  ("build/gen/trap", "build/sw/gen_trap", "scripts/gen/gen_trap_programs.py"),
    "rand": ("build/gen/random", "build/sw/random", "scripts/gen/gen_random_rv32i.py"),
    "c":    ("firmware/rv32/apps", "build/sw/c", None),
}


def read_args(args_file: Path) -> dict:
    out = {}
    if args_file.is_file():
        for line in args_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("+") and "=" in line:
                k, v = line[1:].split("=", 1)
                out[k] = v
    return out


def soc_compatible(args_file: Path) -> bool:
    """Can this program run on the SoC memory map with the same expectations?

    Unmapped data addresses are rejected by soc_dmem_router (CMI error, no AXI
    transaction), so they trap as in the core test bench, with or without the
    SmartConnect. Only accesses that hit a real SYSCTRL register behave
    differently (they succeed in the SoC)."""
    a = read_args(args_file)
    if any(k in a for k in ("tb_i_err_lo", "tb_d_err_lo")):
        return False                                   # no error-injection windows in the SoC
    cause = int(a["tb_expect_cause"], 16) if "tb_expect_cause" in a else None
    tval = int(a.get("tb_expect_tval", "0"), 16)
    if cause in (5, 7):
        if 0x1000_0000 <= tval <= 0x1000_0FFF:
            return False                               # a real SYSCTRL register in the SoC
    return True


def unit_tests(seeds: list[int]) -> dict[str, TestSpec]:
    tests = {"iss/selftest": TestSpec(top="", sources=["reference/rv32i_iss/test_iss.py"],
                                      kind="python")}
    for name, (top, srcs, inc) in UNIT_TESTS.items():
        for s in seeds:
            tests[f"unit/{name}@{s}"] = TestSpec(top=top, sources=srcs,
                                                 filelists=["rtl/cpu/cpu.f"],
                                                 incdirs=inc, seed=s)
    for name, (top, srcs, inc) in SOC_UNIT_TESTS.items():
        for s in seeds:
            tests[f"unit/{name}@{s}"] = TestSpec(top=top, sources=srcs,
                                                 filelists=["rtl/cpu/cpu.f", "rtl/soc/soc.f"],
                                                 incdirs=inc, seed=s)
    return tests


def group_programs(group: str) -> list[str]:
    src = REPO_ROOT / PROGRAM_GROUPS[group][0]
    pattern = "*.c" if group == "c" else "*.S"
    return sorted(p.stem for p in src.glob(pattern)) if src.is_dir() else []


def core_programs() -> list[str]:
    return group_programs("core")


def program_tests(group: str, seeds: list[int]) -> dict[str, TestSpec]:
    tests = {}
    top, srcs = CORE_TB
    out_dir = REPO_ROOT / PROGRAM_GROUPS[group][1]
    for prog in group_programs(group):
        if f"{group}/{prog}" in SOC_ONLY_PROGRAMS:
            continue
        args_file = out_dir / prog / f"{prog}.args"

        def spec(extra: list[str], seed: int) -> TestSpec:
            return TestSpec(top=top, sources=srcs, filelists=["rtl/cpu/cpu.f"],
                            plusargs=[f"@{args_file.as_posix()}", *extra], seed=seed,
                            program=f"{group}/{prog}", args_file=args_file.as_posix())

        if group == "core":
            for cfg, (pa, seeded) in CORE_CONFIGS.items():
                if seeded:
                    for s in seeds:
                        tests[f"core/{prog}/{cfg}@{s}"] = spec(pa, s)
                else:
                    tests[f"core/{prog}/{cfg}"] = spec(pa, 1)
            if prog in RESET_PROGRAMS:
                for mode in RESET_MODES:
                    for s in seeds:
                        tests[f"core/{prog}/reset_{mode}@{s}"] = spec(
                            ["+reset_test=1", f"+reset_mode={mode}",
                             f"+reset_after={150 + 97 * (s % 23)}",
                             "+ready_pct=60", "+lat_min=2", "+lat_max=6"], s)
        elif group == "c":
            tests[f"c/{prog}/zero"] = spec(CORE_CONFIGS["zero"][0], 1)
        else:
            tests[f"{group}/{prog}/zero"] = spec(CORE_CONFIGS["zero"][0], 1)
            tests[f"{group}/{prog}/slow@{seeds[0]}"] = spec(CORE_CONFIGS["slow"][0], seeds[0])
    return tests


def soc_tests(seeds: list[int]) -> dict[str, TestSpec]:
    """Programs of every group on the SoC subsystem (Icarus, direct AXI connection)."""
    tests = {}
    top, srcs = SOC_TB
    for group in PROGRAM_GROUPS:
        out_dir = REPO_ROOT / PROGRAM_GROUPS[group][1]
        for prog in group_programs(group):
            args_file = out_dir / prog / f"{prog}.args"
            if args_file.is_file() and not soc_compatible(args_file):
                continue

            def spec(extra: list[str], seed: int) -> TestSpec:
                return TestSpec(top=top, sources=srcs, filelists=["rtl/cpu/cpu.f", "rtl/soc/soc.f"],
                                plusargs=[f"@{args_file.as_posix()}", *extra], seed=seed,
                                program=f"{group}/{prog}", args_file=args_file.as_posix(),
                                compare=f"{group}/{prog}" not in SOC_ONLY_PROGRAMS)

            tests[f"soc/{group}/{prog}"] = spec([], 1)
            if f"{group}/{prog}" == "c/c_sysctrl":        # reset while an AXI access is pending
                for s in seeds:
                    tests[f"soc/c/{prog}/reset_dmem@{s}"] = spec(
                        ["+reset_test=1", "+reset_mode=dmem", f"+reset_after={150 + 211 * (s % 5)}"], s)
            if group == "core" and prog in RESET_PROGRAMS:
                for mode in SOC_RESET_MODES:
                    for s in seeds:
                        tests[f"soc/core/{prog}/reset_{mode}@{s}"] = spec(
                            ["+reset_test=1", f"+reset_mode={mode}", f"+reset_after={150 + 97 * (s % 23)}"], s)
    return tests


def core_tests(seeds: list[int], programs: list[str] | None = None) -> dict[str, TestSpec]:
    """Directed core tests (kept for scripts/sim/run_mutants.py)."""
    t = program_tests("core", seeds)
    if programs is not None:
        t = {k: v for k, v in t.items() if k.split("/")[1] in programs}
    return t


def select(names: list[str], patterns: list[str]) -> list[str]:
    if not patterns:
        return names
    chosen = []
    for n in names:
        for p in patterns:
            if n == p or n.startswith(p + "/") or n.startswith(p + "@") or fnmatch.fnmatchcase(n, p):
                chosen.append(n)
                break
    return chosen


# ---------------------------------------------------------------------------
# Tools / compile cache
# ---------------------------------------------------------------------------
def find_tool(name: str) -> str:
    env_dir = os.environ.get("RISCV_EMG_IVERILOG_BIN")
    if env_dir:
        for cand in (Path(env_dir) / name, Path(env_dir) / (name + ".exe")):
            if cand.is_file():
                return str(cand)
        sys.exit(f"run_sim: {name} not found in RISCV_EMG_IVERILOG_BIN={env_dir}")
    found = shutil.which(name)
    if not found:
        sys.exit(f"run_sim: {name} not found on PATH (or set RISCV_EMG_IVERILOG_BIN)")
    return found


def parse_filelist(path: str) -> tuple[list[str], list[str]]:
    """Return (sources, incdirs) from a .f file; paths are repo-root relative."""
    sources: list[str] = []
    incdirs: list[str] = []
    full = REPO_ROOT / path
    if not full.is_file():
        raise FileNotFoundError(f"file list not found: {path}")
    for raw in full.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].split("//", 1)[0].strip()
        if not line:
            continue
        if line.startswith("+incdir+"):
            incdirs.extend(d for d in line[len("+incdir+"):].split("+") if d)
        else:
            sources.append(line)
    return sources, incdirs


_compile_cache: dict[tuple, tuple[bool, Path, Path, int, str]] = {}


def compile_tb(spec: TestSpec, iverilog: str, wave: bool, out_dir: Path | None = None):
    """Compile once per unique (top, sources, incdirs, wave). Returns
    (ok, vvp_path, log_path, warnings, reason)."""
    sources: list[str] = []
    incdirs: list[str] = []
    for fl in spec.filelists:
        s, i = parse_filelist(fl)
        sources += s
        incdirs += i
    sources += spec.sources
    incdirs = list(dict.fromkeys(incdirs + spec.incdirs + COMMON_INCDIRS))
    key = (spec.top, tuple(sources), tuple(incdirs), wave)
    if key in _compile_cache:
        return _compile_cache[key]

    out = out_dir or (BUILD_DIR / "_compile" / f"{spec.top}{'_wave' if wave else ''}_{len(_compile_cache)}")
    out.mkdir(parents=True, exist_ok=True)
    vvp_file, log = out / "sim.vvp", out / "compile.log"
    vvp_file.unlink(missing_ok=True)
    for src in sources:
        if not (REPO_ROOT / src).is_file():
            res = (False, vvp_file, log, 0, f"missing source {src}")
            _compile_cache[key] = res
            return res
    cmd = [iverilog, *IVERILOG_FLAGS, "-s", spec.top, "-o", str(vvp_file)]
    cmd += [f"-I{d}" for d in incdirs]
    if wave:
        cmd.append("-DDUMP_WAVES")
    cmd += sources
    comp = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    text = comp.stdout + comp.stderr
    log.write_text("$ " + subprocess.list2cmdline(cmd) + "\n" + text, encoding="utf-8")
    warnings = len([l for l in text.splitlines() if re.search(r"warning", l, re.I)])
    ok = comp.returncode == 0 and vvp_file.is_file()
    res = (ok, vvp_file, log, warnings, "" if ok else f"compile failed (exit {comp.returncode})")
    _compile_cache[key] = res
    return res


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------
RESULT_RE = re.compile(r"^TEST_RESULT: (PASS|FAIL) checks=(\d+) errors=(\d+)", re.M)
TRACE_RE = re.compile(r"^TRACE: retired=(\d+) hash=([0-9a-fA-Fx]+)", re.M)
PERF_RE = re.compile(r"^PERF: (.*)$", re.M)


@dataclass
class Result:
    name: str
    status: str            # PASS / FAIL
    checks: int = 0
    errors: int = 0
    warnings: int = 0
    seconds: float = 0.0
    reason: str = ""
    log: Path | None = None
    trace: tuple[str, str] | None = None
    perf: str = ""
    program: str = ""
    compared: int = 0          # events compared with the ISS (0 = not compared)


def expand_plusargs(items: list[str]) -> list[str]:
    """'@file' entries are replaced by the lines of that file."""
    out = []
    for it in items:
        if it.startswith("@"):
            p = Path(it[1:])
            if not p.is_file():
                raise FileNotFoundError(f"plusarg file not found: {p}")
            out += [l.strip() for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        else:
            out.append(it)
    return out


_iss_cache: dict[str, tuple[Path, Path]] = {}


def iss_reference(spec: TestSpec) -> tuple[Path, Path]:
    """Run the ISS once per program image; return (trace, mem) paths."""
    if spec.args_file in _iss_cache:
        return _iss_cache[spec.args_file]
    out = ISS_DIR / spec.program
    out.mkdir(parents=True, exist_ok=True)
    iss, lines = rv32i_iss.run(Path(spec.args_file))
    trace, mem = out / "iss.trace", out / "iss.mem"
    trace.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
    words = [int.from_bytes(iss.mem[i:i + 4], "little") for i in range(0, iss.mem_bytes, 4)]
    mem.write_text("".join(f"{w:08x}\n" for w in words), encoding="ascii", newline="\n")
    _iss_cache[spec.args_file] = (trace, mem)
    return trace, mem


def run_python_test(name: str, spec: TestSpec, timeout: float) -> "Result":
    t0 = time.monotonic()
    out_dir = BUILD_DIR / name.replace("/", "__")
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(REPO_ROOT / spec.sources[0])]
    res = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout)
    text = res.stdout + res.stderr
    log = out_dir / "sim.log"
    log.write_text("$ " + subprocess.list2cmdline(cmd) + "\n" + text, encoding="utf-8")
    m = re.search(r"ISS_SELFTEST: (PASS|FAIL) checks=(\d+) errors=(\d+)", text)
    ok = res.returncode == 0 and m is not None and m.group(1) == "PASS"
    return Result(name, "PASS" if ok else "FAIL", int(m.group(2)) if m else 0,
                  int(m.group(3)) if m else 0, 0, time.monotonic() - t0,
                  "" if ok else f"self-test failed (exit {res.returncode})", log)


def run_test(name: str, spec: TestSpec, iverilog: str, vvp: str, seed: int,
             wave: bool, allow_warnings: bool, timeout: float,
             extra_plusargs: list[str], private_compile: bool = False) -> Result:
    if spec.kind == "python":
        return run_python_test(name, spec, timeout)
    t0 = time.monotonic()
    out_dir = BUILD_DIR / name.replace("/", "__").replace("@", "_s")
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in ("sim.log", "wave.vcd", "rtl.trace", "rtl.mem", "compare.log"):
        (out_dir / stale).unlink(missing_ok=True)

    ok, vvp_file, clog, warnings, reason = compile_tb(
        spec, iverilog, wave, out_dir if private_compile else None)
    if not ok:
        return Result(name, "FAIL", warnings=warnings, seconds=time.monotonic() - t0,
                      reason=reason, log=clog, program=spec.program)
    if warnings and not allow_warnings:
        return Result(name, "FAIL", warnings=warnings, seconds=time.monotonic() - t0,
                      reason=f"{warnings} compile warning(s)", log=clog, program=spec.program)

    try:
        plusargs = expand_plusargs(spec.plusargs)
    except FileNotFoundError as exc:
        return Result(name, "FAIL", reason=str(exc), program=spec.program)
    if spec.program:
        plusargs += [f"+trace_file={(out_dir / 'rtl.trace').as_posix()}",
                     f"+mem_dump={(out_dir / 'rtl.mem').as_posix()}"]
    run_cmd = [vvp, "-n", str(vvp_file), f"+seed={seed}", *plusargs, *extra_plusargs]
    try:
        sim = subprocess.run(run_cmd, cwd=out_dir, capture_output=True, text=True,
                             timeout=timeout)
        sim_text, rc = sim.stdout + sim.stderr, sim.returncode
    except subprocess.TimeoutExpired as exc:
        sim_text = exc.stdout if isinstance(exc.stdout, str) else ""
        rc = None
    log = out_dir / "sim.log"
    log.write_text(f"# compile log: {clog.relative_to(REPO_ROOT).as_posix()}\n"
                   "$ " + subprocess.list2cmdline(run_cmd) + "\n" + sim_text, encoding="utf-8")
    elapsed = time.monotonic() - t0

    results = RESULT_RE.findall(sim_text)
    error_lines = [l for l in sim_text.splitlines() if l.startswith("ERROR:")]
    checks = int(results[-1][1]) if results else 0
    errors = int(results[-1][2]) if results else len(error_lines)
    tr = TRACE_RE.search(sim_text)
    pf = PERF_RE.search(sim_text)

    reason = ""
    if rc is None:
        reason = f"simulator killed after {timeout:.0f} s timeout"
    elif len(results) != 1:
        reason = f"expected exactly one TEST_RESULT line, found {len(results)}"
    elif results[0][0] != "PASS":
        reason = "testbench reported FAIL"
    elif error_lines:
        reason = f"{len(error_lines)} ERROR line(s) despite PASS"
    elif rc != 0:
        reason = f"vvp exit status {rc}"
    elif checks == 0:
        reason = "no checks executed"
    elif spec.program and not tr:
        reason = "no TRACE line"
    compared = 0
    if spec.program and spec.compare and not reason and not (out_dir / "rtl.trace").is_file():
        reason = "RTL trace file missing"
    if spec.program and spec.compare and not reason:
        iss_trace, iss_mem = iss_reference(spec)
        dump = Path(spec.args_file).with_suffix(".dump")
        ok, text, compared = trace_compare.compare(out_dir / "rtl.trace", iss_trace,
                                                   out_dir / "rtl.mem", iss_mem, dump)
        (out_dir / "compare.log").write_text(text + "\n", encoding="utf-8")
        if not ok:
            reason = "RTL/ISS mismatch (see compare.log)"
            log = out_dir / "compare.log"
    return Result(name, "FAIL" if reason else "PASS", checks, errors, warnings, elapsed,
                  reason, log, (tr.group(1), tr.group(2).lower()) if tr else None,
                  pf.group(1) if pf else "", spec.program, compared)


def check_traces(results: list[Result]) -> None:
    """All passing runs of one program must have the same retire trace."""
    by_prog: dict[str, list[Result]] = {}
    for r in results:
        if r.program and r.status == "PASS":
            by_prog.setdefault(r.program, []).append(r)
    for prog, runs in by_prog.items():
        ref = next((r for r in runs if r.name.endswith("/zero")), runs[0])
        for r in runs:
            if r.trace != ref.trace:
                r.status = "FAIL"
                r.reason = (f"trace {r.trace} differs from {ref.name} {ref.trace}")


def parse_seeds(text: str) -> list[int]:
    seeds = []
    for item in text.split(","):
        item = item.strip()
        if item == "random":
            seeds.append(random.randrange(1, 2**31 - 1))
        elif item:
            seeds.append(int(item, 0))
    if not seeds:
        raise ValueError("empty seed list")
    return seeds


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tests", nargs="*", help="test names / prefixes / wildcards (default: all)")
    ap.add_argument("--list", action="store_true", help="list the selected tests and exit")
    ap.add_argument("--seeds", default="1,2,3",
                    help="comma-separated seeds, 'random' allowed (default 1,2,3)")
    ap.add_argument("--seed", help="single seed (same as --seeds N)")
    ap.add_argument("--wave", action="store_true", help="dump wave.vcd (defines DUMP_WAVES)")
    ap.add_argument("--allow-warnings", action="store_true",
                    help="do not fail on iverilog compile warnings")
    ap.add_argument("--timeout", type=float, default=300.0,
                    help="per-test simulator timeout in seconds (default 300)")
    ap.add_argument("--plusarg", action="append", default=[], metavar="ARG",
                    help="extra +ARG for every test (repeatable), e.g. --plusarg tb_selftest_fail")
    ap.add_argument("--no-build", action="store_true",
                    help="do not regenerate/rebuild the test programs")
    ap.add_argument("--rand-seed", type=int, default=1, help="first random-program seed (default 1)")
    ap.add_argument("--rand-count", type=int, default=40, help="random programs ending normally (default 40)")
    ap.add_argument("--rand-trap-count", type=int, default=20,
                    help="random programs ending in a trap (default 20)")
    args = ap.parse_args()

    try:
        seeds = parse_seeds(args.seed if args.seed else args.seeds)
    except ValueError:
        print("run_sim: --seeds must be integers or 'random'", file=sys.stderr)
        return 2

    if not args.no_build:
        print("generating programs ...", flush=True)
        for group, (_, _, gen) in PROGRAM_GROUPS.items():
            if gen is None:
                continue
            cmd = [sys.executable, str(REPO_ROOT / gen)]
            if group == "rand":
                cmd += ["--seed", str(args.rand_seed), "--count", str(args.rand_count),
                        "--trap-count", str(args.rand_trap_count)]
            g = subprocess.run(cmd, capture_output=True, text=True)
            print("  " + ((g.stdout + g.stderr).strip().splitlines() or ["(no output)"])[-1])
            if g.returncode != 0:
                print(g.stdout + g.stderr)
                return 1

    registry = unit_tests(seeds)
    for group in PROGRAM_GROUPS:
        registry.update(program_tests(group, seeds))
    registry.update(soc_tests(seeds))
    names = select(list(registry), args.tests)
    if args.tests and not names:
        print(f"run_sim: no test matches {' '.join(args.tests)}; use --list", file=sys.stderr)
        return 2
    if args.list:
        for n in names:
            print(n)
        print(f"{len(names)} test(s)")
        return 0

    groups_used = sorted({registry[n].program.split("/")[0] for n in names if registry[n].program})
    if groups_used and not args.no_build:
        print("building programs (scripts/sw/build_asm_tests.py) ...", flush=True)
        for group in groups_used:
            src, out, _ = PROGRAM_GROUPS[group]
            b = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "sw" / "build_asm_tests.py"),
                                "-q", "--kind", "c" if group == "c" else "asm",
                                "--src", str(REPO_ROOT / src), "--out", str(REPO_ROOT / out)],
                               capture_output=True, text=True)
            tail = (b.stdout + b.stderr).strip().splitlines()
            print(f"  {group}: " + (tail[-1] if tail else "(no output)"))
            if b.returncode != 0:
                print(b.stdout + b.stderr)
                print("run_sim: program build failed", file=sys.stderr)
                return 1

    iverilog, vvp = find_tool("iverilog"), find_tool("vvp")
    version = subprocess.run([iverilog, "-V"], capture_output=True, text=True).stdout
    version = version.splitlines()[0] if version else "unknown"
    print(f"iverilog : {iverilog}")
    print(f"version  : {version} ({' '.join(IVERILOG_FLAGS)})")
    print(f"seeds    : {','.join(map(str, seeds))}")
    print(f"tests    : {len(names)}")
    print(f"build dir: {BUILD_DIR.relative_to(REPO_ROOT).as_posix()}")
    print()

    extra = [f"+{p.lstrip('+')}" for p in args.plusarg]
    results = []
    for name in names:
        spec = registry[name]
        res = run_test(name, spec, iverilog, vvp, spec.seed, args.wave,
                       args.allow_warnings, args.timeout, extra)
        results.append(res)
        line = (f"[{res.status}] {name:34s} checks={res.checks:<8d} errors={res.errors:<4d} "
                f"{res.seconds:5.1f}s")
        if res.trace:
            line += f"  retired={res.trace[0]} hash={res.trace[1]}"
        if res.compared:
            line += f"  iss=match({res.compared})"
        if res.reason:
            line += f"  -- {res.reason}"
        print(line, flush=True)
        if res.status == "FAIL" and res.log and res.log.is_file():
            tail = res.log.read_text(encoding="utf-8").splitlines()[-25:]
            print("    | " + "\n    | ".join(tail))

    check_traces(results)
    late = [r for r in results if r.reason.startswith("trace ")]
    for r in late:
        print(f"[FAIL] {r.name} -- {r.reason}")

    perf_rows = [r for r in results if r.program and r.name.endswith("/zero") and r.perf]
    if perf_rows:
        print("\nPERF (zero-wait memory):")
        for r in perf_rows:
            print(f"  {r.program:24s} {r.perf}")

    n_cmp = sum(1 for r in results if r.compared and r.status == "PASS")
    n_ev = sum(r.compared for r in results if r.status == "PASS")
    n_prog = sum(1 for r in results if r.program)
    print(f"\nISS COMPARISON: {n_cmp}/{n_prog} program runs identical to the reference ISS "
          f"({n_ev} events compared)")
    failed = [r.name for r in results if r.status != "PASS"]
    summary = (f"\nSUMMARY: {len(results) - len(failed)}/{len(results)} passed, "
               f"seeds={','.join(map(str, seeds))}" + (f", FAILED: {' '.join(failed)}" if failed else ""))
    print(summary)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    (BUILD_DIR / "summary.txt").write_text(
        "\n".join(f"{r.status} {r.name} checks={r.checks} errors={r.errors} "
                  f"{('trace=' + '/'.join(r.trace)) if r.trace else ''} {r.reason}".rstrip()
                  for r in results) + summary + "\n", encoding="utf-8")
    (BUILD_DIR / "summary.json").write_text(json.dumps(
        [{"name": r.name, "status": r.status, "checks": r.checks, "errors": r.errors,
          "trace": r.trace, "perf": r.perf, "reason": r.reason, "program": r.program,
          "compared": r.compared,
          "args_file": registry[r.name].args_file} for r in results], indent=1),
        encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
