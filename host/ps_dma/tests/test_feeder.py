"""DmaFeeder against the register model: programming order, buffer contents, errors stop everything, no retry."""

import unittest

import numpy as np

from ps_dma import registers as R
from ps_dma.dataset import Segment
from ps_dma.feeder import DmaFeeder, FeederError, layout
from ps_dma.tests.fake_dma import DmaWindow, FakeHw, FakeMem, SrcWindow


def seg(label, n, v=1):
    f = (np.arange(n * 4, dtype=np.int32).reshape(n, 4) * v - 3 * v).astype(np.int16)
    return Segment(label, f, {})


class Clock:
    """fake time: every sleep advances it"""
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def make(size=1 << 20, phys=0x7000_0000):
    mem = FakeMem(size, phys)
    hw = FakeHw(mem)
    c = Clock()
    fd = DmaFeeder(DmaWindow(hw), SrcWindow(hw), mem, sleep=c.sleep, clock=c.now)
    return fd, hw, mem


class TestLayout(unittest.TestCase):
    def test_aligned_offsets(self):
        offs, total = layout([seg("a", 3), seg("b", 600), seg("c", 1)])
        self.assertEqual(offs, [0, 64, 64 + 4800])
        self.assertEqual(total, 64 + 4800 + 64)
        self.assertTrue(all(o % R.ADDR_ALIGN == 0 for o in offs))

    def test_bad_segments_refused(self):
        with self.assertRaises(FeederError):
            layout([Segment("x", np.zeros((3, 2), dtype=np.int16), {})])
        with self.assertRaises(FeederError):
            layout([Segment("x", np.zeros((3, 4), dtype=np.int32), {})])
        with self.assertRaises(FeederError):
            layout([Segment("x", np.zeros((0, 4), dtype=np.int16), {})])


class TestFeeder(unittest.TestCase):
    def test_frames_reach_the_stream_unchanged_in_order(self):
        fd, hw, mem = make()
        segs = [seg("a", 600, 1), seg("b", 7, -5), seg("c", 10000, 3)]
        fd.load(segs)
        fd.start(50000, 0x100)
        self.assertEqual((hw.period, hw.seq_base, hw.src_run, hw.rs), (50000, 0x100, True, True))
        fd.run()
        self.assertEqual(hw.violations, [])
        self.assertEqual(len(hw.delivered), 3)
        for d, s in zip(hw.delivered, segs):
            np.testing.assert_array_equal(d["frames"], s.frames)
        self.assertEqual(mem.flushed, 1)
        self.assertEqual(hw.delivered[1]["addr"], mem.physical_address + fd.offs[1])
        self.assertEqual(fd.counters()["frames"], 10607)
        self.assertEqual([s.label for s in fd.sent], ["a", "b", "c"])

    def test_repeat_order(self):
        fd, hw, _ = make()
        fd.load([seg("a", 10), seg("b", 20)])
        fd.start(0)
        fd.run([0, 0, 1, 1, 1])
        self.assertEqual([d["bytes"] for d in hw.delivered], [80, 80, 160, 160, 160])
        self.assertEqual(hw.violations, [])

    def test_address_above_4g(self):
        fd, hw, mem = make(phys=0x8_0000_0000)
        fd.load([seg("a", 10)])
        fd.start(0)
        fd.run()
        self.assertEqual(hw.sa_msb, 8)
        self.assertEqual(hw.violations, [])
        self.assertEqual(hw.delivered[0]["addr"], mem.physical_address)

    def test_waits_for_idle(self):
        fd, hw, _ = make()
        hw.busy_polls = 5
        fd.load([seg("a", 10), seg("b", 10)])
        fd.start(0)
        fd.run()
        self.assertEqual(len(hw.delivered), 2)
        self.assertEqual(hw.busy_left, 0)

    def test_error_stops_everything(self):
        fd, hw, _ = make()
        hw.fail_on_transfer = 1
        fd.load([seg("a", 10), seg("b", 10), seg("c", 10)])
        fd.start(0)
        with self.assertRaisesRegex(FeederError, "DMASR"):
            fd.run()
        self.assertEqual(len(hw.delivered), 1)          # no retry, nothing after the failing one
        self.assertFalse(hw.src_run)
        self.assertFalse(hw.rs)
        self.assertFalse(fd.started)

    def test_timeout_stops_everything(self):
        fd, hw, _ = make()
        fd.load([seg("a", 10)])
        fd.start(0)
        hw.hang = True
        with self.assertRaisesRegex(FeederError, "timeout"):
            fd.send(0)
        self.assertFalse(hw.src_run)
        self.assertFalse(fd.started)

    def test_not_our_design_refused_before_any_write(self):
        fd, hw, _ = make()
        hw.src_id = 0x52504C31                          # the replay page
        fd.load([seg("a", 10)])
        with self.assertRaisesRegex(FeederError, "source ID"):
            fd.start(0)
        self.assertEqual(hw.resets, 0)

    def test_send_needs_start_and_buffer_size_and_alignment(self):
        fd, _, _ = make()
        fd.load([seg("a", 10)])
        with self.assertRaises(FeederError):
            fd.send(0)
        fd, _, _ = make(size=100)
        with self.assertRaises(FeederError):
            fd.load([seg("a", 100)])
        fd, _, _ = make(phys=0x7000_0008)
        with self.assertRaises(FeederError):
            fd.load([seg("a", 1)])

    def test_stop_is_safe_in_any_state(self):
        fd, hw, _ = make()
        fd.stop()
        fd.stop()
        self.assertFalse(hw.src_run)

    def test_should_stop_ends_between_transfers(self):
        fd, hw, _ = make()
        fd.load([seg("a", 10)])
        fd.start(0)
        n = {"k": 0}

        def stop():
            n["k"] += 1
            return n["k"] > 2
        fd.run([0] * 10, stop)
        self.assertEqual(len(hw.delivered), 2)


if __name__ == "__main__":
    unittest.main()
