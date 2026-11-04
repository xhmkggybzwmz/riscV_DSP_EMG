"""DC blocker: coefficients, DC rejection, step, sine + DC, channels, reset, chunking."""

import math
import unittest

import numpy as np

from emgml.dsp.dc_remove import DCBlocker, dc_blocker_sos

FS = 2000.0


def sine(f, n, amp=1.0, fs=FS):
    return amp * np.sin(2 * np.pi * f * np.arange(n) / fs)


class TestDCBlocker(unittest.TestCase):
    def test_coefficients(self):
        sos = dc_blocker_sos(5.0, FS)
        r = math.exp(-2 * math.pi * 5.0 / FS)
        np.testing.assert_allclose(sos, [[(1 + r) / 2, -(1 + r) / 2, 0, 1, -r, 0]])
        # gain exactly 1 at fs/2 (z = -1): g*2 / (1 + r)
        self.assertAlmostEqual(sos[0, 0] * 2 / (1 + r), 1.0)

    def test_constant_positive_and_negative_dc(self):
        for c in (3.0, -7.5):
            f = DCBlocker(5.0, FS, 1)
            y = f.process(np.full((4000, 1), c))
            self.assertAlmostEqual(y[0, 0], c * (1 + math.exp(-2 * math.pi * 5 / FS)) / 2)
            self.assertLess(abs(y[-1, 0]), 1e-9 * abs(c))       # r^4000 ~ e^-63

    def test_step(self):
        x = np.r_[np.zeros(100), np.ones(3000)].reshape(-1, 1)
        y = DCBlocker(5.0, FS, 1).process(x)
        self.assertTrue(np.all(y[:100] == 0))
        self.assertGreater(y[100, 0], 0.99)
        self.assertTrue(np.all(np.diff(y[100:, 0]) <= 0))         # monotonic decay after the step
        self.assertLess(abs(y[-1, 0]), 1e-6)

    def test_sine_plus_dc(self):
        n = 8000
        x = (sine(100, n) + 2.0).reshape(-1, 1)
        y = DCBlocker(5.0, FS, 1).process(x)[4000:, 0]
        self.assertLess(abs(np.mean(y)), 1e-3)
        self.assertAlmostEqual(np.sqrt(2) * np.std(y), 1.0, delta=0.005)   # |H(100 Hz)| ~ 0.999

    def test_channels_independent(self):
        rng = np.random.default_rng(0)
        x = np.c_[np.full(1000, 5.0), sine(80, 1000), rng.normal(size=1000)]
        y = DCBlocker(5.0, FS, 3).process(x)
        for c in range(3):
            np.testing.assert_allclose(y[:, c], DCBlocker(5.0, FS, 1).process(x[:, [c]])[:, 0], rtol=0, atol=1e-12)

    def test_reset_and_chunking(self):
        x = np.random.default_rng(1).normal(size=(5000, 2)) + 1.0
        f = DCBlocker(5.0, FS, 2)
        whole = f.process(x)
        f.reset()
        parts = np.vstack([f.process(x[a:b]) for a, b in [(0, 1), (1, 777), (777, 2048), (2048, 5000)]])
        np.testing.assert_allclose(parts, whole, rtol=0, atol=1e-12)
        f.reset()
        np.testing.assert_array_equal(f.process(x), whole)


if __name__ == "__main__":
    unittest.main()
