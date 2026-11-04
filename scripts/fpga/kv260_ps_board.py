#!/usr/bin/env python3
"""Stage 6A board-side helper (runs ON the KV260 Ubuntu, python3 standard library).

Copied by scripts/fpga/kv260_6a.py to ~/riscv-emg-soc/stage6a/ and run with sudo.
No JTAG. The PL is configured through the Linux FPGA manager with our own device
tree overlay (fpga-full region, firmware-name + the four PL resets, like the
k26-starter-kits app); every file stays in the stage6a directory: the firmware
search path is pointed there only while the overlay is applied.

  check                      read-only: FPGA manager, overlays, PL0 clock, HPM0 width, dfx-mgr
                             apps, processes holding PL-related device nodes
  load <name.bit.bin>        configure the PL (overlay rv32_6a); refuses unless the
                             pre-load conditions hold (see below)
  unload                     remove the overlay rv32_6a
  test --resets N --starts S1,S2,.. --run-wait T --out FILE
                             power-on result (after T s), pl_clk0 measurement, N re-runs
                             through CTRL.cpu_reset (T s each), one trace per start

Safety: before any access to the debug window (0xA000_0000, kv260_dbg_axil) the
script requires: FPGA manager "operating", overlay rv32_6a "applied", PL0 clock
active (CRL_APB PL0_REF_CTRL bit 24) and the HPM0_FPD width = 32 bit
(FPD_SLCR AFI_FS[9:8] = 0 per the xlnx,afi-fpga binding: 0x000 32, 0x100 64,
0x200 128 bit; the design uses 32). An access to an unconfigured or unclocked PL
port would hang the system. The PS registers are only read.

Pre-load conditions (load refuses otherwise): FPGA manager "operating"; no overlay
except pynq (xmutil unloadapp first); PL0 clock active and HPM0_FPD width = 32 bit
(the designs use a 32-bit HPM0); no process holding a PL device node (/dev/uio*,
/dev/dri/card1, /dev/dri/renderD128 = zocl); dfx-mgrd holding /dev/dma_heap/reserved
is its idle state and allowed.

Load: the FPGA manager flags are set to 0 (full bitstream, no DMA-buf) while the
overlay is applied and restored afterwards. FPGA_MGR_CONFIG_DMA_BUF = BIT(8) = 0x100
per this board's kernel headers (/usr/src/linux-headers-5.15.0-1027-xilinx-zynqmp,
include/linux/fpga/fpga-mgr.h). The restore in `finally` runs only if this process
survives: a kernel oops, SIGKILL or power loss leaves flags / firmware path as they
were at that moment - the host (kv260_6a.py psboard) compares the state before and
after and reports anything not restored. dfx-mgr leaves 0x100
(FPGA_MGR_CONFIG_DMA_BUF) after xmutil unloadapp; a firmware-name load in that
mode crashed the kernel (scatterlist BUG in cma_heap_attach, 2026-10-26).
"""

from __future__ import annotations

import argparse
import mmap
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
FW_DIR = HERE / "fw"
OVL = Path("/sys/kernel/config/device-tree/overlays/rv32_6a")
FW_PATH = Path("/sys/module/firmware_class/parameters/path")
FPGA = Path("/sys/class/fpga_manager/fpga0")
DBG_BASE, DBG_SIZE = 0xA000_0000, 0x40000
PL0_REF_CTRL, AFI_FS = 0xFF5E_00C0, 0xFD61_5000

REGS = {"ID": 0x00, "VERSION": 0x04, "CTRL": 0x08, "STATUS": 0x0C, "FIRST_TOHOST": 0x10,
        "TOHOST_WRITES": 0x14, "TOHOST_VALUE": 0x18, "TRAP_CAUSE": 0x1C, "TRAP_PC": 0x20,
        "TRAP_TVAL": 0x24, "RUN_CYCLES": 0x28, "RETIRED": 0x2C, "TOHOST_RETIRED": 0x30,
        "RESET_RELEASES": 0x34, "FREE_CYCLES": 0x38, "FETCH_PC": 0x3C, "LED": 0x40,
        "SCRATCH": 0x44, "TRACE_START": 0x80, "TRACE_COUNT": 0x84, "TRACE_DEPTH": 0x88}
ID_VALUE = 0x5256_3641


def read_phys32(addr: int) -> int:
    fd = os.open("/dev/mem", os.O_RDONLY | os.O_SYNC)
    try:
        page = addr & ~0xFFF
        m = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED, mmap.PROT_READ, offset=page)
        v = struct.unpack_from("<I", m, addr - page)[0]
        m.close()
        return v
    finally:
        os.close(fd)


def sysfs(p: Path) -> str:
    try:
        return p.read_text().strip()
    except OSError as e:
        return f"<{e.strerror}>"


