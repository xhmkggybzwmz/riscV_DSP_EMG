"""Record-level splits, leakage checks, window construction and train-only scaling (synthetic data)."""

import unittest

import numpy as np

from emgml.dsp import DSPConfig
from emgml.models.lda import make_lda
from emgml.recording import Recording
from emgml.splits import Fold, check_fold, inner_folds, p1_folds, p2_folds, window_mask
from emgml.windowing import feature_columns, make_windows


def synth_records(subjects=(1, 2), sessions=(1, 2, 3), labels=(0, 1, 2), trials=range(1, 8), n=1200):
    recs = []
    for s in subjects:
        for se in sessions:
            for l in labels:
                for t in trials:
                    rng = np.random.default_rng(hash((s, se, l, t)) % 2**32)
                    x = rng.normal(scale=0.02 * (1 + l), size=(n, 2))
                    recs.append(Recording(x=x, fs=2000.0, dataset="SYN", subject=s, session=se, trial=t, label=l,
                                          channel_names=["a", "b"], units="mV", source_path="", sha256=""))
    return recs


class TestSplits(unittest.TestCase):
    def setUp(self):
        self.keys = [r.key for r in synth_records()]

    def test_p1(self):
        folds = p1_folds(self.keys)
        self.assertEqual(len(folds), 2 * 3 * 7)
        for f in folds:
            check_fold(self.keys, f)
            self.assertEqual(len(f.test), 3)            # one trial of each of 3 labels
            self.assertEqual(len(f.train), 18)
            te_trials = {self.keys[i][3] for i in f.test}
            tr_trials = {self.keys[i][3] for i in f.train}
            self.assertEqual(len(te_trials), 1)
            self.assertFalse(te_trials & tr_trials)

    def test_p2(self):
        folds = p2_folds(self.keys)
        self.assertEqual(len(folds), 2 * 3)
        for f in folds:
            check_fold(self.keys, f)
            self.assertEqual({self.keys[i][2] for i in f.test} & {self.keys[i][2] for i in f.train}, set())
            self.assertEqual(len(f.test), 21)
            self.assertEqual(len(f.train), 42)

    def test_inner_folds_stay_inside_training(self):
        for proto, folds in (("P1", p1_folds(self.keys)), ("P2", p2_folds(self.keys))):
            for f in folds[:4]:
                inner = inner_folds(self.keys, f.train, proto)
                self.assertGreaterEqual(len(inner), 2)
                for tr, va in inner:
                    self.assertTrue(set(tr) | set(va) <= set(f.train))
                    self.assertFalse(set(tr) & set(va))
                    self.assertFalse((set(tr) | set(va)) & set(f.test))

    def test_leak_detection(self):
        f = p1_folds(self.keys)[0]
        with self.assertRaises(AssertionError):
            check_fold(self.keys, Fold("P1", f.subject, "bad", f.train + f.test[:1], f.test))
        with self.assertRaises(AssertionError):           # a test-trial record moved into training
            other = next(i for i in f.train)
            check_fold(self.keys, Fold("P1", f.subject, "bad", f.train, f.test + (other,)))
        g = p2_folds(self.keys)[0]
        foreign = next(i for i, k in enumerate(self.keys) if k[1] != g.subject)
        with self.assertRaises(AssertionError):
            check_fold(self.keys, Fold("P2", g.subject, "bad", g.train + (foreign,), g.test))


class TestWindowing(unittest.TestCase):
    def setUp(self):
        self.recs = synth_records(subjects=(1,), sessions=(1,), labels=(0, 1), trials=(1, 2))
        self.cfg = DSPConfig(n_ch=2)

    def test_windows_per_record_and_labels(self):
        ws = make_windows(self.recs, self.cfg)
        # 1200 samples: ends 599, 699, ..., 1199 -> 7 windows per record
        self.assertEqual(len(ws.y), 4 * 7)
        for i, r in enumerate(self.recs):
            m = ws.record == i
            self.assertTrue(np.all(ws.y[m] == r.label))
            np.testing.assert_array_equal(ws.end[m], np.arange(599, 1200, 100))
        self.assertEqual(ws.X.shape[1], 2 * 5)
        self.assertEqual(ws.feature_names[:2], ["a:MAV", "a:RMS"])

    def test_record_is_its_own_stream(self):
        """Features of a record do not depend on which records come before it."""
        a = make_windows(self.recs, self.cfg)
        b = make_windows(self.recs[::-1], self.cfg)
        np.testing.assert_array_equal(a.X[a.record == 0], b.X[b.record == len(self.recs) - 1])

    def test_trim(self):
        ws = make_windows(self.recs, self.cfg, trim_start_s=0.25)     # 500 samples: windows starting >= 500
        self.assertTrue(np.all(ws.end - 399 >= 500))

    def test_feature_columns(self):
        names = ["a:MAV", "a:RMS", "a:WL", "b:MAV", "b:RMS", "b:WL"]
        np.testing.assert_array_equal(feature_columns(names, ("RMS",)), [1, 4])

    def test_scaler_uses_training_windows_only(self):
        recs = synth_records(subjects=(1,), sessions=(1, 2, 3), labels=(0, 1, 2), trials=(1, 2, 3))
        ws = make_windows(recs, DSPConfig(n_ch=2))
        f = p2_folds(ws.records)[0]
        tr = window_mask(ws.record, f.train)
        te = window_mask(ws.record, f.test)
        m = make_lda().fit(ws.X[tr], ws.y[tr])
        np.testing.assert_allclose(m.named_steps["scale"].mean_, ws.X[tr].mean(axis=0))
        self.assertFalse(np.allclose(m.named_steps["scale"].mean_, ws.X[tr | te].mean(axis=0)))


if __name__ == "__main__":
    unittest.main()
