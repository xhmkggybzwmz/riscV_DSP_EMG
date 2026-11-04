import unittest

from spi_monitor.monitor import ID, SpiMonitor


class FakeMmio:
    def __init__(self):
        self.regs = {0: ID}
    def read(self, off):
        return self.regs.get(off, off)
    def write(self, off, value):
        self.regs[off] = value


class FakeMmioBad(FakeMmio):
    def __init__(self):
        self.regs = {0: 0}


class FakeMmioBankChange(FakeMmio):
    def __init__(self):
        super().__init__()
        self.changed = False

    def read(self, off):
        if off == 0x100 and not self.changed:
            self.regs[0x0C] = 1
            self.changed = True
        return super().read(off)


class MonitorTest(unittest.TestCase):
    def test_snapshot_and_samples(self):
        mmio = FakeMmio()
        mon = SpiMonitor(mmio)
        mon.verify()
        snap = mon.snapshot()
        self.assertEqual(snap.good_frames, 0x0C)
        self.assertEqual(mon.samples()[3], 0x10C)
        frame_index, samples = mon.frame()
        self.assertEqual(frame_index, 0x20)
        self.assertEqual(samples[3], 0x10C)
        mon.clear()
        self.assertEqual(mmio.regs[4], 1)

    def test_bad_id(self):
        with self.assertRaises(RuntimeError):
            SpiMonitor(FakeMmioBad()).verify()

    def test_frame_retries_if_bank_changes(self):
        frame_index, samples = SpiMonitor(FakeMmioBankChange()).frame()
        self.assertEqual(frame_index, 0x20)
        self.assertEqual(samples[0], 0x100)
