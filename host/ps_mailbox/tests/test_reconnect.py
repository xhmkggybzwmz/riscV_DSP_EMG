"""Producer resets (new epoch), reader restarts, PL reconfiguration (model only)."""

import unittest

from ps_mailbox import protocol as P
from ps_mailbox.fake_mmio import FakeMailbox, FakeProducer, make_result
from ps_mailbox.pynq_reader import MailboxReader


def records(events):
    return [e for e in events if e["type"] == "record"]


class TestReconnect(unittest.TestCase):
    def test_producer_restart_new_epoch(self):
        box = FakeMailbox()
        prod = FakeProducer(box)
        prod.init(0)
        rd = MailboxReader(box.ps)
        for n in range(1, 6):
            prod.publish(make_result(n))
        self.assertEqual(len(records(rd.poll())), 5)
        # CPU reset in the middle of record 6: invalidated, partly written, never published
        g = prod.publish_steps(make_result(6))
        for _ in range(5):
            next(g)
        prod2 = FakeProducer(box)
        prod2.init(0)                       # firmware restart: epoch + 1, ring headers cleared
        self.assertEqual(prod2.epoch, 2)
        for n in range(1, 4):
            prod2.publish(make_result(n))
        evs = rd.poll()
        self.assertEqual([e["type"] for e in evs][:2], ["resync", "attach"])
        self.assertEqual([r["result_index"] for r in records(evs)], [1, 2, 3])
        self.assertEqual(rd.stats["resyncs"], 1)
        self.assertEqual(rd.stats["errors"], 0)

    def test_reader_restart_resumes(self):
        box = FakeMailbox()
        prod = FakeProducer(box)
        prod.init(0)
        rd = MailboxReader(box.ps)
        for n in range(1, 8):
            prod.publish(make_result(n))
        first = records(rd.poll(max_records=4))
        rd2 = MailboxReader(box.ps)          # new process, same epoch: resume at CONSUMER_INDEX
        evs = rd2.poll()
        self.assertTrue(evs[0]["resume"])
        rest = records(evs)
        self.assertEqual([r["result_index"] for r in first + rest], list(range(1, 8)))
        self.assertEqual(box.ps.read32(P.hoff("CONSUMED_CNT")), 7)

    def test_reconfiguration(self):
        box = FakeMailbox()
        prod = FakeProducer(box)
        prod.init(0)
        rd = MailboxReader(box.ps)
        prod.publish(make_result(1))
        rd.poll()
        box.reconfigure()                    # new bitstream: BRAM zero
        self.assertEqual(rd.poll(), [])
        self.assertEqual(rd.stats["not_ready"], 1)
        prod = FakeProducer(box)
        prod.init(0)                         # epoch 1 again, different run
        prod.publish(make_result(1))
        prod.publish(make_result(2))
        # the old reader state says epoch 1 as well, but its CONSUMER_EPOCH word is 0 after the
        # reload: it must attach again and read the new run from its start (no silent skip)
        evs = rd.poll()
        self.assertIn("reattach", [e["type"] for e in evs])
        self.assertEqual([r["result_index"] for r in records(evs)], [1, 2])
        self.assertEqual(rd.stats["errors"], 0)

    def test_reconfiguration_new_reader(self):
        box = FakeMailbox()
        prod = FakeProducer(box)
        prod.init(0)
        prod.publish(make_result(1))
        box.reconfigure()
        prod = FakeProducer(box)
        prod.init(0)
        for n in range(1, 4):
            prod.publish(make_result(n))
        rd = MailboxReader(box.ps)
        self.assertEqual([r["result_index"] for r in records(rd.poll())], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
