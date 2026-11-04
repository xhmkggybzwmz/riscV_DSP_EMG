"""Ring behaviour with the producer model (software simulation, not RTL / board)."""

import unittest

from ps_mailbox import protocol as P
from ps_mailbox.fake_mmio import FakeMailbox, FakeProducer, make_result
from ps_mailbox.pynq_reader import MailboxReader


def records(events):
    return [e for e in events if e["type"] == "record"]


class TestRing(unittest.TestCase):
    def setUp(self):
        self.box = FakeMailbox()
        self.prod = FakeProducer(self.box)
        self.prod.init(0)
        self.rd = MailboxReader(self.box.ps)

    def test_nothing_before_publish(self):
        self.assertEqual(records(self.rd.poll()), [])
        self.assertEqual(self.rd.stats["attaches"], 1)

    def test_single_record(self):
        self.prod.publish(make_result(1))
        rs = records(self.rd.poll())
        self.assertEqual(len(rs), 1)
        r = make_result(1)
        self.assertEqual(rs[0]["scores"], r["scores"])
        self.assertEqual(rs[0]["argmax"], r["argmax"])
        self.assertEqual(rs[0]["window_end_seq"], 399)
        self.assertEqual(self.box.ps.read32(P.hoff("CONSUMER_INDEX")), 1)
        self.assertEqual(self.box.ps.read32(P.hoff("CONSUMED_CNT")), 1)

    def test_sixteen_then_full(self):
        for n in range(1, 23):
            self.prod.publish(make_result(n))
        self.assertEqual((self.prod.published, self.prod.drops), (16, 6))
        rs = records(self.rd.poll())
        self.assertEqual([r["result_index"] for r in rs], list(range(1, 17)))
        self.assertEqual(self.rd.stats["gaps"], 0)
        # space again: the next record carries F_DROP_BEFORE, the gap of 6 is visible
        self.prod.publish(make_result(23))
        rs = records(self.rd.poll())
        self.assertEqual(rs[0]["result_index"], 23)
        self.assertTrue(rs[0]["flags"] & P.F_DROP_BEFORE)
        self.assertEqual(self.rd.stats["gaps"], 6)
        self.assertEqual(self.rd.stats["errors"], 0)

    def test_no_overwrite_of_unconsumed(self):
        for n in range(1, 17):
            self.prod.publish(make_result(n))
        snap = bytes(self.box.mem)
        self.assertEqual(self.prod.publish(make_result(17)), 1)
        changed = [o for o in range(P.RING_OFFSET, P.RING_OFFSET + P.RING_SIZE * P.RECORD_SIZE, 4)
                   if snap[o:o + 4] != self.box.mem[o:o + 4]]
        self.assertEqual(changed, [], "a full ring must not be written")

    def test_slow_consumer(self):
        got = []
        for n in range(1, 101):
            self.prod.publish(make_result(n))
            if n % 3 == 0:
                got += records(self.rd.poll(max_records=1))
        got += records(self.rd.poll())
        self.assertEqual(self.rd.stats["errors"], 0)
        self.assertEqual(len(got), self.prod.published)
        self.assertEqual(self.rd.stats["gaps"] + 100 - got[-1]["result_index"], self.prod.drops)

    def test_index_wrap(self):
        box = FakeMailbox()
        prod = FakeProducer(box)
        prod.init(0xFFFFFFF8)
        rd = MailboxReader(box.ps)
        got = []
        for n in range(1, 41):
            prod.publish(make_result(n))
            got += records(rd.poll())
        self.assertEqual([r["result_index"] for r in got], list(range(1, 41)))
        self.assertEqual(got[8]["publish_index"], 0)
        self.assertEqual(rd.stats["errors"], 0)

    def test_live_policy(self):
        for n in range(1, 6):
            self.prod.publish(make_result(n))
        rd = MailboxReader(self.box.ps, policy="live")
        self.assertEqual(records(rd.poll()), [])
        self.prod.publish(make_result(6))
        self.assertEqual([r["result_index"] for r in records(rd.poll())], [6])


if __name__ == "__main__":
    unittest.main()
