"""Notch: coefficients, attenuation at 50/60 Hz, effect on nearby frequencies, state."""

import unittest

import numpy as np
from scipy import signal

from emgml.dsp.notch import Notch, notch_sos

FS = 2000.0


def steady_gain_db(filt, f, n=20000, skip=10000):
    x = np.sin(2 * np.pi * f * np.arange(n) / FS).reshape(-1, 1)
    y = filt.process(x)[skip:, 0]
    return 20 * np.log10(np.std(y) / np.std(x[skip:, 0]))


class TestNotch(unittest.TestCase):
    def test_coefficients_match_iirnotch(self):
        for f0 in (50.0, 60.0):
            b, a = signal.iirnotch(f0, 30.0, fs=FS)
            np.testing.assert_allclose(notch_sos(f0, 30.0, FS)[0], np.r_[b, a] / a[0])

    def test_attenuation_at_f0(self):
        for f0 in (50.0, 60.0):
            self.assertLess(steady_gain_db(Notch(f0, 30.0, FS, 1), f0), -40.0)

    def test_nearby_frequencies(self):
        n = Notch(50.0, 30.0, FS, 1)
        d = n.describe()
        self.assertAlmostEqual(d["bandwidth_hz"], 50.0 / 30.0)
        for f, max_loss in ((40.0, 0.3), (60.0, 0.3), (100.0, 0.05), (300.0, 0.05)):
            n.reset()
            self.assertGreater(steady_gain_db(n, f), -max_loss, f"{f} Hz")
        self.assertLess(d["max_pole_radius"], 1.0)

    def test_q_changes_bandwidth(self):
        wide, narrow = Notch(50.0, 5.0, FS, 1), Notch(50.0, 60.0, FS, 1)
        self.assertLess(steady_gain_db(wide, 45.0), steady_gain_db(narrow, 45.0))

    def test_multichannel_chunking_reset(self):
        x = np.random.default_rng(3).normal(size=(6000, 4))
        f = Notch(60.0, 30.0, FS, 4)
        whole = f.process(x)
        f.reset()
        parts = np.vstack([f.process(x[a:b]) for a, b in [(0, 100), (100, 137), (137, 4000), (4000, 6000)]])
        np.testing.assert_allclose(parts, whole, rtol=0, atol=1e-12)
        g = Notch(60.0, 30.0, FS, 1)
        np.testing.assert_allclose(whole[:, 2], g.process(x[:, [2]])[:, 0], rtol=0, atol=1e-12)


if __name__ == "__main__":
    unittest.main()
