"""2048 -> 2000 Hz resampling: ratio, length, amplitude/frequency preservation, no cross-record mixing."""

import unittest

import numpy as np

from emgml.resample import describe, resample, resample_ratio


class TestResample(unittest.TestCase):
    def test_ratio_and_length(self):
        self.assertEqual(resample_ratio(2048.0, 2000.0), (125, 128))
        y = resample(np.zeros((10240, 4)), 2048.0, 2000.0)
        self.assertEqual(y.shape, (10000, 4))
        d = describe(2048.0, 2000.0)
        self.assertEqual((d["up"], d["down"], d["filter_taps"]), (125, 128, 2561))

    def test_sine_preserved(self):
        t = np.arange(10240) / 2048.0
        for f in (30.0, 150.0, 400.0):
            x = np.sin(2 * np.pi * f * t)[:, None]
            y = resample(x, 2048.0, 2000.0)[:, 0]
            ty = np.arange(len(y)) / 2000.0
            mid = slice(500, 9500)
            np.testing.assert_allclose(y[mid], np.sin(2 * np.pi * f * ty[mid]), atol=2e-3)

    def test_band_limits_documented(self):
        """Measured properties of the default resample_poly filter (recorded in the E1a report):
        EMG band (<= 500 Hz) passes within 0.5 %; 900 Hz ~0.93; input content between the new
        Nyquist (1000 Hz) and the old one (1024 Hz) is only ~-5.5 dB attenuated before folding.
        Acceptable for GRABMyo only because its published spectrum there is ~1e-14 mV^2/Hz."""
        t = np.arange(10240) / 2048.0

        def gain(f):
            y = resample(np.sin(2 * np.pi * f * t)[:, None], 2048.0, 2000.0)[500:9500, 0]
            return float(np.std(y) * np.sqrt(2))

        for f in (30.0, 150.0, 300.0, 500.0):
            self.assertAlmostEqual(gain(f), 1.0, delta=0.005)
        self.assertAlmostEqual(gain(900.0), 0.93, delta=0.02)
        for f in (1010.0, 1020.0):
            self.assertLess(gain(f), 0.6)

    def test_per_recording(self):
        rng = np.random.default_rng(0)
        a, b = rng.normal(size=(2048, 2)), rng.normal(size=(2048, 2))
        np.testing.assert_array_equal(resample(a, 2048.0, 2000.0), resample(a, 2048.0, 2000.0))
        self.assertFalse(np.allclose(resample(a, 2048.0, 2000.0), resample(b, 2048.0, 2000.0)))

    def test_identity(self):
        x = np.arange(12.0).reshape(6, 2)
        np.testing.assert_array_equal(resample(x, 2000.0, 2000.0), x)


if __name__ == "__main__":
    unittest.main()
