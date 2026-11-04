"""Result sources of the gesture server. Each runs in ONE worker thread and hands records to
the server through a non-blocking sink; the network never blocks a source.

    HardwareSource   the KV260 mailbox: PynqMailboxDevice (Overlay(download=False) metadata,
                     artifact / board / HWH / debug-ID checks, once at start) + pynq.MMIO +
                     MailboxReader. The only mailbox consumer of the process; refuses to start
                     when another process maps the mailbox or holds the consumer lock.
    SyntheticSource  "synthetic_demo": a software RV32I producer model (ps_mailbox.fake_mmio)
                     cycling through the six gestures at 20 Hz, consumed by a real MailboxReader
                     (the same consumer path as the hardware). NOT CNN output.
    ReplaySource     "test_fixture": records of a saved JSON Lines file (e.g. the E4a-B board
                     run), with their original spacing or a fixed rate.

sink(rec, meta): rec has the ps_mailbox.pynq_reader.to_json() keys; meta = {"ps_ns",
"mailbox_drop_cnt", "valid", "fixture_origin"}. sink must not block (the server's handoff).
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path

from ps_mailbox import protocol as MP
from ps_mailbox.pynq_reader import MailboxError, MailboxGone, MailboxReader, to_json

LOCK_FILE = "/run/lock/ps_mailbox_consumer.lock"


class SourceError(Exception):
    """the source cannot start (hardware unavailable, ownership, file)"""


class Source:
    label = ""

    def __init__(self, poll_ms: float = 5.0):
        self.poll_ms = poll_ms
        self.state = "starting"
        self.error = None
        self.stats = {"records": 0}
        self._stop = threading.Event()
        self._requested = threading.Event()      # stop() was called (vs. a finite source ending)
        self._thread = None
        self._sink = None

    def open(self) -> None:
        """everything that can fail before the server starts listening"""

    def start(self, sink) -> None:
        self._sink = sink
        self._thread = threading.Thread(target=self._run_guarded, name=f"source-{self.label}", daemon=True)
        self._thread.start()

    def _run_guarded(self) -> None:
        self.state = "running"
        try:
            self._run()
            if self.state == "running":
                self.state = "stopped" if self._requested.is_set() else "done"
        except Exception as e:                 # noqa: BLE001 - reported through status
            self.state, self.error = "error", f"{type(e).__name__}: {e}"
        finally:
            self._release()

    def _run(self) -> None:
        raise NotImplementedError

    def _release(self) -> None:
        """free hardware / files; runs in the worker thread when it ends"""

    def stop(self, timeout: float = 5.0) -> bool:
        """stop reading; True once the worker (the only consumer) has exited"""
        self._requested.set()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            alive = self._thread.is_alive()
        else:
            alive = False
            self._release()
        if not alive and self.state in ("running", "starting"):
            self.state = "stopped"
        return not alive

    def counters(self) -> dict:
        return dict(self.stats)


class MailboxSource(Source):
    """a MailboxReader polling loop on an MmioWindow (shared by hardware and synthetic)"""

    def __init__(self, policy: str = "live", poll_ms: float = 5.0):
        super().__init__(poll_ms)
        self.policy = policy
        self.reader = None
        self.mmio = None
        self.poll_ns = deque(maxlen=4096)        # duration of each reader.poll() (header + records + ACK)
        self.poll_rec_ns = deque(maxlen=1024)    # ... of the polls that returned records

    def _meta(self) -> dict:
        h = self.reader.header or {}
        return {"ps_ns": time.monotonic_ns(), "mailbox_drop_cnt": h.get("drop_cnt", 0), "valid": True}

    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic_ns()
            try:
                evs = self.reader.poll()
            except MailboxGone as e:
                self.state, self.error = "error", f"mailbox gone: {e}"
                return
            dt = time.monotonic_ns() - t0
            self.poll_ns.append(dt)
            if any(ev["type"] == "record" for ev in evs):
                self.poll_rec_ns.append(dt)
            for ev in evs:
                if ev["type"] == "record":
                    self._sink(to_json(ev, self.label), self._meta())
            self._after_poll()
            self._stop.wait(self.poll_ms / 1000.0)

    def _after_poll(self) -> None:
        pass

    def counters(self) -> dict:
        c = {}
        if self.reader is not None:
            s = self.reader.stats
            c = {k: s[k] for k in ("records", "gaps", "dup", "drop_flags", "errors", "resyncs", "attaches",
                                   "reattaches", "stable_retries", "rereads", "polls", "backlog_max")}
            h = self.reader.header or {}
            for name, q in (("poll_ms", self.poll_ns), ("poll_with_records_ms", self.poll_rec_ns)):
                if q:
                    v = sorted(q)
                    c[name] = {"mean": round(sum(v) / len(v) / 1e6, 3), "p99": round(v[int(0.99 * (len(v) - 1))] / 1e6, 3),
                               "max": round(v[-1] / 1e6, 3), "n": len(v)}
            # header words as of the last poll's header read; consumer_index = the reader's own position
            c.update({"reset_epoch": h.get("epoch"), "producer_state": MP.STATE_NAMES.get(h.get("state"), h.get("state")),
                      "mailbox_drop_cnt": h.get("drop_cnt"), "mailbox_error_cnt": h.get("error_cnt"),
                      "producer_index": h.get("producer_index"), "consumer_index": self.reader.cons})
        return c


def devmem_mappers(base: int, size: int) -> list[str]:
    """other processes mapping [base, base + size) of /dev/mem (from /proc/*/maps offsets)"""
    out = []
    if not Path("/proc").is_dir():                  # not Linux (unit tests on the PC)
        return out
    for d in Path("/proc").iterdir():
        if not d.name.isdigit() or int(d.name) == os.getpid():
            continue
        try:
            for line in (d / "maps").read_text().splitlines():
                f = line.split()
                if len(f) >= 6 and f[5] == "/dev/mem":
                    lo, hi = (int(x, 16) for x in f[0].split("-"))
                    off = int(f[2], 16)
                    if off < base + size and off + (hi - lo) > base:
                        out.append(f"{d.name} {(d / 'comm').read_text().strip()}")
                        break
        except (OSError, ValueError):
            pass
    return out


def acquire_consumer_lock(path: str):
    """exclusive, non-blocking flock on the consumer lock file (Linux); returns the open file
    (closing it releases the lock). The file is never removed (no unlink / re-create race).
    - an existing file is opened without O_CREAT: with fs.protected_regular = 2 (Ubuntu) even
      root may not O_CREAT-open another user's file in the sticky /run/lock
    - O_NOFOLLOW: a symlink at the path is refused, never followed
    - only a regular file is accepted (O_NONBLOCK: a FIFO there does not block the open)
    - a file created by someone else between the two opens is simply opened"""
    import fcntl                                          # noqa: PLC0415 (Linux / the board only)
    import stat                                           # noqa: PLC0415
    base = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    fd = None
    for _ in range(3):
        try:
            fd = os.open(path, base)
            break
        except FileNotFoundError:
            try:
                fd = os.open(path, base | os.O_CREAT | os.O_EXCL, 0o644)
                break
            except FileExistsError:
                continue
        except OSError as e:                              # ELOOP (symlink), EISDIR, EACCES, ...
            raise SourceError(f"consumer lock {path}: {e.strerror}") from e
    if fd is None:
        raise SourceError(f"consumer lock {path}: could not be opened")
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise SourceError(f"consumer lock {path}: not a regular file")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        raise SourceError(f"another mailbox consumer holds {path}") from e
    return os.fdopen(fd, "r+")


class HardwareSource(MailboxSource):
    label = "hardware_replay"

    def __init__(self, bit: str, loaded_bin: str | None = None, manifest: str | None = None,
                 config: str | None = None, policy: str = "live", poll_ms: float = 5.0, lock_file: str | None = LOCK_FILE,
                 device_kwargs: dict | None = None):
        super().__init__(policy, poll_ms)
        self.bit, self.loaded_bin, self.manifest, self.config = bit, loaded_bin, manifest, config
        self.lock_file = lock_file                 # None: no consumer lock (tests off the board)
        self.device_kwargs = device_kwargs or {}   # tests: overlay_factory / mmio_factory / sysfs_root
        self.opens = 0
        self.dev = None
        self._lock = None
        self.report = {}

    def open(self) -> None:
        from ps_mailbox.overlay_device import DeviceError, PynqMailboxDevice
        self.opens += 1
        if self.lock_file:
            self._lock = acquire_consumer_lock(self.lock_file)
        self.dev = PynqMailboxDevice(self.bit, manifest=self.manifest, config=self.config, loaded_bin=self.loaded_bin,
                                     **self.device_kwargs)
        self.report = self.dev.report
        try:
            self.dev.verify()              # once: Overlay(download=False), artifacts, board, HWH, debug ID
            base, size = self.dev.mbx
            other = devmem_mappers(base, size)
            if other:
                raise SourceError(f"the mailbox is mapped by other processes: {other}")
            self.mmio = self.dev.open_mailbox()
            self.dev.probe_header(self.mmio)
        except (DeviceError, SourceError) as e:
            self._release()
            raise SourceError(f"hardware unavailable: {e}") from e
        self.reader = MailboxReader(self.mmio, policy=self.policy, guard=self.dev.guard)

    def _release(self) -> None:
        if self.dev is not None:
            self.dev.close()                     # unmaps every window (checked in /proc/self/maps)
            self.report["devmem_mappings_after_close"] = self.dev.report.get("devmem_mappings_after_close")
            self.dev = None
        self.mmio = None
        if self._lock is not None:
            self._lock.close()                   # releases the flock
            self._lock = None


class SyntheticSource(MailboxSource):
    """SIMULATED events: six gestures in turn, each held `hold` results, at `rate_hz`"""
    label = "synthetic_demo"

    def __init__(self, rate_hz: float = 20.0, hold: int = 30, count: int = 0, sequence=None,
                 policy: str = "backlog", poll_ms: float = 5.0):
        super().__init__(policy, poll_ms)
        self.rate_hz, self.hold, self.count = rate_hz, hold, count
        self.sequence = list(sequence) if sequence is not None else list(range(len(MP.LABELS)))
        self._prod_thread = None
        self._produced = threading.Event()      # the finite (count > 0) producer has finished

    @staticmethod
    def scores_for(g: int, n: int) -> list[int]:
        """scores whose argmax is g (ties never happen); small per-result variation"""
        return [(600000 if c == g else -300000 - 50000 * c) + ((n * 7919 + c * 104729) % 20001) - 10000
                for c in range(len(MP.LABELS))]

    def open(self) -> None:
        from ps_mailbox.fake_mmio import FakeMailbox, FakeProducer
        self.box = FakeMailbox()
        self.prod = FakeProducer(self.box)
        self.mmio = self.box.ps
        self.reader = MailboxReader(self.mmio, policy=self.policy)

    def gesture_at(self, n: int) -> int:
        return self.sequence[((n - 1) // self.hold) % len(self.sequence)]

    def _produce(self) -> None:
        self.prod.init(0)
        n, t_next = 0, time.monotonic()
        while not self._stop.is_set() and (self.count <= 0 or n < self.count):
            t_next += 1.0 / self.rate_hz
            self._stop.wait(max(0.0, t_next - time.monotonic()))
            n += 1
            g = self.gesture_at(n)
            end = 399 + 100 * (n - 1)
            self.prod.publish({"index": n, "seq": end & MP.MASK32, "ts": end, "seg": 0, "argmax": g, "lat": 11152,
                               "scores": self.scores_for(g, n)})
        self.prod.done()
        self._produced.set()

    def start(self, sink) -> None:
        self._prod_thread = threading.Thread(target=self._produce, name="synthetic-producer", daemon=True)
        self._prod_thread.start()
        super().start(sink)

    def _after_poll(self) -> None:
        if self.count > 0 and self._produced.is_set() and self.reader.cons == self.prod.prod:
            self._stop.set()

    def stop(self, timeout: float = 5.0) -> bool:
        ok = super().stop(timeout)
        if self._prod_thread is not None:
            self._prod_thread.join(timeout)
        return ok


class ReplaySource(Source):
    """records of saved JSON Lines (ps_mailbox.cli / board_e4b output), re-emitted"""
    label = "test_fixture"

    def __init__(self, files, speed: float = 1.0, rate_hz: float = 0.0, loop: bool = False, max_gap_s: float = 1.0,
                 poll_ms: float = 5.0):
        super().__init__(poll_ms)
        self.files = [Path(f) for f in files]
        self.speed, self.rate_hz, self.loop, self.max_gap_s = speed, rate_hz, loop, max_gap_s
        self.records = []

    def open(self) -> None:
        for f in self.files:
            if not f.is_file():
                raise SourceError(f"{f}: no such file")
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    r = json.loads(line)
                    r["_origin"] = f"{f.name} (saved record, original source {r.get('source', '?')})"
                    self.records.append(r)
        if not self.records:
            raise SourceError("no records to replay")

    def _run(self) -> None:
        while True:
            prev = None
            for r in self.records:
                if self.rate_hz > 0:
                    gap = 1.0 / self.rate_hz
                elif prev is not None and r.get("ps_read_ns") and prev.get("ps_read_ns"):
                    gap = min(self.max_gap_s, max(0.0, (r["ps_read_ns"] - prev["ps_read_ns"]) / 1e9)) / self.speed
                else:
                    gap = 0.05 / self.speed
                if self._stop.wait(gap):
                    return
                prev = r
                self.stats["records"] += 1
                self._sink(r, {"ps_ns": time.monotonic_ns(), "mailbox_drop_cnt": 0, "valid": "check" not in r,
                               "fixture_origin": r["_origin"]})
            if not self.loop:
                return
