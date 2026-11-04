"""The numpy-only conversion on the PS against the PC pipeline (scipy, ml/, the frames compiled into mbx_mg6_rt).
The tests on real data need data/public/grabmyo/raw (not in the repository) and skip without it."""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

from ps_dma import dataset as D

REPO = Path(__file__).resolve().parents[3]
RAW = REPO / "data" / "public" / "grabmyo" / "raw"
FIX = REPO / "host" / "gesture_server" / "tests" / "fixtures" / "e4c_mg6_expected.json"
PROV = REPO / "build" / "e4c" / "fw" / "mg6_data.json"
HAVE_DATA = (RAW / "Session3").is_dir()


def write_record(root: Path, session, participant, gesture, trial, digital: np.ndarray, names, gain=50000.0, base=10):
    rel = D.relpath(session, participant, gesture, trial)
    hea = root / (rel + ".hea")
    hea.parent.mkdir(parents=True, exist_ok=True)
    dat = hea.with_suffix(".dat")
    dat.write_bytes(digital.astype("<i2").tobytes())
    lines = [f"{hea.stem} {digital.shape[1]} 2048 {digital.shape[0]}"]
    for c, n in enumerate(names):
        csum = int(digital[:, c].astype(np.int64).sum()) & 0xFFFF
        lines.append(f"{dat.name} 16 {gain}({base})/mV 16 0 {int(digital[0, c])} {csum} 0 {n}")
    hea.write_text("\n".join(lines) + "\n", encoding="ascii")


class TestResample(unittest.TestCase):
    def test_equals_scipy_resample_poly(self):
        from scipy import signal
        rng = np.random.default_rng(3)
        x = rng.normal(size=(10240, 4)) * 0.05
        y = D.resample_poly(x, 2000, 2048)
        z = signal.resample_poly(x, 125, 128, axis=0, window=("kaiser", D.KAISER_BETA))
        self.assertEqual(y.shape, (10000, 4))
        self.assertEqual(y.shape, z.shape)
        self.assertLess(np.max(np.abs(y - z)), 1e-12)

    def test_short_and_other_ratios(self):
        from scipy import signal
        x = np.random.default_rng(4).normal(size=(333, 2))
        for up, down in ((125, 128), (1, 2), (3, 2)):
            z = signal.resample_poly(x, up, down, axis=0, window=("kaiser", D.KAISER_BETA))
            self.assertLess(np.max(np.abs(D.resample_poly(x, up, down) - z)), 1e-12, (up, down))


class TestCodes(unittest.TestCase):
    def test_rounding_and_saturation(self):
        q, sat = D.to_codes(np.array([[0.5, 1.5, 2.5, -0.5], [40000.0, -40000.0, 1.0, 0.0]]), 1.0)
        self.assertEqual(q.dtype, np.int16)
        self.assertEqual(q.tolist(), [[0, 2, 2, 0], [32767, -32768, 1, 0]])       # half to even, as reference/esi.py
        self.assertEqual(sat, 2)


class TestLoader(unittest.TestCase):
    NAMES = [f"F{k}" for k in range(1, 17)]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.d = np.random.default_rng(5).integers(-3000, 3000, size=(10240, 16), dtype=np.int16)
        write_record(self.root, 3, 1, 17, 5, self.d, self.NAMES)
        self.cfg = D.load_config()

    def tearDown(self):
        self.tmp.cleanup()

    def test_channels_by_name_not_position(self):
        x = D.load_record(self.root, 3, 1, 17, 5, self.cfg)
        self.assertEqual(x.shape, (10000, 4))
        # same record with the columns reversed: same result
        names = list(reversed(self.NAMES))
        write_record(self.root, 3, 1, 16, 1, self.d[:, ::-1], names)
        np.testing.assert_array_equal(D.load_record(self.root, 3, 1, 16, 1, self.cfg), x)

    def test_segment_range_checked(self):
        s = D.load_segment(self.root, 3, 1, 17, 5, self.cfg, 100, 600, "REST")
        self.assertEqual(s.frames.shape, (600, 4))
        self.assertEqual(s.frames.dtype, np.int16)
        self.assertEqual(s.nbytes, 4800)
        with self.assertRaises(D.DatasetError):
            D.load_segment(self.root, 3, 1, 17, 5, self.cfg, 9900, 600)

    def test_corrupt_file_refused(self):
        from ps_dma.wfdb_reader import WFDBError
        dat = self.root / (D.relpath(3, 1, 17, 5) + ".dat")
        raw = bytearray(dat.read_bytes())
        raw[100] ^= 1
        dat.write_bytes(bytes(raw))
        with self.assertRaises(WFDBError):
            D.load_record(self.root, 3, 1, 17, 5, self.cfg)

    def test_reader_is_the_ml_reader(self):
        a = (REPO / "host" / "ps_dma" / "wfdb_reader.py").read_bytes().replace(b"\r\n", b"\n")
        b = (REPO / "ml" / "emgml" / "datasets" / "wfdb_reader.py").read_bytes().replace(b"\r\n", b"\n")
        self.assertEqual(a, b, "host/ps_dma/wfdb_reader.py must stay a copy of the ml reader")


@unittest.skipUnless(HAVE_DATA, "GRABMyo raw data not present")
class TestRealData(unittest.TestCase):
    def setUp(self):
        self.cfg = D.load_config()

    def test_mg6_frames_equal_the_e4c_frame_table(self):
        segs = D.mg6_segments(RAW, FIX, self.cfg)
        self.assertEqual([s.frames.shape for s in segs], [(600, 4)] * 6)
        self.assertEqual(D.frames_sha256(segs), self.cfg["mg6_frames_le_sha256"])

    def test_whole_test_session_equals_the_pc_pipeline(self):
        sys.path[:0] = [str(REPO / "ml"), str(REPO / "reference")]
        from emg_fixed.esi import SourceCalibration, to_esi
        from emgml.datasets import grabmyo
        cal = SourceCalibration("t", "PRE_DSP", "mV", self.cfg["esi_lsb_mV"], 4, ["a", "b", "c", "d"], rule="x",
                                calibration_records=[])
        for g in (17, 16, 15, 12, 11, 4):
            for t in (1, 4, 7):
                pc = grabmyo.load_trial(RAW, 3, 1, g, t, "bip4", 2000.0)
                x = D.load_record(RAW, 3, 1, g, t, self.cfg)
                self.assertLess(np.max(np.abs(x - pc.x)), 1e-12, (g, t))
                np.testing.assert_array_equal(D.to_codes(x, self.cfg["esi_lsb_mV"])[0], to_esi(pc.x, cal)[0])


@unittest.skipUnless(PROV.is_file(), "build/e4c not generated")
class TestProvenance(unittest.TestCase):
    def test_config_equals_the_e4c_provenance(self):
        p = json.loads(PROV.read_text(encoding="utf-8"))
        c = D.load_config()
        self.assertEqual(c["esi_lsb_mV"], p["esi_lsb_mV"])
        self.assertEqual(c["mg6_frames_le_sha256"], p["frame_words_le_sha256"])
        self.assertEqual(hashlib.sha256(json.dumps(c["channels"]).encode()).hexdigest()[:8],
                         hashlib.sha256(json.dumps([["F1", "F9"], ["F3", "F11"], ["F5", "F13"], ["F7", "F15"]]).encode()).hexdigest()[:8])


if __name__ == "__main__":
    unittest.main()
