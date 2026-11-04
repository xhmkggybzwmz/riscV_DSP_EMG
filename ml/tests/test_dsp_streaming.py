"""Stream semantics of the whole pipeline: chunking, SOS, GAP, reconfigure."""

import unittest

import numpy as np

from emgml.dsp import DSPConfig, EMGPreprocessPipeline
from emgml.dsp.config import BandpassConfig, DCConfig, FeatureConfig, NotchConfig
from emgml.dsp.features import window_features

CHUNKS = [100, 37, 1024, 1, 4096, 4742]          # sums to 10000 (the requested pattern)


def make_cfg(kind="iir_butter"):
    return DSPConfig(fs=2000.0, n_ch=4, dc=DCConfig(True, 5.0), notch=NotchConfig(True, 50.0, 30.0),
                     bandpass=BandpassConfig(True, kind, 20.0, 450.0, 2, 101),
                     features=FeatureConfig(400, 100, 200, "continuous", zc_threshold=0.05, ssc_threshold=0.001))


def signal_10000(seed=0):
    rng = np.random.default_rng(seed)
    t = np.arange(10000) / 2000.0
    x = rng.normal(scale=0.3, size=(10000, 4))
    x += 0.5 * np.sin(2 * np.pi * 50 * t)[:, None] + np.array([1.0, -2.0, 0.0, 3.0])
    return x


def run_chunks(p, x, chunks, sos_first=True):
    outs, pos = [], 0
    for i, n in enumerate(chunks):
        outs.append(p.process(x[pos:pos + n], sos=(sos_first and i == 0)))
        pos += n
    wave = np.vstack([o.waveform for o in outs])
    ends = np.concatenate([o.window_end for o in outs])
    feats = np.concatenate([o.features for o in outs if len(o.window_end)], axis=0)
    return wave, ends, feats


class TestChunking(unittest.TestCase):
    def check(self, kind):
        self.assertEqual(sum(CHUNKS), 10000)
        x = signal_10000()
        a = EMGPreprocessPipeline(make_cfg(kind)).run_record(x)
        w, e, f = run_chunks(EMGPreprocessPipeline(make_cfg(kind)), x, CHUNKS)
        np.testing.assert_allclose(w, a.waveform, rtol=0, atol=1e-10)
        np.testing.assert_array_equal(e, a.window_end)
        np.testing.assert_allclose(f[:, :, :3], a.features[:, :, :3], rtol=1e-12, atol=1e-12)
        np.testing.assert_array_equal(f[:, :, 3:], a.features[:, :, 3:])     # ZC/SSC counts exact
        # streaming features == reference definition applied to the whole waveform
        for t, ft in zip(a.window_end, a.features):
            ref = window_features(a.waveform[t - 401:t + 1], 400, "continuous", 0.05, 0.001)
            # float features: same formula, summation order may differ in the last bits
            np.testing.assert_allclose(ft[:, :3], ref[:, :3], rtol=1e-12, atol=1e-12)
            np.testing.assert_array_equal(ft[:, 3:], ref[:, 3:])

    def test_iir(self):
        self.check("iir_butter")

    def test_fir(self):
        self.check("fir_window")


class TestEvents(unittest.TestCase):
    def test_sos_clears_all_state(self):
        x = signal_10000(1)
        p = EMGPreprocessPipeline(make_cfg())
        p.process(x[:3000], sos=True)
        b = p.process(x[3000:], sos=True)
        fresh = EMGPreprocessPipeline(make_cfg()).run_record(x[3000:])
        np.testing.assert_array_equal(b.waveform, fresh.waveform)
        np.testing.assert_array_equal(b.window_end, fresh.window_end)     # schedule restarts at SOS
        np.testing.assert_array_equal(b.features, fresh.features)

    def test_no_event_keeps_state(self):
        x = signal_10000(2)
        p = EMGPreprocessPipeline(make_cfg())
        p.process(x[:3000], sos=True)
        b = p.process(x[3000:])
        fresh = EMGPreprocessPipeline(make_cfg()).run_record(x[3000:])
        self.assertGreater(np.max(np.abs(b.waveform[:50] - fresh.waveform[:50])), 1e-3)
        self.assertEqual(b.window_end[0] % 100, (599 + 0) % 100)

    def test_gap_is_reset(self):
        x = signal_10000(3)
        p, q = EMGPreprocessPipeline(make_cfg()), EMGPreprocessPipeline(make_cfg())
        p.process(x[:2000], sos=True)
        q.process(x[:2000], sos=True)
        a = p.process(x[2500:], gap=True)
        b = q.process(x[2500:], sos=True)
        np.testing.assert_array_equal(a.waveform, b.waveform)
        np.testing.assert_array_equal(a.features, b.features)

    def test_no_window_spans_a_reset(self):
        x = signal_10000(4)
        p = EMGPreprocessPipeline(make_cfg())
        o1 = p.process(x[:650], sos=True)                 # windows end at 599 only
        o2 = p.process(x[650:1100], sos=True)             # 450 samples < first end 599: no window
        self.assertEqual(list(o1.window_end), [599])
        self.assertEqual(len(o2.window_end), 0)

    def test_reconfigure_resets(self):
        x = signal_10000(5)
        p = EMGPreprocessPipeline(make_cfg())
        p.process(x[:5000], sos=True)
        cfg2 = make_cfg()
        cfg2.notch.f0_hz = 60.0
        p.reconfigure(cfg2)
        a = p.process(x[5000:])
        b = EMGPreprocessPipeline(cfg2).run_record(x[5000:])
        np.testing.assert_array_equal(a.waveform, b.waveform)


if __name__ == "__main__":
    unittest.main()
