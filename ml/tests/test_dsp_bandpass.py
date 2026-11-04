"""Band-pass: IIR Butterworth and FIR candidate - response, stability, delay, state."""

import unittest

import numpy as np

from emgml.dsp.bandpass import Bandpass

FS = 2000.0


def steady_gain_db(filt, f, n=16000, skip=8000):
    x = np.sin(2 * np.pi * f * np.arange(n) / FS).reshape(-1, 1)
    y = filt.process(x)[skip:, 0]
    return 20 * np.log10(np.std(y) / np.std(x[skip:, 0]))


class TestIIR(unittest.TestCase):
    def setUp(self):
        self.f = Bandpass("iir_butter", 20.0, 450.0, FS, 1, order=2)

    def test_response(self):
        d = self.f.describe()
        self.assertEqual(len(d["sos"]), 2)                       # N = 2 -> 2 biquads, order 4
        self.assertLess(d["max_pole_radius"], 1.0)
        r = dict(zip(d["response"]["freq_hz"], d["response"]["mag_db"]))
        self.assertAlmostEqual(r[20.0], -3.01, delta=0.05)       # Butterworth edges at -3 dB
        self.assertAlmostEqual(r[450.0], -3.01, delta=0.05)
        self.assertGreater(r[100.0], -0.5)
        self.assertLess(r[5.0], -20.0)

    def test_measured_sines(self):
        self.assertGreater(steady_gain_db(self.f, 150.0), -0.3)
        self.f.reset()
        self.assertLess(steady_gain_db(self.f, 3.0, n=40000, skip=20000), -30.0)
        self.f.reset()
        self.assertLess(steady_gain_db(self.f, 900.0), -12.0)

    def test_chunking(self):
        x = np.random.default_rng(4).normal(size=(5000, 3))
        f = Bandpass("iir_butter", 20.0, 450.0, FS, 3, order=4)
        whole = f.process(x)
        f.reset()
        parts = np.vstack([f.process(x[a:b]) for a, b in [(0, 1), (1, 1025), (1025, 5000)]])
        np.testing.assert_allclose(parts, whole, rtol=0, atol=1e-12)


class TestFIR(unittest.TestCase):
    def test_linear_phase_and_delay(self):
        f = Bandpass("fir_window", 20.0, 450.0, FS, 1, numtaps=101)
        t = f.taps
        np.testing.assert_allclose(t, t[::-1], atol=1e-15)        # symmetric -> linear phase
        self.assertEqual(f.describe()["group_delay_samples"], 50)
        # delay check: a 150 Hz sine comes out shifted by exactly 50 samples
        n = 4000
        x = np.sin(2 * np.pi * 150 * np.arange(n) / FS).reshape(-1, 1)
        y = f.process(x)[:, 0]
        gain = np.std(y[1000:]) / np.std(x[1000:, 0])
        np.testing.assert_allclose(y[1000:3000], gain * x[950:2950, 0], atol=2e-3)

    def test_chunking(self):
        x = np.random.default_rng(5).normal(size=(3000, 2))
        f = Bandpass("fir_window", 20.0, 450.0, FS, 2, numtaps=61)
        whole = f.process(x)
        f.reset()
        parts = np.vstack([f.process(x[a:b]) for a, b in [(0, 30), (30, 31), (31, 3000)]])
        np.testing.assert_allclose(parts, whole, rtol=0, atol=1e-12)


if __name__ == "__main__":
    unittest.main()
