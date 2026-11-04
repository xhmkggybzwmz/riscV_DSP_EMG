"""Fixed-point DSP: arithmetic rules, behaviour on standard signals, state, chunking, bypass, overflow."""

import unittest

import numpy as np

from emg_fixed.dsp.features_int import StreamingFeaturesInt, window_features_int
from emg_fixed.dsp.fixed_dsp import C_FRAC, FixedDSP, FixedSection, quantise_sos, sections_from_config
from emg_fixed.fixed import Overflow, isqrt_floor, quantise_coef, round_shift, sat_signed
from emgml.dsp import DSPConfig, EMGPreprocessPipeline

FS = 2000.0


def cfg(variant="dc_bp20_450", n_ch=4):
    from pathlib import Path
    c = DSPConfig.load(Path(__file__).resolve().parents[3] / "ml" / "configs" / "dsp" / f"{variant}.toml")
    c.n_ch = n_ch
    return c


def i16(a):
    return np.clip(np.round(a), -32768, 32767).astype(np.int16)


class TestPrimitives(unittest.TestCase):
    def test_round_shift_half_up_both_signs(self):
        np.testing.assert_array_equal(round_shift(np.array([3, 2, 1, 0, -1, -2, -3, -5, 5]), 1),
                                      [2, 1, 1, 0, 0, -1, -1, -2, 3])
        np.testing.assert_array_equal(round_shift(np.array([7, 8, -8, -9]), 4), [0, 1, 0, -1])

    def test_saturation_and_isqrt(self):
        np.testing.assert_array_equal(sat_signed(np.array([40000, -40000, 5]), 16), [32767, -32768, 5])
        x = np.array([0, 1, 2, 3, 4, 15, 16, 2**31 - 1, 2**40 + 12345])
        r = isqrt_floor(x)
        self.assertTrue(np.all(r * r <= x) and np.all((r + 1) * (r + 1) > x))

    def test_coefficient_boundaries(self):
        self.assertEqual(quantise_coef(-2.0, 16, 18), -131072)              # exactly representable
        self.assertEqual(quantise_coef(1.9999847, 16, 18), 131071)
        with self.assertRaises(Overflow):
            quantise_coef(2.0, 16, 18)
        with self.assertRaises(Overflow):
            quantise_sos("x", np.array([[1.0, 2.5, 1.0, 1.0, 0.0, 0.0]]))


