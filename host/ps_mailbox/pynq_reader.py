"""PS-side mailbox v1 consumer (protocol.py) on top of an MmioWindow.

    from ps_mailbox.registers import open_window
    from ps_mailbox.pynq_reader import MailboxReader, board_guard, load_config
    cfg = load_config()                                   # configs/kv260_mailbox.json
    mmio = open_window("pynq", int(cfg["ps_base"], 16), cfg["window_size"])
    rd = MailboxReader(mmio, policy="backlog", guard=board_guard(cfg))
    for ev in rd.poll():                                  # call periodically
        ...

The reader only reads / writes the mailbox window of an already configured PL. It never
loads a bitstream (no pynq.Overlay here; overlay_device.py uses Overlay(download=False) for
the hardware metadata only), never writes FPGA manager flags and never touches overlays or
clocks. board_guard() only reads sysfs (overlay status, FPGA manager state) so that the
reader stops before it would access a PL window that is gone.

Events returned by poll(): dicts with "type"
  "record"  one valid result (to_json() gives the output record)
  "attach"  attached to an epoch (policy, start index; "resume": True after a restart)
  "resync"  the producer started a new epoch (CPU / PL reset, new run)
  "reattach" CONSUMER_EPOCH no longer ours (PL reconfigured: the BRAM restarts at zero and
            the new run may reuse the epoch number), attached again
  "error"   protocol problem that was skipped (counted in stats["errors"])
"""

from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path

from . import protocol as P
from .registers import MmioError, MmioWindow

CONFIG = Path(__file__).resolve().parent / "configs" / "kv260_mailbox.json"


class MailboxError(Exception):
    pass


class MailboxNotReady(MailboxError):
    """no valid header yet (BRAM fresh, producer in INIT, header changing)"""


class MailboxVersionError(MailboxError):
    """MAGIC present but an incompatible VERSION / geometry: do not parse"""


class MailboxGone(MailboxError):
    """the board guard failed or an MMIO access failed: stop using the window"""


def load_config(path: str | Path = CONFIG) -> dict:
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    if int(cfg["magic"], 16) != P.MAGIC or int(cfg["version"], 16) != P.VERSION:
        raise MailboxVersionError(f"{path}: config for another protocol version")
    if cfg["ring_size"] != P.RING_SIZE or cfg["record_size"] != P.RECORD_SIZE or cfg["ring_offset"] != P.RING_OFFSET:
        raise MailboxVersionError(f"{path}: geometry differs from protocol.py")
    return cfg


def board_guard(cfg: dict, sysfs_root: str = "/"):
    """callable -> list of problems (empty = PL design present). sysfs reads only."""
    root = Path(sysfs_root)
    ovl = root / "sys/kernel/config/device-tree/overlays" / cfg["board"]["overlay"] / "status"
    state = root / cfg["board"]["fpga_manager"].lstrip("/") / "state"

    def check() -> list[str]:
        bad = []
        try:
            if ovl.read_text().strip() != "applied":
                bad.append(f"overlay {cfg['board']['overlay']} not applied")
        except OSError:
            bad.append(f"overlay {cfg['board']['overlay']} not present")
        try:
            if state.read_text().strip() != "operating":
                bad.append("FPGA manager not operating")
        except OSError:
            bad.append("FPGA manager state not readable")
        return bad
    return check


