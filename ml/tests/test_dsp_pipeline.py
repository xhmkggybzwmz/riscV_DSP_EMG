"""Pipeline configuration: order, pass-through, save/load, reproducibility, description."""

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from emgml.dsp import DSPConfig, EMGPreprocessPipeline
from emgml.dsp.config import BandpassConfig, DCConfig, NotchConfig


class TestPipelineConfig(unittest.TestCase):
    def test_all_disabled_is_identity(self):
        cfg = DSPConfig(n_ch=2, dc=DCConfig(False), notch=NotchConfig(False), bandpass=BandpassConfig(False))
        x = np.random.default_rng(0).normal(size=(1000, 2))
        np.testing.assert_array_equal(EMGPreprocessPipeline(cfg).run_record(x).waveform, x)

    def test_order_is_fixed(self):
        cfg = DSPConfig()
        cfg.stage_order = ("notch", "dc", "bandpass")
        with self.assertRaises(ValueError):
            cfg.validate()

    def test_stage_order_applied(self):
        cfg = DSPConfig(n_ch=1, notch=NotchConfig(True, 50.0, 30.0))
        p = EMGPreprocessPipeline(cfg)
        self.assertEqual([n for n, _ in p.stages], ["dc", "notch", "bandpass"])
        x = np.random.default_rng(1).normal(size=(2000, 1))
        y = x
        for _, st in p.stages:
            st.reset()
        for _, st in p.stages:
            y = st.process(y)
        np.testing.assert_array_equal(EMGPreprocessPipeline(cfg).run_record(x).waveform, y)

    def test_invalid_parameters(self):
        for bad in (dict(bandpass=BandpassConfig(True, "iir_butter", 500.0, 1200.0)),
                    dict(bandpass=BandpassConfig(True, "fir_window", numtaps=100)),
                    dict(dc=DCConfig(True, 0.0))):
            with self.assertRaises(ValueError):
                DSPConfig(**bad).validate()

    def test_save_load_roundtrip(self):
        cfg = DSPConfig(fs=2000.0, n_ch=4, notch=NotchConfig(True, 60.0, 25.0),
                        bandpass=BandpassConfig(True, "fir_window", 10.0, 500.0, numtaps=63))
        cfg.features.zc_threshold = 0.01
        with tempfile.TemporaryDirectory() as d:
            for ext in (".json", ".toml"):
                p = Path(d) / f"dsp{ext}"
                cfg.save(p)
                back = DSPConfig.load(p)
                self.assertEqual(back.to_dict(), cfg.to_dict(), ext)
            json.loads((Path(d) / "dsp.json").read_text())

    def test_reproducible_and_described(self):
        cfg = DSPConfig(n_ch=3, notch=NotchConfig(True, 50.0, 30.0))
        x = np.random.default_rng(2).normal(size=(3000, 3))
        a = EMGPreprocessPipeline(cfg).run_record(x)
        b = EMGPreprocessPipeline(DSPConfig.from_dict(cfg.to_dict())).run_record(x)
        np.testing.assert_array_equal(a.waveform, b.waveform)
        np.testing.assert_array_equal(a.features, b.features)
        d = EMGPreprocessPipeline(cfg).describe()
        self.assertEqual(set(d["stages"]), {"dc", "notch", "bandpass"})
        self.assertIn("sos", d["stages"]["bandpass"])
        json.dumps(d)                                        # serialisable for the reference export


if __name__ == "__main__":
    unittest.main()
