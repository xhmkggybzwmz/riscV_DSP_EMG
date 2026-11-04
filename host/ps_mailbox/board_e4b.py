"""E4a-B board test controller (runs ON the KV260 as root with the PYNQ venv python).

    cd ~/riscv-emg-soc/e4a_b && sudo env XILINX_XRT=/usr PATH=/usr/local/share/pynq-venv/bin:$PATH \\
        /usr/local/share/pynq-venv/bin/python3 \\
        -m ps_mailbox.board_e4b {probe|rt2k|long} --prog mbx_rt2k

The PL must already hold the program's bitstream (scripts/fpga/kv260_ps_board.py load); this
script never loads, unloads or reconfigures anything. Layout of the board directory:
    overlays/<prog>/<prog>.bit .hwh .manifest.json   (same build; PYNQ wants .bit + .hwh)
    fw/rv32_6a.bit.bin                               (what the FPGA manager loaded)
    golden.json                                      (python -m ps_mailbox.golden on the PC)
    results/<prog>/                                  (output)
The controller verifies the device (overlay_device.PynqMailboxDevice), drives the RV32I only
through the debug window (kv260_dbg_axil CTRL.cpu_reset, as kv260_ps_board.py test) and
reads its registers; the mailbox is consumed only by reader processes (python -m
ps_mailbox.cli), one at a time. The controller reads mailbox header words, never writes them.

  probe  device verification, first header read (each word once), MMIO read timing
  reset  one CTRL.cpu_reset pulse (new RV32I run), registers before / after the run; maps the
         debug window only, never the mailbox (a running mailbox consumer stays the only one)
  rt2k   power-on run + --resets soft resets, one reader across all runs (11 records each)
  long   run A: one soft reset, 88 records at 20 Hz without interruption
         run B: reader stopped after --split records and a new reader process started
                after it exited (cold restart)
         run C: as B, the new reader verified beforehand and waiting for the old one to
                exit (--wait-pid, warm restart)
Output: results/<prog>/<cmd>.json (+ reader .jsonl / .err / .report.json); exit 1 on a
failed step (the host script stops the board session then).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import protocol as P
from .overlay_device import DeviceError, PynqMailboxDevice
from .pynq_reader import MailboxError, MailboxReader

ROOT = Path.cwd()
REGS = {"ID": 0x00, "VERSION": 0x04, "CTRL": 0x08, "STATUS": 0x0C, "FIRST_TOHOST": 0x10,
        "TOHOST_WRITES": 0x14, "TOHOST_VALUE": 0x18, "TRAP_CAUSE": 0x1C, "TRAP_PC": 0x20,
        "TRAP_TVAL": 0x24, "RUN_CYCLES": 0x28, "RETIRED": 0x2C, "TOHOST_RETIRED": 0x30,
        "RESET_RELEASES": 0x34, "FREE_CYCLES": 0x38, "FETCH_PC": 0x3C, "LED": 0x40, "SCRATCH": 0x44}


class Step(Exception):
    """a failed step: stop"""


ACTIVE: list[subprocess.Popen] = []      # reader processes started by this controller


def paths(prog: str) -> dict:
    o = ROOT / "overlays" / prog
    return {"bit": o / f"{prog}.bit", "bin": ROOT / "fw" / "rv32_6a.bit.bin", "golden": ROOT / "golden.json",
            "out": ROOT / "results" / prog}


def device(prog: str) -> PynqMailboxDevice:
    p = paths(prog)
    return PynqMailboxDevice(p["bit"], loaded_bin=p["bin"])


class Dbg:
    def __init__(self, w):
        self.w = w

    def regs(self) -> dict:
        return {k: self.w.read32(o) for k, o in REGS.items()}

    def cpu_reset(self) -> None:
        self.w.write32(REGS["CTRL"], 1)          # hold the CPU subsystem in reset
        time.sleep(0.002)
        self.w.write32(REGS["CTRL"], 0)

    def wait_run(self, releases: int, timeout: float) -> dict:
        """registers once run `releases` (RESET_RELEASES) wrote TOHOST"""
        t_end = time.monotonic() + timeout
        while True:
            r = self.regs()
            if r["RESET_RELEASES"] == releases and r["TOHOST_WRITES"] >= 1:
                time.sleep(0.05)
                return self.regs()
            if time.monotonic() > t_end:
                raise Step(f"run {releases}: no TOHOST within {timeout} s: {r}")
            time.sleep(0.01)


def header(w) -> dict:
    """mailbox header through a read-only MailboxReader (no CONSUMER_* writes)"""
    try:
        return MailboxReader(w).read_header()
    except MailboxError as e:
        return {"error": str(e)}


def hexregs(r: dict) -> dict:
    return {k: f"0x{v:08X}" for k, v in r.items()}


class Readers:
    def __init__(self, prog: str, out: Path, seq_base: int):
        self.prog, self.out, self.seq_base = prog, out, seq_base
        self.procs = {}

    def start(self, tag: str, *extra: str) -> subprocess.Popen:
        p = paths(self.prog)
        cmd = [sys.executable, "-m", "ps_mailbox.cli", "--bit", str(p["bit"]), "--loaded-bin", str(p["bin"]),
               "--golden", str(p["golden"]), "--seq-base", str(self.seq_base), "--policy", "backlog",
               "--poll-ms", "5", "--duration", "0", "--timeout", "120",
               "--jsonl", str(self.out / f"{tag}.jsonl"), "--report", str(self.out / f"{tag}.report.json"), *extra]
        err = open(self.out / f"{tag}.err", "w", encoding="utf-8")
        pr = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=err)
        ACTIVE.append(pr)
        self.procs[tag] = (pr, err, time.monotonic())
        return pr

    def text(self, tag: str) -> str:
        return (self.out / f"{tag}.err").read_text(encoding="utf-8", errors="replace")

    def wait_event(self, tag: str, kind: str, timeout: float = 60.0) -> float:
        pr, _, t0 = self.procs[tag]
        while time.monotonic() - t0 < timeout:
            if f'"type": "{kind}"' in self.text(tag):
                return round(time.monotonic() - t0, 3)
            if pr.poll() is not None:
                raise Step(f"reader {tag} exited before {kind} (rc {pr.returncode}): {self.text(tag)[-1500:]}")
            time.sleep(0.01)
        raise Step(f"reader {tag}: no {kind} event within {timeout} s")

    def wait_ready(self, tag: str, timeout: float = 60.0) -> float:
        return self.wait_event(tag, "ready", timeout)

    def lines(self, tag: str) -> int:
        f = self.out / f"{tag}.jsonl"
        return sum(1 for _ in f.open(encoding="utf-8")) if f.is_file() else 0

    def wait_lines(self, tag: str, n: int, timeout: float) -> None:
        pr, _, _ = self.procs[tag]
        t_end = time.monotonic() + timeout
        while self.lines(tag) < n:
            if pr.poll() is not None:
                raise Step(f"reader {tag} exited (rc {pr.returncode}) after {self.lines(tag)} of {n} records")
            if time.monotonic() > t_end:
                raise Step(f"reader {tag}: {self.lines(tag)} of {n} records after {timeout} s")
            time.sleep(0.01)

    def finish(self, tag: str, timeout: float = 30.0) -> dict:
        pr, err, _ = self.procs[tag]
        try:
            rc = pr.wait(timeout)
        except subprocess.TimeoutExpired:
            pr.terminate()                       # SIGTERM: the reader stops polling and unmaps
            rc = pr.wait(10)
        err.close()
        txt = self.text(tag)
        summ = [json.loads(l[8:]) for l in txt.splitlines() if l.startswith("SUMMARY ")]
        evs = [json.loads(l[6:]) for l in txt.splitlines() if l.startswith("EVENT ")]
        recs = [json.loads(l) for l in (self.out / f"{tag}.jsonl").open(encoding="utf-8")] \
            if (self.out / f"{tag}.jsonl").is_file() else []
        return {"rc": rc, "summary": summ[-1] if summ else None, "events": evs, "records": recs,
                "errors": [l for l in txt.splitlines() if l.startswith("ERROR")]}


def no_devmem_holders() -> list[str]:
    """processes (other than this one) with a /dev/mem mapping"""
    out = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit() or int(d.name) == os.getpid():
            continue
        try:
            if any(l.endswith(" /dev/mem") for l in (d / "maps").read_text().splitlines()):
                out.append(f"{d.name} {(d / 'comm').read_text().strip()}")
        except OSError:
            pass
    return out


def setup(prog: str, cmd: str):
    out = paths(prog)["out"]
    out.mkdir(parents=True, exist_ok=True)
    dev = device(prog)
    try:
        dev.verify()
    except DeviceError as e:
        dev.close()
        raise Step(f"device not verified: {e}")
    return out, dev, Dbg(dev.open_debug()), dev.open_mailbox()


# ---------------------------------------------------------------- probe
def cmd_probe(ns) -> dict:
    out, dev, dbg, mw = setup(ns.prog, "probe")
    res = {"device": dev.report}
    dev.probe_header(mw)                          # each word once; DeviceError -> stop
    res["header"] = header(mw)
    res["regs"] = hexregs(dbg.regs())
    t0 = time.monotonic_ns()
    for _ in range(1000):
        mw.read32(P.hoff("MAGIC"))
    res["mmio_read_us"] = round((time.monotonic_ns() - t0) / 1000 / 1000, 3)
    t0, c0 = time.monotonic(), dbg.w.read32(REGS["FREE_CYCLES"])
    time.sleep(ns.freq_wait)
    t1, c1 = time.monotonic(), dbg.w.read32(REGS["FREE_CYCLES"])
    res["freq"] = [(c1 - c0) & 0xFFFFFFFF, round(t1 - t0, 6)]
    dev.close()
    res["devmem_mappings_after_close"] = dev.report["devmem_mappings_after_close"]
    return res


# ---------------------------------------------------------------- reset
def cmd_reset(ns) -> dict:
    out = paths(ns.prog)["out"]
    out.mkdir(parents=True, exist_ok=True)
    dev = device(ns.prog)
    try:
        dev.verify()
    except DeviceError as e:
        dev.close()
        raise Step(f"device not verified: {e}")
    dbg = Dbg(dev.open_debug())
    res = {"device_board": dev.report.get("board"), "debug_id": dev.report.get("debug_id")}
    before = dbg.regs()
    res["before"] = hexregs(before)
    t0 = time.time()
    dbg.cpu_reset()
    res["reset_wall_time"] = t0
    res["after"] = hexregs(dbg.wait_run(before["RESET_RELEASES"] + 1, ns.run_timeout))
    dev.close()
    res["devmem_mappings_after_close"] = dev.report["devmem_mappings_after_close"]
    return res


# ---------------------------------------------------------------- rt2k
def cmd_rt2k(ns) -> dict:
    out, dev, dbg, mw = setup(ns.prog, "rt2k")
    n = ns.records
    res = {"runs": {}, "headers": {}, "device": dev.report}
    res["runs"]["poweron"] = hexregs(dbg.wait_run(1, 15))
    rd = Readers(ns.prog, out, ns.seq_base)
    rd.start("rt2k", "--expect", str(n * (ns.resets + 1)))
    res["reader_ready_s"] = rd.wait_ready("rt2k")
    rd.wait_lines("rt2k", n, 20)
    time.sleep(0.2)
    res["headers"]["poweron"] = header(mw)
    for i in range(1, ns.resets + 1):
        dbg.cpu_reset()
        res["runs"][f"run{i}"] = hexregs(dbg.wait_run(i + 1, 15))
        rd.wait_lines("rt2k", n * (i + 1), 20)
        time.sleep(0.2)
        res["headers"][f"run{i}"] = header(mw)
    res["reader"] = rd.finish("rt2k")
    dev.close()
    res["devmem_holders_after"] = no_devmem_holders()
    return res


# ---------------------------------------------------------------- long
def cmd_long(ns) -> dict:
    out, dev, dbg, mw = setup(ns.prog, "long")
    n = ns.records
    res = {"runs": {}, "headers": {}, "device": dev.report}
    res["runs"]["poweron"] = hexregs(dbg.wait_run(1, 20))
    res["headers"]["poweron"] = header(mw)      # nobody read the power-on run: 16 kept, rest dropped
    rd = Readers(ns.prog, out, ns.seq_base)
    rel = 1
    # run A: the reader first takes the 16 records kept from the power-on run, then the
    # soft reset starts a new epoch read from its first record
    kept = res["headers"]["poweron"].get("published_cnt", 0)
    rd.start("A", "--expect", str(kept + n))
    res["A_ready_s"] = rd.wait_ready("A")
    rd.wait_lines("A", kept, 20)
    dbg.cpu_reset()
    rel += 1
    res["runs"]["A"] = hexregs(dbg.wait_run(rel, 20))
    rd.wait_lines("A", kept + n, 20)
    res["headers"]["A"] = header(mw)
    res["A"] = rd.finish("A")
    # runs B (cold) and C (warm): reader 1 stops after --split records of the new run
    for run in ("B", "C"):
        t1, t2 = f"{run}1", f"{run}2"
        rd.start(t1, "--max-records", str(ns.split))
        res[f"{t1}_ready_s"] = rd.wait_ready(t1)
        if run == "C":
            p1 = rd.procs[t1][0]
            rd.start(t2, "--until-done", "--wait-pid", str(p1.pid))
            rd.wait_event(t2, "standby")         # verified, waiting for reader 1 to exit
        dbg.cpu_reset()
        rel += 1
        r1 = rd.finish(t1, 30)
        res[f"{t1}_exit_monotonic"] = time.monotonic()
        res[t1] = r1
        if run == "B":
            rd.start(t2, "--until-done")
        res[f"{t2}_ready_s"] = rd.wait_ready(t2)
        res["runs"][run] = hexregs(dbg.wait_run(rel, 20))
        res[t2] = rd.finish(t2, 30)
        res["headers"][run] = header(mw)
    dev.close()
    res["devmem_holders_after"] = no_devmem_holders()
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["probe", "reset", "rt2k", "long"])
    ap.add_argument("--prog", required=True)
    ap.add_argument("--resets", type=int, default=3)
    ap.add_argument("--records", type=int, default=11, help="records per run (rt2k 11, long 88)")
    ap.add_argument("--split", type=int, default=30, help="long: records before the reader restart")
    ap.add_argument("--seq-base", type=int, default=0)
    ap.add_argument("--freq-wait", type=float, default=2.0)
    ap.add_argument("--run-timeout", type=float, default=20.0, help="reset: seconds for the run to write TOHOST")
    ap.add_argument("--tag", default="", help="reset: suffix of the output file (reset<tag>.json)")
    ns = ap.parse_args(argv)
    if os.geteuid() != 0:
        print("ERROR run as root (pynq.MMIO maps /dev/mem)")
        return 1
    out = paths(ns.prog)["out"]
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    try:
        res = {"probe": cmd_probe, "reset": cmd_reset, "rt2k": cmd_rt2k, "long": cmd_long}[ns.cmd](ns)
        rc = 0
    except (Step, DeviceError, MailboxError) as e:
        res, rc = {"error": str(e)}, 1
        print(f"ERROR {ns.cmd}: {e}")
    finally:
        for pr in ACTIVE:                        # never leave a reader mapping the PL behind
            if pr.poll() is None:
                pr.terminate()
                try:
                    pr.wait(10)
                except subprocess.TimeoutExpired:
                    pr.kill()
                    pr.wait(5)
    res.update({"cmd": ns.cmd, "prog": ns.prog, "start_time": t0, "elapsed_s": round(time.time() - t0, 3)})
    name = f"{ns.cmd}{ns.tag}.json"
    (out / name).write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    print(f"{ns.cmd.upper()} {'done' if rc == 0 else 'FAILED'} -> {out / name}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