class MailboxReader:
    def __init__(self, mmio: MmioWindow, policy: str = "backlog", guard=None, guard_interval_s: float = 1.0,
                 stable_tries: int = 8, record_tries: int = 3, clock=time.monotonic_ns, history: int = 1024):
        if policy not in ("backlog", "live"):
            raise ValueError("policy: backlog or live")
        self.m, self.policy, self.guard = mmio, policy, guard
        self.guard_interval_ns = int(guard_interval_s * 1e9)
        self.stable_tries, self.record_tries, self.clock = stable_tries, record_tries, clock
        self.epoch = None                   # attached epoch
        self.epoch_start = 0                # EPOCH_START_INDEX of that epoch
        self.header = None                  # header snapshot of the last poll() that read one
        self.cons = 0
        self.last_result = None
        self.rx_epoch = 0
        self._guard_t = None
        self.stats = {"records": 0, "gaps": 0, "dup": 0, "drop_flags": 0, "errors": 0, "resyncs": 0, "attaches": 0,
                      "resumes": 0, "reattaches": 0, "stable_retries": 0, "stable_retry_max": 0, "rereads": 0,
                      "polls": 0, "not_ready": 0, "backlog_max": 0}
        self.intervals_ns = deque(maxlen=history)     # PS read time between consecutive records
        self.poll_ns = deque(maxlen=history)          # time spent in poll() when it returned records
        self.backlog = deque(maxlen=history)          # records available, at polls that found some
        self._t_last = None

    # ------------------------------------------------------------ low level
    def _rd(self, off: int) -> int:
        try:
            return self.m.read32(off)
        except MmioError as e:
            raise MailboxGone(str(e)) from e

    def _wr(self, off: int, v: int) -> None:
        try:
            self.m.write32(off, v)
        except MmioError as e:
            raise MailboxGone(str(e)) from e

    def stable(self, off: int) -> int:
        """two consecutive equal reads (the producer may write the word in the same cycle)"""
        a = self._rd(off)
        for k in range(self.stable_tries):
            b = self._rd(off)
            if a == b:
                if k > self.stats["stable_retry_max"]:
                    self.stats["stable_retry_max"] = k
                return b
            self.stats["stable_retries"] += 1
            a = b
        raise MailboxNotReady(f"word {off:#x} not stable")

    def _check_guard(self, force: bool = False) -> None:
        if self.guard is None:
            return
        now = self.clock()
        if force or self._guard_t is None or now - self._guard_t >= self.guard_interval_ns:
            bad = self.guard()
            self._guard_t = now
            if bad:
                raise MailboxGone("; ".join(bad))

    # ------------------------------------------------------------ header
    def read_header(self) -> dict:
        """consistent header snapshot; MailboxNotReady / MailboxVersionError otherwise"""
        magic = self.stable(P.hoff("MAGIC"))
        if magic != P.MAGIC:
            raise MailboxNotReady(f"MAGIC {magic:#010x}")
        ver = self.stable(P.hoff("VERSION"))
        geo = (self.stable(P.hoff("RING_SIZE")), self.stable(P.hoff("RECORD_SIZE")), self.stable(P.hoff("RING_OFFSET")))
        e1, s1 = self.stable(P.hoff("RESET_EPOCH")), self.stable(P.hoff("STATE"))
        if s1 == P.STATE_INIT:
            raise MailboxNotReady("producer initialising")
        if ver >> 16 != P.VERSION_MAJOR or geo != (P.RING_SIZE, P.RECORD_SIZE, P.RING_OFFSET):
            raise MailboxVersionError(f"VERSION {ver:#010x} geometry {geo}")
        h = {"epoch": e1, "state": s1}
        for n in ("PRODUCER_INDEX", "EPOCH_START_INDEX", "PUBLISHED_CNT", "RESULT_CNT", "DROP_CNT", "ERROR_CNT",
                  "LAST_ERROR", "HEARTBEAT", "CONSUMER_INDEX", "CONSUMER_EPOCH", "CONSUMED_CNT"):
            h[n.lower()] = self.stable(P.hoff(n))
        e2, s2 = self.stable(P.hoff("RESET_EPOCH")), self.stable(P.hoff("STATE"))
        if (e1, s1) != (e2, s2):
            raise MailboxNotReady("header changed while reading")
        if s1 not in (P.STATE_RUNNING, P.STATE_DONE, P.STATE_ERROR):
            raise MailboxNotReady(f"STATE {s1}")
        return h

    # ------------------------------------------------------------ attach
    def attach(self, h: dict) -> dict:
        prod = h["producer_index"]
        if h["consumer_epoch"] == h["epoch"] and P.used(prod, h["consumer_index"]) <= P.RING_SIZE:
            self.cons = h["consumer_index"]          # this consumer owned the epoch: resume
            self.rx_epoch = h["consumed_cnt"]
            resume = True
            self.stats["resumes"] += 1
        else:
            self.cons = prod if self.policy == "live" else h["epoch_start_index"]
            self._wr(P.hoff("CONSUMER_INDEX"), self.cons)
            self._wr(P.hoff("CONSUMED_CNT"), 0)
            self._wr(P.hoff("CONSUMER_EPOCH"), h["epoch"])
            self.rx_epoch = 0
            resume = False
        if self.epoch is not None and self.epoch != h["epoch"]:
            self.stats["resyncs"] += 1
        self.epoch = h["epoch"]
        self.epoch_start = h["epoch_start_index"]
        self.last_result = None
        self.stats["attaches"] += 1
        return {"type": "attach", "epoch": self.epoch, "start": self.cons, "policy": self.policy, "resume": resume}

    # ------------------------------------------------------------ records
    def read_record(self, index: int) -> dict | None:
        """validated record `index` or None (epoch changed under the reader)"""
        base = P.slot_offset(index)
        for _ in range(self.record_tries):
            w = [self._rd(base + 4 * k) for k in range(P.RECORD_WORDS)]
            h2, p2 = self._rd(base), self._rd(base + 4)
            rec = P.decode_record(w)
            probs = P.record_problems(rec, index, self.epoch)
            if not probs and h2 == w[0] and p2 == w[1]:
                return rec
            self.stats["rereads"] += 1
            if self.stable(P.hoff("RESET_EPOCH")) != self.epoch:
                return None
        raise MailboxError(f"record {index}: {probs or ['changed while reading']}")

    def acknowledge(self, index: int) -> None:
        """record `index` consumed: CONSUMER_INDEX = index + 1"""
        self.cons = (index + 1) & P.MASK32
        self.rx_epoch += 1
        self._wr(P.hoff("CONSUMER_INDEX"), self.cons)
        self._wr(P.hoff("CONSUMED_CNT"), self.rx_epoch)

    def _account(self, rec: dict) -> None:
        if self.last_result is not None:
            step = (rec["result_index"] - self.last_result) & P.MASK32
            if step == 0 or step > 0x80000000:
                self.stats["dup"] += 1
                self.stats["errors"] += 1
            elif step > 1:
                self.stats["gaps"] += step - 1
                if not rec["flags"] & P.F_DROP_BEFORE:
                    self.stats["errors"] += 1
        if rec["flags"] & P.F_DROP_BEFORE:
            self.stats["drop_flags"] += 1
        self.last_result = rec["result_index"]

    # ------------------------------------------------------------ poll
    def poll(self, max_records: int | None = None) -> list[dict]:
        """one pass: header, (re)attach, all available records (up to max_records)"""
        t0 = self.clock()
        self.stats["polls"] += 1
        self._check_guard()
        try:
            h = self.read_header()
        except MailboxNotReady:
            self.stats["not_ready"] += 1
            return []
        self.header = h
        events = []
        if self.epoch != h["epoch"]:
            if self.epoch is not None:
                events.append({"type": "resync", "old_epoch": self.epoch, "epoch": h["epoch"]})
            events.append(self.attach(h))
        elif h["consumer_epoch"] != self.epoch:
            # our own CONSUMER_EPOCH word was lost (BRAM re-initialised by a new bitstream,
            # whose run can start at the same epoch number) or taken over: attach again
            self.stats["reattaches"] += 1
            events.append({"type": "reattach", "epoch": h["epoch"], "consumer_epoch": h["consumer_epoch"]})
            self.epoch = None
            events.append(self.attach(h))
        avail = P.used(h["producer_index"], self.cons)
        if avail > P.RING_SIZE:
            self.stats["errors"] += 1
            events.append({"type": "error", "what": f"PRODUCER_INDEX - CONSUMER_INDEX = {avail}"})
            self.epoch = None                       # re-attach on the next poll
            return events
        if avail:
            self.backlog.append(avail)
            self.stats["backlog_max"] = max(self.stats["backlog_max"], avail)
        n = avail if max_records is None else min(avail, max_records)
        for _ in range(n):
            try:
                rec = self.read_record(self.cons)
            except MailboxError as e:
                self.stats["errors"] += 1
                events.append({"type": "error", "what": str(e)})
                self.epoch = None
                break
            if rec is None:                          # producer restarted while reading
                self.epoch = None
                break
            self._account(rec)
            rec["epoch_seq"] = (rec["publish_index"] - self.epoch_start) & P.MASK32
            self.acknowledge(rec["publish_index"])
            now = self.clock()
            rec["ps_read_ns"] = now
            if self._t_last is not None:
                self.intervals_ns.append(now - self._t_last)
            self._t_last = now
            self.stats["records"] += 1
            rec["type"] = "record"
            events.append(rec)
        if any(e["type"] == "record" for e in events):
            self.poll_ns.append(self.clock() - t0)
        return events


def to_json(rec: dict, source: str = "emg_replay") -> dict:
    """output record (JSON Lines); scores in the frozen E2/E3 class order"""
    g = rec["gesture"]
    return {
        "result_index": rec["result_index"], "window_end_seq": rec["window_end_seq"],
        "gesture": P.gesture_name(g), "gesture_id": g, "argmax": rec["argmax"],
        "scores": rec["scores"], "labels": list(P.LABELS), "source": source, "valid": True,
        "publish_index": rec["publish_index"], "epoch": rec["epoch"], "epoch_seq": rec.get("epoch_seq"),
        "segment": rec["segment"],
        "esi_timestamp": rec["esi_timestamp"], "flags": rec["flags"],
        "drop_before": bool(rec["flags"] & P.F_DROP_BEFORE),
        "cnn_lat_cycles": rec["cnn_lat_cycles"], "detect_cycle": rec["detect_cycle"],
        "publish_cycle": rec["publish_cycle"],
        "publish_minus_detect_cycles": (rec["publish_cycle"] - rec["detect_cycle"]) & P.MASK32,
        "ps_read_ns": rec.get("ps_read_ns"),
    }
