"""Long and real-time runs of the reader on the software mailbox (Python model only)."""

import random
import statistics
import threading
import time
import tracemalloc
import unittest

from ps_mailbox.fake_mmio import FakeMailbox, FakeProducer, make_result
from ps_mailbox.pynq_reader import MailboxReader


class TestSoak(unittest.TestCase):
    def test_one_hour_of_20hz_bounded_memory(self):
        """72000 records (1 h at 20 Hz, not in real time): memory of the reader stays bounded"""
        box = FakeMailbox()
        prod = FakeProducer(box)
        prod.init(0)
        rd = MailboxReader(box.ps)
        tracemalloc.start()
        base = None
        n_rec = 0
        for n in range(1, 72001):
            prod.publish(make_result(n))
            n_rec += sum(e["type"] == "record" for e in rd.poll())
            if n == 2000:
                base = tracemalloc.take_snapshot()
        grown = sum(s.size_diff for s in tracemalloc.take_snapshot().compare_to(base, "filename"))
        tracemalloc.stop()
        self.assertEqual(n_rec, 72000)
        self.assertEqual(rd.stats["errors"], 0)
        self.assertEqual(rd.stats["gaps"], 0)
        self.assertLess(grown, 256 * 1024, f"reader memory grew by {grown} bytes")

    def test_random_consumer_speed(self):
        rng = random.Random(5)
        box = FakeMailbox()
        prod = FakeProducer(box)
        prod.init(0)
        rd = MailboxReader(box.ps)
        last = None
        for n in range(1, 10001):
            prod.publish(make_result(n))
            if rng.random() < 0.5:                    # on average 0.6 records per published one
                for e in rd.poll(max_records=rng.choice([1, 1, 1, 2, 1])):
                    if e["type"] == "record":
                        last = e["result_index"]
        for e in rd.poll():
            if e["type"] == "record":
                last = e["result_index"]
        self.assertEqual(rd.stats["errors"], 0)
        self.assertEqual(rd.stats["records"], prod.published)
        self.assertEqual(rd.stats["gaps"] + 10000 - last, prod.drops)
        self.assertGreater(prod.drops, 0)

    def test_realtime_20hz(self):
        """producer thread at 20 Hz, reader polling every 5 ms, 3 s"""
        box = FakeMailbox()
        prod = FakeProducer(box)
        prod.init(0)
        stop = threading.Event()

        def producer():
            t_next = time.monotonic()
            k = 0
            while not stop.is_set():
                t_next += 0.05
                time.sleep(max(0.0, t_next - time.monotonic()))
                k += 1
                prod.publish(make_result(k))

        th = threading.Thread(target=producer, daemon=True)
        rd = MailboxReader(box.ps)
        th.start()
        t_end = time.monotonic() + 3.0
        while time.monotonic() < t_end:
            rd.poll()
            time.sleep(0.005)
        stop.set()
        th.join()
        rd.poll()
        iv = [x / 1e6 for x in rd.intervals_ns]
        self.assertEqual(rd.stats["errors"], 0)
        self.assertEqual(rd.stats["records"], prod.published)
        self.assertEqual(prod.drops, 0)
        self.assertGreaterEqual(rd.stats["records"], 55)
        self.assertAlmostEqual(statistics.mean(iv[1:-1]), 50.0, delta=3.0)
        print(f"\n  20 Hz model run: {rd.stats['records']} records, interval mean {statistics.mean(iv):.2f} ms "
              f"min {min(iv):.2f} max {max(iv):.2f} ms, poll max {max(rd.poll_ns) / 1e6:.3f} ms")


if __name__ == "__main__":
    unittest.main()
