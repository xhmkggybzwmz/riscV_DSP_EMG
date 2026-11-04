"""Reader robustness: invalid headers, torn records, concurrency, MMIO failures (model only)."""

import json
import unittest

from ps_mailbox import protocol as P
from ps_mailbox.fake_mmio import FakeMailbox, FakeProducer, make_result
from ps_mailbox.pynq_reader import MailboxGone, MailboxReader, MailboxVersionError, board_guard, to_json
from ps_mailbox.registers import MmioError


def records(events):
    return [e for e in events if e["type"] == "record"]


class TestHeader(unittest.TestCase):
    def test_fresh_bram_not_ready(self):
        box = FakeMailbox()
        rd = MailboxReader(box.ps)
        self.assertEqual(rd.poll(), [])
        self.assertEqual(rd.stats["not_ready"], 1)
        self.assertEqual(box.ps.writes, 0, "no write before a valid header")

    def test_init_state_not_ready(self):
        box = FakeMailbox()
        FakeProducer(box).init(0)
        box.cpu.write32(P.hoff("STATE"), P.STATE_INIT)
        rd = MailboxReader(box.ps)
        self.assertEqual(rd.poll(), [])

    def test_bad_version_refused(self):
        box = FakeMailbox()
        FakeProducer(box).init(0)
        box.cpu.write32(P.hoff("VERSION"), 0x00020000)
        with self.assertRaises(MailboxVersionError):
            MailboxReader(box.ps).poll()

    def test_bad_geometry_refused(self):
        box = FakeMailbox()
        FakeProducer(box).init(0)
        box.cpu.write32(P.hoff("RING_SIZE"), 32)
        with self.assertRaises(MailboxVersionError):
            MailboxReader(box.ps).poll()


class TestRecords(unittest.TestCase):
    def setUp(self):
        self.box = FakeMailbox()
        self.prod = FakeProducer(self.box)
        self.prod.init(0)
        self.rd = MailboxReader(self.box.ps)
        self.rd.poll()

    def test_corrupted_record_not_accepted(self):
        self.prod.publish(make_result(1))
        self.box.mem[P.slot_offset(0) + 8:P.slot_offset(0) + 12] = (99).to_bytes(4, "little")   # epoch word
        evs = self.rd.poll()
        self.assertEqual(records(evs), [])
        self.assertEqual(self.rd.stats["errors"], 1)
        self.assertEqual(self.box.ps.read32(P.hoff("CONSUMER_INDEX")), 0, "invalid record not acknowledged")

    def test_argmax_mismatch_detected(self):
        r = make_result(1)
        r["argmax"] = (r["argmax"] + 1) % 6
        self.prod.publish(r)
        self.assertEqual(records(self.rd.poll()), [])
        self.assertEqual(self.rd.stats["errors"], 1)

    def test_interleaved_writer_never_torn(self):
        """the reader runs between every single store of the producer"""
        got = []
        for n in range(1, 60):
            g = self.prod.publish_steps(make_result(n))
            while True:
                got += records(self.rd.poll())
                try:
                    next(g)
                except StopIteration:
                    break
            if n % 5 == 0:
                got += records(self.rd.poll())
        got += records(self.rd.poll())
        self.assertEqual(self.rd.stats["errors"], 0)
        self.assertEqual([r["result_index"] for r in got], list(range(1, 60)))
        for r in got:
            self.assertEqual(r["scores"], make_result(r["result_index"])["scores"])

    def test_writer_inside_record_read(self):
        """producer stores between the reader's word reads of one record"""
        self.prod.publish(make_result(1))
        g = self.prod.publish_steps(make_result(2))
        self.box.ps.before_read = lambda off: next(g, None)
        got = []
        for _ in range(40):
            got += records(self.rd.poll())
        self.box.ps.before_read = None
        got += records(self.rd.poll())
        self.assertEqual([r["result_index"] for r in got], [1, 2])
        self.assertEqual(self.rd.stats["errors"], 0)

    def test_json_output(self):
        self.prod.publish(make_result(3))
        j = to_json(records(self.rd.poll())[0])
        r = make_result(3)
        self.assertEqual(j["scores"], r["scores"])
        self.assertEqual(j["gesture"], P.LABELS[r["argmax"]])
        self.assertEqual(j["result_index"], 3)
        self.assertTrue(j["valid"])
        self.assertEqual(j["labels"], list(P.LABELS))
        json.dumps(j)


class TestSafety(unittest.TestCase):
    def test_bounds_and_alignment(self):
        box = FakeMailbox()
        with self.assertRaises(MmioError):
            box.ps.read32(P.WINDOW_SIZE)
        with self.assertRaises(MmioError):
            box.ps.read32(2)
        with self.assertRaises(MmioError):
            box.ps.write32(P.hoff("PRODUCER_INDEX"), 1)    # CPU-owned word

    def test_mapping_gone(self):
        box = FakeMailbox()
        prod = FakeProducer(box)
        prod.init(0)
        rd = MailboxReader(box.ps)
        rd.poll()
        box.ps.fail_after = 3
        with self.assertRaises(MailboxGone):
            for _ in range(5):
                rd.poll()

    def test_guard_stops_reader(self):
        box = FakeMailbox()
        FakeProducer(box).init(0)
        state = {"ok": True}
        rd = MailboxReader(box.ps, guard=lambda: [] if state["ok"] else ["overlay rv32_6a not present"],
                           guard_interval_s=0)
        rd.poll()
        state["ok"] = False
        with self.assertRaises(MailboxGone):
            rd.poll()

    def test_board_guard_sysfs(self):
        import tempfile
        from pathlib import Path
        cfg = {"board": {"overlay": "rv32_6a", "fpga_manager": "/sys/class/fpga_manager/fpga0"}}
        with tempfile.TemporaryDirectory() as d:
            g = board_guard(cfg, d)
            self.assertEqual(len(g()), 2)
            (Path(d) / "sys/kernel/config/device-tree/overlays/rv32_6a").mkdir(parents=True)
            (Path(d) / "sys/kernel/config/device-tree/overlays/rv32_6a/status").write_text("applied\n")
            (Path(d) / "sys/class/fpga_manager/fpga0").mkdir(parents=True)
            (Path(d) / "sys/class/fpga_manager/fpga0/state").write_text("operating\n")
            self.assertEqual(g(), [])


if __name__ == "__main__":
    unittest.main()