def state() -> dict:
    ov = Path("/sys/kernel/config/device-tree/overlays")
    pl0 = read_phys32(PL0_REF_CTRL)
    afi = read_phys32(AFI_FS)
    return {"fpga_state": sysfs(FPGA / "state"), "fpga_flags": sysfs(FPGA / "flags"),
            "overlays": {o.name: sysfs(o / "status") for o in sorted(ov.iterdir())} if ov.is_dir() else {},
            "PL0_REF_CTRL": pl0, "pl0_clkact": (pl0 >> 24) & 1,
            "pl0_div0": (pl0 >> 8) & 0x3F, "pl0_div1": (pl0 >> 16) & 0x3F, "pl0_srcsel": pl0 & 7,
            "AFI_FS": afi, "hpm0_width_sel": (afi >> 8) & 3, "hpm1_width_sel": (afi >> 10) & 3,
            "fw_path": sysfs(FW_PATH)}


# processes holding PL-related device nodes, from `fuser -v` (psmisc); dfx-mgrd's
# reserved DMA heap is its normal idle state
PL_NODES = ["/dev/uio0", "/dev/uio1", "/dev/uio2", "/dev/uio3", "/dev/uio4", "/dev/dri/card1",
            "/dev/dri/renderD128", "/dev/dma_heap/reserved", "/dev/dma_heap/system"]
ALLOWED = {("/dev/dma_heap/reserved", "dfx-mgrd")}


def holders() -> list[tuple[str, str, str]]:
    # fuser -v: the PIDs go to stdout, the table (user, access, command; no PID column)
    # to stderr, one row per PID in the same order
    out = []
    for node in PL_NODES:
        if not Path(node).exists():
            continue
        r = subprocess.run(["fuser", "-v", node], capture_output=True, text=True)
        pids = [x.rstrip("cefFrm") for x in r.stdout.split()]
        rows = []
        for line in r.stderr.splitlines():
            tok = line.split()
            if not tok or tok[0] == "USER":
                continue
            if tok[0].endswith(":"):
                tok = tok[1:]
            if len(tok) >= 3:
                rows.append(tok[-1])
        for pid, cmd in zip(pids, rows):
            out.append((node, pid, cmd))
    return out


def dfx_apps() -> str:
    r = subprocess.run(["xmutil", "listapps"], capture_output=True, text=True)
    lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
    return " | ".join(lines[1:]) if len(lines) > 1 else "<none>"


def cmd_check(_ns) -> int:
    for k, v in state().items():
        print(f"STATE {k} {hex(v) if isinstance(v, int) and k.isupper() else v}")
    print(f"STATE dfx_apps {dfx_apps()}")
    for node, pid, cmd in holders():
        print(f"STATE holder {node} pid={pid} {cmd}{' (allowed)' if (node, cmd) in ALLOWED else ''}")
    return 0


def load_blockers() -> list[str]:
    s = state()
    bad = []
    if s["fpga_state"] != "operating":
        bad.append(f"fpga_manager state {s['fpga_state']}")
    others = [o for o in s["overlays"] if o not in ("pynq",)]
    if others:
        bad.append(f"other overlays loaded: {others} (xmutil unloadapp first)")
    if s["pl0_clkact"] != 1:
        bad.append("PL0 clock not active")
    if s["hpm0_width_sel"] != 0:
        bad.append(f"HPM0_FPD width select {s['hpm0_width_sel']} (designs: 32 bit = 0)")
    busy = [(n, p, c) for n, p, c in holders() if (n, c) not in ALLOWED]
    if busy:
        bad.append("PL device nodes in use: " + ", ".join(f"{n} by {c}[{p}]" for n, p, c in busy))
    if s["fpga_flags"].startswith("<") or s["fw_path"].startswith("<"):
        bad.append("FPGA manager flags / firmware path not readable")
    return bad


def preconditions() -> list[str]:
    s = state()
    bad = []
    if s["fpga_state"] != "operating":
        bad.append(f"fpga_manager state {s['fpga_state']}")
    if s["overlays"].get("rv32_6a") != "applied":
        bad.append("overlay rv32_6a not applied")
    if s["pl0_clkact"] != 1:
        bad.append("PL0 clock not active")
    if s["hpm0_width_sel"] != 0:
        bad.append(f"HPM0_FPD width select {s['hpm0_width_sel']} (design: 32 bit = 0)")
    return bad