class TestFixedDSP(unittest.TestCase):
    def setUp(self):
        self.secs = sections_from_config(cfg())

    def run_fixed(self, x, secs=None):
        return FixedDSP(secs or self.secs, x.shape[1]).process(x, sos=True)

    def test_coefficients_match_float(self):
        c = cfg()
        from scipy import signal
        f = FixedDSP(self.secs, 1)
        sos_q = f.sos_quantised()
        ref = np.vstack([st.sos for _, st in EMGPreprocessPipeline(c).stages])
        self.assertLess(np.max(np.abs(sos_q - ref)), 2.0 ** -C_FRAC)
        w = np.array([5.0, 20.0, 100.0, 300.0, 450.0, 900.0])
        _, h_q = signal.sosfreqz(sos_q, worN=w, fs=FS)
        _, h_f = signal.sosfreqz(ref, worN=w, fs=FS)
        db = 20 * np.log10(np.abs(h_q) / np.abs(h_f))
        self.assertLess(np.max(np.abs(db)), 0.05)
        self.assertLess(max(np.max(np.abs(np.roots([1, s.a[0] / 65536, s.a[1] / 65536]))) for s in self.secs), 1.0)

    def test_constant_input_removed(self):
        for c in (12000, -9000):
            y = self.run_fixed(np.full((6000, 1), c, dtype=np.int16))
            self.assertLessEqual(np.max(np.abs(y[-1000:])), 1)

    def test_sine_passband_and_stopband(self):
        t = np.arange(12000) / FS
        y = self.run_fixed(i16(10000 * np.sin(2 * np.pi * 150 * t))[:, None])
        self.assertAlmostEqual(np.std(y[6000:]) / np.std(10000 * np.sin(2 * np.pi * 150 * t[6000:])), 1.0, delta=0.01)
        y = self.run_fixed(i16(10000 * np.sin(2 * np.pi * 3 * t))[:, None])
        self.assertLess(np.std(y[6000:]), 0.05 * 10000)

    def test_close_to_float_reference(self):
        rng = np.random.default_rng(0)
        x = i16(rng.normal(scale=4000, size=(8000, 4)) + 1000)
        y = self.run_fixed(x).astype(float)
        yf = EMGPreprocessPipeline(cfg()).run_record(x.astype(float)).waveform
        e = y[200:] - yf[200:]
        self.assertLess(np.sqrt(np.mean(e ** 2)), 1.0)            # < 1 LSB rms
        self.assertLess(np.max(np.abs(e)), 4.0)

    def test_impulse_and_step(self):
        x = np.zeros((4000, 1), dtype=np.int16)
        x[100] = 30000
        y = self.run_fixed(x)
        self.assertTrue(np.all(y[:100] == 0))
        self.assertGreater(np.max(np.abs(y)), 1000)
        self.assertLessEqual(np.max(np.abs(y[-500:])), 1)
        s = np.zeros((4000, 1), dtype=np.int16)
        s[100:] = 20000
        self.assertLessEqual(np.max(np.abs(self.run_fixed(s)[-500:])), 1)

    def test_extremes_saturate_not_wrap(self):
        x = np.tile(np.array([32767, -32768], dtype=np.int16), 3000)[:, None]       # fs/2 full scale
        f = FixedDSP(sections_from_config(cfg("none")), 1)
        np.testing.assert_array_equal(f.process(x), x)                              # bypass: identical
        x = np.repeat(np.array([32767, -32768] * 30, dtype=np.int16), 100)[:, None]  # full-scale square wave
        f = FixedDSP(self.secs, 1)
        y = f.process(x, sos=True)
        self.assertEqual(y.dtype, np.int16)
        self.assertGreater(f.stats["output_saturations"], 0)                      # overshoot is clipped
        self.assertEqual(f.stats["internal_saturations"], 0)                      # 8x internal headroom
        self.assertLess(max(f.stats["max_abs_acc"]), 2 ** 47)

    def test_internal_overflow_saturates(self):
        hot = [FixedSection("hot", (131071, 0, 0), (-131072, 65535))]               # gain ~2, pole ~1: grows
        f = FixedDSP(hot, 1)
        y = f.process(np.full((400, 1), 30000, dtype=np.int16))
        self.assertGreater(f.stats["internal_saturations"], 0)
        self.assertEqual(int(y[-1, 0]), 32767)

    def test_channels_independent(self):
        rng = np.random.default_rng(1)
        x = i16(rng.normal(scale=3000, size=(3000, 3)))
        y = self.run_fixed(x)
        for c in range(3):
            np.testing.assert_array_equal(y[:, c], self.run_fixed(x[:, [c]])[:, 0])

    def test_chunking_sos_gap(self):
        rng = np.random.default_rng(2)
        x = i16(rng.normal(scale=5000, size=(10000, 4)))
        whole = self.run_fixed(x)
        f = FixedDSP(self.secs, 4)
        parts, pos = [], 0
        for i, n in enumerate([100, 37, 1024, 1, 4096, 4742]):
            parts.append(f.process(x[pos:pos + n], sos=(i == 0)))
            pos += n
        np.testing.assert_array_equal(np.vstack(parts), whole)
        f.process(x[:3000], sos=True)
        np.testing.assert_array_equal(f.process(x[3000:], sos=True), self.run_fixed(x[3000:]))
        f.process(x[:3000], sos=True)
        np.testing.assert_array_equal(f.process(x[3000:], gap=True), self.run_fixed(x[3000:]))
        f.process(x[:3000], sos=True)
        self.assertFalse(np.array_equal(f.process(x[3000:]), self.run_fixed(x[3000:])))

    def test_int16_required(self):
        with self.assertRaises(TypeError):
            FixedDSP(self.secs, 1).process(np.zeros((10, 1)))


class TestIntFeatures(unittest.TestCase):
    def test_hand_values(self):
        # history 0, -1 ; window [1, -2, 3, 3, -1]: SUMSQ 24, SUMABS 10, WL 14, ZC 4, SSC 3 (as float test)
        seg = np.array([0, -1, 1, -2, 3, 3, -1], dtype=np.int16)[:, None]
        v = window_features_int(seg, 5)[0]
        R = int(round((1 << 24) / 5))
        self.assertEqual(v[5], 24)
        self.assertEqual(v[0], int(np.floor(np.sqrt(round_shift(np.array([24 * R]), 24)[0]))))
        self.assertEqual(list(v[2:5]), [14, 4, 3])
        self.assertEqual(window_features_int(seg, 5, t_zc=4)[0, 3], 2)        # |d| = 2,3,5,4 -> 5 and 4 count

    def test_full_scale_widths(self):
        seg = np.full((402, 2), -32768, dtype=np.int16)
        v = window_features_int(seg, 400)
        self.assertEqual(v[0, 5], 400 * 32768 ** 2)
        # spec value: R = round(2^24/400) = 41943 is ~1e-6 below 2^24/400, so MS = 2^30 - 1000 and
        # floor(sqrt) = 32767; the exact RMS is 32768 -> spec error <= 1 LSB (documented)
        R = int(round((1 << 24) / 400))
        ms = int(round_shift(np.array([400 * 32768 ** 2 * R]), 24)[0])
        self.assertEqual(v[0, 0], int(np.floor(np.sqrt(ms))))
        self.assertEqual(v[0, 0], 32767)

    def test_streaming_equals_windows(self):
        y = np.random.default_rng(3).integers(-20000, 20000, size=(3000, 2)).astype(np.int16)
        sf = StreamingFeaturesInt(2)
        e1, f1 = sf.process(y[:777], sos=True)
        e2, f2 = sf.process(y[777:])
        ends = np.concatenate([e1, e2])
        feats = np.concatenate([f for f in (f1, f2) if len(f)])
        np.testing.assert_array_equal(ends, np.arange(599, 3000, 100))
        for t, fv in zip(ends, feats):
            np.testing.assert_array_equal(fv, window_features_int(y[t - 401:t + 1], 400))


if __name__ == "__main__":
    unittest.main()
