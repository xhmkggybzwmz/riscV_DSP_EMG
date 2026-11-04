"""Checks against the downloaded GRABMyo G0 files (skipped when the data are not present)."""

import csv
import hashlib
import unittest
from pathlib import Path

import numpy as np

from emgml.datasets import grabmyo
from emgml.datasets.wfdb_reader import read_digital, to_physical
from emgml.labels import GRABMYO_TO_UNIFIED

REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / "data" / "public" / "grabmyo"
RAW = ROOT / "raw"
HAVE = (ROOT / "manifest_G0.csv").exists()


@unittest.skipUnless(HAVE, "GRABMyo G0 not downloaded")
class TestGrabmyoG0(unittest.TestCase):
    def test_manifest_complete_and_hashes(self):
        with (ROOT / "manifest_G0.csv").open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        dat = [r for r in rows if r["kind"] == "dat"]
        self.assertEqual(len(dat), 378)
        self.assertEqual(sum(r["kind"] == "hea" for r in rows), 378)
        self.assertTrue(all(r["status"] in ("verified", "reference_checksums") for r in rows))
        # re-hash a sample of files against the official SHA256SUMS entries
        for r in dat[::47]:
            self.assertEqual(hashlib.sha256((RAW / r["relpath"]).read_bytes()).hexdigest(), r["sha256_expected"])
        self.assertLessEqual(sum(int(r["listed_bytes"]) for r in rows), 300_000_000)

    def test_header_and_data(self):
        h, d = read_digital(RAW / (grabmyo.record_relpath(2, 3, 4, 5) + ".hea"))
        self.assertEqual((h.n_sig, h.fs, h.n_samples), (32, 2048.0, 10240))
        self.assertEqual(d.shape, (10240, 32))
        names = [s.description for s in h.signals]
        self.assertEqual(names[:16], [f"F{k}" for k in range(1, 17)])
        self.assertEqual(names[16], "U1")
        self.assertTrue(all(s.units == "mV" for s in h.signals))
        phys = to_physical(h, d)
        rms = np.sqrt(np.mean(phys[:, :16] ** 2, axis=0))
        self.assertTrue(np.all((rms > 1e-3) & (rms < 1.0)), rms)     # tens of uV .. sub-mV: plausible sEMG

    def test_bipolar_is_difference_of_named_channels(self):
        rel = grabmyo.record_relpath(1, 2, 16, 3)
        h, d = read_digital(RAW / (rel + ".hea"))
        phys = to_physical(h, d)
        r = grabmyo.load_trial(RAW, 1, 2, 16, 3, "bip4", fs_out=None)
        np.testing.assert_allclose(r.x[:, 0], phys[:, 0] - phys[:, 8])     # F1 - F9
        np.testing.assert_allclose(r.x[:, 3], phys[:, 6] - phys[:, 14])    # F7 - F15
        self.assertEqual(r.channel_names, ["F1-F9", "F3-F11", "F5-F13", "F7-F15"])

    def test_resampled_record(self):
        r = grabmyo.load_trial(RAW, 3, 1, 17, 7, "bip8")
        self.assertEqual(r.x.shape, (10000, 8))
        self.assertEqual((r.fs, r.fs_original, r.label, r.dataset_label), (2000.0, 2048.0, 0, 17))

    def test_label_map(self):
        self.assertEqual(sorted(GRABMYO_TO_UNIFIED), [4, 11, 12, 15, 16, 17])
        self.assertEqual(sorted(GRABMYO_TO_UNIFIED.values()), [0, 1, 2, 3, 4, 5])


if __name__ == "__main__":
    unittest.main()