def cmd_load(ns) -> int:
    bad = load_blockers()
    if bad:
        print("ERROR not loading: " + "; ".join(bad))
        return 1
    if OVL.exists():
        print("ERROR overlay rv32_6a already applied (unload first)")
        return 1
    src = FW_DIR / ns.name
    if not src.is_file():
        print(f"ERROR {src} missing")
        return 1
    dst = FW_DIR / "rv32_6a.bit.bin"               # the name in rv32_6a.dtbo
    if src != dst:
        dst.write_bytes(src.read_bytes())
    old = sysfs(FW_PATH)
    old_flags = sysfs(FPGA / "flags")
    (FPGA / "flags").write_text("0\n")            # full bitstream from firmware, no DMA-buf
    # the 5.15 firmware loader does not strip a trailing newline from this
    # parameter, so no "\n"; an empty write never reaches the kernel, so an
    # empty value is restored by writing a single NUL (copied as a C string)
    FW_PATH.write_bytes(str(FW_DIR).encode())
    try:
        OVL.mkdir()
        (OVL / "dtbo").write_bytes((FW_DIR / "rv32_6a.dtbo").read_bytes())
        time.sleep(0.5)
    finally:
        restore = old if old and not old.startswith("<") else ""
        FW_PATH.write_bytes(restore.encode() if restore else b"\0")
        if not old_flags.startswith("<"):
            (FPGA / "flags").write_text(old_flags + "\n")
    st = sysfs(OVL / "status")
    print(f"LOAD {ns.name} overlay={st} fpga_state={sysfs(FPGA / 'state')} flags={old_flags}->0->"
          f"{sysfs(FPGA / 'flags')} fw_path_restored='{sysfs(FW_PATH)}'")
    return 0 if st == "applied" else 1


def cmd_unload(_ns) -> int:
    if OVL.exists():
        OVL.rmdir()
    print(f"UNLOAD overlay_present={OVL.exists()} fpga_state={sysfs(FPGA / 'state')}")
    return 0


class Dbg:
    def __init__(self):
        self.fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        self.m = mmap.mmap(self.fd, DBG_SIZE, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE,
                           offset=DBG_BASE)

    def rd(self, off: int) -> int:
        return struct.unpack_from("<I", self.m, off)[0]

    def wr(self, off: int, v: int) -> None:
        struct.pack_into("<I", self.m, off, v & 0xFFFF_FFFF)

    def regs(self) -> dict:
        return {k: self.rd(o) for k, o in REGS.items()}

    def rerun(self, wait: float) -> None:
        self.wr(REGS["CTRL"], 1)                   # hold the CPU subsystem in reset
        time.sleep(0.002)
        self.wr(REGS["CTRL"], 0)
        time.sleep(wait)                           # the program's run time (+ margin)


def cmd_test(ns) -> int:
    bad = preconditions()
    if bad:
        print("ERROR preconditions: " + "; ".join(bad))
        return 1
    d = Dbg()
    out = []
    ident = d.rd(REGS["ID"])
    if ident != ID_VALUE:
        print(f"ERROR ID {ident:08x}")
        return 1
    out.append(f"STATE {' '.join(f'{k}={v}' for k, v in state().items() if k != 'overlays')}")
    time.sleep(ns.run_wait)                        # the power-on run must have finished
    out += [f"RUN poweron {k} {v:08x}" for k, v in d.regs().items()]
    t0, c0 = time.monotonic(), d.rd(REGS["FREE_CYCLES"])
    time.sleep(ns.freq_wait)
    t1, c1 = time.monotonic(), d.rd(REGS["FREE_CYCLES"])
    out.append(f"FREQ {(c1 - c0) & 0xFFFF_FFFF} {t1 - t0:.6f}")
    d.wr(REGS["SCRATCH"], 0x5A5A_A5A5)
    out.append(f"SCRATCH {d.rd(REGS['SCRATCH']):08x}")
    for i in range(1, ns.resets + 1):
        d.rerun(ns.run_wait)
        out += [f"RUN run{i} {k} {v:08x}" for k, v in d.regs().items()]
    for s in [int(x) for x in ns.starts.split(",") if x != ""]:
        d.wr(REGS["TRACE_START"], s)
        d.rerun(ns.run_wait)
        regs = d.regs()
        n = regs["TRACE_COUNT"]
        out.append(f"SEG {s} {n}")
        out += [f"SEGREG {s} {k} {v:08x}" for k, v in regs.items()]
        for i in range(n):
            base = 0x20000 + 32 * i
            out.append("REC " + " ".join(f"{d.rd(base + 4 * w):08x}" for w in range(8)))
    d.wr(REGS["TRACE_START"], 0)
    Path(ns.out).write_text("\n".join(out) + "\n")
    print(f"TEST done lines={len(out)} out={ns.out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    lp = sub.add_parser("load")
    lp.add_argument("name")
    sub.add_parser("unload")
    tp = sub.add_parser("test")
    tp.add_argument("--resets", type=int, default=10)
    tp.add_argument("--starts", default="0")
    tp.add_argument("--freq-wait", type=float, default=5.0)
    tp.add_argument("--run-wait", type=float, default=0.02,
                    help="seconds to wait for a program run (stage 6A programs: 0.02)")
    tp.add_argument("--out", required=True)
    ns = ap.parse_args()
    if os.geteuid() != 0:
        print("ERROR run with sudo")
        return 1
    return {"check": cmd_check, "load": cmd_load, "unload": cmd_unload, "test": cmd_test}[ns.cmd](ns)


if __name__ == "__main__":
    sys.exit(main())
