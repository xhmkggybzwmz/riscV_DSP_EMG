"""WFDB reader on synthetic records: calibration, checks, and refusal of unsupported formats."""

import tempfile
import unittest
from pathlib import Path

import numpy as np

from emgml.datasets.wfdb_reader import WFDBError, parse_header, read_digital, to_physical


def write_record(d: Path, digital: np.ndarray, gains, baselines, fmt="16", names=None, fs=2048,
                 checksum_delta=0, init_delta=0, extra_bytes=b""):
    n, c = digital.shape
    names = names or [f"C{i}" for i in range(c)]
    lines = [f"rec {c} {fs} {n}"]
    for i in range(c):
        col = digital[:, i].astype(np.int64)
        cs = (int(col.sum()) + checksum_delta) & 0xFFFF
        lines.append(f"rec.dat {fmt} {gains[i]}({baselines[i]})/mV 16 0 {int(col[0]) + init_delta} {cs} 0 {names[i]}")
    (d / "rec.hea").write_text("\n".join(lines) + "\n", encoding="ascii")
    (d / "rec.dat").write_bytes(digital.astype("<i2").tobytes() + extra_bytes)
    return d / "rec.hea"


class TestWFDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        rng = np.random.default_rng(0)
        self.dig = rng.integers(-30000, 30000, size=(500, 3)).astype(np.int16)
        self.gains = [1000.0, 96551.51987465202, 200.0]
        self.base = [0, 12891, -5]

    def tearDown(self):
        self.tmp.cleanup()

    def test_calibration_per_channel(self):
        hea = write_record(self.d, self.dig, self.gains, self.base)
        h, d = read_digital(hea)
        self.assertEqual((h.n_sig, h.fs, h.n_samples), (3, 2048.0, 500))
        np.testing.assert_array_equal(d, self.dig)
        phys = to_physical(h, d)
        for c in range(3):
            np.testing.assert_allclose(phys[:, c], (self.dig[:, c].astype(np.float64) - self.base[c]) / self.gains[c])
        self.assertEqual([s.units for s in h.signals], ["mV"] * 3)

    def test_baseline_defaults_to_adc_zero(self):
        hea = write_record(self.d, self.dig, self.gains, self.base)
        txt = hea.read_text().replace("1000.0(0)/mV 16 0", "1000.0/mV 16 7")
        hea.write_text(txt)
        h = parse_header(hea)
        self.assertEqual(h.signals[0].baseline, 7)

    def test_checksum_mismatch_raises(self):
        hea = write_record(self.d, self.dig, self.gains, self.base, checksum_delta=1)
        with self.assertRaises(WFDBError):
            read_digital(hea)

    def test_initial_value_mismatch_raises(self):
        hea = write_record(self.d, self.dig, self.gains, self.base, init_delta=1)
        with self.assertRaises(WFDBError):
            read_digital(hea)

    def test_length_mismatch_raises(self):
        hea = write_record(self.d, self.dig, self.gains, self.base, extra_bytes=b"\x00\x00")
        with self.assertRaises(WFDBError):
            read_digital(hea)

    def test_unsupported_format_raises(self):
        for fmt in ("212", "16x2", "16+24", "80"):
            hea = write_record(self.d, self.dig, self.gains, self.base, fmt=fmt)
            with self.assertRaises(WFDBError, msg=fmt):
                parse_header(hea)

    def test_zero_gain_raises(self):
        hea = write_record(self.d, self.dig, [0, 1, 1], self.base)
        with self.assertRaises(WFDBError):
            parse_header(hea)


if __name__ == "__main__":
    unittest.main()
