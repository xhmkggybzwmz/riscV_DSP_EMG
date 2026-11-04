"""EMG1 capture loader on a synthetic packet stream (same format as emg_host.py writes)."""

import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np

from emgml.datasets.emg1_capture import _emg_inspect, load_capture


def packet(index, samples, fs=4000, fmt=0x01):
    ch = samples.shape[1]
    payload = samples.astype("<u2").tobytes()
    head = b"EMG1" + struct.pack("<BBBBIIHH", 1, fmt, ch, 0, fs, index, samples.shape[0], len(payload))
    crc = _emg_inspect().crc16_ccitt_false(head + payload)
    return head + payload + crc.to_bytes(2, "little")


class TestEMG1Capture(unittest.TestCase):
    def test_runs_split_at_discontinuity(self):
        rng = np.random.default_rng(0)
        blocks = [rng.integers(0, 4096, size=(8, 2)) for _ in range(5)]
        idx = [0, 8, 16, 40, 48]                      # gap between 16+8=24 and 40
        data = b"".join(packet(i, b) for i, b in zip(idx, blocks))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cap.bin"
            p.write_bytes(data)
            recs = load_capture(p)
        self.assertEqual([r.n_samples for r in recs], [24, 16])
        np.testing.assert_array_equal(recs[0].x, np.vstack(blocks[:3]))
        np.testing.assert_array_equal(recs[1].x, np.vstack(blocks[3:]))
        self.assertEqual(recs[1].extra["first_sample_index"], 40)
        self.assertEqual((recs[0].fs, recs[0].units, recs[0].label, recs[0].n_ch), (4000.0, "ADC_LSB", -1, 2))

    def test_crc_matches_reference_value(self):
        # CRC-16/CCITT-FALSE check value of "123456789" is 0x29B1
        self.assertEqual(_emg_inspect().crc16_ccitt_false(b"123456789"), 0x29B1)


if __name__ == "__main__":
    unittest.main()
