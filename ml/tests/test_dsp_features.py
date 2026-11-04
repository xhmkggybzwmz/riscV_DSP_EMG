"""MAV/RMS/WL/ZC/SSC against hand-computed answers (definitions in emgml/dsp/features.py)."""

import math
import unittest

import numpy as np

from emgml.dsp.features import StreamingFeatures, window_features

F = ("MAV", "RMS", "WL", "ZC", "SSC")


def col(v):
    return np.asarray(v, dtype=np.float64).reshape(-1, 1)


class TestIndependentWindow(unittest.TestCase):
    # window x = [1, -2, 3, 3, -1], N = 5
    #   MAV = (1+2+3+3+1)/5 = 2.0
    #   RMS = sqrt((1+4+9+9+1)/5) = sqrt(4.8)
    #   d = [-3, 5, 0, -4]                    -> WL = 12
    #   ZC pairs (1,-2) (-2,3) (3,-1) cross   -> ZC = 3 (T=0); |d| = 3, 5, 4
    #   SSC centres -2: (-3)(-5)=15>0 ; 3: (5)(0)=0 ; 3: (0)(4)=0 -> SSC = 1
    x = col([1, -2, 3, 3, -1])

    def test_values(self):
        v = window_features(self.x, 5, "independent", 0.0, 0.0, F)[0]
        self.assertAlmostEqual(v[0], 2.0)
        self.assertAlmostEqual(v[1], math.sqrt(4.8))
        self.assertAlmostEqual(v[2], 12.0)
        self.assertEqual(v[3], 3)
        self.assertEqual(v[4], 1)

    def test_zc_threshold_equality_counts(self):
        # T_zc = 4: |d| = 3 (no), 5 (yes), 4 (yes: equality counts)
        self.assertEqual(window_features(self.x, 5, "independent", 4.0, 0.0, F)[0, 3], 2)
        self.assertEqual(window_features(self.x, 5, "independent", 4.0001, 0.0, F)[0, 3], 1)

    def test_ssc_threshold_equality_counts(self):
        self.assertEqual(window_features(self.x, 5, "independent", 0.0, 15.0, F)[0, 4], 1)
        self.assertEqual(window_features(self.x, 5, "independent", 0.0, 15.0001, F)[0, 4], 0)

    def test_zero_is_not_a_crossing(self):
        # pairs (0,1): product 0 -> no ; (1,-1) -> yes
        self.assertEqual(window_features(col([0, 1, -1]), 3, "independent", 0.0, 0.0, F)[0, 3], 1)
        # (0,-1) and (-1,0): never; (1,0),(0,-1): never
        self.assertEqual(window_features(col([1, 0, -1]), 3, "independent", 0.0, 0.0, F)[0, 3], 0)

    def test_flat_is_not_a_slope_change(self):
        self.assertEqual(window_features(col([2, 2, 2, 2]), 4, "independent", 0.0, 0.0, F)[0, 4], 0)

    def test_independent_ignores_history(self):
        seg = np.vstack([col([100, -100]), self.x])
        a = window_features(seg, 5, "independent", 0.0, 0.0, F)
        b = window_features(self.x, 5, "independent", 0.0, 0.0, F)
        np.testing.assert_array_equal(a, b)


class TestContinuousWindow(unittest.TestCase):
    # history x[t-N-1] = 0, x[t-N] = -1, window [1, -2, 3, 3, -1]
    #   WL : pairs from (-1,1) on: 2 + 12 = 14
    #   ZC : (-1,1) also crosses -> 4
    #   SSC: centres -1: (-1-0)(-1-1)=2 ; 1: (2)(3)=6 ; -2: 15 ; 3: 0 ; 3: 0 -> 3
    #   MAV/RMS only use the window
    seg = col([0, -1, 1, -2, 3, 3, -1])

    def test_values(self):
        v = window_features(self.seg, 5, "continuous", 0.0, 0.0, F)[0]
        self.assertAlmostEqual(v[0], 2.0)
        self.assertAlmostEqual(v[1], math.sqrt(4.8))
        self.assertAlmostEqual(v[2], 14.0)
        self.assertEqual(v[3], 4)
        self.assertEqual(v[4], 3)

    def test_needs_two_history_samples(self):
        with self.assertRaises(ValueError):
            window_features(self.seg[1:], 5, "continuous", 0.0, 0.0, F)

    def test_multichannel_and_sign(self):
        seg = np.hstack([self.seg, -self.seg, 1e6 * self.seg])
        v = window_features(seg, 5, "continuous", 0.0, 0.0, F)
        np.testing.assert_allclose(v[0], v[1])           # sign-symmetric features
        np.testing.assert_allclose(v[2, :3], 1e6 * v[0, :3])
        np.testing.assert_array_equal(v[2, 3:], v[0, 3:])  # counts are scale-free at T = 0

    def test_rms_range(self):
        rng = np.random.default_rng(0)
        x = rng.normal(size=(402, 3)) * [1, 1e-3, 1e4]
        v = window_features(x, 400, "continuous", 0.0, 0.0, F)
        w = x[-400:]
        self.assertTrue(np.all(v[:, 1] >= 0))
        self.assertTrue(np.all(v[:, 1] <= np.max(np.abs(w), axis=0) + 1e-12))
        self.assertTrue(np.all(v[:, 0] <= v[:, 1] + 1e-12))   # MAV <= RMS (Cauchy-Schwarz)


class TestStreamingSchedule(unittest.TestCase):
    def test_schedule_and_equivalence(self):
        rng = np.random.default_rng(1)
        y = rng.normal(size=(3000, 2))
        sf = StreamingFeatures(2, window=400, step=100, settle=200, boundary="continuous",
                               zc_thr=0.1, ssc_thr=0.01)
        ends, feats = sf.process(y)
        self.assertEqual(ends[0], 200 + 400 - 1)
        self.assertTrue(np.all(np.diff(ends) == 100))
        self.assertEqual(ends[-1], 2999)
        for t, f in zip(ends, feats):
            ref = window_features(y[t - 401:t + 1], 400, "continuous", 0.1, 0.01)
            np.testing.assert_array_equal(f, ref)

    def test_reset_restarts_schedule(self):
        y = np.random.default_rng(2).normal(size=(700, 1))
        sf = StreamingFeatures(1, 400, 100, 200, "continuous", 0.0, 0.0)
        e1, _ = sf.process(y)
        sf.reset()
        e2, _ = sf.process(y)
        np.testing.assert_array_equal(e1, e2)
        np.testing.assert_array_equal(e1, [599, 699][:len(e1)])


if __name__ == "__main__":
    unittest.main()
