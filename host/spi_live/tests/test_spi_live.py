import math
import unittest

from spi_live.capture import BLOCK, Recorder
from spi_live.capture_uart import Parser, crc16
from spi_live.level import ActivityMeter, rms_from_energy
from spi_live.source import O_ENERGY, O_LEVEL_SEQ, LevelReader


class FakeWindow:
    def __init__(self):
        self.regs = {O_ENERGY: 0, O_LEVEL_SEQ: 0}

    def read32(self, o):
        return self.regs[o]


class TestLevel(unittest.TestCase):
    def test_rms_inverts_the_hardware_counter(self):
        y = 1000
        energy = (128 * y * y) >> 6
        self.assertAlmostEqual(rms_from_energy(energy), y, places=6)

    def test_calibrated_range_and_clipping(self):
        m = ActivityMeter(100.0, 1100.0, alpha=1.0)
        e = lambda r: int(r * r * 2)
        self.assertEqual(m.update(e(50)), 0.0)
        self.assertAlmostEqual(m.update(e(600)), 0.5, places=2)
        self.assertEqual(m.update(e(5000)), 1.0)

    def test_smoothing_moves_part_of_the_way(self):
        m = ActivityMeter(0.0, 100.0, alpha=0.5)
        m.update(0)
        self.assertAlmostEqual(m.update(int(100 * 100 * 2)), 0.5, places=6)

    def test_uncalibrated_tracks_what_it_has_seen(self):
        m = ActivityMeter(alpha=1.0)
        self.assertFalse(m.calibrated)
        self.assertEqual(m.update(200), 0.0)
        self.assertEqual(m.update(20000), 1.0)
        self.assertLess(m.update(5000), 1.0)

    def test_rejects_half_a_calibration(self):
        with self.assertRaises(ValueError):
            ActivityMeter(1.0, None)
        with self.assertRaises(ValueError):
            ActivityMeter(5.0, 5.0)


class TestReader(unittest.TestCase):
    def test_one_message_per_new_value(self):
        w = FakeWindow()
        r = LevelReader(w, ActivityMeter(0.0, 1000.0, alpha=1.0))
        w.regs[O_LEVEL_SEQ], w.regs[O_ENERGY] = 1, 2 * 500 * 500
        m = r.poll()
        self.assertEqual((m["type"], m["level_seq"]), ("level", 1))
        self.assertAlmostEqual(m["intensity"], 0.5, places=3)
        self.assertTrue(m["calibrated"])
        self.assertIsNone(r.poll())
        w.regs[O_LEVEL_SEQ] = 2
        self.assertEqual(r.poll()["level_seq"], 2)


class TestRecorder(unittest.TestCase):
    def test_lost_blocks_are_counted_and_excluded(self):
        r = Recorder()
        s = [2100] * BLOCK
        r.add(0, s, 1, 0)
        r.add(3 * BLOCK, s, 1, 0)
        a = r.arrays()
        self.assertEqual(r.lost, 2)
        self.assertEqual(len(a["u16"]), 4 * BLOCK)
        self.assertEqual(int((a["label"] == -1).sum()), 2 * BLOCK)
        self.assertEqual(int((a["label"] == 1).sum()), 2 * BLOCK)

    def test_index_wraps(self):
        r = Recorder()
        s = [0] * BLOCK
        r.add(0xFFFF_FF80, s, 0, 0)
        r.add(0, s, 0, 0)
        self.assertEqual(r.lost, 0)

    def test_restart_of_the_stm32_is_an_error(self):
        r = Recorder()
        s = [0] * BLOCK
        r.add(1000 * BLOCK, s, 0, 0)
        with self.assertRaises(RuntimeError):
            r.add(0, s, 0, 0)


class TestUartParser(unittest.TestCase):
    @staticmethod
    def frame(index, value=2000, rate=2000):
        body = b"EMG1" + bytes([1, 1, 1, 0]) + rate.to_bytes(4, "little") + index.to_bytes(4, "little") + bytes([0x80, 0, 0, 1])
        body += b"".join((value + k).to_bytes(2, "little") for k in range(BLOCK))
        return body + crc16(body).to_bytes(2, "little")

    def test_stream_with_garbage_and_split_reads(self):
        p = Parser()
        data = b"xx" + self.frame(0) + self.frame(128)
        got = p.feed(data[:100]) + p.feed(data[100:])
        self.assertEqual([(g[0], g[1]) for g in got], [(0, 2000), (128, 2000)])
        self.assertEqual(got[1][2][5], 2005)
        self.assertEqual(p.skipped, 2)

    def test_corrupt_frame_is_rejected_and_the_next_one_found(self):
        p = Parser()
        bad = bytearray(self.frame(0))
        bad[50] ^= 1
        got = p.feed(bytes(bad) + self.frame(128))
        self.assertEqual([g[0] for g in got], [128])
        self.assertEqual(p.bad_crc, 1)

    def test_wrong_header_is_counted(self):
        p = Parser()
        f = bytearray(self.frame(0))
        f[6] = 2
        f[276:278] = crc16(bytes(f[:276])).to_bytes(2, "little")
        self.assertEqual(p.feed(bytes(f)), [])
        self.assertEqual(p.bad_format, 1)


if __name__ == "__main__":
    unittest.main()
