"""Mailbox simulation for PC tests: shared memory, PS / CPU views, a producer model.

FakeMailbox      the 4 KB window as a bytearray (zero = freshly configured BRAM)
  .ps            MmioWindow for the reader; writes outside the consumer words raise
                 (ownership), optional hook before every read (lets a test run producer
                 steps "concurrently" between two reader accesses), fault injection
  .cpu           MmioWindow for the producer model; writes to the consumer words raise
FakeProducer     Python model of firmware/rv32/mailbox/mbx_common.h (mbx_init,
                 mbx_consumer, mbx_publish): same write order, same drop rules.
                 publish_steps() yields after every single store, so a test can
                 interleave reader accesses with an unfinished record.
make_result(n)   deterministic CNN-like result number n (scores, argmax, seq, ts)
This is a software model: a PASS here is not a board or RTL result.
"""

from __future__ import annotations

from . import protocol as P
from .registers import MmioError, MmioWindow

PS_WORDS = {P.hoff(n) for n, (_, who, _) in P.HEADER.items() if who == "ps"}


class _View(MmioWindow):
    def __init__(self, box: "FakeMailbox", who: str):
        super().__init__(P.WINDOW_SIZE)
        self.box, self.who = box, who
        self.before_read = None          # callable(off) or None
        self.fail_after = None           # raise MmioError after this many accesses

    def _count(self) -> None:
        if self.fail_after is not None:
            if self.fail_after <= 0:
                raise MmioError("simulated: mapping gone (overlay removed)")
            self.fail_after -= 1

    def _rd(self, off: int) -> int:
        self._count()
        if self.before_read is not None:
            self.before_read(off)
        return int.from_bytes(self.box.mem[off:off + 4], "little")

    def _wr(self, off: int, value: int) -> None:
        self._count()
        if (off in PS_WORDS) != (self.who == "ps"):
            raise MmioError(f"{self.who} wrote {off:#x}, owned by the other side")
        self.box.mem[off:off + 4] = value.to_bytes(4, "little")


class FakeMailbox:
    def __init__(self):
        self.mem = bytearray(P.WINDOW_SIZE)
        self.ps = _View(self, "ps")
        self.cpu = _View(self, "cpu")

    def reconfigure(self) -> None:
        """a new bitstream: BRAM back to its (zero) initial content"""
        self.mem[:] = bytes(P.WINDOW_SIZE)


def make_result(n: int, seq_base: int = 0) -> dict:
    """CNN result number n (1-based, like CNN RES_INDEX); window end = 399 + 100 (n - 1)"""
    scores = [((n * 7919 + c * 104729) % 2000001) - 1000000 for c in range(6)]
    end = 399 + 100 * (n - 1)
    return {"index": n, "seq": (seq_base + end) & P.MASK32, "ts": end, "seg": 0,
            "argmax": P.argmax_lowest(scores), "lat": 11152, "scores": scores}


class FakeProducer:
    """model of the RV32I producer (mbx_common.h)"""

    def __init__(self, box: FakeMailbox, ring_size: int = P.RING_SIZE):
        self.m = box.cpu
        self.ring = ring_size
        self.cycle = 0
        self.epoch = self.start = self.prod = 0
        self.published = self.results = self.drops = self.errors = 0
        self.drop_pending = False

    def _w(self, name: str, v: int) -> None:
        self.m.write32(P.hoff(name), v)

    def _r(self, name: str) -> int:
        return self.m.read32(P.hoff(name))

    def init(self, start: int = 0) -> None:
        old = 0
        if self._r("MAGIC") == P.MAGIC and (self._r("VERSION") >> 16) == P.VERSION_MAJOR:
            old = self._r("RESET_EPOCH")
        self.epoch = (old + 1) & P.MASK32 or 1
        self.start = self.prod = start & P.MASK32
        self.published = self.results = self.drops = self.errors = 0
        self.drop_pending = False
        self._w("STATE", P.STATE_INIT)
        for s in range(P.RING_SIZE):
            self.m.write32(P.slot_offset(s), 0)
        for name, v in (("VERSION", P.VERSION), ("RING_SIZE", P.RING_SIZE), ("RECORD_SIZE", P.RECORD_SIZE),
                        ("RING_OFFSET", P.RING_OFFSET), ("RESET_EPOCH", self.epoch),
                        ("PRODUCER_INDEX", self.start), ("EPOCH_START_INDEX", self.start),
                        ("PUBLISHED_CNT", 0), ("RESULT_CNT", 0), ("DROP_CNT", 0), ("ERROR_CNT", 0),
                        ("LAST_ERROR", P.ERR_NONE), ("HEARTBEAT", self.cycle), ("MAGIC", P.MAGIC)):
            self._w(name, v)
        self._w("STATE", P.STATE_RUNNING)

    def consumer(self) -> int:
        for _ in range(4):
            e1, c1 = self._r("CONSUMER_EPOCH"), self._r("CONSUMER_INDEX")
            c2, e2 = self._r("CONSUMER_INDEX"), self._r("CONSUMER_EPOCH")
            if e1 == e2 and c1 == c2:
                return c1 if e1 == self.epoch else self.start
        return (self.prod - self.ring) & P.MASK32

    def publish_steps(self, r: dict, flags: int = P.F_POSTPROC | P.F_ARGMAX_OK):
        """generator: one store per step; returns 0 published, 1 dropped (full), 2 bad consumer"""
        self.results += 1
        self._w("RESULT_CNT", self.results)
        yield
        used = P.used(self.prod, self.consumer())
        if used > self.ring:
            self.errors += 1
            self._w("ERROR_CNT", self.errors)
            self._w("LAST_ERROR", P.ERR_BAD_CONSUMER)
            self.drop_pending = True
            return 2
        if used == self.ring:
            self.drops += 1
            self._w("DROP_CNT", self.drops)
            self.drop_pending = True
            return 1
        i = self.prod
        if self.drop_pending:
            flags |= P.F_DROP_BEFORE
        if self.published == 0:
            flags |= P.F_NEW_SEGMENT
        self.cycle += 1000
        rec = {"flags": flags, "publish_index": i, "epoch": self.epoch, "result_index": r["index"],
               "window_end_seq": r["seq"], "esi_timestamp": r["ts"], "gesture": r["argmax"],
               "argmax": r["argmax"], "segment": r["seg"], "scores": r["scores"], "cnn_lat_cycles": r["lat"],
               "detect_cycle": self.cycle, "publish_cycle": self.cycle + 600}
        words = P.encode_record(rec)
        base = P.slot_offset(i)
        self.m.write32(base, 0)
        yield
        for w in range(1, P.RECORD_WORDS):
            self.m.write32(base + 4 * w, words[w])
            yield
        self.m.write32(base, words[0])
        yield
        self.prod = (i + 1) & P.MASK32
        self._w("PRODUCER_INDEX", self.prod)
        yield
        self.published += 1
        self._w("PUBLISHED_CNT", self.published)
        self.drop_pending = False
        return 0

    def publish(self, r: dict, flags: int = P.F_POSTPROC | P.F_ARGMAX_OK) -> int:
        g = self.publish_steps(r, flags)
        while True:
            try:
                next(g)
            except StopIteration as e:
                return e.value

    def done(self) -> None:
        self._w("STATE", P.STATE_DONE)
