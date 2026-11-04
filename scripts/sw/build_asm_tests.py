#!/usr/bin/env python3
"""Build the RV32I assembly tests in tests/cpu/asm/ for the core testbench.

For every tests/cpu/asm/<name>.S this produces, in build/sw/cpu/<name>/:
    <name>.elf    linked with tests/cpu/common/{crt0.S,link.ld}
    <name>.bin    objcopy -O binary (image starts at address 0)
    <name>.hex    one 32-bit little-endian word per line, for $readmemh
    <name>.dump   objdump -d (no aliases, numeric register names)
    <name>.args   testbench plusargs derived from tb_* symbols (nm)
and build/sw/cpu/manifest.json listing all programs.

Checks performed on every ELF:
    - ELF attribute Tag_RISCV_arch is plain rv32i (no M/A/C/F/D/Zicsr/... )
    - ELF header flags: no RVC, soft-float ABI
    - _start is at the reset vector 0, image fits in 64 KB

Toolchain lookup: $RISCV_EMG_RISCV_GCC_BIN, default: the toolchain bundled
with Vivado 2026.1 (E:/Xilinx2026/2026.1/gnu/riscv/nt/bin).

Usage:  python scripts/sw/build_asm_tests.py [--src DIR --out DIR] [names...]
        (default: tests/cpu/asm -> build/sw/cpu; generated programs use other dirs)
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "tests" / "cpu" / "asm"
COMMON = REPO_ROOT / "tests" / "cpu" / "common"
OUT_DIR = REPO_ROOT / "build" / "sw" / "cpu"
DEFAULT_GCC_BIN = os.environ.get("RISCV_EMG_GCC_BIN") or str(
    Path(os.environ.get("RISCV_EMG_VIVADO_ROOT", r"E:\Xilinx2026\2026.1\Vivado")).parent / "gnu" / "riscv" / "nt" / "bin")
PREFIX = "riscv64-unknown-elf-"
MEM_BYTES = 64 * 1024
RESET_VECTOR = 0

CFLAGS = ["-march=rv32i", "-mabi=ilp32", "-nostdlib", "-nostartfiles", "-static",
          "-Wl,--no-relax", "-Wa,-march=rv32i", "-Wl,--build-id=none",
          "-Wl,--no-warn-rwx-segments", f"-I{COMMON}",
          "-T", str(COMMON / "link.ld")]

# --kind c: bare-metal C programs (firmware/rv32/apps) with firmware/rv32/bsp
BSP = REPO_ROOT / "firmware" / "rv32" / "bsp"
C_FLAGS = ["-march=rv32i", "-mabi=ilp32", "-O2", "-ffreestanding", "-fno-builtin",
           "-nostdlib", "-nostartfiles", "-static", "-Wall", "-Wextra", "-Werror",
           "-Wl,--no-relax", "-Wl,--build-id=none", "-Wl,--no-warn-rwx-segments",
           f"-I{BSP}", "-T", str(BSP / "link.ld")]

EXTRA_INC: list[str] = []          # --inc: extra include directories (e.g. generated headers under build/)

ARCH_RE = re.compile(r'Tag_RISCV_arch:\s*"([^"]+)"')
PLAIN_RV32I_RE = re.compile(r"^rv32i\d+p\d+$")


def tool(name: str) -> str:
    bin_dir = Path(os.environ.get("RISCV_EMG_RISCV_GCC_BIN", DEFAULT_GCC_BIN))
    exe = bin_dir / (PREFIX + name + (".exe" if os.name == "nt" else ""))
    if not exe.is_file():
        sys.exit(f"build_asm_tests: {exe} not found (set RISCV_EMG_RISCV_GCC_BIN)")
    return str(exe)


def run(cmd: list[str]) -> str:
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"command failed ({res.returncode}): {' '.join(cmd)}\n"
                           f"{res.stdout}{res.stderr}")
    return res.stdout


def build_one(name: str, gcc: str, objcopy: str, objdump: str, readelf: str,
              nm: str, src_dir: Path = SRC_DIR, out_dir: Path = OUT_DIR, kind: str = "asm") -> dict:
    out = out_dir / name
    out.mkdir(parents=True, exist_ok=True)
    elf, binf, hexf = out / f"{name}.elf", out / f"{name}.bin", out / f"{name}.hex"

    if kind == "c":         # libgcc (rv32i/ilp32 multilib) provides __mulsi3, __divsi3, ...
        run([gcc, *C_FLAGS, *EXTRA_INC, "-o", str(elf), str(BSP / "crt0.S"), str(src_dir / f"{name}.c"), "-lgcc"])
    else:
        run([gcc, *CFLAGS, "-o", str(elf), str(COMMON / "crt0.S"), str(src_dir / f"{name}.S")])

    # --- ISA / ABI checks -------------------------------------------------
    attrs = run([readelf, "-A", str(elf)])
    m = ARCH_RE.search(attrs)
    if not m or not PLAIN_RV32I_RE.match(m.group(1)):
        raise RuntimeError(f"{name}: unexpected Tag_RISCV_arch {m.group(1) if m else None!r}")
    header = run([readelf, "-h", str(elf)])
    flags = re.search(r"Flags:\s*(0x[0-9a-fA-F]+)", header)
    if not flags or int(flags.group(1), 16) & 0x1:          # EF_RISCV_RVC
        raise RuntimeError(f"{name}: ELF flags indicate RVC: {flags.group(1) if flags else None}")
    if int(flags.group(1), 16) & 0x6:                        # float ABI bits
        raise RuntimeError(f"{name}: ELF flags indicate a hard-float ABI")

    # --- symbols ------------------------------------------------------------
    syms: dict[str, int] = {}
    for line in run([nm, str(elf)]).splitlines():
        parts = line.split()
        if len(parts) == 3:
            syms[parts[2]] = int(parts[0], 16)
    if syms.get("_start") != RESET_VECTOR:
        raise RuntimeError(f"{name}: _start at {syms.get('_start')}, expected {RESET_VECTOR}")

    # --- image --------------------------------------------------------------
    run([objcopy, "-O", "binary", str(elf), str(binf)])
    image = binf.read_bytes()
    if len(image) > MEM_BYTES:
        raise RuntimeError(f"{name}: image {len(image)} bytes exceeds {MEM_BYTES}")
    image += b"\0" * (-len(image) % 4)
    words = [int.from_bytes(image[i:i + 4], "little") for i in range(0, len(image), 4)]
    hexf.write_text("".join(f"{w:08x}\n" for w in words), encoding="ascii", newline="\n")

    dump = run([objdump, "-d", "-M", "no-aliases,numeric", str(elf)])
    (out / f"{name}.dump").write_text(dump, encoding="utf-8", newline="\n")

    # --- testbench plusargs ---------------------------------------------------
    plusargs = [f"+hex={hexf.as_posix()}"]
    for sym in sorted(s for s in syms if s.startswith("tb_") and s not in ("tb_pass", "tb_fail")):
        plusargs.append(f"+{sym}={syms[sym]:08x}")
    (out / f"{name}.args").write_text("\n".join(plusargs) + "\n", encoding="ascii", newline="\n")

    return {"name": name, "words": len(words), "arch": m.group(1),
            "expect_trap": "tb_expect_cause" in syms, "plusargs": plusargs}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="*")
    ap.add_argument("--src", type=Path, default=SRC_DIR, help="directory with <name>.S")
    ap.add_argument("--out", type=Path, default=OUT_DIR, help="output directory")
    ap.add_argument("--kind", choices=("asm", "c"), default="asm",
                    help="asm: <name>.S + tests/cpu/common; c: <name>.c + firmware/rv32/bsp")
    ap.add_argument("-q", "--quiet", dest="verbose", action="store_false",
                    help="only print errors and the summary")
    ap.add_argument("--inc", type=Path, action="append", default=[],
                    help="--kind c: extra include directory (repeatable)")
    ns = ap.parse_args()
    EXTRA_INC.extend(f"-I{d.resolve()}" for d in ns.inc)
    src_dir, out_dir = ns.src.resolve(), ns.out.resolve()
    names = ns.names or sorted(p.stem for p in src_dir.glob("*.c" if ns.kind == "c" else "*.S"))
    gcc, objcopy, objdump = tool("gcc"), tool("objcopy"), tool("objdump")
    readelf, nm = tool("readelf"), tool("nm")
    version = run([gcc, "--version"]).splitlines()[0]
    out_dir.mkdir(parents=True, exist_ok=True)

    programs, failed = [], []

    def job(name: str):
        try:
            return name, build_one(name, gcc, objcopy, objdump, readelf, nm, src_dir, out_dir, ns.kind), None
        except (RuntimeError, OSError) as exc:
            return name, None, exc

    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 2)) as pool:
        for name, info, exc in pool.map(job, names):        # results in input order
            if exc is not None:
                failed.append(name)
                print(f"[ERROR] {name}: {exc}")
                continue
            programs.append(info)
            if ns.verbose:
                kind = "trap" if info["expect_trap"] else "pass"
                print(f"[built] {name:24s} {info['words']:5d} words  arch={info['arch']}  expect={kind}")

    # merge with an existing manifest when only some programs were rebuilt
    mpath = out_dir / "manifest.json"
    merged = {}
    if ns.names and mpath.is_file():
        merged = {p["name"]: p for p in json.loads(mpath.read_text(encoding="utf-8"))["programs"]}
    merged.update({p["name"]: p for p in programs})
    manifest = {"assembler": version, "cflags": CFLAGS[:9],
                "programs": [merged[k] for k in sorted(merged)]}
    mpath.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"assembler: {version}")
    print(f"built {len(programs)} program(s), {len(failed)} failure(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
