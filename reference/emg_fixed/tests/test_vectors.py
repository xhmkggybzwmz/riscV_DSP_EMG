"""Vector export: packed word layout and segment handling (synthetic model)."""

import tempfile
import unittest
from pathlib import Path

import numpy as np

from emg_fixed import vectors
from emg_fixed.dsp.fixed_dsp import sections_from_config
from emg_fixed.esi import FLAG_GAP, FLAG_SOS, SRC_REPLAY, make_frames
from emg_fixed.tests.test_fixed_dsp import cfg
from emg_fixed.tests.test_int_cnn import make_q


class TestVectors(unittest.TestCase):
    def test_pack_and_frame_word(self):
        self.assertEqual(vectors.pack([1, -1, 2], 8), 0x02FF01)
        fr = make_frames(np.array([[1, -2, 3, -4]], dtype=np.int16), SRC_REPLAY, seq0=0xABCDEF01)
        w = vectors.frame_words(fr)[0]
        self.assertEqual(w & 0xFFFF, 1)
        self.assertEqual((w >> 16) & 0xFFFF, 0xFFFE)
        self.assertEqual((w >> 128) & 0xFFFF, FLAG_SOS)
        self.assertEqual(w >> 144, 0xABCDEF01)

    def test_negative_values_twos_complement(self):
        with tempfile.TemporaryDirectory() as d:
            vectors.write_words(Path(d) / "w.hex", [-1, -128, 127, 0], 8)
            self.assertEqual((Path(d) / "w.hex").read_text().split(), ["FF", "80", "7F", "00"])
            vectors.write_words(Path(d) / "c.hex", [-131072, 131071], 18)
            self.assertEqual((Path(d) / "c.hex").read_text().split(), ["20000", "1FFFF"])
            vectors.write_words(Path(d) / "b.hex", [-51605], 32)
            self.assertEqual((Path(d) / "b.hex").read_text().split(), ["FFFF366B"])

    def test_scenario_export_with_gap(self):
        _, q = make_q(4)
        rng = np.random.default_rng(0)
        x = np.clip(rng.normal(scale=3000, size=(1500, 4)), -32768, 32767).astype(np.int16)
        fr = make_frames(x, SRC_REPLAY, gap_at=(800,), seq_jump_at={800: 5})
        secs = sections_from_config(cfg())
        with tempfile.TemporaryDirectory() as d:
            s = vectors.export_scenario(Path(d) / "sc", "sc", fr, q, secs, 4, "PRE_DSP", True, "test")
            self.assertEqual([seg["start"] for seg in s["segments"]], [0, 800])
            self.assertTrue(s["segments"][1]["starts_with_gap"])
            self.assertEqual(s["decisions"], 5 + 4)          # settle 0: 800 samples -> 399..799 ; 700 -> 399..699
            lines = (Path(d) / "sc" / "input_frames.hex").read_text().split()
            self.assertEqual(len(lines), 1500)
            self.assertEqual(int(lines[800], 16) >> 128 & 0xFFFF & FLAG_GAP, FLAG_GAP)
            self.assertEqual(int(lines[800], 16) >> 144, 805)
            self.assertEqual(len((Path(d) / "sc" / "logits.hex").read_text().split()), 9)
            self.assertEqual(s["files"]["conv1_act"]["width_bits"], 16 * 8 + 32)


if __name__ == "__main__":
    unittest.main()
