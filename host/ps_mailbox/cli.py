"""Mailbox reader command line (docs/soc/e4a_mailbox.md).

On the KV260 (root, PYNQ venv), with the E4a bitstream already loaded by the board tools:
    sudo env XILINX_XRT=/usr PATH=/usr/local/share/pynq-venv/bin:$PATH \\
        /usr/local/share/pynq-venv/bin/python3 -m ps_mailbox.cli \\
        --bit overlays/mbx_rt2k/mbx_rt2k.bit --loaded-bin fw/rv32_6a.bit.bin --duration 10 --jsonl out.jsonl
  --bit: PynqMailboxDevice (overlay_device.py): artifacts / board / pynq.Overlay(download=False)
  metadata / debug ID verified, then pynq.MMIO on the address taken from the HWH. Without
  --bit: pynq.MMIO / /dev/mem on the config address (--backend), board guard only.
On the PC (software producer, 20 Hz, no hardware):
    python -m ps_mailbox.cli --fake --duration 3

Output: one JSON object per record on stdout (or --jsonl FILE); events ("EVENT {...}", the
first one "ready" once the window is open) and the summary ("SUMMARY {...}") on stderr.
Stops after --duration s, --max-records records, --expect records (duration 0), when the
attached run is DONE and fully consumed (--until-done), on SIGINT / SIGTERM, or with exit 3
after --timeout s. Exit 1: --expect not met, protocol errors, golden / index mismatches.
The reader never loads a bitstream or touches FPGA manager / overlays.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import statistics
import sys
import threading
import time

from . import protocol as P
from .pynq_reader import MailboxError, MailboxGone, MailboxReader, board_guard, load_config, to_json


def fake_source(rate_hz: float, n: int):
    """software producer thread at rate_hz on a FakeMailbox; returns (window, thread, stop)"""
    from .fake_mmio import FakeMailbox, FakeProducer, make_result
    box = FakeMailbox()
    prod = FakeProducer(box)
    stop = threading.Event()

    def run():
        prod.init(0)
        k = 0
        t_next = time.monotonic()
        while not stop.is_set() and (n <= 0 or k < n):
            t_next += 1.0 / rate_hz
            time.sleep(max(0.0, t_next - time.monotonic()))
            k += 1
            prod.publish(make_result(k))
        prod.done()

    th = threading.Thread(target=run, daemon=True)
    th.start()
    return box.ps, th, stop


def process_alive(pid: int) -> bool:
    """False once the process has exited (a zombie has released its mappings already)"""
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def ms_stats(ns_values, prefix: str) -> dict:
    v = sorted(x / 1e6 for x in ns_values)
    if not v:
        return {}
    return {f"{prefix}_mean": round(statistics.mean(v), 3), f"{prefix}_min": round(v[0], 3),
            f"{prefix}_p99": round(v[min(len(v) - 1, int(0.99 * len(v)))], 3), f"{prefix}_max": round(v[-1], 3),
            f"{prefix}_n": len(v)}


class IndexCheck:
    """per epoch: (RESULT_INDEX - 1) - epoch_seq is the number of results dropped before the
    record; it may only grow at a record flagged DROP_BEFORE. Golden content by RESULT_INDEX."""

    def __init__(self, golden, seq_base: int):
        self.golden, self.seq_base = golden, seq_base
        self.diff = {}
        self.stats = {"golden_ok": 0, "golden_mismatch": 0, "index_errors": 0, "baseline": {}}

    def __call__(self, rec: dict) -> list[str]:
        out = []
        d = (rec["result_index"] - 1) - rec["epoch_seq"]
        e = rec["epoch"]
        if e not in self.diff:
            self.stats["baseline"][str(e)] = d
        elif d != self.diff[e] and not (d > self.diff[e] and rec["flags"] & P.F_DROP_BEFORE):
            out.append(f"RESULT_INDEX {rec['result_index']} / epoch_seq {rec['epoch_seq']}: dropped count "
                       f"{self.diff[e]} -> {d} without DROP_BEFORE")
            self.stats["index_errors"] += 1
        self.diff[e] = d
        if self.golden is not None:
            from .golden import check
            bad = check(rec, self.golden, rec["result_index"] - 1, self.seq_base)
            if bad:
                self.stats["golden_mismatch"] += 1
                out += bad
            else:
                self.stats["golden_ok"] += 1
        return out


def main(argv=None) -> int:
    t_start = time.monotonic()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None, help="mailbox config JSON (default configs/kv260_mailbox.json)")
    ap.add_argument("--bit", default=None, help="E4a .bit (matching .hwh and .manifest.json beside it): "
                                                "Overlay(download=False)-verified device")
    ap.add_argument("--manifest", default=None, help="manifest (default <bit>.manifest.json)")
    ap.add_argument("--loaded-bin", default=None, help="firmware file the FPGA manager loaded (sha256 check)")
    ap.add_argument("--allow-dirty", action="store_true", help="accept a bitstream built from a dirty tree")
    ap.add_argument("--backend", choices=["pynq", "devmem"], default="pynq", help="without --bit")
    ap.add_argument("--fake", action="store_true", help="software producer instead of the hardware")
    ap.add_argument("--fake-rate", type=float, default=20.0)
    ap.add_argument("--fake-count", type=int, default=0, help="records the fake producer makes (0 = endless)")
    ap.add_argument("--policy", choices=["backlog", "live"], default="backlog")
    ap.add_argument("--poll-ms", type=float, default=5.0)
    ap.add_argument("--duration", type=float, default=10.0, help="seconds (0 = until a stop condition or Ctrl-C)")
    ap.add_argument("--max-records", type=int, default=0, help="stop after this many records (0 = no limit)")
    ap.add_argument("--until-done", action="store_true", help="stop when the attached run is DONE and consumed")
    ap.add_argument("--timeout", type=float, default=0.0, help="give up (exit 3) after this many seconds")
    ap.add_argument("--wait-pid", type=int, default=0, help="after verification, wait for this process to exit "
                                                            "before opening the mailbox (warm restart)")
    ap.add_argument("--golden", default=None, help="golden JSON (ps_mailbox.golden): check every record")
    ap.add_argument("--seq-base", type=int, default=0)
    ap.add_argument("--jsonl", default=None)
    ap.add_argument("--report", default=None, help="write device report + summary JSON here")
    ap.add_argument("--source", default="emg_replay")
    ap.add_argument("--expect", type=int, default=-1)
    ap.add_argument("--no-guard", action="store_true", help="skip the sysfs overlay / FPGA manager check")
    ns = ap.parse_args(argv)

    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    cfg = load_config(ns.config) if ns.config else load_config()
    stop = dev = None
    report = {"pid": os.getpid()}
    golden = None
    if ns.golden:
        from .golden import load_golden
        golden = load_golden(ns.golden)
    ichk = IndexCheck(golden, ns.seq_base)
    if ns.fake:
        mmio, _, stop = fake_source(ns.fake_rate, ns.fake_count)
        guard = None
    elif ns.bit:
        from .overlay_device import DeviceError, PynqMailboxDevice
        dev = PynqMailboxDevice(ns.bit, manifest=ns.manifest, config=ns.config, loaded_bin=ns.loaded_bin,
                                require_clean=not ns.allow_dirty)
        report["device"] = dev.report
        try:
            dev.verify()
            if ns.wait_pid:
                print("EVENT " + json.dumps({"type": "standby", "wait_pid": ns.wait_pid,
                                             "verify_s": round(time.monotonic() - t_start, 3)}),
                      file=sys.stderr, flush=True)
                t_w = time.monotonic()
                while process_alive(ns.wait_pid) and not stopping.is_set():
                    time.sleep(0.002)
                report["waited_for_pid_s"] = round(time.monotonic() - t_w, 3)
                dev.verify_board()          # FPGA manager / overlay / loaded firmware again
            mmio = dev.open_mailbox()
            dev.probe_header(mmio)
        except DeviceError as e:
            print(f"ERROR: device not verified: {e}", file=sys.stderr)
            print("REPORT " + json.dumps(report, default=str), file=sys.stderr)
            dev.close()
            return 2
        guard = None if ns.no_guard else dev.guard
    else:
        guard = None if ns.no_guard else board_guard(cfg)
        if guard is not None and guard():
            print(f"ERROR: PL design not present: {guard()}", file=sys.stderr)
            return 2
        from .registers import open_window
        mmio = open_window(ns.backend, int(cfg["ps_base"], 16), cfg["window_size"])
    rd = MailboxReader(mmio, policy=ns.policy, guard=guard)
    out = open(ns.jsonl, "w", encoding="utf-8") if ns.jsonl else sys.stdout
    t_ready = time.monotonic()
    print("EVENT " + json.dumps({"type": "ready", "startup_s": round(t_ready - t_start, 3), "pid": os.getpid()}),
          file=sys.stderr, flush=True)
    t_end = t_ready + ns.duration if ns.duration > 0 else None
    t_limit = t_start + ns.timeout if ns.timeout > 0 else None
    rc = 0
    poll_all, loop_late, proc_ns = [], [], []
    t_prev = None
    why = "duration"
    try:
        while t_end is None or time.monotonic() < t_end:
            if stopping.is_set():
                why = "signal"
                break
            if t_limit is not None and time.monotonic() > t_limit:
                why = "timeout"
                rc = 3
                break
            t0 = time.monotonic_ns()
            if t_prev is not None:
                loop_late.append(max(0, t0 - t_prev - int(ns.poll_ms * 1e6)))
            t_prev = t0
            room = ns.max_records - rd.stats["records"] if ns.max_records else None
            evs = rd.poll(room)
            t1 = time.monotonic_ns()
            poll_all.append(t1 - t0)
            for ev in evs:
                if ev["type"] == "record":
                    tp = time.monotonic_ns()
                    j = to_json(ev, ns.source)
                    bad = ichk(j)
                    if bad:
                        j["check"] = bad
                        print("EVENT " + json.dumps({"type": "mismatch", "result_index": j["result_index"],
                                                     "epoch": j["epoch"], "what": bad}), file=sys.stderr)
                    out.write(json.dumps(j) + "\n")
                    out.flush()
                    proc_ns.append(time.monotonic_ns() - tp)
                else:
                    print(f"EVENT {json.dumps(ev)}", file=sys.stderr, flush=True)
            if ns.max_records and rd.stats["records"] >= ns.max_records:
                why = "max_records"
                break
            if 0 <= ns.expect <= rd.stats["records"] and t_end is None:
                why = "expect"
                break
            if ns.until_done and not evs and rd.epoch is not None:
                try:
                    h = rd.read_header()
                except MailboxError:
                    h = None
                if h and h["epoch"] == rd.epoch and h["state"] == P.STATE_DONE and h["producer_index"] == rd.cons:
                    why = "done"
                    break
            time.sleep(ns.poll_ms / 1000.0)
    except MailboxGone as e:
        print(f"ERROR: mailbox gone: {e}", file=sys.stderr)
        why, rc = "gone", 2
    except KeyboardInterrupt:
        why = "interrupt"
    finally:
        if stop is not None:
            stop.set()
        if out is not sys.stdout:
            out.close()
    s = dict(rd.stats)
    s["stop"] = why
    s["startup_s"] = round(t_ready - t_start, 3)
    s["run_s"] = round(time.monotonic() - t_ready, 3)
    s.update(ms_stats(rd.intervals_ns, "interval_ms"))
    if rd.intervals_ns:
        s["rate_hz"] = round(1e9 / statistics.mean(rd.intervals_ns), 3)
    s.update(ms_stats(rd.poll_ns, "poll_with_records_ms"))
    s.update(ms_stats(poll_all, "poll_ms"))
    s.update(ms_stats(loop_late, "loop_late_ms"))
    s.update(ms_stats(proc_ns, "json_ms"))
    if rd.backlog:
        s["backlog_mean"] = round(statistics.mean(rd.backlog), 3)
    s["mmio_reads"], s["mmio_writes"] = mmio.reads, mmio.writes
    s.update({k: v for k, v in ichk.stats.items()})
    try:
        h = rd.read_header()
        s.update({"epoch": h["epoch"], "producer_state": P.STATE_NAMES.get(h["state"], h["state"]),
                  "producer_index": h["producer_index"], "consumer_index": h["consumer_index"],
                  "consumer_epoch": h["consumer_epoch"], "consumed_cnt": h["consumed_cnt"],
                  "published": h["published_cnt"], "result_cnt": h["result_cnt"], "drop_cnt": h["drop_cnt"],
                  "producer_errors": h["error_cnt"], "last_error": h["last_error"]})
    except MailboxError as e:
        s["header"] = f"not readable: {e}"
    if ns.expect >= 0 and s["records"] != ns.expect:
        rc = rc or 1
    if s["errors"] or s["dup"] or ichk.stats["golden_mismatch"] or ichk.stats["index_errors"]:
        rc = rc or 1
    mmio.close()
    if dev is not None:
        dev.close()
        s["devmem_mappings_after_close"] = dev.report.get("devmem_mappings_after_close")
    print("SUMMARY " + json.dumps(s), file=sys.stderr)
    if ns.report:
        report["summary"] = s
        with open(ns.report, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1, default=str)
    return rc


if __name__ == "__main__":
    sys.exit(main())
